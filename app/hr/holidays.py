"""T-5.LEAVE.01 — the holiday calendar: the days the company does not work.

A holiday is a **dated row**, per company and — where a market states them per region — per
region, seeded from the localization pack (T-0.LOC.01 carries the Philippines' list with the
type each day has there). Nothing about a market's holidays is compiled in here: the pack is
the source, and the loader refuses a pack it cannot trust before any of it reaches a
calendar.

Three rules this module is built around:

* **One resolver, so attendance, leave and payroll cannot disagree.** :func:`day_status` is
  the only answer to "is this day worked" — a holiday is a holiday for the roster that
  resolves a shift, for the leave that is counted in working days and for the payroll that
  pays a month's working days, rather than three readings of one table.
* **A region is asked for, never guessed.** A calendar entry with no region is the whole
  market's (what the pack ships); one with a region applies to that region, and it is
  resolved first. Asking about a day states which region is being asked about, because a
  country's national holidays and a region's local ones are different facts.
* **A closed period is closed.** Adding or removing a holiday inside a month the ledger has
  locked (T-1.ACCT.03) is **refused**: a period that has been reported on cannot be
  restated by editing the calendar underneath it. This is the "prevented" half of the
  criterion — the alternative it allows would be to record the change and let it stand.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import CheckConstraint, Date, ForeignKey, Index, String, Uuid, or_, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db import Base
from app.ledger.periods import period_is_locked
from app.localization import load_pack

# The kinds of holiday a pack states. The Philippines pack uses both words — "regular" for
# the days the law fixes and "special" for the proclaimed ones — and nothing here depends on
# what each is paid at: that is the payroll's business (T-5.PAY.01), and the pack's.
HOLIDAY_KINDS = ("regular", "special")

# The two statuses a day can have. Working is the default: a day is worked unless the
# calendar says otherwise, which is the reading that cannot invent a holiday nobody stated.
WORKING = "working"
HOLIDAY = "holiday"


class HolidayError(ValueError):
    """The calendar refused what was asked of it."""


class InvalidHolidayError(HolidayError):
    """A holiday failed validation at entry — a date, a name or a kind."""


class DuplicateHolidayError(HolidayError):
    """That day is already a holiday for that region — one day, one entry."""


class ClosedPeriodError(HolidayError):
    """The day falls in a month the ledger has locked: the calendar cannot restate it."""


class Holiday(Base):
    """One non-working day, for one company and region."""

    __tablename__ = "holiday"
    __table_args__ = (
        CheckConstraint(
            "kind IN (" + ", ".join(f"'{kind}'" for kind in HOLIDAY_KINDS) + ")",
            name="ck_holiday_kind",
        ),
        # One entry per company, region and day. `region` null is the whole market — what the
        # pack ships — and NULLS NOT DISTINCT is what makes that statement hold: Postgres
        # treats nulls as distinct in a unique index by default, which is exactly the hole
        # that would let two company-wide rows for one day exist.
        Index(
            "uq_holiday_company_region_day",
            "company_id",
            "region",
            "holiday_date",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    # The region the day belongs to, or null for the whole market. Free text because the
    # plan names no regional vocabulary: the pack's own country is what a seeded calendar
    # leaves null, and a market that states its regions states them here.
    region: Mapped[str | None] = mapped_column(String(16))
    holiday_date: Mapped[date] = mapped_column(Date, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)


# A holiday is a **datum about a day, not a master**: no document points at one, and the
# criterion for this task is that the calendar can be *changed* — with a closed month
# refused and every change attributable. So a wrong entry is removed and the right one
# stated, rather than retired by marking: a soft-deleted row would keep the day's unique key
# and make the correction impossible. Both changes are on the T-0.AUDIT.02 trail, which is
# what "explicitly audited" means here, and T-1.ACCT.03's lock is what stops either from
# restating a closed period.


def _required(value: Any, what: str) -> str:
    stated = "" if value is None else str(value).strip()
    if not stated:
        raise InvalidHolidayError(f"{what} is required")
    return stated


def _date_or_refuse(value: Any, what: str) -> date:
    """A calendar date from a date, a datetime or the ISO string the boundary carries."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError as exc:
            raise InvalidHolidayError(f"not {what}: {value!r}") from exc
    raise InvalidHolidayError(f"{what} is a date, not {value!r}")


def _region(value: Any) -> str | None:
    """The region as stated, or null for the whole market — an empty string is neither."""
    return None if value is None else _required(value, "a region")


def _refuse_closed_period(session: Session, *, company_id: uuid.UUID, on: date, what: str) -> None:
    """Refuse a calendar change inside a locked month (T-1.ACCT.03).

    The period is closed because its figures have been reported on. A holiday added or
    removed underneath it would restate a month nobody may restate — but by a route the
    ledger's own guard does not cover, since no posting is involved. So the guard is here.
    """
    if period_is_locked(session, company_id=company_id, on=on):
        raise ClosedPeriodError(
            f"{on:%Y-%m} is closed for posting, so {what} cannot change in it: a closed period"
            " is not restated — open it (T-1.ACCT.03) if the calendar really was wrong"
        )


