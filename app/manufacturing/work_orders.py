"""T-4.WO.01 — the work order: what to make, from which version, and what it takes.

A work order is the commitment to produce something, so what it must contain is
everything the shop floor needs to be told, frozen at the moment it was raised:

* **The requirements are the BOM explosion, copied.** :func:`create_work_order` asks
  T-4.BOM.01's :func:`~app.manufacturing.bom.explode` for the ordered quantity and
  writes each level as a :class:`WorkOrderRequirement` row — same function, same
  up-lifted quantities, including scrap at every level, so a work order and the BOM it
  came from cannot disagree about what the job takes.
* **The version is pinned, and the route with it.** The order names the `bom` row it
  was raised against, and copies the routing's operations onto itself. Editing a BOM
  afterwards means revising it into a **new version**, which leaves this order's
  `bom_id`, its requirements and its operations exactly as they were: a work order
  raised yesterday still means what it meant yesterday.
* **A missing route is reported, not assumed.** The plan does not say whether an item
  may be built without a routing, so this does not decide it either: the order is
  raised and :func:`missing_route` answers *yes, this one has no operations* rather
  than a caller inferring that its route is somehow implied. What is refused is an
  item with **no released BOM at all** — there is nothing to expand, and inventing
  requirements would be worse than refusing.
* **Status moves are a transition, not an assignment.** :func:`advance` walks
  ``planned → released → in_progress → completed → closed``, refusing a jump the shop
  floor could not have made; the table carries the company dimension, so T-0.AUDIT.02's
  trail records every move with its before and after without this module writing one.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.db import Base
from app.manufacturing.bom import BomError, explode, released_bom
from app.manufacturing.routing import RoutingOperation, operations
from app.stock.items import Item

MONEY = Numeric(20, 6)
SCALE = Decimal("0.000001")

PLANNED, RELEASED, IN_PROGRESS, COMPLETED, CLOSED = (
    "planned",
    "released",
    "in_progress",
    "completed",
    "closed",
)
WORK_ORDER_STATUSES = (PLANNED, RELEASED, IN_PROGRESS, COMPLETED, CLOSED)

# What each status may become next. A work order is a sequence of commitments — a plan,
# a release, work happening, output, and the books closed on it — and skipping a step
# is a state nobody could have observed.
TRANSITIONS = {
    PLANNED: (RELEASED,),
    RELEASED: (IN_PROGRESS,),
    IN_PROGRESS: (COMPLETED,),
    COMPLETED: (CLOSED,),
    CLOSED: (),
}

MANUAL, MRP = "manual", "mrp"
SOURCES = (MANUAL, MRP)


class WorkOrderError(BomError):
    """The work order refused what was asked of it."""


class DuplicateWorkOrderError(WorkOrderError):
    """That number is taken in this company."""


class NoBomError(WorkOrderError):
    """There is no released BOM to build the item from."""


class WorkOrderStateError(WorkOrderError):
    """The status change does not follow from the one the order is in."""


class WorkOrder(Base):
    """One job: make this much of this item, from this version of its BOM."""

    __tablename__ = "work_order"
    __table_args__ = (
        UniqueConstraint("company_id", "number", name="uq_work_order_number"),
        CheckConstraint("quantity > 0", name="ck_work_order_quantity"),
        CheckConstraint(
            "status IN ("
            + ", ".join(f"'{status}'" for status in WORK_ORDER_STATUSES)
            + ")",
            name="ck_work_order_status",
        ),
        CheckConstraint("source IN ('manual', 'mrp')", name="ck_work_order_source"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("item.id"), nullable=False, index=True)
    quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    uom: Mapped[str] = mapped_column(String(16), nullable=False)
    # The BOM row this was raised against: the exact version, so a later revision of
    # the item's BOM cannot reach back into a job that is already released.
    bom_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bom.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=PLANNED)
    source: Mapped[str] = mapped_column(String(8), nullable=False, default=MANUAL)
    created_on: Mapped[date] = mapped_column(Date, nullable=False)
    due_on: Mapped[date | None] = mapped_column(Date, index=True)
    memo: Mapped[str | None] = mapped_column(String(200))

    item: Mapped[Item] = relationship()
    requirements: Mapped[list[WorkOrderRequirement]] = relationship(
        back_populates="work_order",
        order_by="WorkOrderRequirement.level, WorkOrderRequirement.id",
    )
    route: Mapped[list[WorkOrderOperation]] = relationship(
        back_populates="work_order", order_by="WorkOrderOperation.sequence"
    )

    def __repr__(self) -> str:  # pragma: no cover - a convenience for a caller's log
        return f"WorkOrder({self.number} {self.quantity} {self.status})"


class WorkOrderRequirement(Base):
    """How much of one component this job takes — the explosion, frozen."""

    __tablename__ = "work_order_requirement"
    __table_args__ = (
        CheckConstraint("quantity_required > 0", name="ck_work_order_requirement_quantity"),
        CheckConstraint("level >= 1", name="ck_work_order_requirement_level"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    work_order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("work_order.id"), nullable=False, index=True
    )
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("item.id"), nullable=False, index=True)
    # Total for the whole job, scrap already inside it: what WO.03 issues against.
    quantity_required: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    uom: Mapped[str] = mapped_column(String(16), nullable=False)
    # Where in the tree it came from, so a requirement can be explained by its level.
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    path: Mapped[str | None] = mapped_column(String(400))

    work_order: Mapped[WorkOrder] = relationship(back_populates="requirements")
    item: Mapped[Item] = relationship()


class WorkOrderOperation(Base):
    """One step of the pinned route: the routing, copied onto the job."""

    __tablename__ = "work_order_operation"
    __table_args__ = (
        UniqueConstraint("work_order_id", "sequence", name="uq_work_order_operation_seq"),
        CheckConstraint("sequence >= 1", name="ck_work_order_operation_sequence"),
        CheckConstraint("setup_minutes >= 0", name="ck_work_order_operation_setup"),
        CheckConstraint("run_minutes_per_unit > 0", name="ck_work_order_operation_run"),
        CheckConstraint("planned_quantity > 0", name="ck_work_order_operation_quantity"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    work_order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("work_order.id"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    work_center_code: Mapped[str | None] = mapped_column(String(32), index=True)
    setup_minutes: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    run_minutes_per_unit: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    # How many units this operation is planned to run over — the job's quantity when it
    # was raised, kept on the step so a load is computed from the step itself.
    planned_quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)

    work_order: Mapped[WorkOrder] = relationship(back_populates="route")


def create_work_order(
    session: Session,
    *,
    company_id: uuid.UUID,
    item: Item,
    quantity: Any,
    number: str,
    created_on: date,
    due_on: date | None = None,
    source: str = MANUAL,
    memo: str | None = None,
) -> WorkOrder:
    """Raise a **planned** work order, expanding the item's released BOM into requirements.

    The BOM must be released: a draft is still being written, and a job raised against
    it would be planned from a make-up that is not the one in use. A released BOM with
    **no routing** is allowed and visible (:func:`missing_route`) rather than refused —
    the plan does not say such an item may not be built, and reporting beats assuming.
    """
    wanted = str(number or "").strip()
    if not wanted:
        raise WorkOrderError("a work order needs a number")
    if item.company_id != company_id:
        raise WorkOrderError(f"item {item.sku!r} belongs to another company")
    if session.scalar(
        select(WorkOrder).where(
            WorkOrder.company_id == company_id, WorkOrder.number == wanted
        )
    ) is not None:
        raise DuplicateWorkOrderError(f"work order {wanted!r} already exists in this company")
    amount = quantity if isinstance(quantity, Decimal) else Decimal(str(quantity))
    if amount <= 0:
        raise WorkOrderError(f"a work order makes a positive quantity, got {amount}")
    chosen = str(source).strip().lower()
    if chosen not in SOURCES:
        raise WorkOrderError(f"a work order comes from {', '.join(SOURCES)}, not {source!r}")
    bom = released_bom(session, item)
    if bom is None:
        raise NoBomError(
            f"{item.sku!r} has no released BOM, so there is nothing to expand: release a"
            " version first (T-4.WO.01)"
        )

    order = WorkOrder(
        company_id=company_id,
        number=wanted,
        item_id=item.id,
        quantity=amount.quantize(SCALE),
        uom=item.base_uom,
        bom_id=bom.id,
        status=PLANNED,
        source=chosen,
        created_on=created_on,
        due_on=due_on,
        memo=memo,
    )
    session.add(order)
    session.flush()
    _copy_requirements(session, order=order, bom=bom)
    _copy_route(session, order=order, bom=bom)
    session.flush()
    return order


def _copy_requirements(session: Session, *, order: WorkOrder, bom) -> None:
    """Write the explosion — totals per item — as this job's requirements."""
    plan = explode(session, bom, quantity=order.quantity)
    for row in plan["levels"]:
        item = _item_by_sku(session, company_id=order.company_id, sku=row["item"])
        existing = _requirement_for(session, order=order, item_id=item.id)
        if existing is not None:
            # The same component at two levels adds up into one row, and the row keeps
            # the **shallowest** level it was seen at: a component this job takes itself
            # is never filed away under a sub-assembly that also uses it — which would
            # hide it from `direct_requirements` and let a receipt complete without it.
            # The quantity stays the total for the job, both uses together.
            existing.quantity_required = (
                Decimal(existing.quantity_required) + Decimal(row["quantity"])
            ).quantize(SCALE)
            if int(row["level"]) < int(existing.level):
                existing.level = int(row["level"])
                existing.path = row["path"]
            continue
        session.add(
            WorkOrderRequirement(
                company_id=order.company_id,
                work_order_id=order.id,
                item_id=item.id,
                quantity_required=Decimal(row["quantity"]).quantize(SCALE),
                uom=row["uom"],
                level=int(row["level"]),
                path=row["path"],
            )
        )
    session.flush()


