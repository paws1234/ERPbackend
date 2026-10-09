"""T-4.WC.02 — the load over a horizon: what each centre has to do, and when.

Basic capacity planning is one question asked per centre per period: *how many
minutes of work land here, and how many are there to do it in*. Four decisions:

* **The load is the work orders' own operation times.** Each open work order's
  operations load the period their order is due in (its `due_on`, or the day it was
  raised where no date was stated — and the report says which), at
  :func:`~app.manufacturing.routing.operation_minutes`'s arithmetic: the setup once
  per batch, the run per unit of the planned quantity. Nothing is spread, rounded or
  smoothed, so the chart is reproducible from the orders themselves (T-4.WC.02's
  acceptance criterion) and the check reconciles it by hand.
* **Overload is stated against the effective capacity.** The centre's own figure
  (T-4.WC.01) has its downtime allowance applied and is **prorated to the period the
  report is bucketed in** — a week-rated centre read over a day gets a seventh of its
  week — and the report carries both the gross capacity and the source period, so a
  proration is visible rather than implied. Only planned, released and in-progress
  orders load a centre: a completed job is not work still to do.
* **An operation that names no centre is reported, not dropped.** An unassigned
  operation (or one naming a code the company has not registered) has nowhere to
  load, so it is listed with its minutes and its order. Silently ignoring it would
  make the chart look lighter than the work actually released.
* **Constrained levelling is not here.** Finite scheduling is T-6.ADV.01; this is the
  view that shows a planner where the problem is, not a solver that moves it.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.manufacturing.bom import BomError
from app.manufacturing.routing import operation_minutes
from app.manufacturing.work_centers import (
    WorkCenter,
    effective_capacity_minutes,
)
from app.manufacturing.work_orders import (
    CLOSED,
    COMPLETED,
    IN_PROGRESS,
    PLANNED,
    RELEASED,
    WorkOrder,
    route_of,
)
from app.stock.items import Item

MINUTES_SCALE = Decimal("0.000001")

# The statuses that still hold work to do. A completed order has been made and a
# closed one is accounted for; neither loads a centre.
OPEN_STATUSES = (PLANNED, RELEASED, IN_PROGRESS)
DONE_STATUSES = (COMPLETED, CLOSED)

# How long each period a capacity may be stated per is, in days — the divisor a
# period is prorated by. Month is 30: the plan names a month, not a calendar, and a
# capacity model that shifted with the month's length would make two months' loads
# incomparable.
PERIOD_DAYS = {"day": 1, "week": 7, "month": 30}


class CapacityError(BomError):
    """The capacity view refused what was asked of it."""


def period_capacity(center: WorkCenter, *, days: int) -> Decimal:
    """The centre's effective capacity for a bucket `days` long."""
    per_period = PERIOD_DAYS[str(center.capacity_period)]
    return (
        effective_capacity_minutes(center) * Decimal(days) / Decimal(per_period)
    ).quantize(MINUTES_SCALE)


def _buckets(start: date, end: date, bucket_days: int) -> list[tuple[date, date, int]]:
    """The horizon cut into buckets, each with the number of days it holds."""
    if bucket_days < 1:
        raise CapacityError(f"a bucket is at least one day, got {bucket_days}")
    if end < start:
        raise CapacityError(f"the horizon ends ({end}) before it starts ({start})")
    out: list[tuple[date, date, int]] = []
    cursor = start
    while cursor <= end:
        last = min(cursor + timedelta(days=bucket_days - 1), end)
        out.append((cursor, last, (last - cursor).days + 1))
        cursor = last + timedelta(days=1)
    return out


def _open_orders(
    session: Session, *, company_id: uuid.UUID, start: date, end: date
) -> list[WorkOrder]:
    """Every order still holding work, whose dated day falls inside the horizon."""
    rows = list(
        session.scalars(
            select(WorkOrder)
            .where(
                WorkOrder.company_id == company_id,
                WorkOrder.status.in_(OPEN_STATUSES),
            )
            .order_by(WorkOrder.number)
        )
    )
    inside = []
    for order in rows:
        day = _dated_by(order)
        if start <= day <= end:
            inside.append(order)
    return inside


