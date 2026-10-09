"""T-4.BOM.02 — the routing: which operations a BOM's item goes through, in order.

A BOM says what a product is made of; the routing says what is *done* to it, and in
what order. Three decisions, each of them about not leaving a reader to guess:

* **Sequence is dense and unique, from 1.** An operation's sequence is what the shop
  floor follows, so a gap or a duplicate is refused as it is written rather than
  discovered by whoever starts at the missing number. New operations append; a number
  that is not the next one is refused with the number that was expected.
* **Times state their own basis.** Setup is incurred **once per batch** and the run
  time is **per unit**: the two are stored as such and reported with their basis
  labelled (`per_batch`, `per_unit`), so nobody has to infer from a column name
  whether a figure is per piece or for the whole run. A run time stated for a batch of
  *n* units is restated per unit on the way in — `run_basis_units` keeps what the
  planner actually stated — because a stored figure and the basis it is stored in must
  not be two different facts (the same reason T-1.INV.01 keeps one conversion row).
* **A component belongs to an operation, or to the BOM.** A line's `operation_id` is
  null for a component consumed by the job as a whole, and names the operation that
  consumes it otherwise: :func:`routing` reports both groups, so a component that is
  accounted for at an operation is still found on the BOM and never dropped for being
  somewhere specific. The work centre is named by **code** here — T-4.WC.01 owns what
  a code's capacity, downtime and rate are — and an operation with no code is
  reported as unassigned rather than quietly loading nobody's capacity (T-4.WC.02).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.db import Base
from app.manufacturing.bom import DRAFT, SCALE, Bom, BomError, BomLine, lines_of

MONEY = Numeric(20, 6)
MINUTES_SCALE = Decimal("0.000001")

# The basis each stored time is stated in, and it is stated on every read.
SETUP_BASIS = "per_batch"
RUN_BASIS = "per_unit"


class RoutingError(BomError):
    """The routing refused what was asked of it."""


class RoutingLockedError(RoutingError):
    """The BOM is released: its routing is frozen like its lines."""


class DuplicateOperationError(RoutingError):
    """That sequence is taken, or that operation name is used twice."""


class SequenceGapError(RoutingError):
    """The sequence would leave a number nobody performs."""


class UnknownOperationError(RoutingError):
    """The operation named is not one of this BOM's."""