def _copy_route(session: Session, *, order: WorkOrder, bom) -> None:
    """Copy the BOM's routing onto the job, so a later revision cannot change it."""
    for step in operations(session, bom):
        session.add(
            WorkOrderOperation(
                company_id=order.company_id,
                work_order_id=order.id,
                sequence=step.sequence,
                name=step.name,
                work_center_code=step.work_center_code,
                setup_minutes=step.setup_minutes,
                run_minutes_per_unit=step.run_minutes_per_unit,
                planned_quantity=order.quantity,
            )
        )
    session.flush()


def _requirement_for(
    session: Session, *, order: WorkOrder, item_id: uuid.UUID
) -> WorkOrderRequirement | None:
    return session.scalar(
        select(WorkOrderRequirement).where(
            WorkOrderRequirement.work_order_id == order.id,
            WorkOrderRequirement.item_id == item_id,
        )
    )


def _item_by_sku(session: Session, *, company_id: uuid.UUID, sku: str) -> Item:
    item = session.scalar(select(Item).where(Item.company_id == company_id, Item.sku == sku))
    if item is None:  # pragma: no cover - the explosion read it from the same table
        raise WorkOrderError(f"no item {sku!r} in this company")
    return item


def requirements_of(session: Session, order: WorkOrder) -> list[WorkOrderRequirement]:
    """The job's requirements, lowest level first — the whole explosion it was raised from."""
    return list(
        session.scalars(
            select(WorkOrderRequirement)
            .where(WorkOrderRequirement.work_order_id == order.id)
            .order_by(WorkOrderRequirement.level, WorkOrderRequirement.path)
        )
    )


