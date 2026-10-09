"""T-4.MRP.02 — what the plan asks for, as a document a planner can act on.

A net requirement is a figure; a suggestion is that figure with a *type*, a *date* and
a *state*, so there is something to convert when the planner agrees with it:

* **One suggestion per net requirement, and the quantity is the requirement.** Not a
  rounded lot or a comfort margin: the engine computed what is missing (T-4.MRP.01) and
  the suggestion repeats it, so a plan and the order raised from it cannot disagree.
  The date is the bucket the requirement falls in — for a purchased item, the day the
  order has to be **placed** (its lead time applied), and for a made one the day it is
  wanted.
* **A planner converts it; nothing releases itself.** The plan does not procure or
  schedule anything on its own — this system's plan names no automatic release — so the
  conversion is an explicit act that creates the document the suggestion asks for: a
  **work order** (T-4.WO.01, sourced `mrp`) or a **purchase requisition** (T-2.PROC.02),
  which then earns its approval through the workflow like any other requisition.
* **A suggestion converts once.** Conversion marks it, with the document it produced and
  who made it, and a second attempt is refused — a second work order for the same
  shortage would double the supply the plan asked for.
* **A stale suggestion is flagged before it is converted.** If a later run over the same
  inputs states something else for the same item and bucket — because stock arrived,
  because an order was placed, because the demand moved — then the suggestion is no
  longer what the plan says, and converting it would buy the wrong quantity. Staleness is
  answered by comparing with the newest run rather than by trusting the suggestion's own
  age, and a stale one is refused unless the planner overrides it and says why.
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
    Text,
    UniqueConstraint,
    Uuid,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.db import Base
from app.manufacturing.bom import BomError
from app.manufacturing.mrp import MAKE, MrpRequirement, MrpRun, runs_of
from app.manufacturing.work_orders import create_work_order
from app.procurement.requisitions import create_requisition, requisition_by_number, submit
from app.stock.items import Item

MONEY = Numeric(20, 6)
SCALE = Decimal("0.000001")

OPEN, CONVERTED = "open", "converted"
SUGGESTION_STATES = (OPEN, CONVERTED)

PRODUCE, PURCHASE = "produce", "purchase"
WORK_ORDER, REQUISITION = "work_order", "purchase_requisition"


class SuggestionError(BomError):
    """The suggestion refused what was asked of it."""


class AlreadyConvertedError(SuggestionError):
    """The suggestion has already become a document."""


class StaleSuggestionError(SuggestionError):
    """A later run states something else for this item and bucket."""


class UnknownSuggestionError(SuggestionError):
    """No such suggestion."""


class MrpSuggestion(Base):
    """One net requirement as an action: what to do, how much, and by when."""

    __tablename__ = "mrp_suggestion"
    __table_args__ = (
        # One suggestion per requirement: the plan cannot ask twice for the same shortage.
        UniqueConstraint("requirement_id", name="uq_mrp_suggestion_requirement"),
        CheckConstraint("quantity > 0", name="ck_mrp_suggestion_quantity"),
        CheckConstraint("kind IN ('produce', 'purchase')", name="ck_mrp_suggestion_kind"),
        CheckConstraint("state IN ('open', 'converted')", name="ck_mrp_suggestion_state"),
        CheckConstraint(
            "state = 'open' OR (converted_type IS NOT NULL AND converted_id IS NOT NULL)",
            name="ck_mrp_suggestion_converted_names_its_document",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mrp_run.id"), nullable=False, index=True
    )
    requirement_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mrp_requirement.id"), nullable=False, index=True
    )
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("item.id"), nullable=False, index=True)
    # Exactly the net requirement it came from.
    quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    # The bucket the requirement falls in, and — for a bought item — the day the order
    # has to be placed for it to arrive in time.
    needed_by: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    release_on: Mapped[date | None] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default=OPEN)
    converted_type: Mapped[str | None] = mapped_column(String(24))
    converted_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    converted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    converted_by: Mapped[str | None] = mapped_column(String(64))

    run: Mapped[MrpRun] = relationship()
    requirement: Mapped[MrpRequirement] = relationship()
    item: Mapped[Item] = relationship()

    def __repr__(self) -> str:  # pragma: no cover - a convenience for a caller's log
        return f"MrpSuggestion({self.kind} {self.quantity} by {self.needed_by} {self.state})"


def raise_suggestions(session: Session, run: MrpRun) -> list[MrpSuggestion]:
    """Turn a run's net requirements into suggestions — one each, once.

    Idempotent: a run that already has suggestions returns them, so a caller cannot
    double the plan by asking twice.
    """
    existing = suggestions_of(session, run)
    if existing:
        return existing
    for requirement in session.scalars(
        select(MrpRequirement)
        .where(MrpRequirement.run_id == run.id, MrpRequirement.net > 0)
        .order_by(MrpRequirement.bucket_start, MrpRequirement.level)
    ):
        session.add(
            MrpSuggestion(
                company_id=run.company_id,
                run_id=run.id,
                requirement_id=requirement.id,
                item_id=requirement.item_id,
                quantity=Decimal(requirement.net).quantize(SCALE),
                needed_by=requirement.bucket_start,
                release_on=requirement.release_on,
                kind=PRODUCE if requirement.kind == MAKE else PURCHASE,
                state=OPEN,
            )
        )
    session.flush()
    return suggestions_of(session, run)


def suggestions_of(session: Session, run: MrpRun) -> list[MrpSuggestion]:
    """The run's suggestions, in the plan's own canonical order (T-4.MRP.01).

    By bucket, then item, then level — the same order :func:`plan_sorted` puts the plan
    in, rather than by insertion id: a planner reading the list sees it in the order the
    plan states, and two runs over the same data list their suggestions identically.
    """
    return list(
        session.scalars(
            select(MrpSuggestion)
            .join(MrpRequirement, MrpRequirement.id == MrpSuggestion.requirement_id)
            .join(Item, Item.id == MrpSuggestion.item_id)
            .where(MrpSuggestion.run_id == run.id)
            .order_by(MrpRequirement.bucket_start, Item.sku, MrpRequirement.level)
        )
    )


def open_suggestions(session: Session, run: MrpRun) -> list[MrpSuggestion]:
    """The run's suggestions a planner has not acted on yet."""
    return [row for row in suggestions_of(session, run) if row.state == OPEN]


