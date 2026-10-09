"""T-4.WC.01 — work centres: what a station can do, and what an hour of it costs.

A work centre is the thing capacity is measured in and the thing labour is costed
at, so three figures define it and each is stored in the form it will be read in:

* **Capacity is per period, and the period is stated.** A centre's capacity is a
  number of minutes *per day*, *per week* or *per month* (:data:`PERIODS`), and
  :func:`capacity_of` reports the period with the figure — a bare number would leave
  every reader to assume, and a month's capacity read as a day's is a plan nobody can
  build. A period that is not one of the three is refused.
* **Downtime reduces the capacity everything downstream uses.** The stored figure is
  the gross time the centre is manned; :func:`effective_capacity_minutes` applies the
  downtime allowance, and that — not the gross figure — is what T-4.WC.02 loads and
  what overload is judged against. Storing the net figure beside the gross one would
  be a second truth that can drift; the allowance is applied where it is read.
* **An hourly rate is dated.** :class:`WorkCenterRate` holds one rate per
  effective-from date, and :func:`rate_on` picks the rate in force **on the day the
  work happened** — so re-rating a centre today cannot restate what a job finished
  last month cost (T-4.WO.05 reads it that way). A date carries one rate: changing a
  rate is a new row on a new date, never an edit of the row a past job was costed at.

A zero is refused rather than stored: a centre with no capacity can never be loaded
and a zero rate silently values an hour of work at nothing, and both are almost always
a field somebody left blank. The refusal says which field, so the omission is fixed
rather than compensated for later.
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
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.audit import SoftDeleteMixin
from app.db import Base
from app.manufacturing.bom import BomError

MONEY = Numeric(20, 6)
MINUTES_SCALE = Decimal("0.000001")
HUNDRED = Decimal(100)

# The periods a capacity may be stated per. The plan names capacity, not its unit, so
# the unit is the centre's own to state and is reported back with every figure.
PERIODS = ("day", "week", "month")


class WorkCenterError(BomError):
    """The work centre refused what was asked of it."""


class DuplicateWorkCenterError(WorkCenterError):
    """That code is taken in this company."""


class UnknownWorkCenterError(WorkCenterError):
    """No work centre carries that code."""


class RateAlreadyDatedError(WorkCenterError):
    """That date already carries a rate: a change is a new date, not an edit."""


class WorkCenter(SoftDeleteMixin, Base):
    """One station: how long it is manned per period, and how much of that it loses."""

    __tablename__ = "work_center"
    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_work_center_code"),
        CheckConstraint(
            "capacity_period IN (" + ", ".join(f"'{period}'" for period in PERIODS) + ")",
            name="ck_work_center_period",
        ),
        # Zero capacity is refused; the period states what the figure is per.
        CheckConstraint("capacity_minutes > 0", name="ck_work_center_capacity"),
        CheckConstraint(
            "downtime_percent >= 0 AND downtime_percent < 100",
            name="ck_work_center_downtime",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # The gross minutes the centre is manned, and what they are per.
    capacity_minutes: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    capacity_period: Mapped[str] = mapped_column(String(8), nullable=False)
    # The share of that time the centre is expected to be down. Stored, applied on
    # read: the gross figure is what the planner stated, the effective one is derived.
    downtime_percent: Mapped[Decimal] = mapped_column(
        Numeric(9, 4), nullable=False, default=Decimal(0)
    )

    rates: Mapped[list[WorkCenterRate]] = relationship(
        back_populates="work_center", order_by="WorkCenterRate.effective_from"
    )

    def __repr__(self) -> str:  # pragma: no cover - a convenience for a caller's log
        return f"WorkCenter({self.code} {self.capacity_minutes}/{self.capacity_period})"


class WorkCenterRate(Base):
    """What an hour of this centre costs, from a date — the dated wage of the shop."""

    __tablename__ = "work_center_rate"
    __table_args__ = (
        UniqueConstraint(
            "work_center_id", "effective_from", name="uq_work_center_rate_date"
        ),
        CheckConstraint("hourly_rate > 0", name="ck_work_center_rate"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    work_center_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("work_center.id"), nullable=False, index=True
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    hourly_rate: Mapped[Decimal] = mapped_column(MONEY, nullable=False)

    work_center: Mapped[WorkCenter] = relationship(back_populates="rates")


def _amount(value: Any, what: str) -> Decimal:
    amount = value if isinstance(value, Decimal) else Decimal(str(value))
    return amount.quantize(MINUTES_SCALE)


def create_work_center(
    session: Session,
    *,
    company_id: uuid.UUID,
    code: str,
    name: str,
    capacity_minutes: Any,
    capacity_period: str,
    downtime_percent: Any = 0,
) -> WorkCenter:
    """Register a station: its capacity per period, and the share of it it loses."""
    wanted = str(code or "").strip()
    if not wanted:
        raise WorkCenterError("a work centre needs a code")
    if session.scalar(
        select(WorkCenter).where(
            WorkCenter.company_id == company_id, WorkCenter.code == wanted
        )
    ) is not None:
        raise DuplicateWorkCenterError(f"this company already has a work centre {wanted!r}")
    period = str(capacity_period or "").strip().lower()
    if period not in PERIODS:
        raise WorkCenterError(
            f"capacity is stated per one of {', '.join(PERIODS)}, not {capacity_period!r}"
        )
    capacity = _amount(capacity_minutes, "capacity")
    if capacity <= 0:
        raise WorkCenterError(
            f"work centre {wanted!r} states {capacity} minutes of capacity per {period}:"
            " a centre nobody can load is a wrong figure, not a decision (T-4.WC.01)"
        )
    downtime = _amount(downtime_percent, "downtime")
    if downtime < 0 or downtime >= HUNDRED:
        raise WorkCenterError(
            f"a downtime allowance is at least 0 and less than 100 percent, got {downtime}"
        )
    center = WorkCenter(
        company_id=company_id,
        code=wanted,
        name=str(name or wanted).strip(),
        capacity_minutes=capacity,
        capacity_period=period,
        downtime_percent=downtime,
    )
    session.add(center)
    session.flush()
    return center


def work_center_by_code(session: Session, *, company_id: uuid.UUID, code: str) -> WorkCenter:
    """The centre carrying a code, or a refusal — what a routing's code must resolve to."""
    center = session.scalar(
        select(WorkCenter).where(
            WorkCenter.company_id == company_id, WorkCenter.code == str(code).strip()
        )
    )
    if center is None:
        raise UnknownWorkCenterError(
            f"no work centre {code!r} in this company; a route that names one must be"
            " loadable (T-4.WC.01)"
        )
    return center


