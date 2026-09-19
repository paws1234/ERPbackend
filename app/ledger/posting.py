"""T-0.CORE.01 — the single double-entry posting primitive.

Every module in every phase posts through :func:`post_journal_entry`; nothing
else writes ``journal_entry`` / ``journal_line``.  The invariant — **at least
two lines and total debit equal to total credit** — is checked here *and*
enforced again in the database by deferred constraint triggers, so a writer that
bypasses this module is refused by the storage boundary too, not only by the
caller.

Amounts are exact ``Decimal`` values mapped to ``numeric``: no float ever
touches a posting.

T-1.ACCT.01 made the two link columns of §2.1 real references: ``account_id`` →
``account.id`` (the chart of accounts) and ``party_id`` → ``party.id``
(T-0.PARTY.01). A caller still states the **code** — DOMAIN-MODELS.md §3 fixes the
code as "what a posting line states" — and this module resolves it to the row
inside the posting company, so a line can never name an account or a party that
does not exist.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    DDL,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    Uuid,
    event,
    func,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.audit import append_only
from app.company import company_base_currency
from app.db import Base
from app.ledger.accounts import account_by_code
from app.ledger.currency import RATE, rate_for
from app.ledger.periods import PeriodLockedError, period_is_locked
from app.party import party_by_code

# Amount scale: ISO 4217 minor units plus the precision unit costs and FX need.
MONEY = Numeric(20, 6)

# A posting with fewer lines than this cannot express a double entry.
MIN_LINES = 2


class UnbalancedEntryError(ValueError):
    """Raised when a set of lines cannot be posted as it stands."""


class IncompleteSourceError(ValueError):
    """A posting named half a source document — a type without an id, or the reverse.

    T-1.ACCT.02: an entry records the document that produced it, and a document
    is the pair. Half of a pair names nothing, so it is refused rather than
    stored as a link nobody can follow.
    """


class JournalEntry(Base):
    """One balanced posting: the unit that is committed or refused as a whole."""

    __tablename__ = "journal_entry"
    __table_args__ = (
        # A source document is a pair or nothing (T-1.ACCT.02): half of one names
        # nothing, so the table refuses it even for a writer that bypasses the
        # primitive.
        CheckConstraint(
            "(source_type IS NULL) = (source_id IS NULL)",
            name="ck_journal_entry_source_pair",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Owning company — the scoping dimension on every posting, isolated by the
    # row-level security app/db.py puts on this table (T-0.CORE.03).
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    posting_date: Mapped[date] = mapped_column(Date, nullable=False)
    # Currency the lines are stated in; conversion to the base currency is
    # T-1.ACCT.05 and stores the rate, not the caller.
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    # The rate the entry's amounts are carried at (T-1.ACCT.05): 1 for an entry in
    # the company's base currency, otherwise the rate stored for **this posting
    # date** — so a document keeps the rate it was posted at, and its base amount
    # is `amount × exchange_rate`, derivable exactly and never re-read from today.
    exchange_rate: Mapped[Decimal] = mapped_column(
        RATE, nullable=False, default=Decimal(1)
    )
    memo: Mapped[str | None] = mapped_column(Text)
    # The document that produced the entry (T-1.ACCT.02): its type and its id in
    # the owning module, the pair T-0.MODELS.01 fixed the shape of. A manual
    # entry has none — there was no document — so both stay null.
    source_type: Mapped[str | None] = mapped_column(String(32), index=True)
    source_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    lines: Mapped[list[JournalLine]] = relationship(
        back_populates="entry", order_by="JournalLine.line_no"
    )


class JournalLine(Base):
    """One side of a posting: a debit or a credit against an account."""

    __tablename__ = "journal_line"
    __table_args__ = (
        CheckConstraint("debit >= 0 AND credit >= 0", name="ck_journal_line_no_negative"),
        CheckConstraint(
            "NOT (debit > 0 AND credit > 0)", name="ck_journal_line_one_side_only"
        ),
        CheckConstraint("debit + credit > 0", name="ck_journal_line_not_zero"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    entry_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("journal_entry.id"), nullable=False, index=True
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    # §2.1's **account** link: the reference (T-1.ACCT.01) and the code as the
    # caller stated it. Both are written here from one resolution, so they cannot
    # disagree; the code stays because a posting is history — the text a reader
    # sees does not change when the chart is reorganised or an account retired.
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("account.id"), nullable=False, index=True
    )
    account: Mapped[str] = mapped_column(String(64), nullable=False)
    debit: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    credit: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    # §2.1's **party link**, where the posting is attributable to one: the
    # reference (T-0.PARTY.01's master) and the party's code as stated.
    party_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("party.id"), index=True)
    party: Mapped[str | None] = mapped_column(String(64))

    entry: Mapped[JournalEntry] = relationship(back_populates="lines")


# --- The invariant, at the storage boundary ---------------------------------
# The checks the primitive makes are the caller's copy of the rule. These
# triggers are the rule: they are DEFERRABLE INITIALLY DEFERRED, so a complete
# posting is judged as a set at COMMIT, and any writer — including raw SQL that
# bypasses post_journal_entry — is refused there. Postgres-only by design: on
# any other backend the DDL fails loudly rather than leaving the ledger
# unguarded.
#
# ponytail: the deferred row trigger re-sums the entry once per line, so posting
# n lines costs O(n²) on the entry's own rows. Ceiling: postings are tens of
# lines. Upgrade path if a module ever posts hundreds: switch the line trigger to
# FOR EACH STATEMENT and keep one entry trigger.

_BALANCE_FUNCTION = DDL(
    """
CREATE OR REPLACE FUNCTION journal_entry_balances() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    target  uuid;
    n       integer;
    debits  numeric;
    credits numeric;
