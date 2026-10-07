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
from app.pos.shifts import refunds_on
from app.pos.sales import (
    BANK_KEY,
    CASH_KEY,
    COMPLETED,
    DOC_TYPE,
    OUTPUT_TAX_KEY,
    REFUND_DOC_TYPE,
    REVENUE_KEY,
    PosSale,
)
from app.stock.entries import StockLedgerEntry

MONEY_SCALE = Decimal("0.000001")


def _sales(
    session: Session, *, company_id: uuid.UUID, on: date, terminal: str | None = None
) -> list[PosSale]:
    """The sales the day rang up — read from their entries, as the reports read them.

    A sale refunded later is still a sale this day's till took and its entry is still in
    this day's ledger, so both sides of the comparison count it. The refund, when it
    happens, is a document of its own day.
    """
    statement = select(PosSale).where(
        PosSale.company_id == company_id,
        PosSale.sold_on == on,
        PosSale.journal_entry_id.is_not(None),
    )
    if terminal is not None:
        statement = statement.where(PosSale.terminal == str(terminal))
    return list(session.scalars(statement.order_by(PosSale.terminal, PosSale.number)))


def _gl_totals(
    session: Session,
    *,
    sale_ids: list[uuid.UUID],
    refund_ids: list[uuid.UUID] | None = None,
) -> dict[str, Decimal]:
    """The accounts the given sales and refunds moved, account code -> net debit.

    Both kinds of entry, because a day's till can do both: the sales of the day and the
    refunds it made. Each is found by its own source type and the document it names.
    """
    pairs = [(DOC_TYPE, sale_ids)]
    if refund_ids:
        pairs.append((REFUND_DOC_TYPE, refund_ids))
    total: dict[str, Decimal] = {}
    for source_type, ids in pairs:
        if not ids:
            continue
        rows = session.execute(
            select(JournalLine.account, func.sum(JournalLine.debit - JournalLine.credit))
            .select_from(JournalLine)
            .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
            .where(
                JournalEntry.source_type == source_type,
                JournalEntry.source_id.in_(ids),
            )
            .group_by(JournalLine.account)
        ).all()
        for account, value in rows:
            total[account] = total.get(account, Decimal(0)) + Decimal(value)
    return {account: value.quantize(MONEY_SCALE) for account, value in total.items()}


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
    refunds = refunds_on(session, company_id=company_id, on=on, terminal=terminal)
    tenders = tender_breakdown(sales)
    # What the day gave back: the reversal of a refunded sale, posted on the day the
    # refund was made. Its tenders' money went the other way, so each account's
    # expectation carries it with the opposite sign.
    refunded_tenders = tender_breakdown(refunds)
    gl = _gl_totals(
        session, sale_ids=[sale.id for sale in sales],
        refund_ids=[sale.id for sale in refunds],
    )

    # Accumulated per account, not written as a literal keyed by one: two mapping keys
    # may legally point at the same account (a company that banks its card takings into
    # the same account the drawer posts to is the obvious one), and a literal would let
    # the later key overwrite the earlier and report a difference that is only a
    # collision between two names for one account.
    expected: dict[str, Decimal] = {}
    for key, value in (
        (REVENUE_KEY, sum((sale.net_amount for sale in refunds), Decimal(0))
         - sum((sale.net_amount for sale in sales), Decimal(0))),
        (OUTPUT_TAX_KEY, sum((sale.tax_amount for sale in refunds), Decimal(0))
         - sum((sale.tax_amount for sale in sales), Decimal(0))),
        (CASH_KEY, tenders.get(CASH, {}).get("applied", Decimal(0))
         - refunded_tenders.get(CASH, {}).get("applied", Decimal(0))),
        (BANK_KEY, sum(
            (totals["applied"] for kind, totals in tenders.items() if kind != CASH),
            Decimal(0),
        ) - sum(
            (totals["applied"] for kind, totals in refunded_tenders.items()
             if kind != CASH),
            Decimal(0),
        )),
    ):
        account = mapped_account(session, company_id=company_id, key=key).code
        expected[account] = (
            expected.get(account, Decimal(0)) + Decimal(value)
        ).quantize(MONEY_SCALE)
    differences = {
        account: (gl.get(account, Decimal(0)) - value).quantize(MONEY_SCALE)
        for account, value in expected.items()
    }
    return {
        "on": on,
        "terminal": terminal,
        "sales": len(sales),
        "gross": sum((sale.gross_amount for sale in sales), Decimal(0)).quantize(MONEY_SCALE),
        "refunds": len(refunds),
        "refunded": sum((sale.gross_amount for sale in refunds), Decimal(0)).quantize(
            MONEY_SCALE
        ),
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

    The movements are the ones that **happened this day**, by their own posting date —
    a refund of an earlier day's sale must not drag that sale's original issue into
    today's comparison, where no line of today's documents explains it.
    """
    sales = _sales(session, company_id=company_id, on=on, terminal=terminal)
    refunds = refunds_on(session, company_id=company_id, on=on, terminal=terminal)
    expected: dict[tuple[uuid.UUID, uuid.UUID | None], Decimal] = {}
    nameless: list[str] = []
    for sale in sales:
        for line in sale.lines:
            key = (line.item_id, line.variant_id)
            expected[key] = expected.get(key, Decimal(0)) - line.quantity
            if line.movement_id is None:
                nameless.append(f"{sale.number}/{line.line_no}")
    for sale in refunds:
        # The goods came back, so the day's expectation is that they are on the shelf
        # again — the return the refund posted.
        for line in sale.lines:
            key = (line.item_id, line.variant_id)
            expected[key] = expected.get(key, Decimal(0)) + line.quantity
    posted: dict[tuple[uuid.UUID, uuid.UUID | None], Decimal] = {}
    if sales or refunds:
        for movement in session.scalars(
            select(StockLedgerEntry).where(
                StockLedgerEntry.posting_date == on,
                StockLedgerEntry.source_type.in_(
                    [DOC_TYPE, REFUND_DOC_TYPE]
                    if refunds else [DOC_TYPE]
                ),
                StockLedgerEntry.source_id.in_(
                    [sale.id for sale in (*sales, *refunds)]
                ),
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
        "refunds": len(refunds),
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
    terminals = {
        row
        for row in session.scalars(
            select(PosSale.terminal).where(
                PosSale.company_id == company_id,
                PosSale.sold_on == on,
                PosSale.status == COMPLETED,
            )
        )
    }
    # A till whose only document of the day is a refund still has a day to state — and
    # is exactly the till a whole-day figure would have hidden.
    terminals |= {
        refund.terminal
        for refund in refunds_on(session, company_id=company_id, on=on)
    }
    return [
        reconcile(session, company_id=company_id, on=on, terminal=terminal)
        for terminal in sorted(terminals)
    ]
