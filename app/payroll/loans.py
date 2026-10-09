"""T-5.PAY.03 — loans and advances, and the instalment a payroll run recovers from them.

A loan is an **advance somebody took**, so it is attributed: who asked for it and why are on
the record from the first row. What is recovered from it is the second thing this module
keeps, and the outstanding balance is **neither** — it is derived, the principle the leave
balance follows (T-5.LEAVE.02): `outstanding` is the principal minus the recoveries, so no
figure can drift away from the movements that made it.

Four rules this module is built around:

* **The schedule is derived from one number.** A loan states what is recovered **per payroll
  run**; the schedule is then as many instalments of that amount as the principal needs, with
  the last one the remainder. Nobody maintains a table of due dates, and the last instalment
  cannot be forgotten — *"recovery follows the schedule"* is arithmetic here rather than a list
  somebody keeps in step.
* **Never more than is owed, and never more than the policy allows.** The recovery for a period
  is the smallest of the instalment, the outstanding balance and the company's policy cap —
  a percentage of what is left of the employee's pay at the point the deduction is applied
  (the pack's own schedule says "after statutory deductions", which is where the engine asks).
  What the cap held back is recorded as **deferred** on the recovery, so a short recovery is a
  stated fact and not a smaller number nobody can explain.
* **Recovery is idempotent per period, in the database.** One loan, one payroll period, one
  recovery: recomputing a run — even as a new revision — finds the recovery already there and
  adds nothing. That is what stops a re-run charging an employee twice.
* **The last recovery closes the loan.** A loan whose outstanding balance reaches zero is
  settled, with who settled it and when, and nothing further is ever recovered from it.

What is deliberately *not* here: interest (the plan names instalment recovery, not interest),
the structure's own loan line (T-5.PAY.01 states it and the engine fills it), and posting the
recovery to the ledger (T-5.PAY.07).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import (
    Boolean,
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

from app.audit import append_only, deny_hard_delete
from app.db import Base
from app.hr.employees import Employee
from app.ledger.periods import period_is_locked

MONEY = Numeric(20, 6)
PERCENT = Numeric(6, 3)


class LoanError(ValueError):
    """The loan subsystem refused what was asked of it."""


class InvalidLoanError(LoanError):
    """An amount, a policy or an attribution failed validation at entry."""


class NoPolicyError(LoanError):
    """A recovery was asked for in a company that states no recovery policy."""


class ClosedPeriodError(LoanError):
    """The recovery would be recorded inside a locked month."""


class LoanSettledError(LoanError):
    """A settled loan was asked for something a settled loan cannot do."""


class LoanPolicy(Base):
    """How much of an employee's remaining pay a company will let a loan recovery take.

    Dated and appended like every other configuration row in the platform (T-5.PAY.01): a
    policy change applies from its own date, and the periods recovered before it read as they
    did. A company with no policy **in force** cannot recover anything: a cap nobody stated is
    not "unlimited".
    """

    __tablename__ = "loan_policy"
    __table_args__ = (
        UniqueConstraint("company_id", "effective_from", name="uq_loan_policy_effective"),
        CheckConstraint(
            "max_recovery_percent >= 0 AND max_recovery_percent <= 100",
            name="ck_loan_policy_percent",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    max_recovery_percent: Mapped[Decimal] = mapped_column(PERCENT, nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    stated_by: Mapped[str] = mapped_column(String(64), nullable=False)
    note: Mapped[str | None] = mapped_column(String(255))


class EmployeeLoan(Base):
    """One advance: what was lent, what is recovered per run, and when it was settled.

    The outstanding balance is not a column — see the module docstring.
    """

    __tablename__ = "employee_loan"
    __table_args__ = (
        UniqueConstraint("company_id", "reference", name="uq_employee_loan_reference"),
        CheckConstraint("principal > 0", name="ck_employee_loan_principal"),
        CheckConstraint("instalment_amount > 0", name="ck_employee_loan_instalment"),
        CheckConstraint("char_length(actor) > 0", name="ck_employee_loan_actor"),
        CheckConstraint("char_length(reason) > 0", name="ck_employee_loan_reason"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id"), nullable=False, index=True
    )
    reference: Mapped[str] = mapped_column(String(32), nullable=False)
    principal: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    # What one payroll run recovers. The schedule is derived from it (see the docstring).
    instalment_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    started_on: Mapped[date] = mapped_column(Date, nullable=False)
    # Who took the advance, and why — an advance nobody can attribute is not a record.
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    settled_on: Mapped[date | None] = mapped_column(Date)
    settled_by: Mapped[str | None] = mapped_column(String(64))

    employee: Mapped[Employee] = relationship()


class LoanRecovery(Base):
    """One period's recovery from one loan — the movement the outstanding balance is made of."""

    __tablename__ = "loan_recovery"
    __table_args__ = (
        # One loan, one payroll period, one recovery: a recomputation finds it and adds nothing.
        UniqueConstraint("loan_id", "period", name="uq_loan_recovery_period"),
        CheckConstraint("amount >= 0", name="ck_loan_recovery_amount"),
        CheckConstraint("deferred >= 0", name="ck_loan_recovery_deferred"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    loan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee_loan.id"), nullable=False, index=True
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id"), nullable=False, index=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    period: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    on_date: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    # What the policy cap held back this period, so a short recovery is stated rather than
    # smaller than expected for reasons nobody wrote down.
    deferred: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    policy_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("loan_policy.id"))
    # Why this movement is what it is, where the row would otherwise be silent: a settlement
    # records who asked for it and why, since it is not a scheduled instalment.
    note: Mapped[str | None] = mapped_column(String(255))
    # Whether this recovery was the one that closed the loan.
    settled_loan: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    loan: Mapped[EmployeeLoan] = relationship()


# A policy is configuration and a recovery is history: appended, never rewritten. A loan is a
# document — it is settled, never removed.
append_only(LoanPolicy.__table__)
append_only(LoanRecovery.__table__)
deny_hard_delete(EmployeeLoan.__table__)


def _money(value: Any) -> Decimal:
    """An amount at the column's own scale, so sums come out as read."""
    return Decimal(value).quantize(Decimal("0.000001"))


def _text(value: Any, what: str) -> str:
    """The text as written, or a refusal."""
    written = "" if value is None else str(value).strip()
    if not written:
        raise InvalidLoanError(f"{what} is required")
    return written


def _date_or_refuse(value: Any, what: str) -> date:
    """A `date`, or a refusal."""
    if isinstance(value, date) and not hasattr(value, "hour"):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise InvalidLoanError(f"not {what}: {value!r} (a date is `YYYY-MM-DD`)") from exc


def _decimal_or_refuse(value: Any, what: str) -> Decimal:
    """An exact decimal, or a refusal — a float never reaches money (DOMAIN-MODELS §2)."""
    if isinstance(value, float):
        raise InvalidLoanError(f"{what} is an exact decimal or a string, not the float {value!r}")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, AttributeError, ValueError) as exc:
        raise InvalidLoanError(f"not {what}: {value!r}") from exc
    if not number.is_finite():
        raise InvalidLoanError(f"{what} is a finite number, not {number}")
    return number


