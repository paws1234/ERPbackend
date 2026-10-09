"""T-4.WO.04 — receiving the finished goods, and closing the work in progress.

A production receipt does two things at once, and this module keeps them in the same
transaction so the books cannot show one without the other:

* **The components the receipt consumes are issued with it.** Receiving a *fraction*
  of the order consumes that fraction of the BOM requirement — the job cannot report
  half its output while the material for all of it sits in WIP — so the shortfall is
  issued proportionally through T-4.WO.03's :func:`~app.manufacturing.issues.issue_material`,
  at the store's own value. Where the balance is not fully issued and the caller names
  no location to draw it from, the receipt is **refused with the shortfall named**
  rather than accepted with the consumption quietly missing.
* **The receipt clears WIP by exactly what it takes out.** Its value is the material
  the job has consumed, apportioned over the quantity being received — so the last
  receipt takes the residue and the WIP account ends the job at **zero** rather than at
  a rounding remainder. The stock ledger row and the journal entry are T-1.INV.05's
  and T-1.INV.07's (the movement's counterpart is the `work_in_progress` mapping key),
  so the receipt is a movement like any other and appears in the ledger beside the
  issues it answers.
* **What is left unreconciled is stated.** :func:`consumption_report` answers, per
  requirement, how much was required, issued and consumed, whether the difference is
  inside the tolerance, and what WIP stands at — the figure a cost accountant checks
  before the job is closed.
* **Labour is not here.** What the booked time is worth, and the variance against the
  standard, is T-4.WO.05's posting; this module moves the material the job actually
  used.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Numeric,
    String,
    Uuid,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.audit import set_actor
from app.db import Base
from app.manufacturing.bom import BomError
from app.manufacturing.issues import (
    ISSUE_TOLERANCE_PERCENT,
    issue_material,
    issued_quantity,
    issued_value,
    outstanding,
)
from app.manufacturing.work_orders import (
    COMPLETED,
    IN_PROGRESS,
    WorkOrder,
    direct_requirements,
)
from app.stock.entries import StockLedgerEntry
from app.stock.items import Item
from app.stock.locations import Location
from app.stock.transactions import receive

MONEY = Numeric(20, 6)
SCALE = Decimal("0.000001")
HUNDRED = Decimal(100)

# The receipt's source type — the document the stock ledger row names, and the key the
# mapping table resolves to the work-in-progress account it credits.
SOURCE_TYPE = "work_order_receipt"


class ReceiptError(BomError):
    """The production receipt refused what was asked of it."""


class WorkOrderNotRunning(ReceiptError):
    """Only a job that is being worked can report output."""


class OverReceiptError(ReceiptError):
    """The receipt would report more than the order was raised for."""


class ConsumptionNotCovered(ReceiptError):
    """The receipt would leave components unissued and no location was named to draw them."""


class WorkOrderReceipt(Base):
    """One receipt of finished goods from a work order."""

    __tablename__ = "work_order_receipt"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_work_order_receipt_quantity"),
        CheckConstraint("value >= 0", name="ck_work_order_receipt_value"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    work_order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("work_order.id"), nullable=False, index=True
    )
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("item.id"), nullable=False, index=True)
    location_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("location.id"), nullable=False, index=True
    )
    quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    uom: Mapped[str] = mapped_column(String(16), nullable=False)
    # What the receipt took out of work in progress — positive here, and what the
    # ledger entry credits to WIP and debits to inventory.
    value: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    movement_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stock_ledger_entry.id"), nullable=False, index=True
    )
    posted_on: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    # Whether this receipt completed the order's quantity.
    completes: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    work_order: Mapped[WorkOrder] = relationship()
    item: Mapped[Item] = relationship()
    location: Mapped[Location] = relationship()
    movement: Mapped[StockLedgerEntry] = relationship()


def receipts_of(session: Session, order: WorkOrder) -> list[WorkOrderReceipt]:
    """Every receipt on the order, oldest first — the movement's own order, as issues are."""
    return list(
        session.scalars(
            select(WorkOrderReceipt)
            .join(StockLedgerEntry, StockLedgerEntry.id == WorkOrderReceipt.movement_id)
            .where(WorkOrderReceipt.work_order_id == order.id)
            .order_by(WorkOrderReceipt.posted_on, StockLedgerEntry.created_at)
        )
    )


