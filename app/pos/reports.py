"""T-3.POS.04 — the Z-Report: what a shift and a day actually took.

A Z-Report is the till's own account of itself, so the only thing that makes it worth
reading is that it is **derived from the sales** rather than kept beside them:

* **Every figure comes from the documents, and the day is the sum of the shifts.** The
  shift report walks T-3.POS.01's completed sales of that shift, T-3.POS.02's tenders
  and drawer movements, and T-3.POS.03's count; the day report adds the shifts of one
  day together — exactly, not approximately, because it adds the same rows rather than
  rounding each shift first. A sale that happened outside any shift (a company that
  does not manage drawers) is its own line rather than being dropped.
* **Tax is the pack's, broken out.** The report states the tax each sale carried, as
  T-3.AR.01's shared selling rule applied it, so a Z-Report and the invoice for the
  same goods cannot disagree about what VAT was charged.
* **Voids and refunds are their own lines.** They are different things — an abandoned
  basket took nothing, a refunded sale gave money back — so they are counted and
  valued apart from the sales, and every one of them carries a reason.
* **It is immutable once the shift is closed.** Nothing is stored to freeze: the shift
  is closed, no further sale can be stamped with it (T-3.POS.03), and a voided sale
  leaves the sales it is counted in — so reprinting a closed shift's report a year
  later reproduces the same figures from the same rows.

Voiding lives here because the report is what has to show it: :func:`void_sale`
abandons an open basket (nothing ever moved) or **refunds** a completed one — the
stock goes back on the shelf at the value it left at, and a reversing entry is posted
against the sale's own — so a refund is a document with an effect rather than a
correction somebody made to a figure.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ledger.posting import JournalEntry, post_journal_entry
from app.pos.drawer import drawer_state, movements_for, tender_breakdown
from app.pos.sales import (
    COMPLETED,
    MONEY_SCALE,
    OPEN,
    VOID,
    PosSale,
    PosSaleLine,
)
from app.pos.shifts import (
    OPENING_FLOAT_REASON,
    PosShift,
    shift_movements,
    shift_sales,
    shift_totals,
)
from app.stock.items import Item, ItemVariant
from app.stock.locations import Location
from app.stock.transactions import receive

# The document type a refund's stock return carries, and the one its entry carries, so
# a reversed sale is findable from the Z-Report that reports it.
REFUND_DOC_TYPE = "pos_sale_refund"


class ReportError(ValueError):
    """The report refused what was asked of it."""


class VoidNotAllowed(ReportError):
    """The sale is in no state a void applies to."""


def void_sale(
    session: Session,
    sale: PosSale,
    *,
    reason: str,
    actor: str,
    at: datetime | None = None,
) -> PosSale:
    """Void an open basket, or refund a completed sale — reversing what it did.

    An **open** basket never moved anything, so voiding it is a state and nothing
    else. A **completed** sale is refunded: its stock goes back to the location it
    left, at the value it left at (so the costing side reverses exactly), and a
    reversing entry is posted against the tender side and the revenue — a new entry,
    never an edit, because the ledger is append-only.

    A reason and an actor are required, and a sale that is already void is refused:
    refunding twice would put the goods back twice and reverse the revenue twice.
    """
    stated = str(reason or "").strip()
    if not stated:
        raise ReportError(
            f"voiding {sale.number!r} needs a reason; a sale that no longer stands is"
            " something somebody decided"
        )
    who = str(actor or "").strip()
    if not who:
        raise ReportError("a void names who made it")
    if sale.status == VOID:
        raise VoidNotAllowed(f"sale {sale.number!r} is already void")
    if sale.status not in (OPEN, COMPLETED):
        raise VoidNotAllowed(f"sale {sale.number!r} is {sale.status}, which takes no void")
    if sale.status == COMPLETED:
        location = session.get(Location, sale.location_id)
        for line in sale.lines:
            movement = _movement_for(session, line)
            if movement is None:  # pragma: no cover - a completed line names its issue
                continue
            receive(
                session,
                item=session.get(Item, line.item_id),
                location=location,
                uom=line.uom,
                quantity=line.quantity,
                # Back at exactly the value it left at, negated: the issue's value is
                # negative, so the return is its mirror and the costing reverses to the
                # last decimal.
                value=-movement.value,
                currency=sale.currency,
                source_type=REFUND_DOC_TYPE,
                source_id=sale.id,
                posting_date=sale.sold_on,
                variant=(
                    session.get(ItemVariant, line.variant_id) if line.variant_id else None
                ),
            )
        entry = session.get(JournalEntry, sale.journal_entry_id)
        mirror: list[dict[str, Any]] = [
            {"account": line.account, "debit": line.credit, "credit": line.debit}
            for line in entry.lines
        ]
        reversal = post_journal_entry(
            session,
            company_id=sale.company_id,
            posting_date=sale.sold_on,
            currency=sale.currency,
            memo=f"refund of POS sale {sale.number} ({stated})",
            source_type=REFUND_DOC_TYPE,
            source_id=sale.id,
            lines=mirror,
        )
        sale.reversal_entry_id = reversal.id
    sale.status = VOID
    sale.voided_at = at or datetime.now(timezone.utc)
    sale.voided_by = who
    sale.void_reason = stated
    session.flush()
    return sale


def _movement_for(session: Session, line: PosSaleLine):
    """The stock movement a sale line issued, or ``None`` where it names none."""
    from app.stock.entries import StockLedgerEntry

    if line.movement_id is None:
        return None
    return session.get(StockLedgerEntry, line.movement_id)


def _voided_sales(
    session: Session, *, company_id: uuid.UUID, shift: PosShift | None, on: date
) -> list[PosSale]:
    """The sales voided today — of this shift where there is one, of the day otherwise."""
    statement = select(PosSale).where(
        PosSale.company_id == company_id,
        PosSale.status == VOID,
        PosSale.sold_on == on,
    )
    if shift is not None:
        statement = statement.where(PosSale.shift_id == shift.id)
    return list(session.scalars(statement.order_by(PosSale.number)))


def shift_report(session: Session, shift: PosShift) -> dict:
    """The Z-Report for one shift: sales, tax, tenders, voids, refunds and the drawer.

    Every figure is the shift's own totals (T-3.POS.03's `shift_totals`), so a
    Z-Report and a shift cannot disagree about what the shift took — and the drawer
    section is the closing count the shift recorded, with its variance and reason.
    """
    totals = shift_totals(session, shift)
    sales = shift_sales(session, shift)
    voided = _voided_sales(
        session, company_id=shift.company_id, shift=shift, on=shift.opened_on
    )
    refunds = [sale for sale in voided if sale.journal_entry_id is not None]
    abandoned = [sale for sale in voided if sale.journal_entry_id is None]
    report = {
        "shift": str(shift.id),
        "terminal": shift.terminal,
        "opened_on": shift.opened_on,
        "closed_at": shift.closed_at,
        "status": shift.status,
        **{key: totals[key] for key in
           ("sales", "net", "tax", "gross", "tenders", "movements", "change_paid",
            "opening_float", "expected_cash")},
        # The two apart, as the task asks: a basket that was abandoned took nothing,
        # a sale that was refunded gave its money back.
        "voids": {
            "count": len(abandoned),
            "value": sum((sale.gross_amount for sale in abandoned), Decimal(0)).quantize(
                MONEY_SCALE
            ),
            "sales": [sale.number for sale in abandoned],
        },
        "refunds": {
            "count": len(refunds),
            "value": sum((sale.gross_amount for sale in refunds), Decimal(0)).quantize(
                MONEY_SCALE
            ),
            "sales": [sale.number for sale in refunds],
        },
        "drawer": {
            "opening_float": totals["opening_float"],
            "movements": totals["movements"],
            "expected": totals["expected_cash"],
            "counted": shift.counted_cash,
            "variance": shift.variance,
            "variance_reason": shift.variance_reason,
        },
    }
    # The one check a reader should be able to make for themselves: the tenders add up
    # to the sales, to the last decimal.
    applied = sum(
        (row["applied"] for row in report["tenders"].values()), Decimal(0)
    ).quantize(MONEY_SCALE)
    report["tenders_applied"] = applied
    report["ties"] = applied == report["gross"]
    return report


def _day_sales(session: Session, *, company_id: uuid.UUID, on: date) -> list[PosSale]:
    """The day's completed sales, oldest first — the rows every bucket is cut from."""
    return list(
        session.scalars(
            select(PosSale)
            .where(
                PosSale.company_id == company_id,
                PosSale.status == COMPLETED,
                PosSale.sold_on == on,
            )
            .order_by(PosSale.number)
        )
    )


