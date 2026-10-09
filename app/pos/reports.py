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
    REFUND_DOC_TYPE,
    COMPLETED,
    MONEY_SCALE,
    OPEN,
    VOID,
    PosSale,
    PosSaleLine,
)
from app.pos.shifts import (
    PosShift,
    abandoned_baskets,
    refund_cash,
    refunds_on,
    shift_totals,
)
from app.stock.items import Item, ItemVariant
from app.stock.locations import Location
from app.stock.transactions import receive

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
    on: date | None = None,
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

    A refund is dated **when it happens** (`on`, today by default), not when the sale it
    reverses was rung up: backdating it would restate a period that has been reported
    and closed, and a refund made a week later would fail on a locked period for no
    reason but the calendar.
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
    refunded_on = on or (at.date() if at is not None else date.today())
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
                posting_date=refunded_on,
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
            posting_date=refunded_on,
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


def shift_report(session: Session, shift: PosShift, *, on: date | None = None) -> dict:
    """The Z-Report for one shift: sales, tax, tenders, voids, refunds and the drawer.

    Every figure is the shift's own totals (T-3.POS.03's `shift_totals`), so a
    Z-Report and a shift cannot disagree about what the shift took — and the drawer
    section is the closing count the shift recorded, with its variance and reason.

    `on` asks for the shift's **slice of one day** rather than its whole life: the day
    report takes its shifts that way, so a till left trading past midnight is reported
    on the day it traded instead of being moved to a bucket it did not come from.
    """
    totals = shift_totals(session, shift, on=on)
    abandoned = abandoned_baskets(
        session,
        company_id=shift.company_id,
        on=on or shift.opened_on,
        terminal=shift.terminal,
    )
    report = {
        "shift": str(shift.id),
        "terminal": shift.terminal,
        "opened_on": shift.opened_on,
        "on": on,
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
            "count": len(totals["refunds"]),
            "value": totals["refunded"],
            "sales": totals["refunds"],
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
    """The sales the day rang up, oldest first — what the shiftless bucket is cut from.

    A sale later refunded is still one the day rang up (its entry says so), so a refund
    made tomorrow cannot take it out of today's figures. The day's *shifts* read their
    own sales through `shift_totals`, which is what keeps the day the sum of the shift
    reports.
    """
    return list(
        session.scalars(
            select(PosSale)
            .where(
                PosSale.company_id == company_id,
                PosSale.journal_entry_id.is_not(None),
                PosSale.sold_on == on,
            )
            .order_by(PosSale.number)
        )
    )


def _bucket(sales: list[PosSale], movements, refunds: list[PosSale]) -> dict:
    """The shiftless till's worth of figures, from its own rows.

    A **shift's** row is not built here: it is the shift's own report for the day
    (:func:`_shift_bucket`). `refunds` are the sales **refunded** on this day where no
    shift of the day was trading: they are not sales of the day (the day's sales are the
    ones it rang up), but the cash they hand back leaves this drawer, so the drawer's
    own expectation carries them.
    """
    state = drawer_state(sales, movements)
    refunded = refund_cash(refunds)
    return {
        "sales": len(sales),
        "net": sum((sale.net_amount for sale in sales), Decimal(0)).quantize(MONEY_SCALE),
        "tax": sum((sale.tax_amount for sale in sales), Decimal(0)).quantize(MONEY_SCALE),
        "gross": sum((sale.gross_amount for sale in sales), Decimal(0)).quantize(MONEY_SCALE),
        "tenders": tender_breakdown(sales),
        "movements": state["movements"],
        "change_paid": state["change_paid"],
        "expected_cash": (state["expected"] - refunded).quantize(MONEY_SCALE),
        "refunded": sum((sale.gross_amount for sale in refunds), Decimal(0)).quantize(
            MONEY_SCALE
        ),
        "refunds": [sale.number for sale in refunds],
    }


def _shift_bucket(report: dict, shift: PosShift) -> dict:
    """One shift's day, flattened from that shift's own report — the same figures.

    Nothing is recounted here: the row **is** the shift's Z-Report for the day
    (:func:`shift_report` with `on`), so "the day is the sum of the shift reports" is
    arithmetic over one function rather than two implementations kept agreeing by hand.
    """
    return {
        "shift": report["shift"],
        "terminal": report["terminal"],
        "status": report["status"],
        "opened_on": report["opened_on"],
        "sales": report["sales"],
        "net": report["net"],
        "tax": report["tax"],
        "gross": report["gross"],
        "tenders": report["tenders"],
        "movements": report["movements"],
        "change_paid": report["change_paid"],
        "opening_float": report["opening_float"],
        "expected_cash": report["expected_cash"],
        "refunded": report["refunds"]["value"],
        "refunds": report["refunds"]["sales"],
        "voids": report["voids"],
        "counted": shift.counted_cash,
        "variance": shift.variance,
    }


def day_report(session: Session, *, company_id: uuid.UUID, on: date) -> dict:
    """The day's Z-Report: the day's shift reports added together, exactly.

    **Cross-midnight ownership.** A shift belongs to a day when it **opened** that day
    or when it **traded** on it — sold, moved cash, or was trading when a refund was
    handed back. Each of the day's shifts then contributes its own report *for that
    day* (``shift_report(shift, on=day)``), which is what makes the day exactly the sum
    of those reports: a sale rung after midnight on a till whose shift is still open is
    reported on the day it was sold, in that shift's own line, instead of being moved
    into the bucket for the trade no shift took while the shift's own Z-Report counts
    it. The opening float rides on the day the shift opened, so a shift that spans
    midnight does not state the same float on two days.

    The trade of a till with **no** shift at all — a company that manages no drawers —
    is its own bucket, and so is the cash of a terminal none of the day's shifts was
    trading.
    """
    sales_today = _day_sales(session, company_id=company_id, on=on)
    movements_today = movements_for(session, company_id=company_id, on=on)
    refunds = refunds_on(session, company_id=company_id, on=on)
    traded = {sale.shift_id for sale in sales_today if sale.shift_id is not None} | {
        row.shift_id for row in movements_today if row.shift_id is not None
    }
    every_shift = list(
        session.scalars(
            select(PosShift)
            .where(PosShift.company_id == company_id)
            .order_by(PosShift.terminal, PosShift.opened_at)
        )
    )
    shifts = [shift for shift in every_shift if shift.opened_on == on or shift.id in traded]
    own_terminals = {shift.terminal for shift in shifts}
    per_shift = [
        _shift_bucket(shift_report(session, shift, on=on), shift) for shift in shifts
    ]
    shiftless = _bucket(
        [sale for sale in sales_today if sale.shift_id is None],
        [row for row in movements_today if row.shift_id is None],
        # Refunds made where no shift was trading that day: the till with no drawer
        # management its own self, and a refund rung on a terminal whose shifts are not
        # one of the day's.
        [sale for sale in refunds if sale.terminal not in own_terminals],
    )
    abandoned = abandoned_baskets(session, company_id=company_id, on=on)
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
        # The two apart: a basket abandoned today took nothing, a sale refunded today
        # gave its money back — and it is today's, whatever day the sale was rung up.
        "voids": {
            "count": len(abandoned),
            "value": sum((sale.gross_amount for sale in abandoned), Decimal(0)).quantize(
                MONEY_SCALE
            ),
        },
        "refunds": {
            "count": len(refunds),
            "value": sum((sale.gross_amount for sale in refunds), Decimal(0)).quantize(
                MONEY_SCALE
            ),
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
