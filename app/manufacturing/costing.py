"""T-4.WO.05 — what the job cost, what it should have cost, and the posting of both.

Costing a work order is the last thing that happens to it, and it answers two
questions a shop is judged on: what did this actually take, and how far was that from
what we said it would take.

* **The cost is the documents, added.** Material is the value of the issues (T-4.WO.03,
  each at the costing method's figure) and labour is the **booked time at the rate in
  force on the day it was booked** — not the rate in force today, which is what makes
  re-rating a work centre unable to restate a job that finished last month (the rates
  are dated for exactly this, T-4.WC.01). Scrap is inside the material cost because it
  is inside the requirement the issues met: the extra the job consumed is not lost, it
  is what the extra cost is for.
* **The finished goods end up carrying the job's own cost.** The receipts capitalised
  the material as they took it out of WIP (T-4.WO.04); the labour they could not yet
  know is added here, so the value of what came off the bench is the job's cost rather
  than a material-only figure that would understate every product ever made.
* **The variance is computed and posted, and the posting balances.** The job's cost
  against the produced item's **standard cost** (T-1.INV.01's `standard_cost`, times the
  quantity). One entry states all of it: inventory takes the job's own cost, the
  `labour_applied` account takes what the **standard** allowed the output to absorb
  (the standard less the material the receipts already capitalised), and the
  `production_variance` account takes the difference — credited when the job cost more
  than it was allowed, debited when less, so the account is what the output did *not*
  absorb. The three lines balance by construction, and the ledger's own rules refuse
  them if they do not.
* **Costing twice posts once.** The costing is a row per work order, and a second call
  returns what the first recorded: a re-run of a report, or a retry of a request, cannot
  duplicate the posting. Correcting a costing is a new finding about the documents it
  was computed from, not a second posting.

Costing needs a job that has been **received in full** (output is what there is to
cost) and every work centre it used to have been **rated** — a job whose labour has no
rate is refused rather than valued at nothing.
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
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.db import Base
from app.ledger.posting import JournalEntry, post_journal_entry
from app.manufacturing.bom import BomError
from app.manufacturing.issues import issued_value
from app.manufacturing.job_cards import cards_of, entries_of
from app.manufacturing.receipts import received_value
from app.manufacturing.work_centers import rate_on, work_center_by_code
from app.manufacturing.work_orders import WorkOrder
from app.stock.items import Item
from app.stock.valuation import MissingStandardCostError

MONEY = Numeric(20, 6)
SCALE = Decimal("0.000001")
HOUR_MINUTES = Decimal(60)

# The mapping keys the costing posts through: where the labour came from, and where the
# difference between what the job cost and what it should have cost is stated.
LABOUR_KEY = "labour_applied"
VARIANCE_KEY = "production_variance"


class CostingError(BomError):
    """The costing refused what was asked of it."""


class WorkOrderNotComplete(CostingError):
    """Only a job whose output has all been received has a cost to state."""


class UnratedWorkCenterError(CostingError):
    """A work centre the job used has no rate on the day it was used."""


class WorkOrderCost(Base):
    """One work order's cost, recorded once: the figures and the entries that posted them."""

    __tablename__ = "work_order_cost"
    __table_args__ = (
        # One costing per job. A second call reads this row rather than writing another,
        # which is what makes recosting a re-run rather than a duplicate posting.
        UniqueConstraint("work_order_id", name="uq_work_order_cost_order"),
        CheckConstraint("material_value >= 0", name="ck_work_order_cost_material"),
        CheckConstraint("labour_value >= 0", name="ck_work_order_cost_labour"),
        CheckConstraint("total_value >= 0", name="ck_work_order_cost_total"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    work_order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("work_order.id"), nullable=False, index=True
    )
    quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    material_value: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    labour_value: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    total_value: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    # The standard the job was judged against, and the difference. Positive means the
    # job cost more than it should have (unfavourable).
    expected_value: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    variance_value: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    # What the receipts had already put into finished goods, and therefore what this
    # costing still had to add for the goods to carry the job's own cost.
    capitalised_value: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    costed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # The one entry the costing posted: the cost into finished goods, the labour the
    # standard allowed, and the difference between them.
    entry_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("journal_entry.id"), nullable=False, index=True
    )

    work_order: Mapped[WorkOrder] = relationship()
    entry: Mapped[JournalEntry] = relationship()