def set_policy(
    session: Session,
    *,
    company_id: uuid.UUID,
    max_recovery_percent: Any,
    effective_from: Any,
    actor: str,
    note: str | None = None,
) -> LoanPolicy:
    """State the company's recovery policy from a date: a percentage of the pay that is left."""
    who = _text(actor, "who stated the policy")
    percent = _decimal_or_refuse(max_recovery_percent, "a maximum recovery percentage")
    if not Decimal(0) <= percent <= Decimal(100):
        raise InvalidLoanError(
            f"a maximum recovery percentage is between 0 and 100, not {percent}"
        )
    starts = _date_or_refuse(effective_from, "the effective date")
    existing = session.scalar(
        select(LoanPolicy).where(
            LoanPolicy.company_id == company_id, LoanPolicy.effective_from == starts
        )
    )
    if existing is not None:
        raise InvalidLoanError(
            f"a recovery policy is already stated from {starts}; state the change from a later"
            " date instead of restating the same day"
        )
    policy = LoanPolicy(
        company_id=company_id,
        max_recovery_percent=percent,
        effective_from=starts,
        stated_by=who,
        note=None if note is None else str(note).strip() or None,
    )
    session.add(policy)
    session.flush()
    return policy


def policy_in_force(session: Session, *, company_id: uuid.UUID, on: date) -> LoanPolicy:
    """The recovery policy in force on a date, or a refusal naming what to state."""
    policy = session.scalar(
        select(LoanPolicy)
        .where(LoanPolicy.company_id == company_id, LoanPolicy.effective_from <= on)
        .order_by(LoanPolicy.effective_from.desc())
    )
    if policy is None:
        raise NoPolicyError(
            f"no loan recovery policy is in force on {on}; state the maximum percentage of pay"
            " a recovery may take (T-5.PAY.03: set_policy) before recovering anything"
        )
    return policy


