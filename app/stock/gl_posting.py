"""T-1.INV.07 — stock movements post to the GL, and the two agree.

§2.2 asks for real-time valuation, §3's "Double-Entry Integrity" row requires every
module to post balanced entries, §6 metric 2 is "stock valuation matches the GL
inventory account", and Phase 1's own exit criteria name the same thing. This
module is where that is true or not:

* **Every movement posts.** The three transactions of T-1.INV.05 and the
  adjustment of T-1.INV.06 call :func:`post_movement_to_gl` immediately after they
  write their entry, so a movement without its posting is not a reachable state —
  and both live in the caller's transaction, so a failure takes both away (§2.6 of
  DOMAIN-MODELS.md: "a document and its postings commit together").
* **The counterpart is configuration.** Which account a receipt credits, or an
  issue debits, is an account-mapping key (T-1.ACCT.03): `stock_receipt`,
  `stock_issue`, `stock_adjustment`. A source type nobody mapped is refused rather
  than posted to a guess. A **transfer** maps to the inventory account itself —
  the value leaves and arrives inside inventory, so the entry is balanced and the
  inventory total does not move.
* **The reconciliation is checkable.** :func:`reconcile` compares the GL inventory
  account's movement over a period with the stock ledger's own value movement and
  says whether they agree — a difference is *reported*, never absorbed.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ledger.mapping import mapped_account
from app.ledger.posting import JournalEntry, JournalLine, post_journal_entry
from app.stock.entries import StockLedgerEntry

# Which account-mapping key each kind of movement posts its counterpart to.
SOURCE_ACCOUNT_KEYS = {
    "goods_receipt": "stock_receipt",
    "stock_issue": "stock_issue",
    "stock_transfer": "inventory",
    "inventory_adjustment": "stock_adjustment",
}

MONEY_SCALE = Decimal("0.000001")


class StockPostingError(ValueError):
    """The movement could not be posted to the ledger as asked."""


def account_key_for(source_type: str) -> str:
    """The mapping key a movement's counterpart account lives under."""
    key = SOURCE_ACCOUNT_KEYS.get(str(source_type))
    if key is None:
        raise StockPostingError(
            f"no account mapping is known for {source_type!r}; add it to"
            " SOURCE_ACCOUNT_KEYS and map the key before moving stock through it"
        )
    return key


def post_movement_to_gl(session: Session, *, entry: StockLedgerEntry) -> JournalEntry:
    """Post one stock movement's value to the inventory account and its counterpart.

    The inventory account takes the movement's value with its own sign, and the
    counterpart takes the opposite, so the entry balances by construction and the
    inventory balance moves by exactly what the stock ledger says it moved.
    """
    inventory = mapped_account(session, company_id=entry.company_id, key="inventory").code
    counterpart = mapped_account(
        session, company_id=entry.company_id, key=account_key_for(entry.source_type)
    ).code
    value = entry.value
    lines: list[dict[str, Any]] = [
        {
            "account": inventory,
            "debit": value if value > 0 else Decimal(0),
            "credit": Decimal(0) if value > 0 else -value,
        },
        {
            "account": counterpart,
            "credit": value if value > 0 else Decimal(0),
            "debit": Decimal(0) if value > 0 else -value,
        },
    ]
    return post_journal_entry(
        session,
        company_id=entry.company_id,
        posting_date=entry.posting_date,
        currency=entry.currency,
        source_type=entry.source_type,
        source_id=entry.source_id,
        memo=f"stock movement {entry.id} ({entry.posting_date})",
        lines=lines,
    )


def reconcile(
    session: Session,
    *,
    company_id: uuid.UUID,
    start: date | None = None,
    end: date | None = None,
) -> dict:
    """Compare the GL inventory account with the stock ledger over a period.

    The stock ledger's own value movement, against the inventory account's
    movement in base currency. `balanced` is the §6 metric 2 test; `difference` is
    what it is, so a mismatch is a number to investigate rather than a failed
    assertion.
    """
    inventory = mapped_account(session, company_id=company_id, key="inventory").code

    stock_statement = select(
        func.coalesce(func.sum(StockLedgerEntry.value), 0)
    ).where(StockLedgerEntry.company_id == company_id)
    gl_statement = (
        select(
            func.coalesce(
                func.sum((JournalLine.debit - JournalLine.credit) * JournalEntry.exchange_rate),
                0,
            )
        )
        .select_from(JournalLine)
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .where(JournalEntry.company_id == company_id, JournalLine.account == inventory)
    )
    if start is not None:
        stock_statement = stock_statement.where(StockLedgerEntry.posting_date >= start)
        gl_statement = gl_statement.where(JournalEntry.posting_date >= start)
    if end is not None:
        stock_statement = stock_statement.where(StockLedgerEntry.posting_date <= end)
        gl_statement = gl_statement.where(JournalEntry.posting_date <= end)

    stock_value = Decimal(session.scalar(stock_statement)).quantize(MONEY_SCALE)
    gl_value = Decimal(session.scalar(gl_statement)).quantize(MONEY_SCALE)
    return {
        "company_id": str(company_id),
        "from": start.isoformat() if start else None,
        "to": end.isoformat() if end else None,
        "account": inventory,
        "stock_value": format(stock_value, "f"),
        "gl_value": format(gl_value, "f"),
        "difference": format(gl_value - stock_value, "f"),
        "balanced": gl_value == stock_value,
    }
