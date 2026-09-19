"""T-1.ACCT.02 — the general ledger: append-only postings that name their document.

§2.1 asks for a "GL – immutable transaction ledger (posting date, account, debit,
credit, party links)". The writing half already exists: every module posts through
T-0.CORE.01's primitive into `journal_entry` / `journal_line`, and T-0.AUDIT.01's
trigger makes the two tables refuse an update or a delete. What this module adds
is the rest of DOMAIN-MODELS.md §4:

* **The source document.** An entry records the `(source_type, source_id)` pair
  that produced it, so a document's postings are found from the document — the
  drill-down §2.1 asks for. A manual entry has no document, so both columns stay
  null; the table refuses half a pair either way.
* **The read side, derived from the entries.** :func:`ledger_rows` is the ledger
  as a reader sees it and :func:`account_balance` is an account's balance — the
  sum of its lines, read from the entries themselves. Phase 1 keeps **no balance
  table** on purpose: a stored balance is a second version of the truth that can
  disagree with the ledger it came from, and the reconciliation T-1.INV.07 runs
  exists precisely because that disagreement is possible in other systems.

Nothing here writes: the primitive is the only writer, which is what T-1.ACCT.03
publishes and what T-0.CORE.02's ledger gate proves.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ledger.posting import JournalEntry, JournalLine


def ledger_rows(
    session: Session,
    *,
    company_id: uuid.UUID,
    account_code: str | None = None,
    start: date | None = None,
    end: date | None = None,
) -> list[dict[str, Any]]:
    """One row per journal line, in posting order — optionally one account, one period.

    This is what an account statement and the reconciliation in T-1.INV.07 read;
    the party link and the source document travel with each row, so a reader can
    get from a number to the document that made it without another query shape.
    """
    statement = (
        select(JournalEntry, JournalLine)
        .join(JournalLine, JournalLine.entry_id == JournalEntry.id)
        .where(JournalEntry.company_id == company_id)
        .order_by(
            JournalEntry.posting_date, JournalEntry.created_at, JournalLine.line_no
        )
    )
    if account_code is not None:
        statement = statement.where(JournalLine.account == str(account_code))
    if start is not None:
        statement = statement.where(JournalEntry.posting_date >= start)
    if end is not None:
        statement = statement.where(JournalEntry.posting_date <= end)

    return [
        {
            "entry_id": str(entry.id),
            "line_no": line.line_no,
            "posting_date": entry.posting_date.isoformat(),
            "account": line.account,
            "debit": format(line.debit, "f"),
            "credit": format(line.credit, "f"),
            "party": line.party,
            "currency": entry.currency,
            "memo": entry.memo,
            "source_type": entry.source_type,
            "source_id": None if entry.source_id is None else str(entry.source_id),
        }
        for entry, line in session.execute(statement)
    ]


def account_balance(
    session: Session,
    *,
    company_id: uuid.UUID,
    account_code: str,
    start: date | None = None,
    end: date | None = None,
) -> Decimal:
    """An account's balance — debit-positive, in the company's base currency.

    The sum is taken over the ledger, never over a stored figure: the balance is
    derivable from the entries alone, which is the criterion this task is closed
    on. Each line is restated at the rate its entry was posted at
    (T-1.ACCT.05), so a dollar balance and a peso balance are addable — the same
    measure the statements use. Empty is a real ``Decimal("0")``, not ``None``.
    """
    statement = (
        select(
            func.coalesce(
                func.sum((JournalLine.debit - JournalLine.credit) * JournalEntry.exchange_rate),
                0,
            )
        )
        .select_from(JournalLine)
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .where(JournalEntry.company_id == company_id, JournalLine.account == str(account_code))
    )
    if start is not None:
        statement = statement.where(JournalEntry.posting_date >= start)
    if end is not None:
        statement = statement.where(JournalEntry.posting_date <= end)
    # Restating at each entry's rate widens the scale, so the answer is brought
    # back to the money scale every amount in this platform is stated at.
    return Decimal(session.scalar(statement)).quantize(Decimal("0.000001"))


def entries_for_source(
    session: Session, *, company_id: uuid.UUID, source_type: str, source_id: uuid.UUID
) -> list[JournalEntry]:
    """Every posting one document produced, oldest first — the drill-down.

    A document may post more than once (a receipt and later an adjustment), so
    this is a list rather than one entry.
    """
    return list(
        session.scalars(
            select(JournalEntry)
            .where(
                JournalEntry.company_id == company_id,
                JournalEntry.source_type == str(source_type),
                JournalEntry.source_id == source_id,
            )
            .order_by(JournalEntry.posting_date, JournalEntry.created_at)
        )
    )