def record_loan(
    session: Session,
    employee: Employee,
    *,
    reference: str,
    principal: Any,
    instalment_amount: Any,
    started_on: Any,
    actor: str,
    reason: str,
) -> EmployeeLoan:
    """Record an advance: what was lent, what a run recovers, and who took it on what basis."""
    loan = EmployeeLoan(
        company_id=employee.company_id,
        employee_id=employee.id,
        reference=_text(reference, "a loan reference"),
        principal=_decimal_or_refuse(principal, "a loan principal"),
        instalment_amount=_decimal_or_refuse(instalment_amount, "an instalment amount"),
        started_on=_date_or_refuse(started_on, "the date the advance was made"),
        actor=_text(actor, "who took the advance"),
        reason=_text(reason, "why the advance was made"),
    )
    if loan.principal <= 0:
        raise InvalidLoanError(f"a loan principal is positive, not {loan.principal}")
    if loan.instalment_amount <= 0:
        raise InvalidLoanError(
            f"an instalment is positive, not {loan.instalment_amount}; a loan that recovers"
            " nothing per run never clears"
        )
    session.add(loan)
    session.flush()
    return loan


def loans_of(
    session: Session, employee: Employee, *, settled: bool | None = None
) -> list[EmployeeLoan]:
    """The employee's loans, oldest first, optionally only the open or the settled ones."""
    rows = list(
        session.scalars(
            select(EmployeeLoan)
            .where(EmployeeLoan.employee_id == employee.id)
            .order_by(EmployeeLoan.started_on, EmployeeLoan.reference)
        )
    )
    if settled is None:
        return rows
    return [row for row in rows if (row.settled_on is not None) is settled]


def recoveries_of(session: Session, loan: EmployeeLoan) -> list[LoanRecovery]:
    """The loan's recoveries, oldest first — the movements its outstanding balance is made of."""
    return list(
        session.scalars(
            select(LoanRecovery)
            .where(LoanRecovery.loan_id == loan.id)
            .order_by(LoanRecovery.period)
        )
    )


def recovered_of(session: Session, loan: EmployeeLoan) -> Decimal:
    """Everything recovered from the loan so far, exactly."""
    return sum((row.amount for row in recoveries_of(session, loan)), Decimal(0))


def outstanding_of(session: Session, loan: EmployeeLoan) -> Decimal:
    """What is still owed — **derived** from the principal and the recoveries, never stored."""
    return loan.principal - recovered_of(session, loan)


def outstanding_after(session: Session, loan: EmployeeLoan, *, period: str) -> Decimal:
    """What was still owed **after** a payroll period — what a payslip shows.

    Read from the recoveries up to and including that period rather than from "today", because
    a payslip is a statement about a period: a later recovery must not appear on it.
    """
    recovered = sum(
        (row.amount for row in recoveries_of(session, loan) if row.period <= period), Decimal(0)
    )
    return loan.principal - recovered


def schedule_of(loan: EmployeeLoan) -> list[dict]:
    """The whole schedule, derived: instalments of the stated amount, the last the remainder.

    A schedule nobody has to maintain, and one that cannot lose its last (smaller) instalment —
    which is exactly how a loan that is "nearly cleared" ends up open forever.
    """
    remaining = loan.principal
    schedule: list[dict] = []
    sequence = 1
    while remaining > 0:
        amount = min(loan.instalment_amount, remaining)
        schedule.append({"sequence": sequence, "amount": amount})
        remaining -= amount
        sequence += 1
    return schedule