def suggestion_for(session: Session, requirement: MrpRequirement) -> MrpSuggestion | None:
    """The suggestion a requirement produced, where the run has been converted."""
    return session.scalar(
        select(MrpSuggestion).where(MrpSuggestion.requirement_id == requirement.id)
    )


def is_stale(session: Session, suggestion: MrpSuggestion) -> bool:
    """Whether the **newest** run states something else for this item and bucket.

    Compared with the latest plan rather than with the calendar: a suggestion is stale
    when it no longer agrees with what this system would ask for now, which is exactly
    the case a conversion has to catch.
    """
    runs = runs_of(session, company_id=suggestion.company_id)
    if not runs:
        return False
    newest = runs[-1]
    if newest.id == suggestion.run_id:
        return False
    row = session.scalar(
        select(MrpRequirement).where(
            MrpRequirement.run_id == newest.id,
            MrpRequirement.item_id == suggestion.item_id,
            MrpRequirement.bucket_start == suggestion.needed_by,
        )
    )
    if row is None:
        return True
    return Decimal(row.net).quantize(SCALE) != Decimal(suggestion.quantity).quantize(SCALE)


def latest_plan(session: Session, suggestion: MrpSuggestion) -> Decimal | None:
    """What the newest run says this item needs in this bucket, where it says anything."""
    runs = runs_of(session, company_id=suggestion.company_id)
    if not runs:
        return None
    row = session.scalar(
        select(MrpRequirement).where(
            MrpRequirement.run_id == runs[-1].id,
            MrpRequirement.item_id == suggestion.item_id,
            MrpRequirement.bucket_start == suggestion.needed_by,
        )
    )
    return None if row is None else Decimal(row.net).quantize(SCALE)


