"""T-3.SALES.02 — the lead and opportunity pipeline: configurable stages, movement, the board.

§2.5 asks for a "Lead & Opportunity pipeline (Kanban)". What that needs in the
database is three things, and this module owns exactly those:

* **stages that are configured, not coded.** They live in `pipeline_stage` rows, so
  a company renames "Qualified" or adds "Pilot" without a release. A stage is
  marked `is_won` / `is_lost` rather than named "Won" / "Lost", because the names
  are the company's business and the *meaning* is the code's.
* **movement that records who and when.** Every move writes an `opportunity_move`
  row — the stage it left, the stage it entered, the actor, the instant, and (for a
  loss) the reason. The current stage is a column on the opportunity for the board
  to read cheaply; the history is the row trail, and the trail is what an auditor
  reads.
* **one way into a quotation.** A won opportunity converts once. The quotation it
  produces carries the customer's details across — the customer row itself, not a
  copy of its name and terms — which is what "no re-keying" means here.

**A lead is a customer, not a fourth counterparty master.** §5 names Party
(Customer / Supplier / Employee) and §2.5's pipeline adds no prospect master, so
an opportunity points at a `customer` (T-3.SALES.01). Inventing a separate prospect
record would put a second identity beside T-0.PARTY.01's and have to be merged into
one the day the quote is won.

**The quotation's header is created here, its pricing is not.** T-3.SALES.03 depends
on this task, so the pricing/validity/order-conversion half has to come *after*;
what a won opportunity hands over is the header (`app/sales/quotations.py`), which
carries the customer, its currency and its terms. T-3.SALES.03 extends that module
with priced lines and the conversion to an order.

**The board respects field-level permissions.** `board()` filters each card's
payload through T-0.SEC.01's `readable_fields`, so a restricted field is *absent*
from the card rather than blank — a hidden value must not read as an empty one.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    func,
    or_,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.audit import append_only
from app.db import Base
from app.sales.customers import Customer
from app.sales.quotations import Quotation, create_quotation
from app.security import hidden_fields

# Exact decimals, like every amount in the platform (DOMAIN-MODELS.md §2).
MONEY = Numeric(20, 6)

# The entity name field permissions are stated against (T-0.SEC.01).
BOARD_ENTITY = "opportunity"


class PipelineError(ValueError):
    """The pipeline refused what was asked of it."""


class InvalidPipelineError(PipelineError):
    """A stage or an opportunity failed validation at entry."""


class DuplicateStageError(PipelineError):
    """That stage name or board position is already taken in this company."""


class UnknownStageError(PipelineError):
    """A lookup named a stage this company does not have."""


class StageInUseError(PipelineError):
    """A stage opportunities still sit in cannot be removed."""


class LostReasonRequiredError(PipelineError):
    """An opportunity marked lost without a reason — the one thing a loss must say."""


class NotWonError(PipelineError):
    """An opportunity that has not been won was converted to a quotation."""


class AlreadyConvertedError(PipelineError):
    """That opportunity already produced a quotation; one win, one quotation."""


class PipelineStage(Base):
    """One column of the board. Configured as a row, never as a code branch."""

    __tablename__ = "pipeline_stage"
    __table_args__ = (
        UniqueConstraint("company_id", "name", name="uq_pipeline_stage_company_name"),
        UniqueConstraint("company_id", "position", name="uq_pipeline_stage_company_position"),
        CheckConstraint("position >= 0", name="ck_pipeline_stage_position"),
        # A stage cannot be both the win and the loss column: the board would have
        # no honest way to report a win rate.
        CheckConstraint("NOT (is_won AND is_lost)", name="ck_pipeline_stage_won_or_lost"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    # Left-to-right order on the board. Stated rather than implied by insertion
    # order, so a company can put a stage between two others without rewriting them.
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    is_won: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_lost: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Opportunity(Base):
    """One deal in play — the customer, its value, its owner and where it stands."""

    __tablename__ = "opportunity"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customer.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    value: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    # The subject the deal belongs to — a T-0.SEC.01 subject, so the board can be
    # filtered by who owns what without a second user table.
    owner: Mapped[str] = mapped_column(String(80), nullable=False)
    expected_close: Mapped[date | None] = mapped_column(Date)
    stage_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pipeline_stage.id"), nullable=False, index=True
    )
    # Set only by `lose_opportunity`, and required by it: a lost card with no reason
    # is a card nobody can learn anything from.
    lost_reason: Mapped[str | None] = mapped_column(String(240))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    customer: Mapped[Customer] = relationship()
    stage: Mapped[PipelineStage] = relationship()
    moves: Mapped[list[OpportunityMove]] = relationship(
        back_populates="opportunity", order_by="OpportunityMove.moved_at"
    )


class OpportunityMove(Base):
    """One step of a card's history: where from, where to, who, when, and why."""

    __tablename__ = "opportunity_move"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("opportunity.id"), nullable=False, index=True
    )
    # Nullable on purpose: the first move is the card being placed on the board.
    from_stage_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("pipeline_stage.id"))
    to_stage_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pipeline_stage.id"), nullable=False
    )
    actor: Mapped[str] = mapped_column(String(80), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(240))
    moved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    opportunity: Mapped[Opportunity] = relationship(back_populates="moves")


