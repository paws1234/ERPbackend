"""T-4.MRP.01 — net requirements: what the demand calls for, less what is already coming.

MRP answers one question per item per period — *how much will we be short, and when* —
and this module answers it from documents, in a fixed order, so a planner can check the
arithmetic by hand:

* **Gross demand is the sales orders' unshipped lines**, dated by the day the order was
  placed (T-3.SALES.04 keeps no promised date, so the order's own day is the requirement
  the plan can honestly state), and — where the caller selects it — the components of
  work orders already released, which is the second demand feed this system actually
  has. *The plan names "Sales Orders vs Stock" as the two sides of the netting, and that
  is what happens below: demand against on-hand and open supply. The selectable feeds
  are the ones that exist here, and the run states which it used.*
* **Supply is on-hand plus what is already ordered.** Stock is T-1.INV.03's ledger sum
  (no balance table, so it cannot drift), and open purchase orders contribute their
  **outstanding** quantity (ordered less received) at their required date, in the same
  way open work orders contribute what they have not yet produced. Both are consumed
  earliest-bucket-first, so a delivery that lands in week 2 does not cover week 1.
* **A made item explodes; its lead time moves its components earlier.** The net
  requirement of an item that has a released BOM (T-4.BOM.01) becomes gross demand for
  its components, up-lifted by each line's scrap, dated `lead_time_days` **before** the
  item is wanted — which is when the components have to be there for the parent to be
  built. A purchased item's own lead time states when its order has to be placed.
* **A sub-level shortage propagates upward.** An item whose component is short in the
  bucket that component is needed in cannot be built in that bucket, so its own row is
  marked `constrained` and names the component; the mark travels up the levels, because
  a shortage two levels down is a shortage of the thing it is made into. That is the
  one thing a net requirement per item cannot say for itself.
* **The same data gives the same plan.** Every row is ordered by bucket and item code,
  nothing is read from the clock, and the run records its own inputs (the horizon, the
  bucket length and the demand feeds) so a plan can be reproduced and compared rather
  than trusted.

Leads are on the item (T-1.INV.01's master); *turning* a net requirement into a
suggestion, and a planner's conversion of that suggestion, is T-4.MRP.02, and the
accuracy verification is T-4.MRP.03.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable

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
from app.manufacturing.bom import RELEASED, Bom, BomLine, BomError, lines_of, released_bom, uplift
from app.manufacturing.issues import issued_quantity
from app.manufacturing.receipts import received_quantity
from app.manufacturing.work_orders import (
    CLOSED,
    COMPLETED,
    WorkOrder,
    direct_requirements,
)
from app.stock.entries import on_hand
from app.stock.items import Item

MONEY = Numeric(20, 6)
SCALE = Decimal("0.000001")

# The demand feeds this system can actually select. The plan's "Sales Orders vs Stock"
# is the netting below (demand against stock and open supply) rather than two switches;
# the switches are the two documents that create demand here.
SALES_ORDERS, WORK_ORDERS = "sales_orders", "work_orders"
DEMAND_SOURCES = (SALES_ORDERS, WORK_ORDERS)

MAKE, BUY = "make", "buy"


class MrpError(BomError):
    """The MRP run refused what was asked of it."""


class UnknownDemandSource(MrpError):
    """The run names a demand feed this system does not have."""


class MrpRun(Base):
    """One run of the engine, with the inputs it was given — the plan's own provenance."""

    __tablename__ = "mrp_run"
    __table_args__ = (
        CheckConstraint("horizon_days > 0", name="ck_mrp_run_horizon"),
        CheckConstraint("bucket_days > 0", name="ck_mrp_run_bucket"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    run_on: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    start_on: Mapped[date] = mapped_column(Date, nullable=False)
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False)
    bucket_days: Mapped[int] = mapped_column(Integer, nullable=False)
    # The feeds this run used, in the order it was given them.
    demand_sources: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    requirements: Mapped[list[MrpRequirement]] = relationship(
        back_populates="run", order_by="MrpRequirement.bucket_start, MrpRequirement.level"
    )


class MrpRequirement(Base):
    """One item's net requirement in one bucket — the row a planner reads."""

    __tablename__ = "mrp_requirement"
    __table_args__ = (
        CheckConstraint("level >= 0", name="ck_mrp_requirement_level"),
        CheckConstraint("gross >= 0", name="ck_mrp_requirement_gross"),
        CheckConstraint("net >= 0", name="ck_mrp_requirement_net"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mrp_run.id"), nullable=False, index=True
    )
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("item.id"), nullable=False, index=True)
    bucket_start: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    bucket_end: Mapped[date] = mapped_column(Date, nullable=False)
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    gross: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    available: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    supply: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    net: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    # What the item is short of, stated as the order that has to be placed: a purchase
    # order for a bought item, a work order for a made one, `lead_time_days` earlier.
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    lead_time_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    release_on: Mapped[date | None] = mapped_column(Date)
    # Whether a component it needs is short in the bucket it needs it in.
    constrained: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    constrained_by: Mapped[str | None] = mapped_column(Text)

    run: Mapped[MrpRun] = relationship(back_populates="requirements")
    item: Mapped[Item] = relationship()


def _buckets(start: date, horizon_days: int, bucket_days: int) -> list[tuple[date, date]]:
    if horizon_days <= 0:
        raise MrpError(f"a horizon is at least one day, got {horizon_days}")
    if bucket_days <= 0:
        raise MrpError(f"a bucket is at least one day, got {bucket_days}")
    out = []
    cursor = start
    end = start + timedelta(days=horizon_days - 1)
    while cursor <= end:
        last = min(cursor + timedelta(days=bucket_days - 1), end)
        out.append((cursor, last))
        cursor = last + timedelta(days=1)
    return out


def _bucket_of(buckets: list[tuple[date, date]], day: date) -> int | None:
    """Which bucket a date falls in, or ``None`` where it falls outside the horizon."""
    for index, (opened, last) in enumerate(buckets):
        if opened <= day <= last:
            return index
    return None


def _sales_demand(
    session: Session, *, company_id: uuid.UUID, start: date, end: date
) -> dict[uuid.UUID, list[tuple[date, Decimal]]]:
    """What confirmed orders still call for, per item, dated by the day they were placed."""
    from app.sales.orders import CONFIRMED, SalesOrder

    out: dict[uuid.UUID, list[tuple[date, Decimal]]] = {}
    orders = session.scalars(
        select(SalesOrder)
        .where(
            SalesOrder.company_id == company_id,
            SalesOrder.status == CONFIRMED,
            SalesOrder.ordered_on >= start,
            SalesOrder.ordered_on <= end,
        )
        .order_by(SalesOrder.number)
    )
    for order in orders:
        for line in order.lines:
            if line.item_id is None:
                continue
            remaining = (Decimal(line.quantity) - Decimal(line.shipped_quantity)).quantize(SCALE)
            if remaining <= 0:
                continue
            out.setdefault(line.item_id, []).append((order.ordered_on, remaining))
    return out


def _work_order_demand(
    session: Session, *, company_id: uuid.UUID, start: date, end: date
) -> dict[uuid.UUID, list[tuple[date, Decimal]]]:
    """What jobs already released still have to draw, per **direct** component, at their due date.

    A job draws the material its own item is made of — see
    :func:`~app.manufacturing.work_orders.direct_requirements`; the materials of a
    component that is itself built are drawn by that component's own job, so counting
    them here would demand the same material twice.
    """
    out: dict[uuid.UUID, list[tuple[date, Decimal]]] = {}
    orders = session.scalars(
        select(WorkOrder)
        .where(
            WorkOrder.company_id == company_id,
            WorkOrder.status.notin_((COMPLETED, CLOSED)),
        )
        .order_by(WorkOrder.number)
    )
    for order in orders:
        when = order.due_on or order.created_on
        if when < start or when > end:
            continue
        for requirement in direct_requirements(session, order):
            outstanding = (
                Decimal(requirement.quantity_required) - issued_quantity(session, requirement)
            ).quantize(SCALE)
            if outstanding <= 0:
                continue
            out.setdefault(requirement.item_id, []).append((when, outstanding))
    return out


def _open_supply(
    session: Session, *, company_id: uuid.UUID, item_id: uuid.UUID
) -> list[tuple[date, Decimal]]:
    """Purchase orders and work orders already placed for an item, and when they land."""
    from app.procurement.orders import CLOSED as PO_CLOSED, DRAFT as PO_DRAFT, PurchaseOrder

    rows: list[tuple[date, Decimal]] = []
    for line in session.scalars(
        select(_po_line()).join(PurchaseOrder, PurchaseOrder.id == _po_line().order_id).where(
            _po_line().company_id == company_id,
            _po_line().item_id == item_id,
            PurchaseOrder.status.notin_((PO_DRAFT, PO_CLOSED)),
        )
    ):
        outstanding = (
            Decimal(line.quantity) - Decimal(line.received_quantity)
        ).quantize(SCALE)
        if outstanding > 0:
            rows.append((line.order.required_date, outstanding))
    for order in session.scalars(
        select(WorkOrder).where(
            WorkOrder.company_id == company_id,
            WorkOrder.item_id == item_id,
            WorkOrder.status.notin_((COMPLETED, CLOSED)),
        )
    ):
        outstanding = (
            Decimal(order.quantity) - received_quantity(session, order)
        ).quantize(SCALE)
        if outstanding > 0:
            rows.append((order.due_on or order.created_on, outstanding))
    return sorted(rows, key=lambda row: (row[0], row[1]))


def _po_line():
    from app.procurement.orders import PurchaseOrderLine

    return PurchaseOrderLine


def run_mrp(
    session: Session,
    *,
    company_id: uuid.UUID,
    start: date,
    horizon_days: int,
    bucket_days: int,
    demand_sources: Iterable[str] = (SALES_ORDERS,),
    run_on: date | None = None,
) -> MrpRun:
    """Run the engine over a horizon, writing the plan it computes and returning it.

    The run is a row, not a cache: what the plan said and the inputs it was given are
    both on the record, so a later run can be compared with it rather than replacing it.
    """
    feeds = tuple(str(source).strip().lower() for source in demand_sources)
    unknown = [source for source in feeds if source not in DEMAND_SOURCES]
    if unknown:
        raise UnknownDemandSource(
            f"unknown demand source {unknown[0]!r}; this system's feeds are"
            f" {', '.join(DEMAND_SOURCES)}"
        )
    buckets = _buckets(start, horizon_days, bucket_days)
    end = buckets[-1][1]
    run = MrpRun(
        company_id=company_id,
        run_on=run_on or date.today(),
        start_on=start,
        horizon_days=int(horizon_days),
        bucket_days=int(bucket_days),
        demand_sources=",".join(feeds),
        created_at=datetime.now(timezone.utc),
    )
    session.add(run)
    session.flush()

    plan = _compute(
        session,
        company_id=company_id,
        buckets=buckets,
        feeds=feeds,
        start=start,
        end=end,
    )
    for row in plan:
        session.add(
            MrpRequirement(
                company_id=company_id,
                run_id=run.id,
                item_id=row["item_id"],
                bucket_start=row["bucket_start"],
                bucket_end=row["bucket_end"],
                level=row["level"],
                gross=row["gross"],
                available=row["available"],
                supply=row["supply"],
                net=row["net"],
                kind=row["kind"],
                lead_time_days=row["lead_time_days"],
                release_on=row["release_on"],
                constrained=row["constrained"],
                constrained_by=row["constrained_by"],
            )
        )
    session.flush()
    return run


def _compute(
    session: Session,
    *,
    company_id: uuid.UUID,
    buckets: list[tuple[date, date]],
    feeds: tuple[str, ...],
    start: date,
    end: date,
) -> list[dict]:
    """Gross, supply, net and the explosion, level by level, in a fixed order."""
    gross: dict[tuple[uuid.UUID, int], Decimal] = {}
    level: dict[uuid.UUID, int] = {}
    if SALES_ORDERS in feeds:
        for item_id, entries in _sales_demand(
            session, company_id=company_id, start=start, end=end
        ).items():
            for day, amount in entries:
                index = _bucket_of(buckets, day)
                if index is None:
                    continue
                key = (item_id, index)
                gross[key] = gross.get(key, Decimal(0)) + amount
                level.setdefault(item_id, 0)
    if WORK_ORDERS in feeds:
        for item_id, entries in _work_order_demand(
            session, company_id=company_id, start=start, end=end
        ).items():
            for day, amount in entries:
                index = _bucket_of(buckets, day)
                if index is None:
                    continue
                key = (item_id, index)
                gross[key] = gross.get(key, Decimal(0)) + amount
                level.setdefault(item_id, 0)

    rows: list[dict] = []
    pending = sorted(gross.items(), key=lambda row: (row[0][1], _sku(session, row[0][0])))
    seen: set[tuple[uuid.UUID, int]] = set()
    # Running state per item: the stock still free after the earlier buckets took theirs,
    # and the open supply that has not yet been claimed by one.
    state: dict[uuid.UUID, dict] = {}

    def item_state(item_id: uuid.UUID) -> dict:
        if item_id not in state:
            stock = Decimal(
                on_hand(session, company_id=company_id, item_id=item_id, as_of=end)["quantity"]
            ).quantize(SCALE)
            state[item_id] = {
                "stock": stock,
                "supply": _open_supply(session, company_id=company_id, item_id=item_id),
            }
        return state[item_id]

    while pending:
        (item_id, index), amount = pending.pop(0)
        if (item_id, index) in seen:
            continue
        seen.add((item_id, index))
        item = session.get(Item, item_id)
        opened, last = buckets[index]
        running = item_state(item_id)
        # Stock covers the earliest bucket that needs it first: what an earlier bucket
        # consumed is no longer available to this one.
        available = max(Decimal(0), running["stock"]).quantize(SCALE)
        supply = Decimal(0)
        remaining: list[tuple[date, Decimal]] = []
        for landed_on, quantity in running["supply"]:
            if landed_on <= last and supply < amount:
                take = min(quantity, amount - supply)
                supply += take
                if quantity > take:
                    remaining.append((landed_on, (quantity - take).quantize(SCALE)))
            else:
                remaining.append((landed_on, quantity))
        running["supply"] = remaining
        supply = supply.quantize(SCALE)
        net = max(Decimal(0), (amount - available - supply).quantize(SCALE))
        running["stock"] = (running["stock"] - min(available, amount)).quantize(SCALE)
        bom = released_bom(session, item) if item is not None else None
        lead = int(item.lead_time_days or 0) if item is not None else 0
        rows.append(
            {
                "item_id": item_id,
                "bucket_start": opened,
                "bucket_end": last,
                "level": level.get(item_id, 0),
                "gross": amount.quantize(SCALE),
                "available": available,
                "supply": supply,
                "net": net,
                "kind": MAKE if bom is not None else BUY,
                "lead_time_days": lead,
                "release_on": (opened - timedelta(days=lead)) if lead else opened,
                "constrained": False,
                "constrained_by": None,
            }
        )
        if bom is None or net <= 0:
            continue
        for line in lines_of(session, bom):
            required = (net * Decimal(line.quantity) * uplift(line.scrap_percent)).quantize(
                SCALE
            )
            if required <= 0:
                continue
            # The components have to be there when the parent is *started*, which is its
            # own lead time before it is wanted.
            child_day = opened - timedelta(days=lead)
            child_index = _bucket_of(buckets, child_day)
            if child_index is None:
                child_index = 0 if child_day < buckets[0][0] else len(buckets) - 1
            level[line.item_id] = max(level.get(line.item_id, 0), level.get(item_id, 0) + 1)
            gross[(line.item_id, child_index)] = (
                gross.get((line.item_id, child_index), Decimal(0)) + required
            )
            pending.append(((line.item_id, child_index), required))
            pending.sort(key=lambda row: (row[0][1], _sku(session, row[0][0])))

    rows.sort(key=lambda row: (row["bucket_start"], _sku(session, row["item_id"])))
    _mark_constraints(session, rows=rows, buckets=buckets)
    return rows


def _mark_constraints(
    session: Session, *, rows: list[dict], buckets: list[tuple[date, date]]
) -> None:
    """Mark a row whose components are short in the bucket they are needed in.

    Walked from the deepest level up, so a shortage two levels down reaches the top:
    an item whose component is short cannot be built, which makes it short too.
    """
    short: dict[tuple[uuid.UUID, int], bool] = {
        (row["item_id"], _index_of(buckets, row["bucket_start"])): row["net"] > 0
        for row in rows
    }
    for row in sorted(rows, key=lambda row: (-row["level"], row["bucket_start"])):
        item = session.get(Item, row["item_id"])
        bom = released_bom(session, item)
        if bom is None:
            continue
        index = _index_of(buckets, row["bucket_start"])
        child_index = _index_of(buckets, row["release_on"] or row["bucket_start"])
        missing = []
        for line in lines_of(session, bom):
            if short.get((line.item_id, child_index)):
                missing.append(_sku(session, line.item_id))
        if missing:
            row["constrained"] = True
            row["constrained_by"] = ", ".join(sorted(missing))
            short[(row["item_id"], index)] = True


def _index_of(buckets: list[tuple[date, date]], day: date) -> int:
    for index, (opened, last) in enumerate(buckets):
        if opened <= day <= last:
            return index
    return 0 if day < buckets[0][0] else len(buckets) - 1


def _sku(session: Session, item_id: uuid.UUID) -> str:
    item = session.get(Item, item_id)
    return item.sku if item is not None else str(item_id)


def plan_of(session: Session, run: MrpRun) -> list[dict]:
    """A run's plan as plain rows, ordered by bucket and item — what a comparison reads."""
    return [
        {
            "item": row.item.sku,
            "bucket_start": row.bucket_start,
            "bucket_end": row.bucket_end,
            "level": row.level,
            "gross": Decimal(row.gross).quantize(SCALE),
            "available": Decimal(row.available).quantize(SCALE),
            "supply": Decimal(row.supply).quantize(SCALE),
            "net": Decimal(row.net).quantize(SCALE),
            "kind": row.kind,
            "lead_time_days": row.lead_time_days,
            "release_on": row.release_on,
            "constrained": bool(row.constrained),
            "constrained_by": row.constrained_by,
        }
        for row in session.scalars(
            select(MrpRequirement)
            .where(MrpRequirement.run_id == run.id)
            .order_by(MrpRequirement.bucket_start, MrpRequirement.level, MrpRequirement.id)
        )
    ]


def plan_sorted(rows: list[dict]) -> list[dict]:
    """A plan in one canonical order, so two runs of the same data compare equal."""
    return sorted(rows, key=lambda row: (row["bucket_start"], row["item"], row["level"]))


def inputs_of(run: MrpRun) -> dict:
    """What the run was given — stated on every plan, not remembered by the caller."""
    return {
        "start": run.start_on,
        "horizon_days": run.horizon_days,
        "bucket_days": run.bucket_days,
        "demand_sources": tuple(run.demand_sources.split(",")),
        "run_on": run.run_on,
    }


def runs_of(session: Session, *, company_id: uuid.UUID) -> list[MrpRun]:
    """Every run this company has made, oldest first."""
    return list(
        session.scalars(
            select(MrpRun)
            .where(MrpRun.company_id == company_id)
            .order_by(MrpRun.created_at, MrpRun.run_on)
        )
    )