def recover_for_period(
    session: Session,
    employee: Employee,
    *,
    period: str,
    on: date,
    available: Any,
    run_id: uuid.UUID | None = None,
) -> dict:
    """Recover this period's instalment from every open loan, within the policy.

    `available` is what is left of the pay at the point the deduction is applied — the pack's
    own schedule states loan recovery happens "after statutory deductions", and this is the
    figure the cap is a percentage of. Returns what was recovered, what was deferred, and the
    recoveries themselves; a period already recovered for a loan adds nothing (**idempotent**).
    """
    policy = policy_in_force(session, company_id=employee.company_id, on=on)
    basis = _decimal_or_refuse(available, "the pay available for recovery")
    cap = _money(basis * policy.max_recovery_percent / Decimal(100))
    if period_is_locked(session, company_id=employee.company_id, on=on):
        raise ClosedPeriodError(
            f"{on:%Y-%m} is locked; a loan recovery cannot be recorded into a closed period"
        )
    recovered = Decimal(0)
    deferred = Decimal(0)
    rows: list[LoanRecovery] = []
    for loan in loans_of(session, employee, settled=False):
        if loan.started_on > on:
            continue
        existing = session.scalar(
            select(LoanRecovery).where(
                LoanRecovery.loan_id == loan.id, LoanRecovery.period == period
            )
        )
        if existing is not None:
            # Already recovered for this period: a recomputation adds nothing.
            recovered += existing.amount
            deferred += existing.deferred
            rows.append(existing)
            continue
        outstanding = outstanding_of(session, loan)
        if outstanding <= 0:
            continue
        wanted = min(loan.instalment_amount, outstanding)
        amount = min(wanted, max(Decimal(0), cap - recovered))
        held_back = wanted - amount
        settles = amount >= outstanding
        row = LoanRecovery(
            company_id=employee.company_id,
            loan_id=loan.id,
            employee_id=employee.id,
            run_id=run_id,
            period=period,
            on_date=on,
            amount=_money(amount),
            deferred=_money(held_back),
            policy_id=policy.id,
            settled_loan=settles,
        )
        session.add(row)
        session.flush()
        if settles:
            # The last instalment closed it: nobody settled this deliberately, so it names no
            # settler rather than borrowing the policy's author.
            loan.settled_on = on
            loan.settled_by = None
            session.flush()
        recovered += amount
        deferred += held_back
        rows.append(row)
    return {
        "recovered": _money(recovered),
        "deferred": _money(deferred),
        "cap": cap,
        "policy": policy,
        "recoveries": rows,
    }


def settle_loan(
    session: Session, loan: EmployeeLoan, *, on: date, actor: str, reason: str
) -> LoanRecovery:
    """Recover the whole outstanding balance in one movement, and close the loan.

    What a final settlement is: the employee leaves, or the balance is paid off in one go. It
    is recorded as a recovery like any other — with no payroll run behind it, because no period
    recovered it — and it carries **who** settled it and **why**: a loan closed by an
    adjustment nobody can trace is how a balance disappears from the books.
    """
    who = _text(actor, "who settled the loan")
    why = _text(reason, "why the loan was settled")
    when = _date_or_refuse(on, "the settlement date")
    if loan.settled_on is not None:
        raise LoanSettledError(
            f"loan {loan.reference} was settled on {loan.settled_on}; a settled loan recovers"
            " nothing further"
        )
    outstanding = outstanding_of(session, loan)
    if outstanding <= 0:
        raise LoanSettledError(f"loan {loan.reference} owes nothing")
    row = LoanRecovery(
        company_id=loan.company_id,
        loan_id=loan.id,
        employee_id=loan.employee_id,
        period=f"{when:%Y-%m}",
        on_date=when,
        amount=_money(outstanding),
        deferred=Decimal(0),
        settled_loan=True,
        note=f"final settlement by {who}: {why}",
    )
    session.add(row)
    loan.settled_on = when
    loan.settled_by = who
    session.flush()
    return row


def loan_payload(session: Session, loan: EmployeeLoan) -> dict:
    """One loan as data: what was lent, what is left, and every movement that took it down."""
    schedule = schedule_of(loan)
    recoveries = recoveries_of(session, loan)
    recovered = recovered_of(session, loan)
    outstanding = loan.principal - recovered
    return {
        "id": str(loan.id),
        "employee": loan.employee.number,
        "reference": loan.reference,
        "principal": str(loan.principal),
        "instalment_amount": str(loan.instalment_amount),
        "started_on": loan.started_on.isoformat(),
        "actor": loan.actor,
        "reason": loan.reason,
        "schedule": [str(row["amount"]) for row in schedule],
        "schedule_instalments": len(schedule),
        "recovered": str(recovered),
        "outstanding": str(outstanding),
        "settled": loan.settled_on is not None,
        "settled_on": None if loan.settled_on is None else loan.settled_on.isoformat(),
        "settled_by": loan.settled_by,
        "recoveries": [
            {
                "period": row.period,
                "on": row.on_date.isoformat(),
                "amount": str(row.amount),
                "deferred": str(row.deferred),
                "settled_loan": bool(row.settled_loan),
                "note": row.note,
                "run": None if row.run_id is None else str(row.run_id),
            }
            for row in recoveries
        ],
    }
