"""T-4.WO.02 — job cards: the time the shop floor booked, and what came off the bench.

A job card is one operation's record of itself, and the numbers on it are the only
evidence the shop floor produces, so they are written once and never rewritten:

* **A card belongs to one operation, and an entry is one booking.** A card names the
  work order and the operation (one of the pinned route's steps, T-4.WO.01), and one
  operator; the time and the output are :class:`JobCardEntry` rows under it. Total
  booked time is the **sum of the entries** — there is no running total on the card to
  drift from them — and so is everything derived from time, here and in the costing
  that reads it (T-4.WO.05).
* **Produced and rejected are two figures, and they reconcile to the order.** A
  rejected unit is not a unit produced; both are recorded, the net is derived, and
  :func:`output_of` states the gap against the work order's quantity so a shortfall is
  visible while the job is running rather than at the close.
* **Over the configured limit needs somebody to own it.** An entry that would take a
  card past the operation's planned minutes plus the tolerance is refused unless the
  caller acknowledges the overrun, and the acknowledgement is kept on the entry with
  the reason. The limit is a parameter with the module's stated default, so a company
  that measures differently passes its own.
* **A closed card is closed.** Closing an operation freezes its cards: a further
  booking is refused, and correcting what was recorded is a **new entry** that names
  the entry it corrects, who made it and why — an append, never an edit, so what was
  originally booked is still on the record beside its correction.

What the captured time is *worth* is deliberately not here: pricing minutes at the
work centre's dated rate is T-4.WO.05's question.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Uuid,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.db import Base
from app.manufacturing.bom import BomError
from app.manufacturing.work_orders import (
    COMPLETED,
    WorkOrder,
    WorkOrderOperation,
    route_of,
)

MONEY = Numeric(20, 6)
SCALE = Decimal("0.000001")
HUNDRED = Decimal(100)

OPEN, CLOSED = "open", "closed"
CARD_STATUSES = (OPEN, CLOSED)

# The tolerance a booking may run past an operation's planned minutes before somebody
# has to own it. Stated here as the default the module applies, and passed by any
# caller that measures its own shop differently.
TIME_TOLERANCE_PERCENT = Decimal("10")


class JobCardError(BomError):
    """The job card refused what was asked of it."""


class CardClosedError(JobCardError):
    """The card's operation is closed; a correction is a new, named entry."""


class UnknownOperationStepError(JobCardError):
    """The work order has no such step."""


class OverrunNotAcknowledged(JobCardError):
    """The booking runs past the operation's limit and nobody accepted it."""