def holiday_on(
    session: Session, *, company_id: uuid.UUID, on: date, region: str | None = None
) -> Holiday | None:
    """The holiday `on` is, or ``None`` — the region's own first, then the market's.

    A region's entries and the market's are different facts, and a region that has its own
    entry for a day is the answer for that region: the country's day and a province's day
    can fall on the same date with different names.
    """
    stated = _region(region)
    conditions = [Holiday.company_id == company_id, Holiday.holiday_date == on]
    # `region = x OR region IS NULL`: a region's own entries and the market's together, so a
    # province that has no local day of its own still gets the country's.
    conditions.append(
        Holiday.region.is_(None)
        if stated is None
        else or_(Holiday.region == stated, Holiday.region.is_(None))
    )
    rows = list(session.scalars(select(Holiday).where(*conditions)))
    return next((row for row in rows if row.region == stated), None) or next(
        (row for row in rows if row.region is None), None
    )


def day_status(
    session: Session, *, company_id: uuid.UUID, on: date, region: str | None = None
) -> str:
    """Whether `on` is worked — :data:`WORKING` or :data:`HOLIDAY`.

    The one reading attendance, leave and payroll share, so a roster, a leave day count and a
    payroll period cannot disagree about the same Tuesday. Deterministic: the same day and
    region answer the same thing every time, and a day nobody has stated is **worked**.
    """
    return HOLIDAY if holiday_on(session, company_id=company_id, on=on, region=region) else WORKING


def is_working_day(
    session: Session, *, company_id: uuid.UUID, on: date, region: str | None = None
) -> bool:
    """Whether `on` is a working day — what a leave day count and a period of work read."""
    return day_status(session, company_id=company_id, on=on, region=region) == WORKING


def state_holiday(
    session: Session,
    *,
    company_id: uuid.UUID,
    on: Any,
    name: str,
    kind: str = "regular",
    region: str | None = None,
) -> Holiday:
    """State one non-working day, for a region or for the whole market.

    Refused when the day falls in a **locked** month (the calendar is not restated under a
    period that has been reported on) and when that day is already an entry for that region —
    one day, one entry, rather than two names for the same Tuesday.
    """
    when = _date_or_refuse(on, "a holiday date")
    stated_kind = _required(kind, "a holiday kind").lower()
    if stated_kind not in HOLIDAY_KINDS:
        raise InvalidHolidayError(
            f"unknown holiday kind {kind!r}; a holiday is {', '.join(HOLIDAY_KINDS)}"
        )
    where = _region(region)
    _refuse_closed_period(
        session, company_id=company_id, on=when, what=f"the holiday on {when}"
    )
    if holiday_on(session, company_id=company_id, on=when, region=where) is not None:
        raise DuplicateHolidayError(
            f"{when} is already a holiday for {where or 'the whole market'}; edit that entry"
            " rather than adding a second one"
        )
    holiday = Holiday(
        company_id=company_id,
        region=where,
        holiday_date=when,
        name=_required(name, "a holiday name"),
        kind=stated_kind,
    )
    session.add(holiday)
    session.flush()
    return holiday


def remove_holiday(session: Session, holiday: Holiday) -> None:
    """Take a non-working day out of the calendar — refused inside a locked month.

    The row goes: see the note above `Holiday` on why this is a datum and not a master. The
    removal is recorded by T-0.AUDIT.02 like any other change, so a calendar that lost a day
    can be shown which one and by whom.
    """
    _refuse_closed_period(
        session,
        company_id=holiday.company_id,
        on=holiday.holiday_date,
        what=f"the holiday on {holiday.holiday_date}",
    )
    session.delete(holiday)
    session.flush()


def seed_calendar(
    session: Session,
    *,
    company_id: uuid.UUID,
    market: str,
    year: int | None = None,
    region: str | None = None,
) -> list[Holiday]:
    """Seed the calendar from a market's pack, for a year or for every year it carries.

    The pack is loaded through T-0.LOC.01's loader, so a pack that fails validation never
    reaches a calendar. Seeding is **idempotent**: a day already stated is left exactly as it
    is — including one somebody has since renamed, because a re-seed is not an edit — and the
    function reports only what it added. Days inside a locked month are refused like any
    other calendar change, so seeding a closed year is not a way round the guard.
    """
    pack = load_pack(market)
    where = _region(region)
    seeded: list[Holiday] = []
    for entry in pack.get("holidays") or ():
        when = _date_or_refuse(entry.get("date"), f"a holiday date in the {market} pack")
        if year is not None and when.year != year:
            continue
        if holiday_on(session, company_id=company_id, on=when, region=where) is not None:
            continue
        _refuse_closed_period(
            session,
            company_id=company_id,
            on=when,
            what=f"the holiday on {when} seeded from the {market} pack",
        )
        kind = _required(entry.get("type"), f"a holiday kind in the {market} pack").lower()
        if kind not in HOLIDAY_KINDS:
            raise InvalidHolidayError(
                f"{market}: the pack calls {when} a {kind!r}; a holiday is"
                f" {', '.join(HOLIDAY_KINDS)} — the pack is wrong, and a calendar is not a"
                " place to store a kind nobody defined"
            )
        holiday = Holiday(
            company_id=company_id,
            region=where,
            holiday_date=when,
            name=_required(entry.get("name"), f"a holiday name in the {market} pack"),
            kind=kind,
        )
        session.add(holiday)
        seeded.append(holiday)
    session.flush()
    return seeded
