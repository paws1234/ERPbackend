"""T-3.POS.05 — proving the till's day reached the ledger and the stock it says it did.

§3's "Double-Entry Integrity" row and §2.2's real-time valuation principle are why
this exists: a POS sale posts revenue, tax and the tender it was paid with, and
issues its stock — so the day's takings can be compared with the ledger, and the day's
stock movements with the sales that caused them.

* **Both sides come from the documents.** The POS side is the day's completed sales —
  their net, their tax, their tenders; the GL side is the `journal_line` rows those
  very sales posted, read at the accounts the mappings point at. Nothing keeps a
  running total on either side, so a difference is a real finding.
* **Per day and per terminal, never aggregated away.** The plan's own metric is per
  report period, and a difference that nets out across two tills is exactly the one a
  reconciliation exists to surface, so the figures are stated per terminal as well as
  for the day.
* **It compares; it does not correct.** Nothing here writes an adjustment, and the
  revenue and tax comparisons are one-directional — a POS sale's own entry is what is
  read, so a *missing* posting shows up as a difference of the whole amount.

A voided sale is included on both sides by its own effects: it posted nothing (an
abandoned basket) or a reversing entry (a refund), and T-3.POS.04's report is where
the two are counted apart. Here what matters is that the postings and the documents
agree about the money that actually moved.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ledger.mapping import mapped_account
from app.ledger.posting import JournalEntry, JournalLine
from app.pos.drawer import CASH, tender_breakdown
from app.pos.sales import (
    BANK_KEY,
    CASH_KEY,
    COMPLETED,
    DOC_TYPE,
    OUTPUT_TAX_KEY,
    REVENUE_KEY,
    PosSale,
)
from app.stock.entries import StockLedgerEntry

MONEY_SCALE = Decimal("0.000001")


def _sales(
    session: Session, *, company_id: uuid.UUID, on: date, terminal: str | None = None
) -> list[PosSale]:
    statement = select(PosSale).where(
        PosSale.company_id == company_id,
        PosSale.sold_on == on,
        PosSale.status == COMPLETED,
    )
    if terminal is not None:
        statement = statement.where(PosSale.terminal == str(terminal))
    return list(session.scalars(statement.order_by(PosSale.terminal, PosSale.number)))


def _gl_totals(session: Session, *, sale_ids: list[uuid.UUID]) -> dict[str, Decimal]:
    """What the given sales' own entries put on each account, account code -> net debit."""
    if not sale_ids:
        return {}
    rows = session.execute(
        select(JournalLine.account, func.sum(JournalLine.debit - JournalLine.credit))
        .select_from(JournalLine)
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .where(
            JournalEntry.source_type == DOC_TYPE,
            JournalEntry.source_id.in_(sale_ids),
        )
        .group_by(JournalLine.account)
    ).all()
    return {account: Decimal(total).quantize(MONEY_SCALE) for account, total in rows}


def reconcile_gl(
    session: Session, *, company_id: uuid.UUID, on: date, terminal: str | None = None
) -> dict:
    """The day's takings against the ledger their sales posted, per terminal.

    Each account the till touches is compared with what the sales say should be on it:
    revenue and output tax credited, and each tender's account debited by what that
    tender applied. A sale whose entry is missing shows as a difference of its whole
    amount on every account, which is the point.
    """
    sales = _sales(session, company_id=company_id, on=on, terminal=terminal)
    tenders = tender_breakdown(sales)
    revenue = mapped_account(session, company_id=company_id, key=REVENUE_KEY).code
    tax = mapped_account(session, company_id=company_id, key=OUTPUT_TAX_KEY).code
    cash = mapped_account(session, company_id=company_id, key=CASH_KEY).code
    bank = mapped_account(session, company_id=company_id, key=BANK_KEY).code
    gl = _gl_totals(session, sale_ids=[sale.id for sale in sales])

    expected = {
        revenue: -sum((sale.net_amount for sale in sales), Decimal(0)).quantize(MONEY_SCALE),
        tax: -sum((sale.tax_amount for sale in sales), Decimal(0)).quantize(MONEY_SCALE),
        cash: tenders.get(CASH, {}).get("applied", Decimal(0)).quantize(MONEY_SCALE),
        bank: sum(
            (totals["applied"] for kind, totals in tenders.items() if kind != CASH),
            Decimal(0),
        ).quantize(MONEY_SCALE),
    }
    differences = {
        account: (gl.get(account, Decimal(0)) - value).quantize(MONEY_SCALE)
        for account, value in expected.items()
    }
    return {
        "on": on,
        "terminal": terminal,
        "sales": len(sales),
        "gross": sum((sale.gross_amount for sale in sales), Decimal(0)).quantize(MONEY_SCALE),
        "expected": expected,
        "posted": {account: gl.get(account, Decimal(0)) for account in expected},
        "differences": differences,
        "balanced": all(value == 0 for value in differences.values()),
    }


