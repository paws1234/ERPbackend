"""T-4.WO.03 — issuing material to a work order, and the WIP it puts on the books.

Issuing is where the plan meets the shelf, so three things happen at once and must
agree afterwards: stock leaves a location, the job's requirement is met, and the
ledger says what the material is worth.

* **Issued quantities are tracked against the requirement.** :func:`outstanding`
  answers, per requirement, what the job asked for, what has been issued and what is
  left — derived from the issue rows each time, so the remaining figure can never
  drift from the documents that make it.
* **An over-issue is refused unless somebody overrides it, and the override is
  recorded.** A little over the requirement is normal (a length of stock that does not
  divide evenly), so a tolerance is allowed; past it, the issue is refused unless the
  caller names an actor and a reason, and both are kept on the issue row. Recording
  the exception is the point — an over-issue nobody wrote down is a total nobody can
  explain.
* **The stock movement and the WIP posting are the same document.** The issue goes
  through T-1.INV.05's :func:`~app.stock.transactions.issue`, which writes the stock
  ledger row and posts its GL half in this transaction, and the movement names **this
  issue** as its source. The value is the costing method's (T-1.INV.04), so the WIP
  account holds what the goods cost rather than what somebody typed.

The counterpart account is a mapping key — `work_in_progress` — so a company points
it at its own WIP account; T-4.WO.04 clears it with the finished-goods receipt and
T-4.WO.05 puts the labour and the variance through the same account.
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
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.audit import set_actor
from app.db import Base
from app.manufacturing.bom import BomError
from app.manufacturing.work_orders import (
    WorkOrder,
    WorkOrderRequirement,
    requirements_of,
)
from app.stock.entries import StockLedgerEntry, on_hand
from app.stock.items import Item
from app.stock.locations import Location
from app.stock.transactions import issue

MONEY = Numeric(20, 6)
SCALE = Decimal("0.000001")
HUNDRED = Decimal(100)

# The material issue's source type: the document the stock ledger row names, and the
# key T-1.INV.07's mapping table resolves to the work-in-progress account.
SOURCE_TYPE = "work_order_issue"
WIP_KEY = "work_in_progress"

# How far past a requirement an issue may go without somebody owning it. A little is
# normal — stock does not always divide into the quantity asked for — and past it the
# issue is an exception that is recorded rather than quietly absorbed.
ISSUE_TOLERANCE_PERCENT = Decimal("5")


class IssueError(BomError):
    """The material issue refused what was asked of it."""


class NotRequiredError(IssueError):
    """The item is not one of this order's requirements."""


class OverIssueError(IssueError):
    """The issue runs past the requirement and its tolerance, and nobody overrode it."""


class WorkOrderIssue(Base):
    """One issue of one requirement's material to one work order."""

    __tablename__ = "work_order_issue"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_work_order_issue_quantity"),
        CheckConstraint("value >= 0", name="ck_work_order_issue_value"),
        CheckConstraint(
            "NOT overridden OR (override_reason IS NOT NULL AND override_actor IS NOT NULL)",
            name="ck_work_order_issue_override",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    work_order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("work_order.id"), nullable=False, index=True
    )
    requirement_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("work_order_requirement.id"), nullable=False, index=True
    )
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("item.id"), nullable=False, index=True)
    location_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("location.id"), nullable=False, index=True
    )
    # In the item's base UOM, as the stock ledger stores it.
    quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    uom: Mapped[str] = mapped_column(String(16), nullable=False)
    # What the issue cost, from the costing method — positive here, negative in the
    # ledger entry it wrote, because this row states what left rather than its sign.
    value: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    # The stock ledger row this issue wrote: the document the movement names.
    movement_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stock_ledger_entry.id"), nullable=False, index=True
    )
    posted_on: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    # An issue past the requirement and its tolerance, owned by somebody.
    overridden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    override_actor: Mapped[str | None] = mapped_column(String(64))
    override_reason: Mapped[str | None] = mapped_column(String(200))

    work_order: Mapped[WorkOrder] = relationship()
    requirement: Mapped[WorkOrderRequirement] = relationship()
    item: Mapped[Item] = relationship()
    location: Mapped[Location] = relationship()
    movement: Mapped[StockLedgerEntry] = relationship()