BEGIN
    IF TG_TABLE_NAME = 'journal_entry' THEN
        target := NEW.id;                       -- an entry can only be inserted
    ELSIF TG_OP = 'DELETE' THEN
        target := OLD.entry_id;
    ELSE
        target := NEW.entry_id;
    END IF;

    SELECT count(*), coalesce(sum(debit), 0), coalesce(sum(credit), 0)
      INTO n, debits, credits
      FROM journal_line
     WHERE entry_id = target;

    -- '%%' because SQLAlchemy's DDL wrapper interpolates the statement
    IF n < 2 THEN
        RAISE EXCEPTION 'journal entry %% has %% line(s); a posting needs at least 2',
            target, n;
    END IF;
    IF debits <> credits THEN
        RAISE EXCEPTION 'journal entry %% does not balance: debit %% <> credit %%',
            target, debits, credits;
    END IF;
    RETURN NULL;
END;
$$;
"""
)

_ENTRY_TRIGGER = DDL(
    """
CREATE CONSTRAINT TRIGGER journal_entry_must_have_lines
    AFTER INSERT ON journal_entry
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION journal_entry_balances();
"""
)

_LINE_TRIGGER = DDL(
    """
CREATE CONSTRAINT TRIGGER journal_line_must_balance
    AFTER INSERT OR UPDATE OR DELETE ON journal_line
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION journal_entry_balances();
"""
)

for _ddl in (_BALANCE_FUNCTION, _ENTRY_TRIGGER, _LINE_TRIGGER):
    event.listen(JournalLine.__table__, "after_create", _ddl)


# --- The invariant, at the storage boundary: append-only ---------------------
# T-0.AUDIT.01: a posting is history, so the database refuses to change or remove
# one — the deferred balance triggers above judge a *new* set of lines, these
# refuse a rewrite of an existing one. Registered here because this module owns
# the two tables; the convention itself lives in app/audit.py.
for _table in (JournalEntry.__table__, JournalLine.__table__):
    append_only(_table)


# --- The invariant, at the caller's side ------------------------------------


def _amount(value: Any) -> Decimal:
    """Exact decimal from whatever the caller passed — never through float."""
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _line(session: Session, company_id: uuid.UUID, raw: Mapping[str, Any]) -> dict[str, Any]:
    """One line, with its account and party references resolved.

    The account code is required; the party code is optional and, when stated,
    must name a party of this company — a link to a party nobody created is a
    typo, not a posting.
    """
    account = account_by_code(session, company_id=company_id, code=raw["account"])
    party_code = raw.get("party")
    party_id = None
    if party_code is not None:
        party_id = party_by_code(session, company_id=company_id, code=party_code).id
    return {
        "account_id": account.id,
        "account": account.code,
        "debit": _amount(raw.get("debit", 0)),
        "credit": _amount(raw.get("credit", 0)),
        "party_id": party_id,
        "party": None if party_code is None else str(party_code),
    }


def post_journal_entry(
    session: Session,
    *,
    company_id: uuid.UUID,
    posting_date: date,
    currency: str,
    lines: Iterable[Mapping[str, Any]],
    memo: str | None = None,
    source_type: str | None = None,
    source_id: uuid.UUID | None = None,
) -> JournalEntry:
    """Post one balanced journal entry — the only way into the ledger.

    ``lines`` is an iterable of mappings with ``account`` (an account **code** of
    this company), ``debit``, ``credit`` and optionally ``party`` (a party code).
    ``source_type`` and ``source_id`` name the document that produced the entry —
    stated together or not at all (T-1.ACCT.02), so a drill-down can find a
    document's postings and a manual entry is honestly unlinked.
    Raises :class:`UnbalancedEntryError` when the set has fewer than two lines or
    does not balance, and never persists anything in that case (the flush inside
    is the caller's transaction, so the caller's rollback is enough — no journal
    entry survives a failed document).
    """
    rows = [_line(session, company_id, raw) for raw in lines]
    if len(rows) < MIN_LINES:
        raise UnbalancedEntryError(
            f"a posting needs at least {MIN_LINES} lines, got {len(rows)}"
        )
    if (source_type is None) != (source_id is None):
        raise IncompleteSourceError(
            "a posting names its source document as a pair: got"
            f" source_type={source_type!r} source_id={source_id!r}"
        )
    # T-1.ACCT.04: a closed month takes no more postings. Asked here, in the one
    # interface every module posts through, so no module can forget the lock.
    if period_is_locked(session, company_id=company_id, on=posting_date):
        raise PeriodLockedError(
            f"{posting_date:%Y-%m} is closed for posting; unlock the period"
            " before posting into it"
        )
    # T-1.ACCT.05: a foreign-currency entry is carried at the rate stored for its
    # posting date, so the caller states the currency and never the rate.
    base_currency = company_base_currency(session, company_id=company_id)
    exchange_rate = rate_for(
        session,
        company_id=company_id,
        base_currency=base_currency,
        currency=currency,
        on=posting_date,
    )

    total_debit = sum((row["debit"] for row in rows), Decimal(0))
    total_credit = sum((row["credit"] for row in rows), Decimal(0))
    if total_debit != total_credit:
        raise UnbalancedEntryError(
            f"entry does not balance: debit {total_debit} != credit {total_credit}"
        )

    entry = JournalEntry(
        company_id=company_id,
        posting_date=posting_date,
        currency=currency,
        exchange_rate=exchange_rate,
        memo=memo,
        source_type=source_type,
        source_id=source_id,
    )
    entry.lines = [JournalLine(line_no=n, **row) for n, row in enumerate(rows, start=1)]

    session.add(entry)
    # Surface a rejection here rather than at some later, unrelated commit. The
    # caller owns the commit: a posting is atomic with the document that made it.
    session.flush()
    return entry