def received_quantity(session: Session, order: WorkOrder) -> Decimal:
    """How much output the job has reported, added from its receipts."""
    total = sum((Decimal(row.quantity) for row in receipts_of(session, order)), Decimal(0))
    return total.quantize(SCALE)


def received_value(session: Session, order: WorkOrder) -> Decimal:
    """What the receipts have taken out of work in progress so far."""
    total = sum((Decimal(row.value) for row in receipts_of(session, order)), Decimal(0))
    return total.quantize(SCALE)


def wip_balance(session: Session, order: WorkOrder) -> Decimal:
    """The material still sitting in work in progress for this job.

    What was issued less what the receipts have cleared — the figure the last receipt
    has to take to zero, and the one a cost accountant watches while the job runs.
    """
    return (issued_value(session, order) - received_value(session, order)).quantize(SCALE)


def _proportional_value(
    session: Session, order: WorkOrder, *, quantity: Decimal, completes: bool
) -> Decimal:
    """What a receipt takes out of WIP.

    A partial receipt takes the share of the material the quantity represents; the
    receipt that finishes the order takes **the rest**, so the account ends at zero
    rather than at whatever the division rounded away.
    """
    if completes:
        return wip_balance(session, order)
    issued = issued_value(session, order)
    share = (issued * quantity / Decimal(order.quantity)).quantize(SCALE)
    # A share can never take out more than the job is actually carrying.
    return min(share, (issued - received_value(session, order)).quantize(SCALE)).quantize(SCALE)


def receive_finished_goods(
    session: Session,
    order: WorkOrder,
    *,
    location: Location,
    quantity: Any,
    on: date,
    uom: str | None = None,
    backflush_location: Location | None = None,
    actor: str | None = None,
    tolerance_percent: Any = ISSUE_TOLERANCE_PERCENT,
) -> WorkOrderReceipt:
    """Receive produced quantity into stock, consuming the material it took.

    The components are issued first so the WIP the receipt clears includes what this
    receipt consumed: a job reporting half its output should not have half its
    material still sitting in the account.
    """
    if order.status not in (IN_PROGRESS, COMPLETED):
        raise WorkOrderNotRunning(
            f"work order {order.number!r} is {order.status}: output is reported by a job"
            " that is being worked, so release and start it first (T-4.WO.04)"
        )
    given = quantity if isinstance(quantity, Decimal) else Decimal(str(quantity))
    if given <= 0:
        raise ReceiptError(f"a receipt takes a positive quantity, got {given}")
    ordered = Decimal(order.quantity)
    already = received_quantity(session, order)
    if (already + given) > ordered:
        raise OverReceiptError(
            f"receiving {given} would report {already + given} against a work order for"
            f" {ordered}: a job does not make more than it was raised for (T-4.WO.04)"
        )
    completes = (already + given) == ordered
    if actor:
        set_actor(session, actor)

    if backflush_location is not None:
        _consume_proportionally(
            session,
            order=order,
            received=already + given,
            location=backflush_location,
            on=on,
            actor=actor,
            tolerance_percent=tolerance_percent,
        )
    else:
        _require_covered(session, order=order, received=already + given)

    value = _proportional_value(session, order, quantity=given, completes=completes)
    item = session.get(Item, order.item_id)
    stated_uom = str(uom or order.uom).strip()
    receipt_id = uuid.uuid4()
    movement = receive(
        session,
        item=item,
        location=location,
        uom=stated_uom,
        quantity=given,
        value=value,
        currency=_receipt_currency(session, order),
        source_type=SOURCE_TYPE,
        source_id=receipt_id,
        posting_date=on,
    )
    receipt = WorkOrderReceipt(
        id=receipt_id,
        company_id=order.company_id,
        work_order_id=order.id,
        item_id=item.id,
        location_id=location.id,
        quantity=given.quantize(SCALE),
        uom=stated_uom,
        value=value,
        currency=movement.currency,
        movement_id=movement.id,
        posted_on=on,
        completes=completes,
    )
    session.add(receipt)
    session.flush()
    return receipt