def reconcile_stock(
    session: Session, *, company_id: uuid.UUID, on: date, terminal: str | None = None
) -> dict:
    """The day's stock movements against the sales that caused them, per item.

    Read from the **stock ledger**, not from the lines' own links: every movement the
    day's sales carry — by source type and sale id — is compared with the quantities
    the sales' lines say left the shelf, per item and variant. So goods that left
    without a line (a movement nobody's basket explains), or a line whose movement is
    missing, are both reported rather than netted away.
    """
    sales = _sales(session, company_id=company_id, on=on, terminal=terminal)
    expected: dict[tuple[uuid.UUID, uuid.UUID | None], Decimal] = {}
    nameless: list[str] = []
    for sale in sales:
        for line in sale.lines:
            key = (line.item_id, line.variant_id)
            expected[key] = expected.get(key, Decimal(0)) - line.quantity
            if line.movement_id is None:
                nameless.append(f"{sale.number}/{line.line_no}")
    posted: dict[tuple[uuid.UUID, uuid.UUID | None], Decimal] = {}
    if sales:
        for movement in session.scalars(
            select(StockLedgerEntry).where(
                StockLedgerEntry.source_type == DOC_TYPE,
                StockLedgerEntry.source_id.in_([sale.id for sale in sales]),
            )
        ):
            key = (movement.item_id, movement.variant_id)
            posted[key] = posted.get(key, Decimal(0)) + movement.quantity
    differences = {
        key: (posted.get(key, Decimal(0)) - value).quantize(MONEY_SCALE)
        for key, value in expected.items()
    }
    for key in posted:
        if key not in expected:
            differences[key] = posted[key].quantize(MONEY_SCALE)
    return {
        "on": on,
        "terminal": terminal,
        "sales": len(sales),
        "items": len(expected),
        "expected": {key: value.quantize(MONEY_SCALE) for key, value in expected.items()},
        "posted": {key: posted.get(key, Decimal(0)) for key in set(posted) | set(expected)},
        "lines_without_movement": nameless,
        "differences": differences,
        "balanced": all(value == 0 for value in differences.values()) and not nameless,
    }


def reconcile(
    session: Session, *, company_id: uuid.UUID, on: date, terminal: str | None = None
) -> dict:
    """Both reconciliations for one day — re-runnable, and per terminal as well as whole.

    Called again it returns the same figures, because nothing is cached and both sides
    are read from the rows each time.
    """
    gl = reconcile_gl(session, company_id=company_id, on=on, terminal=terminal)
    stock = reconcile_stock(session, company_id=company_id, on=on, terminal=terminal)
    return {
        "on": on,
        "terminal": terminal,
        "gl": gl,
        "stock": stock,
        "balanced": gl["balanced"] and stock["balanced"],
    }


def per_terminal(session: Session, *, company_id: uuid.UUID, on: date) -> list[dict]:
    """The day's reconciliation one terminal at a time — where a difference is named.

    A day that balances while one till is short and another is long is a day that did
    not balance; this is the view that says so.
    """
    terminals = sorted(
        {
            row
            for row in session.scalars(
                select(PosSale.terminal).where(
                    PosSale.company_id == company_id,
                    PosSale.sold_on == on,
                    PosSale.status == COMPLETED,
                )
            )
        }
    )
    return [
        reconcile(session, company_id=company_id, on=on, terminal=terminal)
        for terminal in terminals
    ]