def _dated_by(order: WorkOrder) -> date:
    """The day an order's work is placed on: the day it is due, or the day it was raised."""
    return order.due_on or order.created_on


def capacity_profile(
    session: Session,
    *,
    company_id: uuid.UUID,
    start: date,
    end: date,
    bucket_days: int = 1,
) -> dict:
    """Load against capacity, per centre per bucket, over one horizon.

    The horizon, the bucket length and the source of each order's date are all reported
    with the figures: a plan nobody can reproduce from the work orders is a chart, not
    a plan.
    """
    buckets = _buckets(start, end, bucket_days)
    centers = list(
        session.scalars(
            select(WorkCenter)
            .where(WorkCenter.company_id == company_id)
            .order_by(WorkCenter.code)
        )
    )
    by_code = {center.code: center for center in centers}
    orders = _open_orders(session, company_id=company_id, start=start, end=end)

    # period_start -> code -> {"minutes", "orders"}
    load: dict[date, dict[str, dict]] = {
        opened: {} for opened, _closed, _days in buckets
    }
    unassigned: list[dict] = []
    unknown: list[dict] = []
    for order in orders:
        day = _dated_by(order)
        opened = max((row for row in buckets if row[0] <= day), key=lambda row: row[0])[0]
        item = session.get(Item, order.item_id)
        for step in route_of(session, order):
            minutes = operation_minutes(step, order.quantity)
            row = {
                "work_order": order.number,
                "item": item.sku,
                "sequence": step.sequence,
                "operation": step.name,
                "minutes": minutes,
                "dated_by": "due_on" if order.due_on else "created_on",
                "on": day,
            }
            if not step.work_center_code:
                unassigned.append(row)
                continue
            if step.work_center_code not in by_code:
                unknown.append({**row, "work_center": step.work_center_code})
                continue
            bucket = load[opened].setdefault(
                step.work_center_code, {"minutes": Decimal(0), "orders": []}
            )
            bucket["minutes"] = (bucket["minutes"] + minutes).quantize(MINUTES_SCALE)
            bucket["orders"].append(
                {**row, "work_center": step.work_center_code}
            )

    periods: list[dict] = []
    for opened, last, days in buckets:
        for center in centers:
            placed = load[opened].get(center.code, {"minutes": Decimal(0), "orders": []})
            capacity = period_capacity(center, days=days)
            periods.append(
                {
                    "period_start": opened,
                    "period_end": last,
                    "days": days,
                    "work_center": center.code,
                    "load_minutes": placed["minutes"],
                    "capacity_minutes": capacity,
                    "capacity_period": center.capacity_period,
                    "capacity_gross_minutes": Decimal(center.capacity_minutes).quantize(
                        MINUTES_SCALE
                    ),
                    "downtime_percent": center.downtime_percent,
                    "overloaded": placed["minutes"] > capacity,
                    "utilisation": (
                        (placed["minutes"] / capacity).quantize(Decimal("0.0001"))
                        if capacity
                        else None
                    ),
                    "orders": placed["orders"],
                }
            )
    loaded = sum((row["load_minutes"] for row in periods), Decimal(0)).quantize(
        MINUTES_SCALE
    )
    return {
        "horizon": {"start": start, "end": end, "bucket_days": bucket_days},
        "work_centers": sorted(by_code),
        "periods": periods,
        "load_minutes": loaded,
        "unassigned_operations": unassigned,
        "unknown_work_centers": unknown,
        "overloaded_periods": sum(1 for row in periods if row["overloaded"]),
    }


def load_for(
    session: Session, *, company_id: uuid.UUID, code: str, on: date
) -> Decimal:
    """One centre's load on one day — the figure a planner asks about a single cell."""
    profile = capacity_profile(
        session, company_id=company_id, start=on, end=on, bucket_days=1
    )
    total = Decimal(0)
    for row in profile["periods"]:
        if row["work_center"] == str(code):
            total += row["load_minutes"]
    return total.quantize(MINUTES_SCALE)