def direct_requirements(
    session: Session, order: WorkOrder
) -> list[WorkOrderRequirement]:
    """The job's **own** components — level 1 — which is the material it draws and consumes.

    A requirement list is the whole BOM explosion (T-4.WO.01), so it also names what the
    components are made of. A job consumes what **its own item** is made of: the rows
    below level 1 are the materials of components that are themselves built, and those
    components' own work orders carry those rows. Drawing them here as well would consume
    the same material twice — 55 tubes for 10 frames, and 55 again for the bicycles that
    took the frames.

    A component the job takes directly *and* that a sub-assembly also uses is one row at
    level 1 carrying both quantities (:func:`_copy_requirements`), so it is counted here;
    that row's figure is the job's total use of it, which is what a receipt insists was
    drawn rather than the direct share alone.
    """
    return [row for row in requirements_of(session, order) if int(row.level) == 1]


def route_of(session: Session, order: WorkOrder) -> list[WorkOrderOperation]:
    """The job's pinned route, in sequence order."""
    return list(
        session.scalars(
            select(WorkOrderOperation)
            .where(WorkOrderOperation.work_order_id == order.id)
            .order_by(WorkOrderOperation.sequence)
        )
    )


def missing_route(session: Session, order: WorkOrder) -> bool:
    """Whether this order has no operations — reported, never silently assumed away."""
    return not route_of(session, order)


