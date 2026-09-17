"""T-0.WF.01 — the configurable multi-level approval engine.

Any document type attaches to one configured chain: purchase requisitions
(T-2.PROC.02), purchase orders (T-2.PROC.06), payment batches (Phase 2),
inventory adjustments (T-1.INV.06) and leave requests (Phase 5) all call the
same three functions, and none of them carries approval rules of its own.

**The chain is data.** A workflow is one row per document type carrying ordered
levels; each level names the amount from which it applies and the role that
decides it. A document is routed by its amount: the levels whose threshold it
reaches are the approvals it needs, in order. Changing the chain is a row
change — no code change, no deploy — which is what §1 principle 6 asks for.

**Decisions are history.** An approve, reject or return is appended to
``approval_decision`` with its actor and reason, and that table is append-only
(T-0.AUDIT.01): the record of who approved what cannot be rewritten afterwards.

The identity that decides is stated by the caller (:func:`decide`); T-0.SEC.01
supplies it from the authenticated request, and only refusals of *permission*
belong there — this module owns the chain's own order.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
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

from app.audit import append_only
from app.db import Base

# One money scale for the whole platform.
MONEY = Numeric(20, 6)

# What a request can be waiting for, and how it ends.
PENDING, APPROVED, REJECTED, RETURNED = "pending", "approved", "rejected", "returned"

# The three things an approver can do.
APPROVE, REJECT, RETURN = "approve", "reject", "return"


class WorkflowError(ValueError):
    """Raised when a document cannot be routed or decided as asked."""


class NoWorkflowConfigured(WorkflowError):
    """A document type was submitted without a chain — refused, never waved through."""


class WorkflowAlreadyConfigured(WorkflowError):
    """A document type already has a chain; change the chain instead of adding one."""


class WrongApprover(WorkflowError):
    """The stated role does not own the level that is waiting."""


class RequestNotPending(WorkflowError):
    """A decision arrived for a request that is already finished."""


class ApprovalWorkflow(Base):
    """The chain one document type follows, per company."""

    __tablename__ = "approval_workflow"
    __table_args__ = (
        UniqueConstraint("company_id", "doc_type", name="uq_approval_workflow_doc_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    # The document type as the owning module names it ("purchase_order",
    # "inventory_adjustment", "leave_request", …). No enumeration in code: a new
    # document type is a row, not a release.
    doc_type: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    levels: Mapped[list[ApprovalLevel]] = relationship(
        back_populates="workflow", order_by="ApprovalLevel.level_no"
    )


class ApprovalLevel(Base):
    """One approval a document may need: from this amount, decided by this role."""

    __tablename__ = "approval_level"
    __table_args__ = (
        UniqueConstraint("workflow_id", "level_no", name="uq_approval_level_order"),
        CheckConstraint("level_no >= 1", name="ck_approval_level_starts_at_one"),
        CheckConstraint("threshold_amount >= 0", name="ck_approval_level_threshold"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("approval_workflow.id"), nullable=False, index=True
    )
    level_no: Mapped[int] = mapped_column(Integer, nullable=False)
    # The amount from which this approval applies (`approval_thresholds`). A level
    # never applies below its threshold, so raising one does not re-route small
    # documents.
    threshold_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    # The role that decides this level (`rbac_roles`, T-0.SEC.01).
    role: Mapped[str] = mapped_column(String(64), nullable=False)

    workflow: Mapped[ApprovalWorkflow] = relationship(back_populates="levels")


class ApprovalRequest(Base):
    """One document waiting for its chain, and where in the chain it stands."""

    __tablename__ = "approval_request"
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending', 'approved', 'rejected', 'returned')",
            name="ck_approval_request_state",
        ),
        CheckConstraint("amount >= 0", name="ck_approval_request_amount"),
        CheckConstraint("current_level >= 1", name="ck_approval_request_level"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("approval_workflow.id"), nullable=False, index=True
    )
    # The document itself: its type and its id in the owning module. A string,
    # because each phase numbers its own documents.
    doc_type: Mapped[str] = mapped_column(String(64), nullable=False)
    document_id: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default=PENDING)
    # The level now waiting; meaningless once the request is finished.
    current_level: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    decisions: Mapped[list[ApprovalDecision]] = relationship(
        back_populates="request", order_by="ApprovalDecision.level_no"
    )


class ApprovalDecision(Base):
    """One approve, reject or return — appended, never changed."""

    __tablename__ = "approval_decision"
    __table_args__ = (
        CheckConstraint(
            "action IN ('approve', 'reject', 'return')", name="ck_approval_decision_action"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("approval_request.id"), nullable=False, index=True
    )
    level_no: Mapped[int] = mapped_column(Integer, nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    request: Mapped[ApprovalRequest] = relationship(back_populates="decisions")


# A decision is history: it cannot be edited or removed (T-0.AUDIT.01).
append_only(ApprovalDecision.__table__)


def _amount(value: Any) -> Decimal:
    """Exact decimal from whatever the caller passed — never through float."""
    return value if isinstance(value, Decimal) else Decimal(str(value))


def configure(
    session: Session,
    *,
    company_id: uuid.UUID,
    doc_type: str,
    name: str,
    levels,
) -> ApprovalWorkflow:
    """Configure the chain for one document type.

    `levels` is an ordered iterable of ``(threshold_amount, role)``; level numbers
    are the order given. This is the whole configuration surface — a caller that
    changes who approves what does it here, in rows.
    """
    rows = list(levels)
    if not rows:
        raise WorkflowError(f"{doc_type} needs at least one level")
    if session.scalar(
        select(ApprovalWorkflow).where(
            ApprovalWorkflow.company_id == company_id,
            ApprovalWorkflow.doc_type == doc_type,
        )
    ) is not None:
        raise WorkflowAlreadyConfigured(
            f"{doc_type} already has an approval chain; change that chain instead of"
            " configuring a second one"
        )

    workflow = ApprovalWorkflow(company_id=company_id, doc_type=doc_type, name=name)
    workflow.levels = [
        ApprovalLevel(
            level_no=level_no,
            threshold_amount=_amount(threshold),
            role=str(role),
        )
        for level_no, (threshold, role) in enumerate(rows, start=1)
    ]
    session.add(workflow)
    session.flush()
    return workflow


def _workflow(session: Session, *, company_id: uuid.UUID, doc_type: str) -> ApprovalWorkflow:
    workflow = session.scalar(
        select(ApprovalWorkflow).where(
            ApprovalWorkflow.company_id == company_id,
            ApprovalWorkflow.doc_type == doc_type,
        )
    )
    if workflow is None:
        raise NoWorkflowConfigured(
            f"no approval chain is configured for {doc_type}; configure one before"
            " submitting a document that needs approval"
        )
    return workflow


def chain_for(session: Session, workflow: ApprovalWorkflow, amount: Any) -> list[ApprovalLevel]:
    """The approvals a document of `amount` needs, in order.

    A level applies from its threshold upwards, so the chain is the levels whose
    threshold the amount reaches, in level order. Fewer levels for a small
    document and more for a large one is the whole point of thresholds.
    """
    wanted = _amount(amount)
    return [level for level in workflow.levels if level.threshold_amount <= wanted]


def start_approval(
    session: Session,
    *,
    company_id: uuid.UUID,
    doc_type: str,
    document_id: Any,
    amount: Any,
) -> ApprovalRequest | None:
    """Route a document through its configured chain.

    Returns the request, or ``None`` when the amount reaches no level — a
    document below every configured threshold needs no approval. A document type
    with no chain at all is refused (:class:`NoWorkflowConfigured`) rather than
    waved through: "nobody configured it" is not "approved".
    """
    workflow = _workflow(session, company_id=company_id, doc_type=doc_type)
    amount = _amount(amount)
    if not chain_for(session, workflow, amount):
        return None

    request = ApprovalRequest(
        company_id=company_id,
        workflow_id=workflow.id,
        doc_type=doc_type,
        document_id=str(document_id),
        amount=amount,
        state=PENDING,
        current_level=1,
    )
    session.add(request)
    session.flush()
    return request


def decide(
    session: Session,
    request: ApprovalRequest,
    *,
    actor: str,
    action: str,
    role: str,
    reason: str | None = None,
) -> ApprovalRequest:
    """Record one decision on a pending request and move it on.

    `role` is the stated role of `actor` — T-0.SEC.01 supplies it from the
    authenticated request; the chain's own order is enforced here, so a decision
    cannot arrive at a level the document has not reached, or from a role the
    level does not name.
    """
    if request.state != PENDING:
        raise RequestNotPending(
            f"{request.doc_type} {request.document_id} is {request.state};"
            " a finished request takes no further decisions"
        )
    if action not in (APPROVE, REJECT, RETURN):
        raise WorkflowError(f"unknown decision {action!r}; expected {APPROVE}, {REJECT} or {RETURN}")
    if not reason and action in (REJECT, RETURN):
        raise WorkflowError(f"{action} needs a reason")

    chain = chain_for(
        session,
        _workflow(session, company_id=request.company_id, doc_type=request.doc_type),
        request.amount,
    )
    waiting = chain[request.current_level - 1]
    if role != waiting.role:
        raise WrongApprover(
            f"level {waiting.level_no} of {request.doc_type} is decided by"
            f" {waiting.role!r}, not {role!r}"
        )

    request.decisions.append(
        ApprovalDecision(
            level_no=waiting.level_no, actor=str(actor), action=action, reason=reason
        )
    )
    if action == APPROVE:
        if request.current_level >= len(chain):
            request.state = APPROVED
        else:
            request.current_level += 1
    elif action == REJECT:
        request.state = REJECTED
    else:
        request.state = RETURNED
    session.flush()
    return request