class RoutingOperation(Base):
    """One operation of one BOM, at one step of the sequence."""

    __tablename__ = "routing_operation"
    __table_args__ = (
        UniqueConstraint("bom_id", "sequence", name="uq_routing_operation_sequence"),
        CheckConstraint("sequence >= 1", name="ck_routing_sequence_starts_at_one"),
        # An operation that takes no time at all is a step somebody forgot to fill in,
        # not a decision: the setup may be zero (some work needs none) but the run
        # time of a make operation is not nothing.
        CheckConstraint("setup_minutes >= 0", name="ck_routing_setup_not_negative"),
        CheckConstraint("run_minutes_per_unit > 0", name="ck_routing_run_positive"),
        CheckConstraint("run_basis_units >= 1", name="ck_routing_basis_units"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    bom_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bom.id"), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # The work centre's code, not its row: T-4.WC.01 owns that master, and the routing
    # must be writable before it (an operation nobody has assigned yet is null).
    work_center_code: Mapped[str | None] = mapped_column(String(32), index=True)
    # Incurred once per batch, whatever the batch holds.
    setup_minutes: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    # Stored per unit; `run_basis_units` remembers the batch the planner stated it for.
    run_minutes_per_unit: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    run_basis_units: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    bom: Mapped[Bom] = relationship()


def _minutes(value: Any, what: str) -> Decimal:
    amount = value if isinstance(value, Decimal) else Decimal(str(value))
    if amount < 0:
        raise RoutingError(f"{what} is not negative, got {amount}")
    return amount.quantize(MINUTES_SCALE)


def operations(session: Session, bom: Bom) -> list[RoutingOperation]:
    """The BOM's operations in the order the shop floor performs them."""
    return list(
        session.scalars(
            select(RoutingOperation)
            .where(RoutingOperation.bom_id == bom.id)
            .order_by(RoutingOperation.sequence)
        )
    )


def operation_at(session: Session, bom: Bom, sequence: int) -> RoutingOperation:
    """One step of the routing, or a refusal naming the steps that exist."""
    operation = session.scalar(
        select(RoutingOperation).where(
            RoutingOperation.bom_id == bom.id, RoutingOperation.sequence == int(sequence)
        )
    )
    if operation is None:
        here = [row.sequence for row in operations(session, bom)]
        raise UnknownOperationError(
            f"BOM v{bom.version} has no operation {sequence}; its steps are {here}"
        )
    return operation


def _require_draft(bom: Bom) -> None:
    if bom.status != DRAFT:
        raise RoutingLockedError(
            f"BOM v{bom.version} is {bom.status}: the routing of a BOM in use is frozen"
            " with it — revise the BOM into a new version to change the route"
            " (T-4.BOM.02)"
        )


def add_operation(
    session: Session,
    bom: Bom,
    *,
    name: str,
    run_minutes: Any,
    run_per_units: int = 1,
    setup_minutes: Any = 0,
    work_center_code: str | None = None,
    sequence: int | None = None,
) -> RoutingOperation:
    """Append one operation to a **draft** BOM's routing.

    `run_minutes` is the time for `run_per_units` units — state it for one unit, or for
    the batch the planner actually timed — and it is stored per unit either way, so
    every operation's run time is comparable. `sequence` may only be the next number:
    passing one that would leave a hole is refused rather than accepted and hidden.
    """
    _require_draft(bom)
    stated = str(name or "").strip()
    if not stated:
        raise RoutingError("an operation needs a name")
    if session.scalar(
        select(RoutingOperation).where(
            RoutingOperation.bom_id == bom.id, RoutingOperation.name == stated
        )
    ) is not None:
        raise DuplicateOperationError(f"BOM v{bom.version} already routes {stated!r}")
    per_units = int(run_per_units)
    if per_units < 1:
        raise RoutingError(f"a run time covers at least one unit, got {per_units}")
    run = _minutes(run_minutes, "a run time")
    if run <= 0:
        raise RoutingError(f"an operation's run time is above zero, got {run}")
    here = operations(session, bom)
    expected = (here[-1].sequence + 1) if here else 1
    wanted = expected if sequence is None else int(sequence)
    if wanted != expected:
        taken = {row.sequence for row in here}
        if wanted in taken:
            raise DuplicateOperationError(
                f"BOM v{bom.version} already has an operation at sequence {wanted}"
            )
        raise SequenceGapError(
            f"the next operation of BOM v{bom.version} is {expected}, not {wanted}:"
            " a routing with a hole in it is a step nobody performs"
        )
    operation = RoutingOperation(
        company_id=bom.company_id,
        bom_id=bom.id,
        sequence=wanted,
        name=stated,
        work_center_code=(str(work_center_code).strip() if work_center_code else None),
        setup_minutes=_minutes(setup_minutes, "a setup time"),
        run_minutes_per_unit=(run / Decimal(per_units)).quantize(MINUTES_SCALE),
        run_basis_units=per_units,
    )
    session.add(operation)
    session.flush()
    return operation


def assign_component(
    session: Session, bom: Bom, *, line: BomLine, operation: RoutingOperation | None
) -> BomLine:
    """Say which operation consumes a component — or that the BOM consumes it generally.

    `operation` of ``None`` is not "unassigned by mistake": it is the statement that
    the line belongs to the job rather than to one step, and :func:`routing` reports
    the two groups apart.
    """
    _require_draft(bom)
    if line.bom_id != bom.id:
        raise RoutingError(
            f"line {line.line_no} belongs to BOM {line.bom_id}, not v{bom.version} of this one"
        )
    if operation is not None and operation.bom_id != bom.id:
        raise UnknownOperationError(
            f"operation {operation.sequence} belongs to another BOM"
        )
    line.operation_id = operation.id if operation is not None else None
    session.flush()
    return line


def run_basis_minutes(operation: RoutingOperation) -> Decimal:
    """The run time as the planner stated it, for the batch they stated it for."""
    return (Decimal(operation.run_minutes_per_unit) * operation.run_basis_units).quantize(
        MINUTES_SCALE
    )


def operation_minutes(operation: RoutingOperation, quantity: Any = Decimal(1)) -> Decimal:
    """What one operation takes for a batch: the setup once, the run per unit.

    The only arithmetic that mixes the two bases, and it is here rather than spread
    over the callers, so capacity planning (T-4.WC.02) and costing (T-4.WO.05) cannot
    disagree about what an operation's hour is.
    """
    units = quantity if isinstance(quantity, Decimal) else Decimal(str(quantity))
    return (
        Decimal(operation.setup_minutes)
        + (Decimal(operation.run_minutes_per_unit) * units)
    ).quantize(MINUTES_SCALE)


def routing_minutes(session: Session, bom: Bom, *, quantity: Any = Decimal(1)) -> Decimal:
    """What the whole routing takes for one batch of `quantity`: every step, in order."""
    total = sum(
        (operation_minutes(row, quantity) for row in operations(session, bom)),
        Decimal(0),
    )
    return total.quantize(MINUTES_SCALE)


def routing(session: Session, bom: Bom) -> dict:
    """The routing as it is followed: every step, its basis, and its components.

    Both groups of components are here — the ones an operation consumes and the ones
    the BOM consumes generally — because a report that showed only one of them would
    look like the other had been dropped.
    """
    lines = lines_of(session, bom)
    steps = []
    claimed: set[uuid.UUID] = set()
    for operation in operations(session, bom):
        consumed = [line for line in lines if line.operation_id == operation.id]
        claimed.update(line.id for line in consumed)
        steps.append(
            {
                "sequence": operation.sequence,
                "operation": operation.name,
                "work_center": operation.work_center_code,
                "setup_minutes": operation.setup_minutes,
                "setup_basis": SETUP_BASIS,
                "run_minutes_per_unit": operation.run_minutes_per_unit,
                "run_basis": RUN_BASIS,
                "run_stated_for_units": operation.run_basis_units,
                "components": [
                    {"line_no": line.line_no, "item_id": line.item_id, "quantity": line.quantity}
                    for line in consumed
                ],
            }
        )
    return {
        "item": session.get(type(bom.item), bom.item_id).sku,
        "bom_version": bom.version,
        "status": bom.status,
        "operations": steps,
        "general_components": [
            {"line_no": line.line_no, "item_id": line.item_id, "quantity": line.quantity}
            for line in lines
            if line.id not in claimed
        ],
    }


def unassigned(session: Session, bom: Bom) -> list[dict]:
    """The operations that name no work centre — reported, never silently not loaded."""
    return [
        {"sequence": row.sequence, "operation": row.name}
        for row in operations(session, bom)
        if not row.work_center_code
    ]


def sequence_check(session: Session, bom: Bom) -> dict:
    """The sequence as it stands: dense from 1, unique — what the refusal above keeps true."""
    seen = [row.sequence for row in operations(session, bom)]
    return {
        "sequences": seen,
        "dense": seen == list(range(1, len(seen) + 1)),
        "duplicates": sorted({value for value in seen if seen.count(value) > 1}),
    }


def count_operations(session: Session, bom: Bom) -> int:
    """How many steps the route has."""
    return int(
        session.scalar(
            select(func.count())
            .select_from(RoutingOperation)
            .where(RoutingOperation.bom_id == bom.id)
        )
        or 0
    )
