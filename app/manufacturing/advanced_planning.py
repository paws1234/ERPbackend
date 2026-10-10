"""T-6.ADV.01 — advanced planning: the same net requirements, placed where the shop can make them.

T-4.MRP.01 nets what is missing; T-4.WC.02 says which periods a centre is over. This
module joins the two. It takes a basic run's net requirements, loads the **make** ones
onto the work centres their routing names, and moves a requirement's release later —
whole buckets at a time — until the centre it needs has room for it. Every move carries
the constraint that caused it, so a planner reads *why* a date moved and not only that
it did.

Three properties make it a plan rather than a second opinion:

* **A move states its cause.** The centre, the load that did not fit, the capacity it did
  not fit into, and the bucket it moved to. A requirement the horizon cannot absorb is
  reported as `unresolved` with the same figures rather than silently overloading.
* **A dataset with room plans exactly as the basic run does.** No load over capacity
  means no move, and the advanced rows *are* the basic net requirements, bucket for
  bucket — :func:`reconcile` is that comparison, stated by the module rather than left
  to whoever is reading two plans.
* **The same data gives the same plan, and a demand change rewrites only what it
  touched.** The ordering is canonical — bucket, item, then the routing's own sequence —
  and never the clock, so two runs over unchanged data are identical;
  :func:`compare_plans` names which rows a change moved and which it left alone.

The load is placed on the day the work is **released** (`release_on`, which T-4.MRP.01
dated `lead_time_days` before the item is wanted), because that is when the shop starts
it. A move later than that date therefore means the requirement is met late, which is
what a capacity-constrained answer honestly is.

The basic plan is never edited: an advanced plan is a row like an MRP run, carrying the
run it was planned from and the inputs it used.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    Uuid,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.db import Base
from app.manufacturing.bom import released_bom
from app.manufacturing.capacity import period_capacity
from app.manufacturing.mrp import MAKE, MrpError, MrpRequirement, MrpRun, plan_of
from app.manufacturing.routing import operation_minutes, operations
from app.manufacturing.work_centers import known_codes, work_center_by_code
from app.stock.items import Item

MONEY = Numeric(20, 6)
SCALE = Decimal("0.000001")


class AdvancedPlanningError(MrpError):
    """The advanced plan refused what was asked of it."""


class AdvancedPlan(Base):
    """One constrained plan, and the basic run it was placed from."""

    __tablename__ = "advanced_plan"
    __table_args__ = (
        CheckConstraint("horizon_days > 0", name="ck_advanced_plan_horizon"),
        CheckConstraint("bucket_days > 0", name="ck_advanced_plan_bucket"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    basic_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mrp_run.id"), nullable=False, index=True
    )
    run_on: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    start_on: Mapped[date] = mapped_column(Date, nullable=False)
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False)
    bucket_days: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    basic_run: Mapped[MrpRun] = relationship()
    rows: Mapped[list[AdvancedPlanRow]] = relationship(
        back_populates="plan",
        order_by="AdvancedPlanRow.planned_release_on, AdvancedPlanRow.basic_bucket_start",
    )


class AdvancedPlanRow(Base):
    """One basic requirement, with the date the shop can actually start it."""

    __tablename__ = "advanced_plan_row"
    __table_args__ = (
        CheckConstraint("moved_buckets >= 0", name="ck_advanced_plan_row_moved"),
        CheckConstraint("quantity >= 0", name="ck_advanced_plan_row_quantity"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("advanced_plan.id"), nullable=False, index=True
    )
    requirement_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mrp_requirement.id"), nullable=False, index=True
    )
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("item.id"), nullable=False, index=True)
    level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    # The bucket the basic run needed it in, and the release the basic run stated: both
    # kept, because a plan compared with the one before it needs what *was* said.
    basic_bucket_start: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    basic_release_on: Mapped[date | None] = mapped_column(Date)
    planned_release_on: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    # The centre whose calendar moved it, and the figures at the bucket it was wanted in:
    # this requirement's own minutes, what the bucket's load would have been with them,
    # and the capacity they did not fit into.
    moved_buckets: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unresolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    constraint: Mapped[str | None] = mapped_column(Text)
    work_center: Mapped[str | None] = mapped_column(String(32))
    load_minutes: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    bucket_load_minutes: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, default=Decimal(0)
    )
    capacity_minutes: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))

    plan: Mapped[AdvancedPlan] = relationship(back_populates="rows")
    item: Mapped[Item] = relationship()


def _buckets(start: date, horizon_days: int, bucket_days: int) -> list[tuple[date, date]]:
    if horizon_days <= 0:
        raise AdvancedPlanningError(f"a horizon is at least one day, got {horizon_days}")
    if bucket_days <= 0:
        raise AdvancedPlanningError(f"a bucket is at least one day, got {bucket_days}")
    out: list[tuple[date, date]] = []
    cursor = start
    end = start + timedelta(days=horizon_days - 1)
    while cursor <= end:
        last = min(cursor + timedelta(days=bucket_days - 1), end)
        out.append((cursor, last))
        cursor = last + timedelta(days=1)
    return out


def _index_of(buckets: list[tuple[date, date]], day: date) -> int:
    """Which bucket a day falls in, clamped to the horizon — the same reading as T-4.MRP.01."""
    for index, (opened, last) in enumerate(buckets):
        if opened <= day <= last:
            return index
    return 0 if day < buckets[0][0] else len(buckets) - 1


def _load_of(
    session: Session, requirement: MrpRequirement
) -> tuple[list[tuple[str, Decimal]], list[str]]:
    """The minutes a net requirement puts on each centre, and the codes nobody registered.

    A **made** item with a released BOM loads the centres its routing names, one entry per
    operation in sequence order, each operation's own setup-plus-run for this quantity
    (:func:`~app.manufacturing.routing.operation_minutes`, so this and the capacity view
    cannot disagree about an operation's minutes). A bought item, a made item with no BOM,
    and an operation with no work centre load nothing — T-4.WC.02 already reports the
    unassigned ones, and this plan does not invent a centre for them.
    """
    if requirement.kind != MAKE or Decimal(requirement.net) <= 0:
        return [], []
    item = session.get(Item, requirement.item_id)
    if item is None:
        return [], []
    bom = released_bom(session, item)
    if bom is None:
        return [], []
    known = known_codes(session, company_id=requirement.company_id)
    load: list[tuple[str, Decimal]] = []
    unknown: list[str] = []
    for step in operations(session, bom):
        code = step.work_center_code
        if not code:
            continue
        minutes = operation_minutes(step, requirement.net)
        if code in known:
            load.append((code, minutes))
        elif code not in unknown:
            unknown.append(code)
    return load, unknown


def run_advanced_plan(
    session: Session,
    *,
    company_id: uuid.UUID,
    run: MrpRun,
    run_on: date | None = None,
) -> AdvancedPlan:
    """Place a basic run's net requirements around the work centres' capacity.

    Buckets are filled earliest-first, and within a bucket the requirements are taken in
    the plan's own canonical order (bucket, then item code), each one's operations in the
    routing's sequence — so which requirement moves when two compete for one centre is a
    property of the data and not of the order the rows happened to be written in.
    """
    if run.company_id != company_id:
        raise AdvancedPlanningError(f"run {run.id} belongs to another company")
    buckets = _buckets(run.start_on, run.horizon_days, run.bucket_days)
    capacities: dict[str, Decimal] = {}
    plan = AdvancedPlan(
        company_id=company_id,
        basic_run_id=run.id,
        run_on=run_on or date.today(),
        start_on=run.start_on,
        horizon_days=run.horizon_days,
        bucket_days=run.bucket_days,
        created_at=datetime.now(timezone.utc),
    )
    session.add(plan)
    session.flush()

    requirements = list(
        session.scalars(
            select(MrpRequirement)
            .join(Item, Item.id == MrpRequirement.item_id)
            .where(MrpRequirement.run_id == run.id)
            .order_by(
                MrpRequirement.bucket_start, Item.sku, MrpRequirement.level, MrpRequirement.id
            )
        )
    )

    placed: dict[tuple[int, str], Decimal] = {}
    for requirement in requirements:
        load, unknown = _load_of(session, requirement)
        basic_release = requirement.release_on or requirement.bucket_start
        index = _index_of(buckets, basic_release)
        moved = 0
        unresolved = False
        constraint: str | None = None
        centre: str | None = None
        load_minutes = Decimal(0)
        bucket_load_minutes = Decimal(0)
        capacity_minutes = Decimal(0)

        if unknown:
            constraint = _unknown_centre(unknown)
        if load:
            for code, _minutes in load:
                if code not in capacities:
                    capacities[code] = period_capacity(
                        work_center_by_code(session, company_id=company_id, code=code),
                        days=run.bucket_days,
                    )
            spot = _first_bucket_with_room(
                load, placed, capacities, buckets=buckets, start=index
            )
            if spot is None:
                # ponytail: the search only looks *later* than the wanted bucket, because
                # the alternative — starting the order before it is released — needs a
                # planner's decision about pulling demand, not this module's. Ceiling: a
                # horizon with no room reports itself unresolved rather than rearranging
                # the shop. Upgrade: search earlier buckets (or split the batch) once a
                # planner asks for work-ahead.
                index, unresolved, cause = _overflow(
                    load, placed, capacities, start=index
                )
                centre, load_minutes, bucket_load_minutes, capacity_minutes = cause
                constraint = _constraint_text(centre, bucket_load_minutes, capacity_minutes)
            else:
                moved = spot - index
                # The figures on the row are the ones that *caused* the move: the centre's
                # load in the bucket the work was wanted in, against its capacity there —
                # not the roomier bucket it was moved to.
                centre, load_minutes, bucket_load_minutes, capacity_minutes = _cause(
                    load, placed, capacities, index=index
                )
                if moved:
                    constraint = (
                        f"moved {moved} bucket(s) at {centre!r}: {bucket_load_minutes} min"
                        f" against {capacity_minutes} min"
                    )
                index = spot
                apply_load(placed, load, index)
                if moved and unknown:
                    constraint = f"{constraint}; {_unknown_centre(unknown)}"

        session.add(
            AdvancedPlanRow(
                company_id=company_id,
                plan_id=plan.id,
                requirement_id=requirement.id,
                item_id=requirement.item_id,
                level=requirement.level,
                kind=requirement.kind,
                quantity=Decimal(requirement.net).quantize(SCALE),
                basic_bucket_start=requirement.bucket_start,
                basic_release_on=requirement.release_on,
                planned_release_on=buckets[index][0] + _within(basic_release, buckets[index]),
                moved_buckets=max(moved, 0),
                unresolved=unresolved,
                constraint=constraint,
                work_center=centre,
                load_minutes=load_minutes,
                bucket_load_minutes=bucket_load_minutes,
                capacity_minutes=capacity_minutes,
            )
        )
    session.flush()
    return plan


def _within(day: date, bucket: tuple[date, date]) -> timedelta:
    """How far into a bucket a day sits, so a released date keeps its offset when it moves."""
    if bucket[0] <= day <= bucket[1]:
        return day - bucket[0]
    return timedelta(0)


def apply_load(
    placed: dict[tuple[int, str], Decimal], load: Iterable[tuple[str, Decimal]], index: int
) -> None:
    """Add a requirement's minutes to the buckets it was placed in."""
    for code, minutes in load:
        placed[(index, code)] = placed.get((index, code), Decimal(0)) + minutes


def _room(
    load: Iterable[tuple[str, Decimal]],
    placed: dict[tuple[int, str], Decimal],
    capacities: dict[str, Decimal],
    *,
    index: int,
) -> list[str]:
    """The centres this load would push over capacity in this bucket, if any."""
    over = []
    for code, minutes in load:
        already = placed.get((index, code), Decimal(0))
        if already + minutes > capacities.get(code, Decimal(0)):
            over.append(code)
    return over


def _first_bucket_with_room(
    load: list[tuple[str, Decimal]],
    placed: dict[tuple[int, str], Decimal],
    capacities: dict[str, Decimal],
    *,
    buckets: list[tuple[date, date]],
    start: int,
) -> int | None:
    for index in range(start, len(buckets)):
        if not _room(load, placed, capacities, index=index):
            return index
    return None


def _overflow(
    load: list[tuple[str, Decimal]],
    placed: dict[tuple[int, str], Decimal],
    capacities: dict[str, Decimal],
    *,
    start: int,
) -> tuple[int, bool, tuple[str, Decimal, Decimal, Decimal]]:
    """Where a requirement the horizon cannot hold stays, and the constraint that says so.

    It stays in the bucket it was wanted in and consumes what room there is: the shop is
    over capacity in that bucket, and the next requirement competes with the load that is
    really there rather than with a figure that pretends the work vanished.
    """
    cause = _cause(load, placed, capacities, index=start)
    apply_load(placed, load, start)
    return start, True, cause


def _cause(
    load: list[tuple[str, Decimal]],
    placed: dict[tuple[int, str], Decimal],
    capacities: dict[str, Decimal],
    *,
    index: int,
) -> tuple[str, Decimal, Decimal, Decimal]:
    """The centre a load tightens most in a bucket: its name, the load's own minutes, the
    bucket's load with them, and the capacity they are measured against."""
    best: tuple[str, Decimal, Decimal, Decimal] | None = None
    for code, minutes in load:
        capacity = Decimal(capacities.get(code, Decimal(0))).quantize(SCALE)
        bucket_load = (placed.get((index, code), Decimal(0)) + minutes).quantize(SCALE)
        own = Decimal(minutes).quantize(SCALE)
        if best is None or (capacity - bucket_load) < (best[3] - best[2]):
            best = (code, own, bucket_load, capacity)
    assert best is not None
    return best


def _constraint_text(centre: str, load: Decimal, capacity: Decimal) -> str:
    return (
        f"no bucket in the horizon has room at {centre!r}: {load} min against"
        f" {capacity} min"
    )


def _unknown_centre(codes: list[str]) -> str:
    return (
        f"work centre {codes[0]!r} is named by the routing and registered by nobody, so"
        " its minutes load no calendar"
    )


def _previous(session: Session, plan: AdvancedPlan) -> AdvancedPlan | None:
    """The plan this one follows, for the stability comparison — never the clock."""
    seen = plans_of(session, company_id=plan.company_id)
    before = [row for row in seen if (row.created_at, row.id.int) < (plan.created_at, plan.id.int)]
    return before[-1] if before else None


def advanced_plan_of(session: Session, plan: AdvancedPlan) -> list[dict]:
    """A plan's rows in one canonical order, so two plans of the same data compare equal."""
    rows = session.scalars(
        select(AdvancedPlanRow)
        .join(Item, Item.id == AdvancedPlanRow.item_id)
        .where(AdvancedPlanRow.plan_id == plan.id)
        .order_by(
            AdvancedPlanRow.planned_release_on, Item.sku, AdvancedPlanRow.level, AdvancedPlanRow.id
        )
    )
    return [
        {
            "item": row.item.sku,
            "level": row.level,
            "kind": row.kind,
            "quantity": Decimal(row.quantity).quantize(SCALE),
            "basic_bucket_start": row.basic_bucket_start,
            "basic_release_on": row.basic_release_on,
            "planned_release_on": row.planned_release_on,
            "moved_buckets": row.moved_buckets,
            "late": row.moved_buckets > 0,
            "unresolved": bool(row.unresolved),
            "work_center": row.work_center,
            "load_minutes": Decimal(row.load_minutes).quantize(SCALE),
            "bucket_load_minutes": Decimal(row.bucket_load_minutes).quantize(SCALE),
            "capacity_minutes": Decimal(row.capacity_minutes).quantize(SCALE),
            "constraint": row.constraint,
        }
        for row in rows
    ]


def constraints_of(session: Session, plan: AdvancedPlan) -> list[dict]:
    """Every move with its cause, and every requirement the horizon could not hold."""
    return [
        {
            "item": row["item"],
            "basic_bucket_start": row["basic_bucket_start"],
            "basic_release_on": row["basic_release_on"],
            "planned_release_on": row["planned_release_on"],
            "work_center": row["work_center"],
            "load_minutes": row["load_minutes"],
            "bucket_load_minutes": row["bucket_load_minutes"],
            "capacity_minutes": row["capacity_minutes"],
            "moved_buckets": row["moved_buckets"],
            "unresolved": row["unresolved"],
            "reason": row["constraint"],
        }
        for row in advanced_plan_of(session, plan)
        if row["moved_buckets"] or row["unresolved"]
    ]


def reconcile(session: Session, plan: AdvancedPlan) -> dict:
    """The advanced rows against the basic run's own net requirements.

    One row per requirement either way, keyed by item and by the bucket the basic run put
    it in: the quantities have to agree — the advanced layer places work, it does not
    invent or round it — and `moved` says how many of them the shop's calendar pushed.
    A dataset with room is therefore `identical`.
    """
    basic = {
        (row["item"], row["bucket_start"]): Decimal(row["net"]).quantize(SCALE)
        for row in plan_of(session, plan.basic_run)
    }
    rows = advanced_plan_of(session, plan)
    differences: list[dict] = []
    advanced_keys: set[tuple[str, date]] = set()
    moved = 0
    for row in rows:
        key = (row["item"], row["basic_bucket_start"])
        advanced_keys.add(key)
        expected = basic.get(key)
        if expected != row["quantity"]:
            differences.append(
                {
                    "item": row["item"],
                    "bucket_start": row["basic_bucket_start"],
                    "basic": expected,
                    "advanced": row["quantity"],
                }
            )
        if row["moved_buckets"]:
            moved += 1
    missing = sorted(key for key in basic if key not in advanced_keys)
    return {
        "rows": len(basic),
        "quantity_differences": differences,
        "missing": missing,
        "moved": moved,
        "identical": not differences and not missing and moved == 0,
    }


def compare_plans(before: list[dict], after: list[dict]) -> dict:
    """Which rows a re-plan moved, and which it left exactly as they were.

    Keyed the way :func:`reconcile` keys them — item and the basic bucket — because that
    is the identity of the requirement itself: an item's demand change must not be able
    to look like a change to another item's row.
    """
    old = {(row["item"], row["basic_bucket_start"]): row for row in before}
    new = {(row["item"], row["basic_bucket_start"]): row for row in after}
    changed = sorted(
        key for key in old.keys() & new.keys() if old[key] != new[key]
    )
    return {
        "changed": changed,
        "unchanged": sorted(key for key in old.keys() & new.keys() if old[key] == new[key]),
        "added": sorted(new.keys() - old.keys()),
        "removed": sorted(old.keys() - new.keys()),
    }


def plans_of(session: Session, *, company_id: uuid.UUID) -> list[AdvancedPlan]:
    """Every advanced plan this company has made, oldest first."""
    return list(
        session.scalars(
            select(AdvancedPlan)
            .where(AdvancedPlan.company_id == company_id)
            .order_by(AdvancedPlan.created_at, AdvancedPlan.run_on)
        )
    )


__all__ = [
    "AdvancedPlan",
    "AdvancedPlanRow",
    "AdvancedPlanningError",
    "advanced_plan_of",
    "compare_plans",
    "constraints_of",
    "plans_of",
    "reconcile",
    "run_advanced_plan",
]