def labour_breakdown(session: Session, order: WorkOrder) -> list[dict]:
    """Every booking priced at the rate in force the day it was booked.

    The rows are kept, not just the total, because "the job cost 480" is not an answer
    a shop can act on — which operation, which day and which rate is.
    """
    rows = []
    for card in cards_of(session, order):
        code = card.operation.work_center_code
        for entry in entries_of(session, card):
            minutes = (Decimal(entry.setup_minutes) + Decimal(entry.run_minutes)).quantize(
                SCALE
            )
            if minutes == 0:
                continue
            if not code:
                raise UnratedWorkCenterError(
                    f"step {int(card.sequence)} of work order {order.number!r} names no"
                    " work centre, so its minutes have no rate to be priced at"
                    " (T-4.WO.05)"
                )
            center = work_center_by_code(session, company_id=order.company_id, code=code)
            when = entry.booked_at.date()
            rate = rate_on(session, center, on=when)
            if rate is None:
                raise UnratedWorkCenterError(
                    f"work centre {code!r} has no rate in force on {when}, when work"
                    f" order {order.number!r} booked time on it: a rate change is dated,"
                    " so a job is costed at the rate that was in force"
                )
            cost = (minutes / HOUR_MINUTES * rate).quantize(SCALE)
            rows.append(
                {
                    "sequence": int(card.sequence),
                    "operation": card.operation.name,
                    "work_center": code,
                    "booked_on": when,
                    "minutes": minutes,
                    "hourly_rate": rate,
                    "cost": cost,
                }
            )
    return rows


def cost_breakdown(session: Session, order: WorkOrder) -> dict:
    """The job's cost from its own documents, before anything is posted."""
    item = session.get(Item, order.item_id)
    material = issued_value(session, order)
    rows = labour_breakdown(session, order)
    labour = sum((row["cost"] for row in rows), Decimal(0)).quantize(SCALE)
    total = (material + labour).quantize(SCALE)
    standard = item.standard_cost
    if standard is None:
        raise MissingStandardCostError(
            f"{item.sku!r} states no standard cost, so work order {order.number!r} has"
            " nothing to be judged against: state one on the item (T-4.WO.05)"
        )
    expected = (Decimal(standard) * Decimal(order.quantity)).quantize(SCALE)
    return {
        "material": material,
        "labour": labour,
        "total": total,
        "expected": expected,
        "variance": (total - expected).quantize(SCALE),
        "capitalised": received_value(session, order),
        "labour_rows": rows,
    }