# A card's history is what an auditor reads, so it is append-only in the database
# (T-0.AUDIT.01) exactly like an approval decision or a match run: `UPDATE` and
# `DELETE` are refused by a trigger. The card's *current* stage is a column on the
# opportunity, so nothing legitimate ever needs to rewrite the trail.
append_only(OpportunityMove.__table__)


def _required(value: Any, what: str) -> str:
    text_value = "" if value is None else str(value).strip()
    if not text_value:
        raise InvalidPipelineError(f"{what} is required")
    return text_value


def define_stage(
    session: Session,
    *,
    company_id: uuid.UUID,
    name: str,
    position: int,
    is_won: bool = False,
    is_lost: bool = False,
) -> PipelineStage:
    """Add one stage to the board — the whole of "configurable without code change".

    A name and a position are each used once per company, checked here as well as by
    the database's own unique constraints so the refusal names the clash instead of
    surfacing as a constraint violation.
    """
    wanted = _required(name, "a stage name")
    wanted_position = int(position)
    clash = session.scalar(
        select(PipelineStage).where(
            PipelineStage.company_id == company_id,
            or_(
                PipelineStage.name == wanted,
                PipelineStage.position == wanted_position,
            ),
        )
    )
    if clash is not None:
        raise DuplicateStageError(
            f"this board already has stage {clash.name!r} at position {clash.position};"
            " a name and a position are each used once per company (T-3.SALES.02)"
        )
    stage = PipelineStage(
        company_id=company_id,
        name=wanted,
        position=wanted_position,
        is_won=bool(is_won),
        is_lost=bool(is_lost),
    )
    session.add(stage)
    session.flush()
    return stage


def stages(session: Session, *, company_id: uuid.UUID) -> list[PipelineStage]:
    """The company's board, left to right."""
    return list(
        session.scalars(
            select(PipelineStage)
            .where(PipelineStage.company_id == company_id)
            .order_by(PipelineStage.position)
        )
    )


def stage_by_name(session: Session, *, company_id: uuid.UUID, name: str) -> PipelineStage:
    """The stage a caller named, or a refusal — never the first one as a fallback."""
    stage = session.scalar(
        select(PipelineStage).where(
            PipelineStage.company_id == company_id, PipelineStage.name == str(name)
        )
    )
    if stage is None:
        known = ", ".join(row.name for row in stages(session, company_id=company_id))
        raise UnknownStageError(
            f"no pipeline stage {name!r} in this company; it has {known or 'none yet'}"
            " (T-3.SALES.02)"
        )
    return stage