class JobCard(Base):
    """One operation's card on one work order, worked by one operator."""

    __tablename__ = "job_card"
    __table_args__ = (
        CheckConstraint("status IN ('open', 'closed')", name="ck_job_card_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    work_order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("work_order.id"), nullable=False, index=True
    )
    operation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("work_order_operation.id"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    operator: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(8), nullable=False, default=OPEN)
    opened_on: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_by: Mapped[str | None] = mapped_column(String(64))

    work_order: Mapped[WorkOrder] = relationship()
    operation: Mapped[WorkOrderOperation] = relationship()
    entries: Mapped[list[JobCardEntry]] = relationship(
        back_populates="card", order_by="JobCardEntry.booked_at"
    )

    def __repr__(self) -> str:  # pragma: no cover - a convenience for a caller's log
        return f"JobCard({self.operator} step {int(self.sequence)} {self.status})"


class JobCardEntry(Base):
    """One booking: time on the card, and the output that came off with it."""

    __tablename__ = "job_card_entry"
    __table_args__ = (
        CheckConstraint("setup_minutes >= 0", name="ck_job_card_entry_setup"),
        CheckConstraint("run_minutes >= 0", name="ck_job_card_entry_run"),
        CheckConstraint("setup_minutes + run_minutes > 0", name="ck_job_card_entry_time"),
        CheckConstraint("produced_quantity >= 0", name="ck_job_card_entry_produced"),
        CheckConstraint("rejected_quantity >= 0", name="ck_job_card_entry_rejected"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    card_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("job_card.id"), nullable=False, index=True
    )
    booked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    setup_minutes: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    run_minutes: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    produced_quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    rejected_quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    note: Mapped[str | None] = mapped_column(String(200))
    # A correction names the entry it corrects and who made it: an append beside what
    # was booked, never an edit of it.
    corrects_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("job_card_entry.id"), index=True
    )
    recorded_by: Mapped[str | None] = mapped_column(String(64))
    # Whether the booking ran past the operation's limit and somebody accepted it.
    overrun_acknowledged_by: Mapped[str | None] = mapped_column(String(64))
    overrun_reason: Mapped[str | None] = mapped_column(String(200))

    card: Mapped[JobCard] = relationship(back_populates="entries")


def _minutes(value: Any, what: str) -> Decimal:
    amount = value if isinstance(value, Decimal) else Decimal(str(value))
    if amount < 0:
        raise JobCardError(f"{what} is not negative, got {amount}")
    return amount.quantize(SCALE)


def _quantity(value: Any, what: str) -> Decimal:
    amount = value if isinstance(value, Decimal) else Decimal(str(value))
    if amount < 0:
        raise JobCardError(f"{what} is not negative, got {amount}")
    return amount.quantize(SCALE)


def open_card(
    session: Session,
    order: WorkOrder,
    *,
    operation_sequence: int,
    operator: str,
    on: date,
    at: datetime | None = None,
) -> JobCard:
    """Issue a card for one step of the order's route, to one operator."""
    who = str(operator or "").strip()
    if not who:
        raise JobCardError("a job card names the operator who worked it")
    step = session.scalar(
        select(WorkOrderOperation).where(
            WorkOrderOperation.work_order_id == order.id,
            WorkOrderOperation.sequence == int(operation_sequence),
        )
    )
    if step is None:
        here = [row.sequence for row in route_of(session, order)]
        raise UnknownOperationStepError(
            f"work order {order.number!r} has no operation {operation_sequence}; its"
            f" steps are {here}"
        )
    card = JobCard(
        company_id=order.company_id,
        work_order_id=order.id,
        operation_id=step.id,
        sequence=step.sequence,
        operator=who,
        status=OPEN,
        opened_on=on,
        opened_at=at or datetime.now(timezone.utc),
    )
    session.add(card)
    session.flush()
    return card


def entries_of(session: Session, card: JobCard) -> list[JobCardEntry]:
    """Every booking on a card, oldest first — the rows every total is made of."""
    return list(
        session.scalars(
            select(JobCardEntry)
            .where(JobCardEntry.card_id == card.id)
            .order_by(JobCardEntry.booked_at, JobCardEntry.id)
        )
    )


def card_minutes(session: Session, card: JobCard) -> dict:
    """What a card has booked: setup, run and their total — the sum of its entries."""
    rows = entries_of(session, card)
    setup = sum((Decimal(row.setup_minutes) for row in rows), Decimal(0)).quantize(SCALE)
    run = sum((Decimal(row.run_minutes) for row in rows), Decimal(0)).quantize(SCALE)
    return {"setup_minutes": setup, "run_minutes": run, "total_minutes": (setup + run)}


def planned_minutes_of(card: JobCard) -> Decimal:
    """The operation's planned time for the job: the setup once, the run per unit."""
    step = card.operation
    return (
        Decimal(step.setup_minutes)
        + Decimal(step.run_minutes_per_unit) * Decimal(step.planned_quantity)
    ).quantize(SCALE)


def overrun_limit(card: JobCard, *, tolerance_percent: Any = TIME_TOLERANCE_PERCENT) -> Decimal:
    """The minutes a card may book before somebody has to accept the overrun."""
    tolerance = (
        tolerance_percent
        if isinstance(tolerance_percent, Decimal)
        else Decimal(str(tolerance_percent))
    )
    return (planned_minutes_of(card) * (Decimal(1) + tolerance / HUNDRED)).quantize(SCALE)


def operation_booked_minutes(
    session: Session, order: WorkOrder, *, sequence: int
) -> Decimal:
    """Everything booked on one **operation**, across every card raised for it.

    The limit an overrun is judged against is the operation's, not a card's: two cards
    on the same step share one planned time, and measuring each card separately would
    hand the second one a fresh budget for the same work.
    """
    total = Decimal(0)
    for card in cards_of(session, order):
        if int(card.sequence) != int(sequence):
            continue
        total += card_minutes(session, card)["total_minutes"]
    return total.quantize(SCALE)


def book_time(
    session: Session,
    card: JobCard,
    *,
    setup_minutes: Any = 0,
    run_minutes: Any = 0,
    produced_quantity: Any = 0,
    rejected_quantity: Any = 0,
    booked_at: datetime | None = None,
    note: str | None = None,
    corrects: JobCardEntry | None = None,
    recorded_by: str | None = None,
    acknowledge_overrun: bool = False,
    overrun_reason: str | None = None,
    tolerance_percent: Any = TIME_TOLERANCE_PERCENT,
) -> JobCardEntry:
    """Book time and output on a card, refusing an unowned overrun and a closed card.

    Time is split into a setup part and a run part because the shop measures them
    differently and the costing prices them from the same minutes; output is split into
    produced and rejected because a rejected unit is not a produced one. The overrun
    limit is the **operation's** planned time however many cards are working it.
    """
    if card.status != OPEN:
        raise CardClosedError(
            f"the card for step {int(card.sequence)} of work order"
            f" {card.work_order.number!r} is closed: further time is a correction, which"
            " names the entry it corrects (T-4.WO.02)"
        )
    setup = _minutes(setup_minutes, "a setup booking")
    run = _minutes(run_minutes, "a run booking")
    if setup + run <= 0:
        raise JobCardError(
            "a booking records time: a card entry with no setup and no run is not a"
            " booking"
        )
    produced = _quantity(produced_quantity, "a produced quantity")
    rejected = _quantity(rejected_quantity, "a rejected quantity")
    limit = overrun_limit(card, tolerance_percent=tolerance_percent)
    so_far = operation_booked_minutes(
        session, card.work_order, sequence=int(card.sequence)
    )
    would_be = (so_far + setup + run).quantize(SCALE)
    acknowledged = None
    if would_be > limit:
        if not acknowledge_overrun:
            raise OverrunNotAcknowledged(
                f"booking {setup + run} more minutes on step {int(card.sequence)} would"
                f" take the operation to {would_be} against a limit of {limit} (the"
                f" planned {planned_minutes_of(card)} plus {tolerance_percent} %):"
                " accept the overrun, naming who does (T-4.WO.02)"
            )
        acknowledged = str(recorded_by or card.operator).strip()
    entry = JobCardEntry(
        company_id=card.company_id,
        card_id=card.id,
        booked_at=booked_at or datetime.now(timezone.utc),
        setup_minutes=setup,
        run_minutes=run,
        produced_quantity=produced,
        rejected_quantity=rejected,
        note=note,
        corrects_entry_id=corrects.id if corrects is not None else None,
        recorded_by=recorded_by,
        overrun_acknowledged_by=acknowledged,
        overrun_reason=(str(overrun_reason).strip() if overrun_reason else None),
    )
    session.add(entry)
    session.flush()
    return entry


def correct_entry(
    session: Session,
    entry: JobCardEntry,
    *,
    actor: str,
    reason: str,
    setup_minutes: Any = 0,
    run_minutes: Any = 0,
    produced_quantity: Any = 0,
    rejected_quantity: Any = 0,
    booked_at: datetime | None = None,
) -> JobCardEntry:
    """Record a correction beside a closed card's entry, naming it and who made it.

    The original row stays exactly as it was booked: the correction is another entry
    that points at it, so the trail reads "this was booked, and here is what was
    recorded afterwards, by whom and why" rather than a figure that changed silently.
    """
    who = str(actor or "").strip()
    stated = str(reason or "").strip()
    if not who or not stated:
        raise JobCardError("a correction names both who made it and why")
    card = session.get(JobCard, entry.card_id)
    if card.status == OPEN:
        raise JobCardError(
            f"the card for step {int(card.sequence)} is still open: book the correction"
            " as ordinary time, or close it first — a correction is for what is closed"
        )
    corrected = JobCardEntry(
        company_id=entry.company_id,
        card_id=card.id,
        booked_at=booked_at or datetime.now(timezone.utc),
        setup_minutes=_minutes(setup_minutes, "a corrected setup booking"),
        run_minutes=_minutes(run_minutes, "a corrected run booking"),
        produced_quantity=_quantity(produced_quantity, "a corrected produced quantity"),
        rejected_quantity=_quantity(rejected_quantity, "a corrected rejected quantity"),
        note=stated,
        corrects_entry_id=entry.id,
        recorded_by=who,
    )
    session.add(corrected)
    session.flush()
    return corrected


def close_card(
    session: Session, card: JobCard, *, actor: str, at: datetime | None = None
) -> JobCard:
    """Close one card: its operation's time is recorded and the card stops taking more."""
    who = str(actor or "").strip()
    if not who:
        raise JobCardError("closing a card names who closed it")
    if card.status == CLOSED:
        raise CardClosedError(f"the card for step {int(card.sequence)} is already closed")
    card.status = CLOSED
    card.closed_at = at or datetime.now(timezone.utc)
    card.closed_by = who
    session.flush()
    return card


def cards_of(session: Session, order: WorkOrder) -> list[JobCard]:
    """Every card raised on a work order, oldest first."""
    return list(
        session.scalars(
            select(JobCard)
            .where(JobCard.work_order_id == order.id)
            .order_by(JobCard.opened_at, JobCard.sequence)
        )
    )


def booked_time(session: Session, order: WorkOrder) -> dict:
    """Everything booked on the order: the sum of its cards' entries, per operation.

    `equals_entries` is the acceptance criterion stated as a figure rather than a
    promise — the per-operation totals are the entries themselves, added.
    """
    total_setup = Decimal(0)
    total_run = Decimal(0)
    entries = 0
    per_operation: dict[int, dict] = {}
    for card in cards_of(session, order):
        rows = entries_of(session, card)
        entries += len(rows)
        setup = sum((Decimal(row.setup_minutes) for row in rows), Decimal(0)).quantize(SCALE)
        run = sum((Decimal(row.run_minutes) for row in rows), Decimal(0)).quantize(SCALE)
        total_setup += setup
        total_run += run
        step = per_operation.setdefault(
            int(card.sequence),
            {
                "sequence": int(card.sequence),
                "operation": card.operation.name,
                "work_center": card.operation.work_center_code,
                "setup_minutes": Decimal(0),
                "run_minutes": Decimal(0),
            },
        )
        step["setup_minutes"] = (step["setup_minutes"] + setup).quantize(SCALE)
        step["run_minutes"] = (step["run_minutes"] + run).quantize(SCALE)
    return {
        "entries": entries,
        "setup_minutes": total_setup.quantize(SCALE),
        "run_minutes": total_run.quantize(SCALE),
        "total_minutes": (total_setup + total_run).quantize(SCALE),
        "per_operation": [per_operation[key] for key in sorted(per_operation)],
    }


def output_of(session: Session, order: WorkOrder) -> dict:
    """What came off the benches: produced, rejected, their net, and the gap to the order.

    Produced and rejected stay apart — a rejected unit was not made — and the net is
    what the order has to show against the quantity it was raised for.
    """
    produced = Decimal(0)
    rejected = Decimal(0)
    for card in cards_of(session, order):
        for row in entries_of(session, card):
            produced += Decimal(row.produced_quantity)
            rejected += Decimal(row.rejected_quantity)
    net = (produced - rejected).quantize(SCALE)
    return {
        "produced": produced.quantize(SCALE),
        "rejected": rejected.quantize(SCALE),
        "net": net,
        "ordered": Decimal(order.quantity).quantize(SCALE),
        "difference": (Decimal(order.quantity) - net).quantize(SCALE),
    }


def time_by_work_center(session: Session, order: WorkOrder) -> dict[str, Decimal]:
    """Booked minutes per work centre — what T-4.WO.05 prices at the dated rate."""
    booked = booked_time(session, order)
    out: dict[str, Decimal] = {}
    for step in booked["per_operation"]:
        code = step["work_center"] or "unassigned"
        minutes = step["setup_minutes"] + step["run_minutes"]
        out[code] = (out.get(code, Decimal(0)) + minutes).quantize(SCALE)
    return out


def is_complete(session: Session, order: WorkOrder) -> bool:
    """Whether every step of the route has been closed and the output is in."""
    cards = cards_of(session, order)
    steps = {row.sequence for row in route_of(session, order)}
    if not steps:
        return False
    closed = {int(card.sequence) for card in cards if card.status == CLOSED}
    return steps <= closed and output_of(session, order)["difference"] <= 0


def finished(session: Session, order: WorkOrder) -> bool:
    """Whether the order has reached the end of its own status walk."""
    return order.status in (COMPLETED, "closed")


def counted_cards(session: Session, order: WorkOrder) -> int:
    """How many cards the order has, closed or not — for a report's line."""
    return int(
        session.scalar(
            select(func.count()).select_from(JobCard).where(JobCard.work_order_id == order.id)
        )
        or 0
    )