def _consume_proportionally(
    session: Session,
    *,
    order: WorkOrder,
    received: Decimal,
    location: Location,
    on: date,
    actor: str | None,
    tolerance_percent: Any,
) -> None:
    """Issue whatever the job's own components are short of the fraction received so far.

    The job's **direct** requirements: the materials of a component that is itself built
    are drawn by that component's own work order (T-4.WO.01's requirement list names them,
    but this job does not consume them).
    """
    share_of_order = (received / Decimal(order.quantity)).quantize(Decimal("0.000000000001"))
    for requirement in direct_requirements(session, order):
        target = (Decimal(requirement.quantity_required) * share_of_order).quantize(SCALE)
        shortfall = (target - issued_quantity(session, requirement)).quantize(SCALE)
        if shortfall <= 0:
            continue
        issue_material(
            session,
            order,
            item=session.get(Item, requirement.item_id),
            location=location,
            quantity=shortfall,
            on=on,
            actor=actor,
            # A backflush is the shop's own consumption at the quantity the BOM calls
            # for: it is never an exception, so it carries no override.
            tolerance_percent=tolerance_percent,
        )


def _require_covered(session: Session, *, order: WorkOrder, received: Decimal) -> None:
    """Refuse a receipt whose consumption nobody has drawn, naming what is missing.

    Judged on the job's **direct** components — what it actually consumes (see
    :func:`~app.manufacturing.work_orders.direct_requirements`).
    """
    share_of_order = (received / Decimal(order.quantity)).quantize(Decimal("0.000000000001"))
    missing = []
    for requirement in direct_requirements(session, order):
        target = (Decimal(requirement.quantity_required) * share_of_order).quantize(SCALE)
        shortfall = (target - issued_quantity(session, requirement)).quantize(SCALE)
        if shortfall > 0:
            missing.append(
                f"{session.get(Item, requirement.item_id).sku} {shortfall}"
            )
    if missing:
        raise ConsumptionNotCovered(
            f"receiving {received} of work order {order.number!r} consumes material"
            f" nobody has issued for it: {', '.join(missing)}. Name the location to draw"
            " it from, so the receipt reports the output and the consumption together"
            " (T-4.WO.04)"
        )


def _receipt_currency(session: Session, order: WorkOrder) -> str:
    from app.company import company_base_currency

    return company_base_currency(session, company_id=order.company_id)


def consumption_report(session: Session, order: WorkOrder) -> dict:
    """What the job required, issued and consumed, and where WIP stands.

    The check a cost accountant makes before closing the job: every requirement's
    issue is inside the tolerance, and the account the receipts were clearing is at
    zero once the order's quantity has been received.
    """
    rows = []
    within = True
    for row in outstanding(session, order):
        allowance = (
            row["required"] * (Decimal(1) + ISSUE_TOLERANCE_PERCENT / HUNDRED)
        ).quantize(SCALE)
        inside = row["issued"] <= allowance
        within = within and inside
        rows.append({**row, "within_tolerance": inside})
    received = received_quantity(session, order)
    return {
        "required": rows,
        "received": received,
        "ordered": Decimal(order.quantity).quantize(SCALE),
        "wip": wip_balance(session, order),
        "within_tolerance": within,
        "complete": received >= Decimal(order.quantity) and wip_balance(session, order) == 0,
    }


def receipt_lines(session: Session, order: WorkOrder) -> list[dict]:
    """The receipts as a report reads them, with what each took out of WIP."""
    return [
        {
            "quantity": Decimal(row.quantity).quantize(SCALE),
            "uom": row.uom,
            "value": Decimal(row.value).quantize(SCALE),
            "posted_on": row.posted_on,
            "completes": bool(row.completes),
            "location": session.get(Location, row.location_id).code,
        }
        for row in receipts_of(session, order)
    ]
