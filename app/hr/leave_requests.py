"""T-5.LEAVE.03 — applying for leave, the chain that decides it, and what a decision does.

The application is a **document**, and everything about it that matters is somebody else's
job: who approves it belongs to the approval engine (T-0.WF.01, configured per document
type — this module only names the type `leave`), what a balance is belongs to the entries
(T-5.LEAVE.02, which this module only adds to), and whether a day is worked belongs to the
calendar (T-5.LEAVE.01). What is left here is the workflow of *one request*:

* **The day count is read, never typed.** `days` is how many of the days in the range the
  calendar says are worked, so a holiday inside a week of leave costs nothing and the figure
  the approver sees is the figure the balance moves by.
* **An application over the balance is refused unless somebody takes it on the record.** The
  refusal names the balance and the ask; the override requires both an actor and a reason,
  which is the difference between a decision and a favour.
* **A balance moves when a decision is made, not when the leave starts.** An approved future
  absence is already spent — deducting it on the day it starts would let two applications
  book the same days twice, each looking affordable when it was made.
* **Exactly once.** The move is posted only when the engine reports the request approved, and
  the entry it posted is named on the request: a second decision on a finished request is
  refused by the engine, and a replay of the posting itself adds nothing.
* **A cancelled application restores what it took and nothing else.** Cancelling a *pending*
  request credits nothing (it never debited anything); cancelling an *approved* one posts the
  days back as an adjustment, because a leave ledger is appended to and never rewritten.

Weekly rest is not stated anywhere in the ledger's calendar (T-5.LEAVE.01 states holidays),
so a day the calendar calls worked is a leave day — the day count is the calendar's answer,
not a guess about somebody's weekends.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    String,
    Uuid,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.audit import deny_hard_delete
from app.db import Base
from app.hr.employees import Employee
from app.hr.holidays import is_working_day
from app.hr.leave import DAYS, LeaveError, LeaveType, balance, record_entry
from app.hr.movements import active_on
from app.hr.org import latest_placement
from app.workflow import (
    APPROVED,
    PENDING,
    ApprovalRequest,
    decide,
    require_chain,
    start_approval,
)

# The document type this module routes: the ledger's `approval_levels` names Leave, and the
# chain for it is configuration (T-0.WF.01), never a branch in this code.
LEAVE_DOC_TYPE = "leave"

# An application's own states: the engine's `pending`, `approved`, `rejected` and `returned`
# are mirrored as they come, and `cancelled` is this module's — a withdrawal is not a
# decision by an approver, so no approval engine has an opinion about it.
REQUEST_STATES = ("pending", "approved", "rejected", "returned", "cancelled")
CANCELLED = "cancelled"


class LeaveRequestError(LeaveError):
    """A leave application or a decision on one was refused."""


class InvalidApplicationError(LeaveRequestError):
    """The dates, the range or the type failed validation at submission."""


class InsufficientBalanceError(LeaveRequestError):
    """The application asks for more days than the balance holds."""


class NotDecidableError(LeaveRequestError):
    """A decision or a cancellation arrived for an application that is finished."""


class LeaveRequest(Base):
    """One application for leave, and where it stands.

    A document rather than a ledger: it has a state that moves, and it names the approval
    request that decides it and the entry that its approval posted. Both are `None` while
    they do not exist, which is a different answer from "there was none".
    """

    __tablename__ = "leave_request"
    __table_args__ = (
        CheckConstraint(
            "state IN (" + ", ".join(f"'{state}'" for state in REQUEST_STATES) + ")",
            name="ck_leave_request_state",
        ),
        CheckConstraint("to_date >= from_date", name="ck_leave_request_range"),
        # Zero days is not an application: a range of nothing but holidays has nothing to
        # approve, and a balance is only moved by an amount somebody asked for.
        CheckConstraint("days > 0", name="ck_leave_request_days"),
        CheckConstraint("char_length(requested_by) > 0", name="ck_leave_request_applicant"),
        # An override is an actor **and** a reason or it is not an override.
        CheckConstraint(
            "(override_actor IS NULL) = (override_reason IS NULL)",
            name="ck_leave_request_override_named",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id"), nullable=False, index=True
    )
    leave_type_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("leave_type.id"), nullable=False, index=True
    )
    from_date: Mapped[date] = mapped_column(Date, nullable=False)
    to_date: Mapped[date] = mapped_column(Date, nullable=False)
    # Read from the calendar, never typed by the applicant (see the module docstring).
    days: Mapped[Decimal] = mapped_column(DAYS, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(16), nullable=False, default=PENDING)
    requested_by: Mapped[str] = mapped_column(String(64), nullable=False)
    approval_request_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("approval_request.id"), index=True
    )
    # The entry an approval posted. Set with the approval, in the same transaction, which is
    # what makes "exactly once" checkable rather than hoped for.
    entry_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("leave_entry.id"))
    # Who took the over-balance decision on the record, and why. Null when the application
    # was within the balance, which is the ordinary case.
    override_actor: Mapped[str | None] = mapped_column(String(64))
    override_reason: Mapped[str | None] = mapped_column(String(255))
    cancelled_by: Mapped[str | None] = mapped_column(String(64))
    cancel_reason: Mapped[str | None] = mapped_column(String(255))

    employee: Mapped[Employee] = relationship()
    leave_type: Mapped[LeaveType] = relationship()
    approval_request: Mapped[ApprovalRequest | None] = relationship()


# A request is a document: never removed, but its state moves — see its own note.
deny_hard_delete(LeaveRequest.__table__)


def _required(value: Any, what: str) -> str:
    """The text as written, or a refusal — an unnamed actor is not an attribution."""
    text = "" if value is None else str(value).strip()
    if not text:
        raise InvalidApplicationError(f"{what} is required")
    return text


def _clean(value: Any) -> str | None:
    """The text, or ``None`` when there is nothing there."""
    text = "" if value is None else str(value).strip()
    return text or None


def _date_or_refuse(value: Any, what: str) -> date:
    """A `date`, or a refusal — a `datetime` is quietly reduced to its day."""
    if isinstance(value, date) and not hasattr(value, "hour"):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise InvalidApplicationError(f"not {what}: {value!r} (a date is `YYYY-MM-DD`)") from exc


def _refuse_outside_employment(
    session: Session, employee: Employee, *, from_date: date, to_date: date
) -> None:
    """Leave is only for days the person is employed — both ends of the range.

    Stated at submission as well as at posting, so an application for days somebody does not
    work is a refusal at the desk rather than a surprise at the approval.
    """
    for on in (from_date, to_date):
        if not active_on(session, employee, on=on):
            raise InvalidApplicationError(
                f"{employee.number!r} was not employed on {on}, so {on} is not leave to apply"
                " for (T-5.EMP.03)"
            )


def working_days(
    session: Session, employee: Employee, *, from_date: Any, to_date: Any
) -> Decimal:
    """How many days of leave the range costs: the days in it the calendar calls worked.

    The single reading of the calendar for leave (T-5.LEAVE.01's own contract), for the
    region the employee's latest placement puts them in — a market holiday is not a Dubai
    holiday.
    """
    start = _date_or_refuse(from_date, "the first day of leave")
    end = _date_or_refuse(to_date, "the last day of leave")
    if end < start:
        raise InvalidApplicationError(f"the last day of leave {end} is before the first {start}")
    placement = latest_placement(session, employee)
    region = placement.location if placement is not None else None
    count = Decimal(0)
    day = start
    while day <= end:
        if is_working_day(session, company_id=employee.company_id, on=day, region=region):
            count += 1
        day += timedelta(days=1)
    return count


def applications_of(
    session: Session, employee: Employee, *, state: str | None = None
) -> list[LeaveRequest]:
    """The employee's applications, oldest first, optionally only those in `state`."""
    query = select(LeaveRequest).where(LeaveRequest.employee_id == employee.id)
    if state is not None:
        query = query.where(LeaveRequest.state == state)
    return list(session.scalars(query.order_by(LeaveRequest.from_date, LeaveRequest.id)))


def leave_on(session: Session, employee: Employee, *, on: date) -> LeaveRequest | None:
    """The approved application covering `on`, or ``None`` — what attendance reads."""
    return session.scalar(
        select(LeaveRequest)
        .where(
            LeaveRequest.employee_id == employee.id,
            LeaveRequest.state == APPROVED,
            LeaveRequest.from_date <= on,
            LeaveRequest.to_date >= on,
        )
        .order_by(LeaveRequest.from_date, LeaveRequest.id)
    )


def leave_in_period(
    session: Session, employee: Employee, *, from_date: date, to_date: date
) -> list[dict]:
    """Every approved leave **day** in the range, with its type and whether it is paid.

    Days rather than requests, because that is what payroll pays: what it needs to know is
    which days of a period an employee was absent, and whether those days are paid (the
    type's own answer, T-5.LEAVE.02).
    """
    days: list[dict] = []
    placement = latest_placement(session, employee)
    region = placement.location if placement is not None else None
    for request in applications_of(session, employee, state=APPROVED):
        day = max(request.from_date, from_date)
        end = min(request.to_date, to_date)
        while day <= end:
            if is_working_day(session, company_id=employee.company_id, on=day, region=region):
                days.append(
                    {
                        "on": day,
                        "leave_type": request.leave_type.code,
                        "paid": bool(request.leave_type.paid),
                        "request": str(request.id),
                    }
                )
            day += timedelta(days=1)
    return sorted(days, key=lambda row: row["on"])


def _post_deduction(session: Session, request: LeaveRequest) -> None:
    """Take the days off the balance once, and name the entry that did it.

    Who did it is not written here: T-0.AUDIT.02 attributes the row from the request context,
    so the act is attributed once rather than twice, in two places that can disagree.
    """
    if request.entry_id is not None:
        return
    entry = record_entry(
        session,
        request.employee,
        leave_type=request.leave_type,
        kind="leave_taken",
        days=-request.days,
        on=date.today(),
        source=f"leave request {request.id}",
    )
    request.entry_id = entry.id
    request.state = APPROVED


def request_leave(
    session: Session,
    employee: Employee,
    *,
    leave_type: LeaveType,
    from_date: Any,
    to_date: Any,
    actor: str,
    reason: str | None = None,
    override_actor: str | None = None,
    override_reason: str | None = None,
) -> LeaveRequest:
    """Apply for leave: validated, priced by the calendar, routed, checked against balance.

    The application reaches the approved state by one of two roads, and both post the same
    single deduction: an amount that reaches no configured level needs no approval
    (T-0.WF.01's rule, not this module's), and one that does waits for its chain. A document
    type with no chain at all is **refused** — "nobody configured a leave approver" is not an
    approval.
    """
    applicant = _required(actor, "the applicant")
    if leave_type.company_id != employee.company_id:
        raise InvalidApplicationError(
            f"leave type {leave_type.code!r} belongs to another company"
        )
    start = _date_or_refuse(from_date, "the first day of leave")
    end = _date_or_refuse(to_date, "the last day of leave")
    _refuse_outside_employment(session, employee, from_date=start, to_date=end)
    days = working_days(session, employee, from_date=start, to_date=end)
    if days <= 0:
        raise InvalidApplicationError(
            f"{start} to {end} holds no working day (T-5.LEAVE.01), so there is nothing to"
            " apply for"
        )

    # The chain is configuration, read at the moment of submission: change who approves leave
    # and the next application follows the new chain, with no code change here. Asked for
    # **before** the request row exists, so "nobody configured a leave approver" refuses an
    # application rather than leaving one half-written.
    require_chain(session, company_id=employee.company_id, doc_type=LEAVE_DOC_TYPE)

    available = balance(session, employee, leave_type=leave_type, on=start)
    over = days > available
    if over:
        # Both, or neither: an override nobody signed is a hole in the balance.
        if not (_clean(override_actor) and _clean(override_reason)):
            raise InsufficientBalanceError(
                f"{employee.number!r} holds {available} day(s) of {leave_type.code!r} on"
                f" {start} and {start} to {end} asks for {days}: refuse it, or state the"
                " override_actor and override_reason that take the difference on the record"
            )
        override_actor = _required(override_actor, "the actor allowing an over-balance leave")
        override_reason = _required(override_reason, "the reason for an over-balance leave")

    request = LeaveRequest(
        company_id=employee.company_id,
        employee_id=employee.id,
        leave_type_id=leave_type.id,
        from_date=start,
        to_date=end,
        days=days,
        reason=None if reason is None else str(reason).strip() or None,
        state=PENDING,
        requested_by=applicant,
        override_actor=None if not over else override_actor,
        override_reason=None if not over else override_reason,
    )
    session.add(request)
    session.flush()

    request.approval_request = start_approval(
        session,
        company_id=employee.company_id,
        doc_type=LEAVE_DOC_TYPE,
        document_id=request.id,
        amount=days,
    )
    if request.approval_request is None:
        # Below every configured threshold: there is no approval to wait for, so the
        # application is decided by the act of making it.
        _post_deduction(session, request)
    session.flush()
    return request


def decide_request(
    session: Session,
    request: LeaveRequest,
    *,
    actor: str,
    action: str,
    role: str,
    reason: str | None = None,
) -> LeaveRequest:
    """Record one decision on a pending application and follow it through.

    The chain's order and who owns a level are the engine's to enforce; what this adds is the
    consequence — an approval that completes the chain posts the deduction, once, and any
    other decision leaves the balance exactly where it was. A request decided when it was
    submitted has nothing left to decide.

    The decision and the movement it makes are **one unit of work**: both are written in the
    caller's transaction, so a posting that cannot be made (an employee whose employment ended
    before the deduction could be recorded, say) takes the decision with it. A caller that
    rolls back has neither, and the engine has no half-decided request to reason about.
    """
    if request.approval_request_id is None:
        raise NotDecidableError(
            f"leave request {request.id} was {request.state} when it was submitted (no"
            " configured level applies); there is nothing to decide"
        )
    decide(
        session,
        request.approval_request,
        actor=actor,
        action=action,
        role=role,
        reason=reason,
    )
    if request.approval_request.state == APPROVED:
        _post_deduction(session, request)
    else:
        # Rejected, returned or still waiting on a further level: the balance does not move.
        request.state = request.approval_request.state
    session.flush()
    return request


def cancel_request(
    session: Session, request: LeaveRequest, *, actor: str, reason: str
) -> LeaveRequest:
    """Withdraw an application — a decision's worth of attribution, without a decision.

    Cancelling a pending application credits nothing: it never took anything. Cancelling an
    approved one gives the days back as an **adjustment** dated today, because the entry that
    took them is history and history is appended to (T-0.AUDIT.01) — the balance is right
    again and the record says why.
    """
    who = _required(actor, "who cancelled the application")
    why = _required(reason, "why the application was cancelled")
    if request.state == APPROVED:
        record_entry(
            session,
            request.employee,
            leave_type=request.leave_type,
            kind="adjustment",
            days=request.days,
            on=date.today(),
            source=f"cancelled leave {request.id}",
        )
    elif request.state != PENDING:
        raise NotDecidableError(
            f"leave request {request.id} is {request.state}; only a pending or an approved"
            " application is cancelled (a returned one is resubmitted as a new application)"
        )
    request.state = CANCELLED
    request.cancelled_by = who
    request.cancel_reason = why
    session.flush()
    return request