def _bucket(sales: list[PosSale], movements) -> dict:
    """One shift's — or the shiftless till's — worth of figures, from its own rows."""
    state = drawer_state(sales, movements)
    return {
        "sales": len(sales),
        "net": sum((sale.net_amount for sale in sales), Decimal(0)).quantize(MONEY_SCALE),
        "tax": sum((sale.tax_amount for sale in sales), Decimal(0)).quantize(MONEY_SCALE),
        "gross": sum((sale.gross_amount for sale in sales), Decimal(0)).quantize(MONEY_SCALE),
        "tenders": tender_breakdown(sales),
        "movements": state["movements"],
        "change_paid": state["change_paid"],
        "expected_cash": state["expected"],
    }


def day_report(session: Session, *, company_id: uuid.UUID, on: date) -> dict:
    """The day's Z-Report: every shift of that day, and the shiftless trade beside them.

    The day's figures are the **sum of the shifts' own numbers**, computed from the
    same rows, so "equal to the sum" is arithmetic rather than a rounding promise. The
    trade that happened on a till with no shift (a company that manages no drawers) —
    and any sale whose shift is not one of the day's — gets its own bucket rather than
    being folded into one of the shifts or dropped.
    """
    shifts = [
        shift
        for shift in
        session.scalars(
            select(PosShift)
            .where(PosShift.company_id == company_id, PosShift.opened_on == on)
            .order_by(PosShift.terminal, PosShift.opened_at)
        )
    ]
    day_sales = _day_sales(session, company_id=company_id, on=on)
    claimed = {shift.id for shift in shifts}
    orphans = [sale for sale in day_sales if sale.shift_id not in claimed]
    # Every sale of the day lands in exactly one bucket: its own shift's, or the one for
    # the trade the day's shifts did not take — including a sale stamped with a shift
    # that opened on another day (a till left open past midnight), which bucketing by
    # the shift's own opening day would have left in neither bucket and so dropped from
    # the day's figures while it sits in the ledger and in that shift's report.
    per_shift = []
    for shift in shifts:
        sales = [sale for sale in day_sales if sale.shift_id == shift.id]
        float_only = [
            row
            for row in shift_movements(session, shift)
            if row.reason != OPENING_FLOAT_REASON
        ]
        bucket = _bucket(sales, float_only)
        bucket.update(
            {
                "shift": str(shift.id),
                "terminal": shift.terminal,
                "status": shift.status,
                "opening_float": Decimal(shift.opening_float).quantize(MONEY_SCALE),
                "counted": shift.counted_cash,
                "variance": shift.variance,
            }
        )
        bucket["expected_cash"] = (
            bucket["expected_cash"] + Decimal(shift.opening_float)
        ).quantize(MONEY_SCALE)
        per_shift.append(bucket)
    shiftless_sales = sorted(orphans, key=lambda sale: sale.number)
    shiftless = _bucket(
        shiftless_sales,
        [
            row
            for row in movements_for(session, company_id=company_id, on=on)
            if row.reason != OPENING_FLOAT_REASON and row.shift_id is None
        ],
    )
    voided = [
        sale
        for sale in session.scalars(
            select(PosSale).where(
                PosSale.company_id == company_id,
                PosSale.status == VOID,
                PosSale.sold_on == on,
            )
        )
    ]
    day = {
        "on": on,
        "shifts": per_shift,
        "shiftless": shiftless,
        "sales": sum(row["sales"] for row in per_shift) + shiftless["sales"],
        "net": sum((row["net"] for row in per_shift), shiftless["net"]).quantize(MONEY_SCALE),
        "tax": sum((row["tax"] for row in per_shift), shiftless["tax"]).quantize(MONEY_SCALE),
        "gross": sum((row["gross"] for row in per_shift), shiftless["gross"]).quantize(
            MONEY_SCALE
        ),
        "tenders": _sum_tenders([row["tenders"] for row in per_shift] + [shiftless["tenders"]]),
        "expected_cash": sum(
            (row["expected_cash"] for row in per_shift), shiftless["expected_cash"]
        ).quantize(MONEY_SCALE),
        "counted_cash": sum(
            (row["counted"] or Decimal(0) for row in per_shift), Decimal(0)
        ).quantize(MONEY_SCALE),
        "variance": sum(
            (row["variance"] or Decimal(0) for row in per_shift), Decimal(0)
        ).quantize(MONEY_SCALE),
        "voids": {
            "count": len([sale for sale in voided if sale.journal_entry_id is None]),
            "value": sum(
                (sale.gross_amount for sale in voided if sale.journal_entry_id is None),
                Decimal(0),
            ).quantize(MONEY_SCALE),
        },
        "refunds": {
            "count": len([sale for sale in voided if sale.journal_entry_id is not None]),
            "value": sum(
                (sale.gross_amount for sale in voided if sale.journal_entry_id is not None),
                Decimal(0),
            ).quantize(MONEY_SCALE),
        },
    }
    return day


def _sum_tenders(buckets: list[dict[str, dict[str, Decimal]]]) -> dict:
    """The tender lines of several buckets added together, keeping the paths apart."""
    combined: dict[str, dict[str, Decimal]] = {}
    for bucket in buckets:
        for kind, totals in bucket.items():
            entry = combined.setdefault(
                kind, {"tendered": Decimal(0), "applied": Decimal(0)}
            )
            entry["tendered"] += totals["tendered"]
            entry["applied"] += totals["applied"]
    return {
        kind: {
            "tendered": totals["tendered"].quantize(MONEY_SCALE),
            "applied": totals["applied"].quantize(MONEY_SCALE),
        }
        for kind, totals in sorted(combined.items())
    }