def remove_stage(session: Session, stage: PipelineStage) -> None:
    """Remove a stage — refused while anything still points at it.

    Both halves are counted: the cards *standing* in the column, and the recorded
    moves that name it either side. A stage whose history still references it cannot
    go, because dropping it would orphan the trail that says how deals travelled.
    """
    held = session.scalar(
        select(func.count())
        .select_from(Opportunity)
        .where(Opportunity.stage_id == stage.id)
    )
    history = session.scalar(
        select(func.count())
        .select_from(OpportunityMove)
        .where(
            or_(
                OpportunityMove.from_stage_id == stage.id,
                OpportunityMove.to_stage_id == stage.id,
            )
        )
    )
    if held or history:
        raise StageInUseError(
            f"stage {stage.name!r} is still referenced — {held} opportunity(ies) stand in"
            f" it and {history} recorded move(s) name it; move or rename instead"
        )
    session.delete(stage)
    session.flush()


def create_opportunity(
    session: Session,
    *,
    company_id: uuid.UUID,
    customer: Customer,
    name: str,
    owner: str,
    value: Any = 0,
    expected_close: date | None = None,
    stage: PipelineStage | None = None,
    actor: str | None = None,
    at: datetime | None = None,
) -> Opportunity:
    """Put one deal on the board, in the first stage unless another is named.

    The opening move is recorded like every other one, so the history of a won deal
    starts at the column it was created in rather than at the first time somebody
    dragged it.
    """
    if customer.company_id != company_id:
        raise InvalidPipelineError(
            f"customer {customer.party.code!r} belongs to another company; an opportunity"
            " is filed under one company's customer (T-3.SALES.02)"
        )
    if stage is not None and stage.company_id != company_id:
        raise UnknownStageError(
            f"stage {stage.name!r} belongs to another company's board"
        )
    if stage is None:
        configured = stages(session, company_id=company_id)
        if not configured:
            raise InvalidPipelineError(
                "this company has no pipeline stages yet; define one before creating an"
                " opportunity (T-3.SALES.02)"
            )
        stage = configured[0]
    opportunity = Opportunity(
        company_id=company_id,
        customer_id=customer.id,
        name=_required(name, "an opportunity name"),
        value=Decimal(str(value)),
        owner=_required(owner, "an owner"),
        expected_close=expected_close,
        stage_id=stage.id,
    )
    session.add(opportunity)
    session.flush()
    session.add(
        OpportunityMove(
            company_id=company_id,
            opportunity_id=opportunity.id,
            from_stage_id=None,
            to_stage_id=stage.id,
            actor=str(actor or opportunity.owner),
            moved_at=at or datetime.now(timezone.utc),
        )
    )
    session.flush()
    return opportunity


def move_opportunity(
    session: Session,
    opportunity: Opportunity,
    *,
    to_stage: PipelineStage,
    actor: str,
    reason: str | None = None,
    at: datetime | None = None,
) -> OpportunityMove:
    """Move a card and record who moved it, when, and (where stated) why.

    A move into the loss column *is* a loss, so it needs the reason: the alternative
    is a board that says a deal ended without saying why.
    """
    if to_stage.company_id != opportunity.company_id:
        raise UnknownStageError(
            f"stage {to_stage.name!r} belongs to another company's board"
        )
    if to_stage.is_lost:
        # A loss is the one move that has to say why; a blank reason is not a reason.
        if reason is None or not str(reason).strip():
            raise LostReasonRequiredError(
                f"opportunity {opportunity.name!r} cannot be lost without a reason; state"
                " why it ended (T-3.SALES.02)"
            )
        reason = str(reason).strip()
    move = OpportunityMove(
        company_id=opportunity.company_id,
        opportunity_id=opportunity.id,
        from_stage_id=opportunity.stage_id,
        to_stage_id=to_stage.id,
        actor=_required(actor, "the actor moving the card"),
        reason=None if reason is None else str(reason).strip(),
        moved_at=at or datetime.now(timezone.utc),
    )
    opportunity.stage_id = to_stage.id
    if to_stage.is_lost:
        opportunity.lost_reason = move.reason
        opportunity.closed_at = move.moved_at
    elif to_stage.is_won:
        opportunity.lost_reason = None
        opportunity.closed_at = move.moved_at
    else:
        # Reopened: the card is back in play, so the *current* state has to say so —
        # a closed-at stamp or a stale loss reason left behind would make an open deal
        # read as a finished one. What it once reached stays in the trail, which is
        # append-only.
        opportunity.lost_reason = None
        opportunity.closed_at = None
    session.add(move)
    session.flush()
    return move


