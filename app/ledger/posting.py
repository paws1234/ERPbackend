"""T-0.CORE.01 — the single double-entry posting primitive.

Every module in every phase posts through :func:`post_journal_entry`; nothing
else writes ``journal_entry`` / ``journal_line``.  The invariant — **at least
two lines and total debit equal to total credit** — is checked here *and*
enforced again in the database by deferred constraint triggers, so a writer that
bypasses this module is refused by the storage boundary too, not only by the
caller.

Amounts are exact ``Decimal`` values mapped to ``numeric``: no float ever
touches a posting.
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
from app.db import Base

# Amount scale: ISO 4217 minor units plus the precision unit costs and FX need.
MONEY = Numeric(20, 6)

# A posting with fewer lines than this cannot express a double entry.
MIN_LINES = 2


class UnbalancedEntryError(ValueError):
    """Raised when a set of lines cannot be posted as it stands."""


class JournalEntry(Base):
    """One balanced posting: the unit that is committed or refused as a whole."""

    __tablename__ = "journal_entry"

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
    memo: Mapped[str | None] = mapped_column(Text)
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
    # Account code for now; T-1.ACCT.01 owns the chart of accounts and turns
    # this into a foreign key.
    account: Mapped[str] = mapped_column(String(64), nullable=False)
    debit: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    credit: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    # Party link, where the posting is attributable to one (T-0.PARTY.01 owns
    # the party master and turns this into a foreign key).
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


def _line(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "account": raw["account"],
        "debit": _amount(raw.get("debit", 0)),
        "credit": _amount(raw.get("credit", 0)),
        "party": raw.get("party"),
    }


def post_journal_entry(
    session: Session,
    *,
    company_id: uuid.UUID,
    posting_date: date,
    currency: str,
    lines: Iterable[Mapping[str, Any]],
    memo: str | None = None,
) -> JournalEntry:
    """Post one balanced journal entry — the only way into the ledger.

    ``lines`` is an iterable of mappings with ``account``, ``debit``, ``credit``
    and optionally ``party``.  Raises :class:`UnbalancedEntryError` when the set
    has fewer than two lines or does not balance, and never persists anything in
    that case (the flush inside is the caller's transaction, so the caller's
    rollback is enough — no journal entry survives a failed document).
    """
    rows = [_line(raw) for raw in lines]
    if len(rows) < MIN_LINES:
        raise UnbalancedEntryError(
            f"a posting needs at least {MIN_LINES} lines, got {len(rows)}"
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
        memo=memo,
    )
    entry.lines = [JournalLine(line_no=n, **row) for n, row in enumerate(rows, start=1)]

    session.add(entry)
    # Surface a rejection here rather than at some later, unrelated commit. The
    # caller owns the commit: a posting is atomic with the document that made it.
    session.flush()
    return entry
