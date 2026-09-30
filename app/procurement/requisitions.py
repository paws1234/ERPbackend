"""T-2.PROC.02 — purchase requisitions, routed through the configured chain.

A requisition is where a need starts: somebody asks for something, says how much of
it and roughly what it costs, and the document then has to earn its approval before
anyone may buy against it. Three things this module is built around:

* **The chain is T-0.WF.01's, not this module's.** A requisition is submitted to the
  shared approval engine under one document type (`purchase_requisition`), and the
  levels, thresholds and roles are rows in that engine. `approval_thresholds` is
  therefore a configuration change, not a release — and there is no approval code
  here at all.
* **An approved requisition is frozen.** Its lines and its total are what was
  approved; changing them would mean the approval on record was given for something
  else, so an edit is refused and the way forward is a **new revision**
  (:func:`revise`) that carries the old one as its predecessor and earns its own
  approval. The original keeps its approval and its history.
* **Nothing may be sourced before it is approved.** :func:`require_sourceable` is
  the gate T-2.PROC.03 and T-2.PROC.05 call, so "requisition approved" is a checked
  state rather than a convention two modules each remember — a document below every
  configured threshold needs no approval and is approved on submission, which is the
  engine's own answer, not a second rule invented here.

Who decided what is not recorded here: T-0.WF.01's `approval_decision` is
append-only, and :func:`approval_history` reads it back in order.
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
    Text,
    UniqueConstraint,
    Uuid,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.db import Base
from app.ledger.currency import currency_by_code
from app.stock.items import item_by_sku
from app.workflow import (
    APPROVE as WF_APPROVE,
    APPROVED as WF_APPROVED,
    PENDING as WF_PENDING,
    REJECT as WF_REJECT,
    REJECTED as WF_REJECTED,
    RETURN as WF_RETURN,
    RETURNED as WF_RETURNED,
    ApprovalDecision,
    ApprovalRequest,
)
from app.workflow import decide as workflow_decide
from app.workflow import start_approval

# One money scale for the whole platform.
MONEY = Numeric(20, 6)

# The document type the approval engine knows requisitions by.
DOC_TYPE = "purchase_requisition"

# A requisition's own state. `pending` means it is with the approval engine; the
# three finished states are the engine's own vocabulary, mirrored here.
DRAFT, PENDING, APPROVED, REJECTED, RETURNED = (
    "draft",
    "pending",
    "approved",
    "rejected",
    "returned",
)

# How the engine's request state maps onto the requisition's.
_FROM_ENGINE = {
    WF_PENDING: PENDING,
    WF_APPROVED: APPROVED,
    WF_REJECTED: REJECTED,
    WF_RETURNED: RETURNED,
}

# The decisions a caller may take, by the names the engine uses.
ACTIONS = (WF_APPROVE, WF_REJECT, WF_RETURN)


class RequisitionError(ValueError):
    """The requisition refused what was asked of it."""


class DuplicateRequisitionError(RequisitionError):
    """That requisition number is already used in this company."""


class IncompleteRequisitionError(RequisitionError):
    """A requisition with nothing in it, or with a line that says nothing."""


class RequisitionLockedError(RequisitionError):
    """An approved requisition is frozen; raise a revision instead."""


class RequisitionStateError(RequisitionError):
    """The asked-for transition does not apply to the requisition's state."""


class NotApprovedError(RequisitionError):
    """Something tried to source a requisition that has not finished approving."""