def known_codes(session: Session, *, company_id: uuid.UUID) -> set[str]:
    """Every code this company has registered — what a route's codes are checked against."""
    return set(
        session.scalars(select(WorkCenter.code).where(WorkCenter.company_id == company_id))
    )


def set_rate(
    session: Session,
    center: WorkCenter,
    *,
    effective_from: date,
    hourly_rate: Any,
) -> WorkCenterRate:
    """Date a new hourly rate for a centre.

    A date carries one rate: stating a second rate for a date already rated is refused
    rather than overwritten, because the figure a past job was costed at has to stay
    on the record. A zero rate is refused for the same reason a zero capacity is — an
    hour of work is not free, and a blank field is not a price.
    """
    rate = _amount(hourly_rate, "an hourly rate")
    if rate <= 0:
        raise WorkCenterError(
            f"an hourly rate is above zero, got {rate}: a rate nobody stated is not a"
            " rate of nothing (T-4.WC.01)"
        )
    if session.scalar(
        select(WorkCenterRate).where(
            WorkCenterRate.work_center_id == center.id,
            WorkCenterRate.effective_from == effective_from,
        )
    ) is not None:
        raise RateAlreadyDatedError(
            f"{center.code} is already rated from {effective_from}: a rate change is a"
            " new date, so the rate a past job was costed at stays on the record"
        )
    row = WorkCenterRate(
        company_id=center.company_id,
        work_center_id=center.id,
        effective_from=effective_from,
        hourly_rate=rate,
    )
    session.add(row)
    session.flush()
    return row


def rate_on(session: Session, center: WorkCenter, *, on: date) -> Decimal | None:
    """What an hour cost **on the day the work happened**, or ``None`` where nothing was.

    The rate in force is the latest one dated on or before that day: a rate raised
    today does not re-price yesterday's job, and a centre nobody had rated yet has no
    cost to state rather than a guessed one.
    """
    row = session.scalar(
        select(WorkCenterRate)
        .where(
            WorkCenterRate.work_center_id == center.id,
            WorkCenterRate.effective_from <= on,
        )
        .order_by(WorkCenterRate.effective_from.desc())
        .limit(1)
    )
    return None if row is None else Decimal(row.hourly_rate).quantize(MINUTES_SCALE)


def rate_history(session: Session, center: WorkCenter) -> list[dict]:
    """Every rate the centre has been on, oldest first — the dated record itself."""
    return [
        {"effective_from": row.effective_from, "hourly_rate": row.hourly_rate}
        for row in session.scalars(
            select(WorkCenterRate)
            .where(WorkCenterRate.work_center_id == center.id)
            .order_by(WorkCenterRate.effective_from)
        )
    ]


def effective_capacity_minutes(center: WorkCenter) -> Decimal:
    """The capacity a plan may use: the gross figure less the downtime allowance.

    Everything downstream loads this and not the stored figure, so a centre that is
    down a tenth of its time accepts a tenth less work — applied where it is read,
    never stored as a second capacity that could drift from the first.
    """
    gross = Decimal(center.capacity_minutes)
    lost = gross * (Decimal(center.downtime_percent) / HUNDRED)
    return (gross - lost).quantize(MINUTES_SCALE)


def capacity_of(session: Session, center: WorkCenter, *, on: date | None = None) -> dict:
    """The centre's capacity, its period, and the rate in force — one statement of each."""
    return {
        "work_center": center.code,
        "capacity_period": center.capacity_period,
        "capacity_minutes": Decimal(center.capacity_minutes).quantize(MINUTES_SCALE),
        "downtime_percent": Decimal(center.downtime_percent).quantize(MINUTES_SCALE),
        "effective_capacity_minutes": effective_capacity_minutes(center),
        "hourly_rate": rate_on(session, center, on=on) if on is not None else None,
    }