def work_order_by_number(
    session: Session, *, company_id: uuid.UUID, number: str
) -> WorkOrder:
    """The order a caller named, or a refusal."""
    order = session.scalar(
        select(WorkOrder).where(
            WorkOrder.company_id == company_id, WorkOrder.number == str(number)
        )
    )
    if order is None:
        raise WorkOrderError(f"no work order {number!r} in this company")
    return order


def bom_of(session: Session, order: WorkOrder):
    """The BOM version this order was raised against — the pin, read back."""
    from app.manufacturing.bom import Bom

    return session.get(Bom, order.bom_id)


def advance(session: Session, order: WorkOrder, *, status: str, actor: str | None = None) -> WorkOrder:
    """Move the order to its next status, refusing a step it could not have taken.

    The move is written to the row, and the row is audited (T-0.AUDIT.02), so who
    released it and when is on the trail without anything being written here.
    """
    wanted = str(status or "").strip().lower()
    if wanted not in WORK_ORDER_STATUSES:
        raise WorkOrderStateError(
            f"{wanted!r} is not a work order status; they are {', '.join(WORK_ORDER_STATUSES)}"
        )
    allowed = TRANSITIONS[order.status]
    if wanted not in allowed:
        raise WorkOrderStateError(
            f"work order {order.number!r} is {order.status} and becomes"
            f" {' or '.join(allowed) if allowed else 'nothing further'}; {wanted} does not"
            " follow from it (T-4.WO.01)"
        )
    order.status = wanted
    session.flush()
    return order


def reconcile_requirements(session: Session, order: WorkOrder) -> dict:
    """The job's requirements against a fresh explosion of its own pinned BOM.

    The one answer the acceptance criterion asks for: the requirements **equal** the
    explosion for the ordered quantity, including scrap at every level — recomputed
    from the version the order names, so the comparison is meaningful even after the
    item's BOM has moved on to a later version.
    """
    bom = bom_of(session, order)
    fresh = explode(session, bom, quantity=order.quantity)
    stored = {
        session.get(Item, row.item_id).sku: Decimal(row.quantity_required).quantize(SCALE)
        for row in requirements_of(session, order)
    }
    differences = {
        sku: {"expected": fresh["required"].get(sku), "stored": stored.get(sku)}
        for sku in set(fresh["required"]) | set(stored)
        if fresh["required"].get(sku) != stored.get(sku)
    }
    return {
        "bom_version": bom.version,
        "quantity": order.quantity,
        "matched": not differences,
        "differences": differences,
    }