def cost_work_order(session: Session, order: WorkOrder) -> WorkOrderCost:
    """Cost a completed job, post the labour and the variance, and record the figures.

    Idempotent on purpose: the costing is what the job's documents said the first time
    it was computed, so a second call returns that row and posts nothing further.
    """
    existing = session.scalar(
        select(WorkOrderCost).where(WorkOrderCost.work_order_id == order.id)
    )
    if existing is not None:
        return existing
    from app.manufacturing.receipts import received_quantity

    if received_quantity(session, order) < Decimal(order.quantity):
        raise WorkOrderNotComplete(
            f"work order {order.number!r} has received"
            f" {received_quantity(session, order)} of its {order.quantity}: there is no"
            " finished output to cost yet (T-4.WO.05)"
        )
    figures = cost_breakdown(session, order)
    item = session.get(Item, order.item_id)
    currency = _currency(session, order)
    to_add = (figures["total"] - figures["capitalised"]).quantize(SCALE)
    # One entry states the whole thing, and each line means something:
    #
    #   inventory          the job's own cost, added to what the receipts capitalised
    #   labour_applied     what the **standard** allowed the output to absorb — the
    #                      standard cost less the material the job used, because the
    #                      material has already been capitalised by the receipts
    #   production_variance the difference between the two: **credited** when the job
    #                      cost more than it was allowed, debited when less. The account
    #                      is therefore what the output did *not* absorb, which is the
    #                      figure a cost accountant closes to cost of sales.
    #
    # The three balance by construction: the applied line and the variance line add up
    # to the difference the inventory line carries.
    allowed_labour = (figures["expected"] - figures["material"]).quantize(SCALE)
    variance = figures["variance"]
    lines = [
        {"account": _mapped(session, order, "inventory"), "debit": to_add},
        {
            "account": _mapped(session, order, LABOUR_KEY),
            "debit": -allowed_labour if allowed_labour < 0 else Decimal(0),
            "credit": allowed_labour if allowed_labour > 0 else Decimal(0),
        },
    ]
    if variance != 0:
        lines.append(
            {
                "account": _mapped(session, order, VARIANCE_KEY),
                "debit": -variance if variance < 0 else Decimal(0),
                "credit": variance if variance > 0 else Decimal(0),
            }
        )
    entry = post_journal_entry(
        session,
        company_id=order.company_id,
        posting_date=_closing_day(session, order),
        currency=currency,
        source_type="work_order_cost",
        source_id=order.id,
        memo=(
            f"work order {order.number}: cost {figures['total']} against a standard of"
            f" {figures['expected']} ({figures['material']} material +"
            f" {figures['labour']} labour)"
        ),
        lines=lines,
    )
    cost = WorkOrderCost(
        company_id=order.company_id,
        work_order_id=order.id,
        quantity=Decimal(order.quantity).quantize(SCALE),
        material_value=figures["material"],
        labour_value=figures["labour"],
        total_value=figures["total"],
        expected_value=figures["expected"],
        variance_value=figures["variance"],
        capitalised_value=figures["capitalised"],
        currency=currency,
        costed_at=datetime.now(timezone.utc),
        entry_id=entry.id,
    )
    session.add(cost)
    session.flush()
    assert item is not None  # the item named the standard the job was judged against
    return cost


def _closing_day(session: Session, order: WorkOrder) -> date:
    """The day the costing is posted on: the last receipt's, or the day it was raised."""
    from app.manufacturing.receipts import receipts_of

    rows = receipts_of(session, order)
    return rows[-1].posted_on if rows else order.created_on


def _currency(session: Session, order: WorkOrder) -> str:
    from app.company import company_base_currency

    return company_base_currency(session, company_id=order.company_id)


def _mapped(session: Session, order: WorkOrder, key: str) -> str:
    from app.ledger.mapping import mapped_account

    return mapped_account(session, company_id=order.company_id, key=key).code


def costing_of(session: Session, order: WorkOrder) -> WorkOrderCost | None:
    """The job's recorded costing, or ``None`` where it has not been costed."""
    return session.scalar(
        select(WorkOrderCost).where(WorkOrderCost.work_order_id == order.id)
    )


def finished_goods_value(session: Session, order: WorkOrder) -> Decimal:
    """What the goods that came off the bench are carried at.

    The receipts' own value — what they put into inventory as they took the material
    out of WIP — plus whatever the costing still had to add for the goods to carry the
    job's cost. Read from the order's documents rather than by scanning entries, so the
    figure is the same arithmetic the postings were made with.
    """
    cost = costing_of(session, order)
    if cost is None:
        return received_value(session, order)
    return (Decimal(cost.total_value)).quantize(SCALE)


def cost_summary(session: Session, order: WorkOrder) -> dict:
    """The costing as a report reads it — the figures, the standard, and the difference."""
    cost = costing_of(session, order)
    if cost is None:
        return {**cost_breakdown(session, order), "costed": False, "finished_goods": None}
    return {
        "material": Decimal(cost.material_value).quantize(SCALE),
        "labour": Decimal(cost.labour_value).quantize(SCALE),
        "total": Decimal(cost.total_value).quantize(SCALE),
        "expected": Decimal(cost.expected_value).quantize(SCALE),
        "variance": Decimal(cost.variance_value).quantize(SCALE),
        "capitalised": Decimal(cost.capitalised_value).quantize(SCALE),
        "costed": True,
        "finished_goods": finished_goods_value(session, order),
        "labour_rows": labour_breakdown(session, order),
    }


def counted(session: Session, *, company_id: uuid.UUID) -> int:
    """How many work orders this company has costed — a report's own line."""
    return int(
        session.scalar(
            select(func.count())
            .select_from(WorkOrderCost)
            .where(WorkOrderCost.company_id == company_id)
        )
        or 0
    )
