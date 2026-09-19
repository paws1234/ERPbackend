"""T-1.ACCT.05 — the currency master, the FX rate service and posting-time conversion.

§2.1 asks for a "Multi-Currency Engine – daily FX rate sync", and §3's
"Multi-Currency" row puts one central service in charge of rates so no module
invents one. Four decisions this module is built around:

* **A rate belongs to a pair and a date.** `FxRate` is one row per company × base
  currency × currency × date, so the rate a posting used is the rate for its
  **posting date**, not today's — and :func:`rate_for` refuses when the date has
  no rate rather than silently using the nearest one.
* **History is not rewritten.** :func:`store_rate` refuses to change the rate of a
  date that is already past (`HistoricalRateError`); a re-sync of *today* may
  correct today's rate, and the audit trail (T-0.AUDIT.02) records either way.
* **A posting stores its rate.** `journal_entry.exchange_rate` is written by the
  primitive when the currency is not the company's base, so a foreign document
  keeps its foreign amount, its rate and — exactly derivable — its base amount.
* **The sync reports failure loudly.** `fx_rate_source` is "Not stated" in the
  plan, so no provider is baked in: a rate source registers itself
  (:func:`register_rate_source`) and is named by configuration
  (`FX_RATE_SOURCE`). A sync with no source configured, or a source that fails,
  raises :class:`FxSyncFailed` and stores **nothing** — a half-synced day of
  rates would misstate every posting made that day.

ponytail: one rate per day per currency, and `fx_sync_schedule` (daily) is not
scheduled here — the job queue is still unpinned (`job_queue`). Ceiling: no
intraday rates, no scheduled run. Upgrade path: when the queue is chosen
(T-0.REPORT.01 owns that variable), call :func:`sync_daily_rates` from its daily
schedule.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.audit import SoftDeleteMixin, deny_hard_delete
from app.db import Base

# The scale rates are stored and applied at — finer than money, because a rate is
# a multiplier rather than an amount.
RATE = Numeric(20, 10)

# The environment variable naming the configured rate source (`fx_rate_source`).
RATE_SOURCE_SETTING = "FX_RATE_SOURCE"


class CurrencyError(ValueError):
    """The currency service refused what was asked of it."""


class UnknownCurrencyError(CurrencyError):
    """A currency nobody registered — refused, never assumed."""


class UnknownRateError(CurrencyError):
    """No rate is stored for that currency on that date."""


class HistoricalRateError(CurrencyError):
    """A past date's rate cannot be changed — append a new date instead."""


class RateSourceError(CurrencyError):
    """The configured rate source is missing or unusable."""


class FxSyncFailed(CurrencyError):
    """The daily sync did not produce rates; nothing was stored."""


class Currency(SoftDeleteMixin, Base):
    """One currency a company keeps books in or against.

    Company-scoped, like every other master: a code means the same thing
    everywhere, but *which* currencies a company deals in is the company's own
    list, and a change to it is a change to that company's books — which is what
    lets T-0.AUDIT.02 attribute it (§6 metric 7) instead of leaving a global row
    nobody owns.
    """

    __tablename__ = "currency"
    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_currency_company_code"),
        CheckConstraint("char_length(code) = 3", name="ck_currency_code_length"),
        CheckConstraint("code = upper(code)", name="ck_currency_code_upper"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(3), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)


# A master: retired by marking, never removed (T-0.AUDIT.01).
deny_hard_delete(Currency.__table__)


class FxRate(Base):
    """One currency's rate against one base currency, on one date."""

    __tablename__ = "fx_rate"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "base_currency", "currency", "rate_date",
            name="uq_fx_rate_pair_date",
        ),
        CheckConstraint("rate > 0", name="ck_fx_rate_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    # The company's reporting currency this rate is quoted against.
    base_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    # 1 `currency` = `rate` × `base_currency`.
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    rate_date: Mapped[date] = mapped_column(Date, nullable=False)
    rate: Mapped[Decimal] = mapped_column(RATE, nullable=False)
    # Where it came from: the registered source's name, or 'manual'.
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="manual")


def register_currency(
    session: Session, *, company_id: uuid.UUID, code: str, name: str
) -> Currency:
    """Add a currency to the company's list, or return the one already there."""
    wanted = str(code).strip().upper()
    if len(wanted) != 3 or not wanted.isalpha():
        raise UnknownCurrencyError(f"{code!r} is not a three-letter currency code")
    known = session.scalar(
        select(Currency).where(Currency.company_id == company_id, Currency.code == wanted)
    )
    if known is None:
        known = Currency(company_id=company_id, code=wanted, name=name)
        session.add(known)
        session.flush()
    return known


def currency_by_code(session: Session, *, company_id: uuid.UUID, code: str) -> Currency:
    """The company's registered currency, or a refusal naming what is missing."""
    known = session.scalar(
        select(Currency).where(
            Currency.company_id == company_id, Currency.code == str(code).strip().upper()
        )
    )
    if known is None:
        raise UnknownCurrencyError(
            f"{code!r} is not a registered currency for this company; register it before"
            " it is used (T-1.ACCT.05: register_currency)"
        )
    return known