def requirement_for(
    session: Session, order: WorkOrder, *, item: Item
) -> WorkOrderRequirement:
    """The order's requirement for an item, or a refusal naming what the job does take.

    Issuing something the job does not call for is not a stock transfer with a story:
    it is material charged to a job that did not ask for it, so it is refused.
    """
    for row in requirements_of(session, order):
        if row.item_id == item.id:
            return row
    required = [session.get(Item, row.item_id).sku for row in requirements_of(session, order)]
    raise NotRequiredError(
        f"work order {order.number!r} does not call for {item.sku!r}; it requires"
        f" {required}"
    )


def _batch(session: Session, *, item: Item, code: str | None):
    """The batch named for this issue, opened nowhere: a batch must have arrived first."""
    from app.stock.batches import batch_by_code

    return None if code is None else batch_by_code(session, item=item, code=str(code).strip())


def _serial(session: Session, *, item: Item, code: str | None):
    """The unit named for this issue, which must already be registered."""
    from app.stock.serials import serial_by_code

    return None if code is None else serial_by_code(session, item=item, code=str(code).strip())


def issues_of(session: Session, order: WorkOrder) -> list[WorkOrderIssue]:
    """Every issue on the order, oldest first.

    Ordered by the day it was posted and then by the **movement's** own creation time,
    not by the issue's uuid: two issues on one day are told apart by which was actually
    written first, so a report of the day reads in the order the store worked.
    """
    return list(
        session.scalars(
            select(WorkOrderIssue)
            .join(StockLedgerEntry, StockLedgerEntry.id == WorkOrderIssue.movement_id)
            .where(WorkOrderIssue.work_order_id == order.id)
            .order_by(WorkOrderIssue.posted_on, StockLedgerEntry.created_at)
        )
    )


def issued_quantity(session: Session, requirement: WorkOrderRequirement) -> Decimal:
    """How much of one requirement has been issued, added from its issue rows."""
    total = session.scalar(
        select(func.coalesce(func.sum(WorkOrderIssue.quantity), 0)).where(
            WorkOrderIssue.requirement_id == requirement.id
        )
    )
    return Decimal(total or 0).quantize(SCALE)


def outstanding(session: Session, order: WorkOrder) -> list[dict]:
    """Every requirement with what it asked for, what is issued and what is left.

    The rows below level 1 are the materials of components this item is built from *and*
    that are themselves built: their own work orders draw them (see
    :func:`~app.manufacturing.work_orders.direct_requirements`), so they show here with
    what was issued against this order, which is usually nothing.
    """
    rows = []
    for requirement in requirements_of(session, order):
        item = session.get(Item, requirement.item_id)
        issued = issued_quantity(session, requirement)
        rows.append(
            {
                "item": item.sku,
                "level": requirement.level,
                "required": Decimal(requirement.quantity_required).quantize(SCALE),
                "issued": issued,
                "remaining": (Decimal(requirement.quantity_required) - issued).quantize(
                    SCALE
                ),
                "uom": requirement.uom,
            }
        )
    return rows