def lose_opportunity(
    session: Session,
    opportunity: Opportunity,
    *,
    actor: str,
    reason: str,
    at: datetime | None = None,
) -> OpportunityMove:
    """Mark a lost deal — the company's own loss column, with its reason."""
    board = stages(session, company_id=opportunity.company_id)
    loss = next((stage for stage in board if stage.is_lost), None)
    if loss is None:
        raise UnknownStageError(
            "this company's board has no loss stage; mark one with is_lost before"
            " losing an opportunity (T-3.SALES.02)"
        )
    return move_opportunity(
        session, opportunity, to_stage=loss, actor=actor, reason=reason, at=at
    )


def convert_to_quotation(
    session: Session,
    opportunity: Opportunity,
    *,
    number: str,
    issued_on: date | None = None,
) -> Quotation:
    """Turn a won deal into a quotation, carrying the customer's details across.

    Refused unless the card stands in a stage the company marked as won, and refused
    a second time for the same opportunity: one win produces one quotation, so the
    board cannot become a way of quietly issuing two.
    """
    stage = session.get(PipelineStage, opportunity.stage_id)
    if stage is None or not stage.is_won:
        raise NotWonError(
            f"opportunity {opportunity.name!r} stands in"
            f" {stage.name if stage else 'no stage'!r}, which is not a won stage; move it to"
            " one before converting it (T-3.SALES.02)"
        )
    existing = session.scalar(
        select(Quotation).where(Quotation.opportunity_id == opportunity.id)
    )
    if existing is not None:
        raise AlreadyConvertedError(
            f"opportunity {opportunity.name!r} already produced quotation"
            f" {existing.number!r}; one win, one quotation (T-3.SALES.02)"
        )
    return create_quotation(
        session,
        company_id=opportunity.company_id,
        customer_id=opportunity.customer_id,
        number=number,
        # The customer's own currency is part of "the details carried across": left
        # unstated, a USD customer's quotation would silently read as the company's
        # base currency instead.
        currency=opportunity.customer.transaction_currency,
        opportunity_id=opportunity.id,
        issued_on=issued_on,
    )


def _visible(payload: dict[str, Any], hidden: set[str]) -> dict[str, Any]:
    """`payload` without the fields this subject may not read."""
    return {key: value for key, value in payload.items() if key not in hidden}


def board(
    session: Session, *, company_id: uuid.UUID, subject: str
) -> list[dict[str, Any]]:
    """The board as the API hands it over: one entry per stage, its cards, filtered.

    Every card is filtered by T-0.SEC.01's field permissions for `subject`, so a field
    the subject may not read is **absent** from the payload rather than null — a
    hidden value must never be mistaken for an empty one.

    The restrictions are read **once** and reused for every card: they depend only on
    the subject and the entity, so asking per card would put an identical query behind
    each one and make the board's cost grow with the number of deals standing on it.
    """
    hidden = hidden_fields(
        session, company_id=company_id, subject=subject, entity=BOARD_ENTITY
    )
    columns: list[dict[str, Any]] = []
    for stage in stages(session, company_id=company_id):
        cards = session.scalars(
            select(Opportunity)
            .where(Opportunity.company_id == company_id, Opportunity.stage_id == stage.id)
            .order_by(Opportunity.name)
        )
        columns.append(
            {
                "stage": {
                    "name": stage.name,
                    "position": stage.position,
                    "is_won": stage.is_won,
                    "is_lost": stage.is_lost,
                },
                "cards": [
                    _visible(
                        {
                            "name": card.name,
                            "value": str(card.value),
                            "owner": card.owner,
                            "expected_close": (
                                None if card.expected_close is None else str(card.expected_close)
                            ),
                            "lost_reason": card.lost_reason,
                        },
                        hidden,
                    )
                    for card in cards
                ],
            }
        )
    return columns