def convert_suggestion(
    session: Session,
    suggestion: MrpSuggestion,
    *,
    actor: str,
    on: date,
    number: str,
    currency: str | None = None,
    estimated_unit_price: Any = 0,
    allow_stale: bool = False,
    stale_reason: str | None = None,
) -> dict:
    """Turn a suggestion into the document it asks for, once.

    A `produce` suggestion becomes a work order — sourced `mrp`, so the shop floor can
    see where the job came from — and a `purchase` one becomes a requisition, which
    then goes through the approval workflow like any other (T-4.MRP.02 and T-2.PROC.02).
    """
    who = str(actor or "").strip()
    if not who:
        raise SuggestionError("a conversion names who made it")
    if suggestion.state == CONVERTED:
        raise AlreadyConvertedError(
            f"suggestion {suggestion.id} already became {suggestion.converted_type}"
            f" {suggestion.converted_id} on {suggestion.converted_at}: converting it"
            " again would ask for the same shortage twice (T-4.MRP.02)"
        )
    if is_stale(session, suggestion):
        if not allow_stale:
            raise StaleSuggestionError(
                f"the plan has moved since this suggestion was raised: it asks for"
                f" {suggestion.quantity} of {suggestion.item.sku!r} by"
                f" {suggestion.needed_by}, and the newest run says"
                f" {latest_plan(session, suggestion)}. Re-run MRP, or convert it anyway"
                " with a reason (T-4.MRP.02)"
            )
        if not str(stale_reason or "").strip():
            raise StaleSuggestionError("converting a stale suggestion anyway needs a reason")
    item = suggestion.item
    if suggestion.kind == PRODUCE:
        document = create_work_order(
            session,
            company_id=suggestion.company_id,
            item=item,
            quantity=suggestion.quantity,
            number=number,
            created_on=on,
            due_on=suggestion.needed_by,
            source="mrp",
            memo=f"MRP suggestion {suggestion.id}",
        )
        made_type, made_id = WORK_ORDER, document.id
    else:
        requisition = create_requisition(
            session,
            company_id=suggestion.company_id,
            number=number,
            requested_by=who,
            needed_by=suggestion.release_on or suggestion.needed_by,
            currency=currency or _currency(session, suggestion),
            memo=f"MRP suggestion {suggestion.id}",
            lines=[
                {
                    "description": item.name,
                    "quantity": suggestion.quantity,
                    "uom": item.base_uom,
                    "estimated_unit_price": estimated_unit_price,
                    "item_sku": item.sku,
                }
            ],
        )
        session.flush()
        # The requisition earns its approval the way every other one does.
        requisition = submit(session, requisition, actor=who)
        made_type, made_id = REQUISITION, requisition.id
    suggestion.state = CONVERTED
    suggestion.converted_type = made_type
    suggestion.converted_id = made_id
    suggestion.converted_at = datetime.now(timezone.utc)
    suggestion.converted_by = who
    session.flush()
    return {
        "suggestion": suggestion.id,
        "kind": suggestion.kind,
        "document": made_type,
        "number": number,
        "quantity": Decimal(suggestion.quantity).quantize(SCALE),
        "needed_by": suggestion.needed_by,
    }


def _currency(session: Session, suggestion: MrpSuggestion) -> str:
    from app.company import company_base_currency

    return company_base_currency(session, company_id=suggestion.company_id)


def conversion_of(session: Session, suggestion: MrpSuggestion) -> dict | None:
    """What a converted suggestion became — the number and state of the document."""
    if suggestion.state != CONVERTED:
        return None
    if suggestion.converted_type == WORK_ORDER:
        from app.manufacturing.work_orders import WorkOrder

        order = session.get(WorkOrder, suggestion.converted_id)
        return {
            "type": WORK_ORDER,
            "number": order.number if order else None,
            "status": order.status if order else None,
            "source": order.source if order else None,
        }
    if suggestion.converted_type == REQUISITION:
        from app.procurement.requisitions import Requisition

        requisition = session.get(Requisition, suggestion.converted_id)
        return {
            "type": REQUISITION,
            "number": requisition.number if requisition else None,
            "status": requisition.status if requisition else None,
        }
    return {"type": suggestion.converted_type, "number": None, "status": None}


def summary(session: Session, run: MrpRun) -> dict:
    """The run's suggestions as a planner reads them: one line each, with its state."""
    return {
        "run": run.id,
        "suggestions": [
            {
                "item": row.item.sku,
                "kind": row.kind,
                "quantity": Decimal(row.quantity).quantize(SCALE),
                "needed_by": row.needed_by,
                "release_on": row.release_on,
                "state": row.state,
                "stale": is_stale(session, row),
                "converted": conversion_of(session, row),
            }
            for row in suggestions_of(session, run)
        ],
    }