class Requisition(Base):
    """A request to buy: the header the lines and the approval hang off."""

    __tablename__ = "purchase_requisition"
    __table_args__ = (
        UniqueConstraint("company_id", "number", name="uq_requisition_company_number"),
        CheckConstraint(
            "status IN ('draft', 'pending', 'approved', 'rejected', 'returned')",
            name="ck_requisition_status",
        ),
        CheckConstraint("revision_no >= 1", name="ck_requisition_revision"),
        # A revision has a predecessor and a first revision does not.
        CheckConstraint(
            "(revision_no = 1) = (revision_of_id IS NULL)",
            name="ck_requisition_revision_pair",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    # What a person types and what later documents quote.
    number: Mapped[str] = mapped_column(String(32), nullable=False)
    # The actor who raised it — the identity the audit trail records.
    requested_by: Mapped[str] = mapped_column(String(64), nullable=False)
    needed_by: Mapped[date] = mapped_column(Date, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    memo: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=DRAFT)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    revision_of_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("purchase_requisition.id"), unique=True, index=True
    )
    # The engine's request for this requisition, where it needed one. Null means the
    # amount reached no configured level, so there was nothing to approve.
    approval_request_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("approval_request.id"), index=True
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    lines: Mapped[list[RequisitionLine]] = relationship(
        back_populates="requisition", order_by="RequisitionLine.line_no"
    )


class RequisitionLine(Base):
    """One thing asked for, with what it is expected to cost."""

    __tablename__ = "purchase_requisition_line"
    __table_args__ = (
        UniqueConstraint("requisition_id", "line_no", name="uq_requisition_line_no"),
        CheckConstraint("line_no >= 1", name="ck_requisition_line_starts_at_one"),
        CheckConstraint("quantity > 0", name="ck_requisition_line_quantity"),
        CheckConstraint(
            "estimated_unit_price >= 0", name="ck_requisition_line_price"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    requisition_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_requisition.id"), nullable=False, index=True
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(String(200), nullable=False)
    # The stock item, where the request is for one. A service has no item, so this
    # stays null rather than forcing a placeholder item into the master.
    item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("item.id"), index=True
    )
    quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    uom: Mapped[str] = mapped_column(String(16), nullable=False)
    estimated_unit_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)

    requisition: Mapped[Requisition] = relationship(back_populates="lines")


def _amount(value: Any) -> Decimal:
    """Exact decimal from whatever the caller passed — never through float."""
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _required(value: Any, what: str) -> str:
    stated = "" if value is None else str(value).strip()
    if not stated:
        raise IncompleteRequisitionError(f"{what} is required")
    return stated


def line_amount(line: RequisitionLine) -> Decimal:
    """What one line is expected to cost: quantity × estimated unit price."""
    return (line.quantity * line.estimated_unit_price).quantize(Decimal("0.000001"))


def requisition_total(requisition: Requisition) -> Decimal:
    """What the whole requisition is expected to cost — the amount that is routed.

    Derived from the lines every time rather than stored beside them: a total that
    can disagree with its own lines is a second answer to the same question.
    """
    return sum((line_amount(line) for line in requisition.lines), Decimal(0)).quantize(
        Decimal("0.000001")
    )


def _locked(requisition: Requisition) -> bool:
    """Whether the requisition has left the state where it may be edited."""
    return requisition.status != DRAFT


def create_requisition(
    session: Session,
    *,
    company_id: uuid.UUID,
    number: str,
    requested_by: str,
    needed_by: date,
    currency: str,
    lines: Any = (),
    memo: str | None = None,
) -> Requisition:
    """Raise a draft requisition, with its lines, in one transaction.

    The currency is resolved through T-1.ACCT.05's master, so a requisition can only
    be raised in a currency the company keeps books against. A repeated number in the
    same company is refused by the database as well as here.
    """
    wanted = _required(number, "a requisition number")
    if session.scalar(
        select(Requisition).where(
            Requisition.company_id == company_id, Requisition.number == wanted
        )
    ) is not None:
        raise DuplicateRequisitionError(
            f"requisition {wanted!r} already exists in this company"
        )
    requisition = Requisition(
        company_id=company_id,
        number=wanted,
        requested_by=_required(requested_by, "who raised the requisition"),
        needed_by=needed_by,
        currency=currency_by_code(session, company_id=company_id, code=currency).code,
        memo=memo,
        status=DRAFT,
        revision_no=1,
    )
    session.add(requisition)
    session.flush()
    for line in lines:
        add_line(session, requisition, **dict(line))
    return requisition


def add_line(
    session: Session,
    requisition: Requisition,
    *,
    description: str,
    quantity: Any,
    uom: str,
    estimated_unit_price: Any,
    item_sku: str | None = None,
) -> RequisitionLine:
    """Add one line to a **draft** requisition.

    An approved requisition is immutable, so this is refused once it has been
    submitted: the way to change what was approved is :func:`revise`, which earns a
    new approval rather than quietly rewriting the one on record.
    """
    if _locked(requisition):
        raise RequisitionLockedError(
            f"requisition {requisition.number!r} is {requisition.status}; its lines are"
            " frozen — raise a revision instead (T-2.PROC.02: revise)"
        )
    amount = _amount(quantity)
    price = _amount(estimated_unit_price)
    if amount <= 0:
        raise IncompleteRequisitionError(f"a line asks for a quantity above zero, got {amount}")
    if price < 0:
        raise IncompleteRequisitionError(f"a price is not negative, got {price}")

    item_id = None
    if item_sku is not None:
        item_id = item_by_sku(
            session, company_id=requisition.company_id, sku=str(item_sku)
        ).id

    line = RequisitionLine(
        company_id=requisition.company_id,
        requisition_id=requisition.id,
        line_no=len(requisition.lines) + 1,
        description=_required(description, "a line description"),
        quantity=amount,
        uom=_required(uom, "a unit of measure"),
        estimated_unit_price=price,
        item_id=item_id,
    )
    requisition.lines.append(line)
    session.flush()
    return line


def submit(session: Session, requisition: Requisition, *, actor: str) -> Requisition:
    """Hand the requisition to the approval engine and record the outcome.

    The engine routes by amount: a requisition that reaches a configured level goes
    to `pending` with that request open, and one that reaches **no** level is
    approved on the spot — the engine's own answer, so a company with no thresholds
    configured does not silently hold every requisition forever. A document type with
    no chain at all is refused by the engine rather than waved through.
    """
    if requisition.status != DRAFT:
        raise RequisitionStateError(
            f"requisition {requisition.number!r} is {requisition.status}; only a draft"
            " can be submitted"
        )
    if not requisition.lines:
        raise IncompleteRequisitionError(
            f"requisition {requisition.number!r} has no lines; there is nothing to approve"
        )
    total = requisition_total(requisition)
    request = start_approval(
        session,
        company_id=requisition.company_id,
        doc_type=DOC_TYPE,
        document_id=requisition.id,
        amount=total,
    )
    requisition.submitted_at = datetime.now(timezone.utc)
    if request is None:
        # Below every threshold: the engine says no approval is needed.
        requisition.status = APPROVED
    else:
        requisition.approval_request_id = request.id
        requisition.status = PENDING
    session.flush()
    return requisition


def record_decision(
    session: Session,
    requisition: Requisition,
    *,
    actor: str,
    action: str,
    role: str,
    reason: str | None = None,
) -> Requisition:
    """Take one decision on a pending requisition, through the engine.

    The engine owns the chain's order and refuses a decision from the wrong role or
    at a level the document has not reached; this function only mirrors the engine's
    resulting state onto the requisition, so the two can never disagree.
    """
    if requisition.status != PENDING or requisition.approval_request_id is None:
        raise RequisitionStateError(
            f"requisition {requisition.number!r} is {requisition.status}; there is no"
            " approval waiting to be decided"
        )
    request = session.get(ApprovalRequest, requisition.approval_request_id)
    if request is None:  # pragma: no cover — the foreign key forbids it
        raise RequisitionStateError(
            f"requisition {requisition.number!r} names an approval request that is gone"
        )
    workflow_decide(
        session, request, actor=actor, action=action, role=role, reason=reason
    )
    requisition.status = _FROM_ENGINE[request.state]
    session.flush()
    return requisition


def approval_history(session: Session, requisition: Requisition) -> list[ApprovalDecision]:
    """Every decision taken on this requisition, oldest first.

    Read from T-0.WF.01's append-only `approval_decision`, so the history is the
    engine's own record rather than a copy that could be edited here.
    """
    if requisition.approval_request_id is None:
        return []
    return list(
        session.scalars(
            select(ApprovalDecision)
            .where(ApprovalDecision.request_id == requisition.approval_request_id)
            .order_by(ApprovalDecision.decided_at, ApprovalDecision.level_no)
        )
    )


def require_sourceable(session: Session, requisition: Requisition) -> Requisition:
    """Refuse everything that is not an approved requisition, by name.

    This is the gate T-2.PROC.03 (RFQ) and T-2.PROC.05 (award → PO) call before they
    do anything: "cannot be sourced until every level approves" is enforced where
    sourcing starts, not assumed by each caller.
    """
    if requisition.status != APPROVED:
        raise NotApprovedError(
            f"requisition {requisition.number!r} is {requisition.status}; it cannot be"
            " sourced until every level of its approval chain has approved"
        )
    return requisition


def revise(
    session: Session,
    requisition: Requisition,
    *,
    number: str,
    actor: str | None = None,
) -> Requisition:
    """Raise a **new draft revision** of a requisition that is no longer a draft.

    The original keeps its state, its lines and its approval history; the revision
    copies the lines and starts its own chain from `draft`. That is how an approved
    requisition changes: by earning a fresh approval for what actually changed,
    rather than by editing the document an approval was given for.
    """
    if requisition.status == DRAFT:
        raise RequisitionStateError(
            f"requisition {requisition.number!r} is still a draft; edit it directly"
            " rather than raising a revision"
        )
    successor = session.scalar(
        select(Requisition).where(Requisition.revision_of_id == requisition.id)
    )
    if successor is not None:
        raise RequisitionStateError(
            f"requisition {requisition.number!r} already has revision"
            f" {successor.number!r}; revise that one"
        )
    revision = Requisition(
        company_id=requisition.company_id,
        number=_required(number, "a revision number"),
        requested_by=requisition.requested_by,
        needed_by=requisition.needed_by,
        currency=requisition.currency,
        memo=requisition.memo,
        status=DRAFT,
        revision_no=requisition.revision_no + 1,
        revision_of_id=requisition.id,
    )
    session.add(revision)
    session.flush()
    for line in requisition.lines:
        add_line(
            session,
            revision,
            description=line.description,
            quantity=line.quantity,
            uom=line.uom,
            estimated_unit_price=line.estimated_unit_price,
            item_sku=None,
        )
        revision.lines[-1].item_id = line.item_id
    session.flush()
    return revision


def requisition_by_number(
    session: Session, *, company_id: uuid.UUID, number: str
) -> Requisition:
    """The requisition a document quotes, or a refusal naming what is missing."""
    found = session.scalar(
        select(Requisition).where(
            Requisition.company_id == company_id,
            Requisition.number == str(number).strip(),
        )
    )
    if found is None:
        raise RequisitionError(f"no requisition {number!r} in this company")
    return found