def _amount(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def store_rate(
    session: Session,
    *,
    company_id: uuid.UUID,
    base_currency: str,
    currency: str,
    on: date,
    rate,
    source: str = "manual",
    today: date | None = None,
) -> FxRate:
    """Store one rate, refusing to rewrite a past date's figure.

    A date already in the past keeps the rate it was posted with; correcting it
    would change what past postings meant. Today's rate may be corrected by a
    re-sync (the audit trail records it). A rate of zero or less is refused by the
    table's own check constraint as well as here.
    """
    base = str(base_currency).strip().upper()
    quote = str(currency).strip().upper()
    if base == quote:
        raise CurrencyError(f"{quote} is the base currency; 1 is not a rate to store")
    currency_by_code(session, company_id=company_id, code=quote)
    currency_by_code(session, company_id=company_id, code=base)
    if today is None:
        today = datetime.now(timezone.utc).date()
    wanted = _amount(rate)
    if wanted <= 0:
        raise CurrencyError(f"a rate must be positive, got {wanted}")
    try:
        wanted = wanted.quantize(Decimal("0.0000000001"))
    except InvalidOperation as exc:
        raise CurrencyError(f"{rate!r} is not a rate") from exc

    existing = session.scalar(
        select(FxRate).where(
            FxRate.company_id == company_id,
            FxRate.base_currency == base,
            FxRate.currency == quote,
            FxRate.rate_date == on,
        )
    )
    if existing is None:
        stored = FxRate(
            company_id=company_id,
            base_currency=base,
            currency=quote,
            rate_date=on,
            rate=wanted,
            source=str(source),
        )
        session.add(stored)
        session.flush()
        return stored
    if existing.rate == wanted:
        return existing
    if on < today:
        raise HistoricalRateError(
            f"{quote}/{base} on {on} is already {existing.rate}; a past date's rate is"
            " not rewritten — store the corrected rate for the date it applies to"
        )
    existing.rate = wanted
    existing.source = str(source)
    session.flush()
    return existing


def rate_for(
    session: Session, *, company_id: uuid.UUID, base_currency: str, currency: str, on: date
) -> Decimal:
    """The rate to use on `on`: 1 for the base currency, else the stored dated rate."""
    base = str(base_currency).strip().upper()
    quote = str(currency).strip().upper()
    if base == quote:
        return Decimal(1)
    currency_by_code(session, company_id=company_id, code=quote)
    stored = session.scalar(
        select(FxRate).where(
            FxRate.company_id == company_id,
            FxRate.base_currency == base,
            FxRate.currency == quote,
            FxRate.rate_date == on,
        )
    )
    if stored is None:
        raise UnknownRateError(
            f"no {quote}/{base} rate is stored for {on}; store one before posting"
            " a document in that currency on that date"
        )
    return stored.rate


def convert(
    session: Session,
    *,
    company_id: uuid.UUID,
    base_currency: str,
    amount,
    currency: str,
    to_currency: str,
    on: date,
) -> Decimal:
    """Convert an amount between two currencies at the rates for `on`.

    Both legs are quoted against the company's base currency, so a cross rate is
    the two rates applied in order — never an invented parity. The result is
    quantized to the money scale.
    """
    value = _amount(amount)
    frm = str(currency).strip().upper()
    to = str(to_currency).strip().upper()
    if frm == to:
        return value
    base = str(base_currency).strip().upper()
    frm_rate = rate_for(session, company_id=company_id, base_currency=base, currency=frm, on=on)
    to_rate = rate_for(session, company_id=company_id, base_currency=base, currency=to, on=on)
    return (value * frm_rate / to_rate).quantize(Decimal("0.000001"))


# --- The daily sync ----------------------------------------------------------
# A rate source is a callable (currency codes → rate against the base) registered
# by whoever integrates a provider. Nothing is baked in: `fx_rate_source` is "Not
# stated" in the plan, so the provider is configuration and an unconfigured
# installation gets a loud refusal rather than a made-up rate.
_rate_sources: dict[str, Callable[[str, list[str]], dict[str, Decimal]]] = {}


def register_rate_source(
    name: str, fetch: Callable[[str, list[str]], dict[str, Decimal]]
) -> None:
    """Register a provider: `fetch(base_currency, currencies) -> {currency: rate}`."""
    _rate_sources[str(name)] = fetch


def configured_source() -> str | None:
    """The provider this deployment is configured to use (`fx_rate_source`)."""
    return os.environ.get(RATE_SOURCE_SETTING) or None


def sync_daily_rates(
    session: Session,
    *,
    company_id: uuid.UUID,
    base_currency: str,
    currencies: list[str],
    on: date | None = None,
    source: str | None = None,
) -> list[FxRate]:
    """Pull one day's rates for `currencies` and store them — all of them or none.

    Raises :class:`FxSyncFailed` when no source is configured, when the named one
    is not registered, or when the provider itself fails. Nothing is stored in any
    of those cases: a day of rates half-written would misstate the postings made
    against it, so the sync is all-or-nothing and reports why.
    """
    day = on or datetime.now(timezone.utc).date()
    name = source or configured_source()
    if name is None:
        raise FxSyncFailed(
            f"no rate source is configured ({RATE_SOURCE_SETTING} is unset);"
            " nothing was synced"
        )
    fetch = _rate_sources.get(name)
    if fetch is None:
        raise FxSyncFailed(
            f"rate source {name!r} is configured but not registered; nothing was synced"
        )

    base = str(base_currency).strip().upper()
    wanted = [str(code).strip().upper() for code in currencies if str(code).strip().upper() != base]
    try:
        fetched = fetch(base, wanted)
    except Exception as exc:  # the provider's failure is reported, not swallowed
        raise FxSyncFailed(f"rate source {name!r} failed on {day}: {type(exc).__name__}: {exc}") from exc

    missing = [code for code in wanted if code not in fetched]
    if missing:
        raise FxSyncFailed(
            f"rate source {name!r} returned no rate for {', '.join(missing)} on {day};"
            " nothing was synced"
        )

    stored = [
        store_rate(
            session,
            company_id=company_id,
            base_currency=base,
            currency=code,
            on=day,
            rate=fetched[code],
            source=name,
        )
        for code in wanted
    ]
    return stored