def issue_material(
    session: Session,
    order: WorkOrder,
    *,
    item: Item,
    location: Location,
    quantity: Any,
    on: date,
    uom: str | None = None,
    actor: str | None = None,
    override: bool = False,
    override_reason: str | None = None,
    tolerance_percent: Any = ISSUE_TOLERANCE_PERCENT,
    batch_code: str | None = None,
    serial_code: str | None = None,
) -> WorkOrderIssue:
    """Issue one requirement's material to the order: stock out, WIP in.

    `quantity` is stated in `uom` (the item's base UOM where none is named) and is
    converted on the way to the ledger, which stores the item's base UOM. The issue
    row is written first so the stock movement can name it as its document, and the
    movement's value — the costing method's answer — is what this row and the WIP
    posting carry.

    `batch_code`/`serial_code` name **which** lot or unit was consumed, and the movement
    stores it (T-6.TRACE.03): tracing a finished good back to its material needs the
    material's own identity on the ledger, and a tracked item cannot be issued without it.
    Naming one is the caller's decision — the batch that was actually taken off the shelf,
    not whichever the costing method would have preferred.
    """
    requirement = requirement_for(session, order, item=item)
    given = quantity if isinstance(quantity, Decimal) else Decimal(str(quantity))
    if given <= 0:
        raise IssueError(f"an issue takes a positive quantity, got {given}")
    tolerance = (
        tolerance_percent
        if isinstance(tolerance_percent, Decimal)
        else Decimal(str(tolerance_percent))
    )
    required = Decimal(requirement.quantity_required)
    already = issued_quantity(session, requirement)
    ceiling = (required * (Decimal(1) + tolerance / HUNDRED)).quantize(SCALE)
    if (already + given) > ceiling:
        if not override:
            raise OverIssueError(
                f"issuing {given} of {item.sku!r} would take work order"
                f" {order.number!r} to {already + given} against a requirement of"
                f" {required} (tolerance {tolerance} % → {ceiling}): override it with"
                " an actor and a reason, so the exception is on the record (T-4.WO.03)"
            )
        who = str(actor or "").strip()
        why = str(override_reason or "").strip()
        if not who or not why:
            raise OverIssueError(
                "an over-issue override names both who accepted it and why"
            )
    if actor:
        set_actor(session, actor)

    # The issue's id is minted here so the stock movement can name it as its document:
    # the movement is written first (it may refuse on availability, which must leave
    # nothing behind), and the issue row then points at it.
    issue_id = uuid.uuid4()
    movement = issue(
        session,
        item=item,
        location=location,
        uom=str(uom or item.base_uom).strip(),
        quantity=given,
        currency=order_currency(session, order),
        source_type=SOURCE_TYPE,
        source_id=issue_id,
        posting_date=on,
        actor=actor,
        batch=_batch(session, item=item, code=batch_code),
        serial=_serial(session, item=item, code=serial_code),
    )
    posted = WorkOrderIssue(
        id=issue_id,
        company_id=order.company_id,
        work_order_id=order.id,
        requirement_id=requirement.id,
        item_id=item.id,
        location_id=location.id,
        quantity=given.quantize(SCALE),
        uom=str(uom or item.base_uom).strip(),
        # The ledger's value is negative for an issue; this row states what left.
        value=(-Decimal(movement.value)).quantize(SCALE),
        currency=movement.currency,
        movement_id=movement.id,
        posted_on=on,
        overridden=bool(override),
        override_actor=(str(actor).strip() if override and actor else None),
        override_reason=(str(override_reason).strip() if override and override_reason else None),
    )
    session.add(posted)
    session.flush()
    return posted


def order_currency(session: Session, order: WorkOrder) -> str:
    """The currency the job's material is booked in: the company's own base currency."""
    from app.company import company_base_currency

    if order.company_id is None:  # pragma: no cover - a work order always has a company
        raise IssueError("a work order without a company has no currency")
    return company_base_currency(session, company_id=order.company_id)


def issued_value(session: Session, order: WorkOrder) -> Decimal:
    """What the order's material has cost so far — the material sitting in WIP."""
    total = sum((Decimal(row.value) for row in issues_of(session, order)), Decimal(0))
    return total.quantize(SCALE)


def movements_of(session: Session, order: WorkOrder) -> list[StockLedgerEntry]:
    """Every stock ledger row the order's issues wrote — the stock side of the drill-down."""
    return [
        row.movement
        for row in issues_of(session, order)
        if row.movement is not None
    ]


def issue_lines(session: Session, order: WorkOrder) -> list[dict]:
    """The issues as a report reads them, newest last, with their stock effect."""
    out = []
    for row in issues_of(session, order):
        item = session.get(Item, row.item_id)
        out.append(
            {
                "item": item.sku,
                "location": session.get(Location, row.location_id).code,
                "quantity": Decimal(row.quantity).quantize(SCALE),
                "uom": row.uom,
                "value": Decimal(row.value).quantize(SCALE),
                "posted_on": row.posted_on,
                "overridden": bool(row.overridden),
                "override_actor": row.override_actor,
            }
        )
    return out


def location_stock(session: Session, *, item: Item, location: Location) -> Decimal:
    """What a location holds of an item — what an issue is checked against by the store."""
    held = on_hand(
        session,
        company_id=item.company_id,
        item_id=item.id,
        location_id=location.id,
    )
    return Decimal(held["quantity"]).quantize(SCALE)


def issued_to_wip(session: Session, order: WorkOrder) -> dict:
    """The WIP the order's material put on the books: what was issued, and for how much."""
    rows = issues_of(session, order)
    return {
        "issues": len(rows),
        "value": issued_value(session, order),
        "overridden": [row.override_reason for row in rows if row.overridden],
    }
