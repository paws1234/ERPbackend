"""T-5.PAY.02 — the payroll engine: a period computed from the contract, the roster and leave.

A run is a **document with a lifecycle** — draft, computed, approved — and its figures are
**rows**: one line per employee, one row per component that produced a figure, and one row per
**day** the employee was paid for or not. That is what makes the criterion "every input is
traceable to its source record" a property of the data rather than a promise: a line's net pay
can be read down to the Tuesday it came from and the punch or leave request behind it.

Six rules this module is built around:

* **Nothing is priced by a default.** The daily rate is the contract's own monthly basic
  salary divided by the period's days; a worked day pays the fraction of the schedule that was
  actually worked; an approved **paid** leave day (T-5.LEAVE.03, read through the leave type's
  own flag) pays a day; a holiday pays a day; a day nobody worked, or an unpaid leave day, pays
  nothing. A day whose **roster states no shift** is not paid a guess: the line is marked
  **incomplete** with the day and the reason, and a run with any incomplete line cannot be
  approved — an employee with incomplete attendance is flagged, never quietly paid a default.
* **Overtime is the attendance classification's figure, priced by the rules in force.** The
  minutes come from T-5.ATT.03 (`classify_day`), the band's multiplier from its rule, and the
  hourly rate from the day's own daily rate against the period's reference schedule — so a
  rest-day premium is priced at the rest-day multiplier, not at ordinary time.
* **Attendance, not assertion.** Worked minutes, late minutes, overtime and absences are read
  from the punches and the roster (T-5.ATT.01/02/03); unpaid leave is read from the leave
  module; the salary is read from the contract (T-5.EMP.01). Lateness reduces pay the only way
  it can without inventing a penalty: the day is paid for the minutes worked against the
  minutes scheduled, and the late minutes are on the record beside it.
* **The structure is the pack's and the company's, in its own order.** Earnings are computed
  first and completely, then the deductions are taken in the order the structure states
  (T-5.PAY.01); a percentage deduction reads the basis its own component names. A component
  whose basis is a loan schedule carries nothing here — T-5.PAY.03 fills it.
* **The same inputs produce the same figures.** Every amount is an exact decimal quantised
  once, every date is stated by the run, and nothing reads a clock except the moment the run
  was computed. A correction does not rewrite a run: it appends a **revision** that supersedes
  it, names who asked and why, and recomputes from the same inputs to the same numbers.
* **An approved run is closed.** Computing it again is refused; the way back is a correction,
  which is an audited act with a reason — the same shape as every other correction in the
  platform.

What is deliberately *not* here: loan recovery (T-5.PAY.03 reads the same lines), report
production (T-5.PAY.04), payslips (T-5.PAY.05), bank files (T-5.PAY.06) and posting to the
ledger (T-5.PAY.07), which all read the rows this module writes rather than recomputing them.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
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
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.audit import append_only, deny_hard_delete
from app.db import Base
from app.hr.employees import Employee, contract_in_force
from app.hr.leave_requests import leave_on
from app.hr.movements import employment_in
from app.hr.overtime import classify_day
from app.payroll import loans
from app.payroll.components import components_in_force, structure_on

RUN_STATES = ("draft", "computed", "approved")
# What a day of the period was, as payroll must be able to say: a day worked, a day of leave
# (paid or not), a holiday, a day nobody turned up for, or a day not worked at all.
INPUT_KINDS = ("worked", "paid_leave", "unpaid_leave", "holiday", "absent", "not_worked")

MONEY = Numeric(20, 6)
SIX = Decimal("0.000001")
_MONTH = re.compile(r"^\d{4}-\d{2}$")

# The codes this module states for itself, because they are read from records rather than from
# the structure. They are rows on the line like any other component, so a payslip shows them.
BASIC_CODE = "BASIC"
OVERTIME_CODE = "OVERTIME"


class PayrollError(ValueError):
    """Payroll refused what was asked of it."""


class InvalidRunError(PayrollError):
    """A period, a cutoff or a state transition failed validation."""


class RunClosedError(PayrollError):
    """An approved run was asked to compute again without a correction."""


class IncompleteAttendanceError(PayrollError):
    """A run with lines that could not be computed from the records was asked to be approved."""


class PayrollRun(Base):
    """One period's payroll, at one revision.

    A document: its state moves (draft, computed, approved) and it can be superseded by the
    next revision. The **lines** never move — see :class:`PayrollLine`.
    """

    __tablename__ = "payroll_run"
    __table_args__ = (
        UniqueConstraint("company_id", "period", "revision", name="uq_payroll_run_revision"),
        CheckConstraint(
            "state IN (" + ", ".join(f"'{state}'" for state in RUN_STATES) + ")",
            name="ck_payroll_run_state",
        ),
        CheckConstraint("revision >= 1", name="ck_payroll_run_revision"),
        CheckConstraint("to_date >= from_date", name="ck_payroll_run_bounds"),
        CheckConstraint(
            "cutoff_date BETWEEN from_date AND to_date", name="ck_payroll_run_cutoff"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    period: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    from_date: Mapped[date] = mapped_column(Date, nullable=False)
    to_date: Mapped[date] = mapped_column(Date, nullable=False)
    # The day the period was cut off at (`payroll_cutoff_day`), stated by the run so a figure
    # can be explained later: attendance after the cutoff belongs to the next period.
    cutoff_date: Mapped[date] = mapped_column(Date, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # The revision this one replaces, and — on the old row — the one that replaced it. The live
    # run for a period is the one nothing supersedes.
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("payroll_run.id"))
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("payroll_run.id"))
    # How the figures were arrived at, on the run itself: the structure in force on this date
    # (T-5.PAY.01) and the pack version(s) its statutory rows came from.
    structure_as_of: Mapped[date] = mapped_column(Date, nullable=False)
    pack_versions: Mapped[str | None] = mapped_column(String(64))
    computed_by: Mapped[str | None] = mapped_column(String(64))
    computed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[str | None] = mapped_column(String(64))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    correction_actor: Mapped[str | None] = mapped_column(String(64))
    correction_reason: Mapped[str | None] = mapped_column(String(255))
    # The ledger entry this revision posted, and the entry that reversed a superseded
    # revision's (T-5.PAY.07). Null is "not posted", which is a different answer from "posted
    # nothing": nothing in a payroll run posts to nothing.
    journal_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("journal_entry.id")
    )
    reversal_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("journal_entry.id")
    )
    posted_on: Mapped[date | None] = mapped_column(Date)
    posted_by: Mapped[str | None] = mapped_column(String(64))

    lines: Mapped[list[PayrollLine]] = relationship(back_populates="run")


class PayrollLine(Base):
    """One employee's pay for one run — the figures, and the day counts they rest on."""

    __tablename__ = "payroll_line"
    __table_args__ = (
        UniqueConstraint("run_id", "employee_id", name="uq_payroll_line_employee"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("payroll_run.id"), nullable=False, index=True
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id"), nullable=False, index=True
    )
    # The terms the pay was computed from, as a record rather than a number that appeared.
    contract_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("employment_contract.id"))
    basic_salary: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    daily_rate: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    period_days: Mapped[int] = mapped_column(Integer, nullable=False)
    employed_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    worked_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    paid_leave_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unpaid_leave_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    holiday_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    absent_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    overtime_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    late_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # The schedule overtime is priced against: the last schedule the period states.
    reference_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    basic_pay: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    gross: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    taxable_gross: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    deductions_total: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    employer_contributions_total: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, default=Decimal(0)
    )
    net: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    # Incomplete attendance: what could not be computed, in words, and no figure invented for
    # it. A run with one of these cannot be approved.
    incomplete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    flag_reason: Mapped[str | None] = mapped_column(String(500))

    run: Mapped[PayrollRun] = relationship(back_populates="lines")
    employee: Mapped[Employee] = relationship()


class PayrollLineComponent(Base):
    """One figure on a line, and the row of the structure that produced it."""

    __tablename__ = "payroll_line_component"
    __table_args__ = (
        UniqueConstraint("line_id", "code", name="uq_payroll_line_component_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    line_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("payroll_line.id"), nullable=False, index=True
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    basis: Mapped[str] = mapped_column(String(24), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    order_no: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    taxable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    account_code: Mapped[str | None] = mapped_column(String(32))
    # Why this figure is what it is, where the basis does not say it by itself — a loan
    # recovery held back by the policy, for instance.
    note: Mapped[str | None] = mapped_column(String(255))
    # The structure's own row, where the figure came from one.
    component_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payroll_component.id")
    )


class PayrollLineInput(Base):
    """One day of the period, and what payroll made of it.

    The unit of traceability: the day, what it was, the minutes worked against the minutes
    scheduled, the late minutes, the overtime and its multiplier — and the **source record**:
    the day's attendance (found from the employee and the date) or the leave request that
    covered it.
    """

    __tablename__ = "payroll_line_input"
    __table_args__ = (
        UniqueConstraint("line_id", "on_date", name="uq_payroll_line_input_day"),
        CheckConstraint(
            "kind IN (" + ", ".join(f"'{kind}'" for kind in INPUT_KINDS) + ")",
            name="ck_payroll_line_input_kind",
        ),
        CheckConstraint("worked_minutes >= 0", name="ck_payroll_line_input_worked"),
        CheckConstraint("overtime_minutes >= 0", name="ck_payroll_line_input_overtime"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    line_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("payroll_line.id"), nullable=False, index=True
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id"), nullable=False, index=True
    )
    on_date: Mapped[date] = mapped_column(Date, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    worked_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    scheduled_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    late_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    overtime_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    multiplier: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    # What the day paid, at this day's own rate — so the line's basic pay is the sum of the
    # days rather than a figure that has to be taken on trust.
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    source_type: Mapped[str] = mapped_column(String(24), nullable=False)
    source_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)


# A run's state moves, so the run row is a document, not a ledger; everything it produced is
# history and is appended, never rewritten.
deny_hard_delete(PayrollRun.__table__)
append_only(PayrollLine.__table__)
append_only(PayrollLineComponent.__table__)
append_only(PayrollLineInput.__table__)


def _text(value: Any, what: str) -> str:
    """The text as written, or a refusal."""
    written = "" if value is None else str(value).strip()
    if not written:
        raise InvalidRunError(f"{what} is required")
    return written


def _money(value: Any) -> Decimal:
    """An amount quantised to the column's own scale, so sums come out as read."""
    return Decimal(value).quantize(SIX, rounding=ROUND_HALF_UP)


def _number(value: Any, what: str) -> Decimal:
    """An exact decimal from a contract figure, or a refusal — never through float."""
    if isinstance(value, float):
        raise InvalidRunError(f"{what} is an exact decimal, not the float {value!r}")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, AttributeError, ValueError) as exc:
        raise InvalidRunError(f"not {what}: {value!r}") from exc
    if not number.is_finite():
        raise InvalidRunError(f"{what} is a finite number, not {number}")
    return number


def period_bounds(period: str) -> tuple[date, date]:
    """The first and last day of a `YYYY-MM` period — the period's own days, stated."""
    written = _text(period, "a payroll period").strip()
    if not _MONTH.match(written):
        raise InvalidRunError(
            f"not a payroll period: {period!r}; a period is `YYYY-MM` and the cycle is monthly"
            " (§4 Phase 5)"
        )
    year, month = (int(part) for part in written.split("-"))
    if not 1 <= month <= 12:
        raise InvalidRunError(f"not a payroll period: {period!r} (a month is 01–12)")
    first = date(year, month, 1)
    following = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return first, following - timedelta(days=1)


def _cutoff_date(period_start: date, period_end: date, cutoff_day: Any) -> date:
    """The day the period was cut off at, inside the period — `payroll_cutoff_day`."""
    if isinstance(cutoff_day, bool) or not isinstance(cutoff_day, int) or not 1 <= cutoff_day <= 31:
        raise InvalidRunError(
            f"a payroll cutoff day is a day of the month (1–31), not {cutoff_day!r}"
        )
    day = min(cutoff_day, period_end.day)
    return max(period_start, date(period_start.year, period_start.month, day))


def start_run(
    session: Session,
    *,
    company_id: uuid.UUID,
    period: str,
    actor: str,
    cutoff_day: int = 15,
) -> PayrollRun:
    """Open a period's payroll: a draft run with the period, the cutoff and the structure it
    will read, and nothing computed yet.

    One **live** run per period: a second is refused by name, because two live runs for one
    month is two answers to the same question. A correction is a new revision of the same run,
    not a second run beside it.
    """
    who = _text(actor, "who opened the run")
    starts, ends = period_bounds(period)
    existing = run_for(session, company_id=company_id, period=period)
    if existing is not None:
        raise InvalidRunError(
            f"{period} already has a live run at revision {existing.revision}"
            f" ({existing.state}); correct that run rather than opening another"
        )
    run = PayrollRun(
        company_id=company_id,
        period=period,
        from_date=starts,
        to_date=ends,
        cutoff_date=_cutoff_date(starts, ends, cutoff_day),
        state="draft",
        revision=1,
        structure_as_of=ends,
    )
    session.add(run)
    session.flush()
    return run


def run_for(session: Session, *, company_id: uuid.UUID, period: str) -> PayrollRun | None:
    """The live run for a period — the one no revision supersedes, or ``None``."""
    return session.scalar(
        select(PayrollRun)
        .where(
            PayrollRun.company_id == company_id,
            PayrollRun.period == period,
            PayrollRun.superseded_by_id.is_(None),
        )
        .order_by(PayrollRun.revision.desc())
    )


def lines_of(session: Session, run: PayrollRun) -> list[PayrollLine]:
    """The run's lines, in employee number order."""
    rows = session.scalars(
        select(PayrollLine).where(PayrollLine.run_id == run.id).order_by(PayrollLine.employee_id)
    ).all()
    return sorted(rows, key=lambda row: row.employee.number)


def line_for(session: Session, run: PayrollRun, employee: Employee) -> PayrollLine | None:
    """One employee's line on a run, or ``None`` when the period did not employ them."""
    return session.scalar(
        select(PayrollLine).where(
            PayrollLine.run_id == run.id, PayrollLine.employee_id == employee.id
        )
    )


def line_components(session: Session, line: PayrollLine) -> list[PayrollLineComponent]:
    """The figures on a line, in the order they were applied."""
    return list(
        session.scalars(
            select(PayrollLineComponent)
            .where(PayrollLineComponent.line_id == line.id)
            .order_by(PayrollLineComponent.order_no, PayrollLineComponent.code)
        )
    )


def line_inputs(session: Session, line: PayrollLine) -> list[PayrollLineInput]:
    """The days of a line, oldest first — the record every figure on it rests on."""
    return list(
        session.scalars(
            select(PayrollLineInput)
            .where(PayrollLineInput.line_id == line.id)
            .order_by(PayrollLineInput.on_date)
        )
    )


def _add_component(
    session: Session,
    line: PayrollLine,
    *,
    code: str,
    name: str,
    kind: str,
    basis: str,
    amount: Decimal,
    source: str,
    order: int,
    taxable: bool,
    account_code: str | None = None,
    component_id: uuid.UUID | None = None,
    note: str | None = None,
) -> None:
    """Record one figure on a line."""
    session.add(
        PayrollLineComponent(
            line_id=line.id,
            company_id=line.company_id,
            code=code,
            name=name,
            kind=kind,
            basis=basis,
            amount=_money(amount),
            source=source,
            order_no=order,
            taxable=taxable,
            account_code=account_code,
            note=note,
            component_id=component_id,
        )
    )


def _day_input(
    session: Session, employee: Employee, *, on: date, daily_rate: Decimal
) -> dict:
    """One day of the period, read from attendance, the roster and leave.

    The day's own `classify_day` (T-5.ATT.03) is the reading — it carries the punches, the
    resolved shift, the holiday status, the late minutes and the overtime band — and this
    function's whole job is to turn it into what payroll pays, keeping every figure and the
    source it came from.
    """
    classified = classify_day(session, employee, on=on)
    leave = leave_on(session, employee, on=on)
    status = classified["status"]
    shift_missing = (
        status == "working" and classified["shift"] is None and not classified["overridden"]
    )
    overtime_minutes = int(classified["overtime_minutes"])
    multiplier = None if classified["multiplier"] is None else Decimal(classified["multiplier"])

    source_type = "attendance"
    source_id = None
    scheduled = int(classified["scheduled_minutes"])
    worked = int(classified["worked_minutes"])
    kind = "not_worked"
    amount = Decimal(0)
    flag: str | None = None

    if leave is not None:
        source_type, source_id = "leave_request", leave.id
        paid = bool(leave.leave_type.paid)
        leave_pay = daily_rate if paid else Decimal(0)
        # A day that was both on leave and worked pays the better of the two: leave is the
        # day's status, and the work done on it is not thrown away.
        worked_pay = (
            daily_rate * Decimal(min(worked, scheduled)) / Decimal(scheduled)
            if scheduled > 0
            else Decimal(0)
        )
        amount = max(leave_pay, worked_pay)
        kind = "paid_leave" if paid else "unpaid_leave"
    elif status != "working":
        # A holiday is a day paid: the calendar states it, not the roster.
        kind = "holiday"
        amount = daily_rate
    elif shift_missing:
        # No shift is on the roster for a working day: payroll will not invent a schedule to
        # pay against, and the run cannot be approved until somebody states one.
        flag = (
            f"{on.isoformat()}: no shift is on the roster for a working day (T-5.ATT.01), so"
            " the day cannot be priced"
        )
    elif classified["overridden"]:
        # A day deliberately stated as not worked. Nothing is paid for it.
        kind = "not_worked"
        amount = Decimal(0)
    elif scheduled > 0:
        kind = "absent" if worked == 0 else "worked"
        # Paid for the minutes worked against the minutes scheduled. Overtime is capped out of
        # this fraction and priced separately, so a long day cannot inflate the basic pay.
        amount = daily_rate * Decimal(min(worked, scheduled)) / Decimal(scheduled)
    else:
        flag = (
            f"{on.isoformat()}: the roster states no shift and the day is not a holiday, so the"
            " day cannot be priced (T-5.ATT.01 / T-5.LEAVE.01)"
        )

    unclassified = int(classified["unclassified_overtime_minutes"])
    if unclassified:
        unpriced = (
            f"{on.isoformat()}: {unclassified} overtime minute(s) are unclassified —"
            f" {classified['unclassified_reason']}"
        )
        flag = unpriced if flag is None else f"{flag} | {unpriced}"

    return {
        "on_date": on,
        "kind": kind,
        "rate": daily_rate,
        "worked": worked,
        "scheduled": scheduled,
        "late": int(classified["late_minutes"]),
        "overtime": overtime_minutes,
        "unclassified": unclassified,
        "multiplier": multiplier,
        "amount": _money(amount),
        "source_type": source_type,
        "source_id": source_id,
        "flag": flag,
    }


def _compute_line(
    session: Session,
    run: PayrollRun,
    employee: Employee,
    *,
    structure_as_of: date,
    component_ids: dict[str, uuid.UUID],
) -> PayrollLine | None:
    """One employee's pay for the run, from their window of employment in the period.

    The window is T-5.EMP.03's `employment_in`, so a mid-period joiner is paid from their hire
    date and a leaver only up to the day before they left — the criteria's lifecycle movement
    is arithmetic here rather than an exception somewhere.
    """
    window = employment_in(
        session, employee, since=run.from_date, until=run.to_date + timedelta(days=1)
    )
    if window is None:
        return None
    starts, _first_day_after = window
    last = window[1] - timedelta(days=1)
    period_days = (run.to_date - run.from_date).days + 1
    closing = contract_in_force(employee, on=last)
    # No contract is no salary — and a line that says so, flagged, keeps the rest of the run
    # computable while stopping it being approved.
    basic_salary = (
        Decimal(0)
        if closing is None
        else _number(closing.basic_salary, f"the basic salary of {employee.number!r}")
    )

    counts = dict.fromkeys(INPUT_KINDS, 0)
    flags: list[str] = []
    if closing is None:
        flags.append(
            f"{employee.number!r} has no contract in force on {last}, so there is no salary to"
            " pay (T-5.EMP.01: record_contract)"
        )
    days: list[dict] = []
    day = starts
    while day <= last:
        # The rate is the day's own: a raise mid-period pays the days before it at the old
        # terms and the days after at the new ones.
        terms = contract_in_force(employee, on=day)
        terms_salary = (
            basic_salary if terms is None else _number(terms.basic_salary, "a basic salary")
        )
        rate = _money(terms_salary / Decimal(period_days))
        read = _day_input(session, employee, on=day, daily_rate=rate)
        counts[read["kind"]] += 1
        if read["flag"] is not None:
            flags.append(read["flag"])
        days.append(read)
        day += timedelta(days=1)

    # The reference schedule overtime is priced against: the last schedule the period states,
    # so a rest-day premium is priced at the same hourly rate as the rest of the month.
    reference = next(
        (row["scheduled"] for row in reversed(days) if row["scheduled"] > 0),
        0,
    )
    overtime_minutes = sum(row["overtime"] for row in days)
    if overtime_minutes and reference <= 0:
        flags.append(
            "overtime is recorded but no day of the period states a schedule to price it"
            " against (T-5.ATT.01)"
        )
    basic_pay = _money(sum(row["amount"] for row in days))

    figures: list[dict] = [
        {
            "code": BASIC_CODE,
            "name": "Basic pay",
            "kind": "earning",
            "basis": "attendance",
            "amount": basic_pay,
            "source": "contract",
            "order": 0,
            "taxable": True,
            "account_code": None,
            "component_id": None,
            "note": None,
        }
    ]
    gross = basic_pay
    if overtime_minutes and reference > 0:
        # Each day's overtime is priced at its own day rate and its own band's multiplier.
        overtime_pay = Decimal(0)
        for row in days:
            if not row["overtime"] or row["multiplier"] is None:
                continue
            overtime_pay += (
                row["rate"]
                * Decimal(row["overtime"])
                * row["multiplier"]
                / Decimal(reference)
            )
        overtime_pay = _money(overtime_pay)
        figures.append(
            {
                "code": OVERTIME_CODE,
                "name": "Overtime",
                "kind": "earning",
                "basis": "attendance",
                "amount": overtime_pay,
                "source": "attendance",
                "order": 1,
                "taxable": True,
                "account_code": None,
                "component_id": None,
                "note": None,
            }
        )
        gross += overtime_pay
    structure = structure_on(session, company_id=run.company_id, on=structure_as_of)
    # Earnings first and completely, then the deductions in the order the structure states:
    # nothing can be taken from pay that has not been worked out yet.
    for row in structure["earnings"]:
        amount = _component_amount(
            row, basic=basic_salary, gross=gross, worked_days=counts["worked"]
        )
        figures.append(
            {
                "code": row["code"],
                "name": row["name"],
                "kind": row["kind"],
                "basis": row["basis"],
                "amount": amount,
                "source": row["source"],
                "order": row["order"],
                "taxable": row["taxable"],
                "account_code": row["account_code"],
                "component_id": component_ids.get(row["code"]),
                "note": None,
            }
        )
        gross += amount
    gross = _money(gross)

    deductions = Decimal(0)
    contributions = Decimal(0)
    pre_tax = Decimal(0)
    for group in ("deductions", "employer_contributions"):
        for row in structure[group]:
            note = None
            if row["basis"] == "loan_schedule":
                amount, note, flagged = _loan_recovery(
                    session,
                    employee,
                    run=run,
                    on=structure_as_of,
                    available=_money(gross - deductions),
                    flags=flags,
                )
                if flagged is not None:
                    # A structural gap (no policy to recover within) leads the reasons: the
                    # flag is what stops the run being approved, and it should be the first
                    # thing read rather than the last of thirty days.
                    flags.insert(0, flagged)
            else:
                amount = _component_amount(
                    row, basic=basic_salary, gross=gross, worked_days=counts["worked"]
                )
            figures.append(
                {
                    "code": row["code"],
                    "name": row["name"],
                    "kind": row["kind"],
                    "basis": row["basis"],
                    "amount": amount,
                    "source": row["source"],
                    "order": row["order"],
                    "taxable": row["taxable"],
                    "account_code": row["account_code"],
                    "component_id": component_ids.get(row["code"]),
                    "note": note,
                }
            )
            if group == "deductions":
                deductions += amount
                if row["taxable"]:
                    pre_tax += amount
            else:
                contributions += amount

    incomplete = bool(flags)
    flag_reason = None
    if flags:
        more = "" if len(flags) <= 3 else f" | +{len(flags) - 3} more day(s) to answer for"
        flag_reason = " | ".join(flags[:3]) + more
    line = PayrollLine(
        run_id=run.id,
        company_id=run.company_id,
        employee_id=employee.id,
        contract_id=None if closing is None else closing.id,
        basic_salary=_money(basic_salary),
        daily_rate=_money(basic_salary / Decimal(period_days)),
        period_days=period_days,
        employed_days=(last - starts).days + 1,
        worked_days=counts["worked"],
        paid_leave_days=counts["paid_leave"],
        unpaid_leave_days=counts["unpaid_leave"],
        holiday_days=counts["holiday"],
        absent_days=counts["absent"],
        late_minutes=sum(row["late"] for row in days),
        overtime_minutes=overtime_minutes,
        reference_minutes=reference,
        basic_pay=basic_pay,
        gross=gross,
        taxable_gross=_money(gross - pre_tax),
        deductions_total=_money(deductions),
        employer_contributions_total=_money(contributions),
        net=_money(gross - _money(deductions)),
        incomplete=incomplete,
        flag_reason=flag_reason,
    )
    session.add(line)
    session.flush()
    for figure in figures:
        _add_component(session, line, **figure)
    for read in days:
        session.add(
            PayrollLineInput(
                line_id=line.id,
                company_id=run.company_id,
                employee_id=employee.id,
                on_date=read["on_date"],
                kind=read["kind"],
                worked_minutes=read["worked"],
                scheduled_minutes=read["scheduled"],
                late_minutes=read["late"],
                overtime_minutes=read["overtime"],
                multiplier=read["multiplier"],
                amount=read["amount"],
                source_type=read["source_type"],
                source_id=read["source_id"],
            )
        )
    session.flush()
    return line


def _loan_recovery(
    session: Session,
    employee: Employee,
    *,
    run: PayrollRun,
    on: date,
    available: Decimal,
    flags: list[str],
) -> tuple[Decimal, str | None, str | None]:
    """What this run recovers from the employee's loans, and what it says about it.

    A loan recovery is a schedule rather than a rate (T-5.PAY.03), and it is capped against
    what is left of the pay at this point — the pack's own schedule puts loan recovery after
    the statutory deductions. An employee who owes nothing carries nothing, and an employee who
    owes something a company has stated no policy for is **flagged** rather than quietly
    recovered from.
    """
    if not loans.loans_of(session, employee, settled=False):
        return Decimal(0), None, None
    try:
        recovered = loans.recover_for_period(
            session,
            employee,
            period=run.period,
            on=on,
            available=available,
            run_id=run.id,
        )
    except loans.NoPolicyError as refusal:
        return Decimal(0), str(refusal), f"{employee.number!r}: {refusal}"
    note = None
    if recovered["deferred"] > 0:
        note = (
            f"{recovered['deferred']} deferred by the recovery policy"
            f" ({recovered['policy'].max_recovery_percent}% of the remaining pay)"
        )
    return recovered["recovered"], note, None


def _component_amount(row: dict, *, basic: Decimal, gross: Decimal, worked_days: int) -> Decimal:
    """What one row of the structure comes to, by the basis its own row states.

    A `loan_schedule` row carries nothing here — the recovery is a schedule, and T-5.PAY.03
    states it. Nothing is guessed for it in the meantime, so a run without loans is unmoved by
    the row being present.
    """
    basis = row["basis"]
    if basis == "fixed":
        return _money(_number(row["amount"], f"the amount of {row['code']}"))
    if basis == "per_worked_day":
        return _money(_number(row["amount"], f"the amount of {row['code']}") * worked_days)
    if basis in ("percent_of_basic", "percent_of_gross"):
        rate = _number(row["rate_percent"], f"the rate of {row['code']}")
        base = basic if basis == "percent_of_basic" else gross
        return _money(base * rate / Decimal(100))
    return Decimal(0)


def compute_run(session: Session, run: PayrollRun, *, actor: str) -> PayrollRun:
    """Compute the period: one line per employee the period employed.

    Refused on an approved run — a closed period is not recomputed, it is corrected — and
    refused on a run that already has lines, because a line is history the moment it exists.
    Every employee of the company is read; an employee the period did not employ gets no line
    rather than a zero one, so the run's lines are the people it paid.
    """
    who = _text(actor, "who computed the run")
    if run.state == "approved":
        raise RunClosedError(
            f"{run.period} is approved; an approved run is not recomputed — correct it with"
            " correct_run(actor, reason) and a new revision"
        )
    if run.state != "draft":
        raise InvalidRunError(
            f"{run.period} is {run.state}; a run is computed once (revision {run.revision}) —"
            " a change to the records is a correction, which appends the next revision"
        )
    actors = list(
        session.scalars(
            select(Employee)
            .where(Employee.company_id == run.company_id)
            .order_by(Employee.number)
        )
    )
    structure = structure_on(session, company_id=run.company_id, on=run.structure_as_of)
    reserved = {
        code
        for code in (BASIC_CODE, OVERTIME_CODE)
        if any(
            row["code"] == code
            for group in ("earnings", "deductions", "employer_contributions")
            for row in structure[group]
        )
    }
    if reserved:
        raise InvalidRunError(
            f"the payroll structure states {', '.join(sorted(reserved))}, which payroll writes"
            " for itself from the records (T-5.PAY.01: restate the component under another"
            " code)"
        )
    run.pack_versions = ",".join(structure["pack_versions"]) or None
    # Which row of the structure stands behind each code, read once for the whole run.
    component_ids = {
        row.code: row.id
        for row in components_in_force(
            session, company_id=run.company_id, on=run.structure_as_of
        )
    }
    for employee in actors:
        _compute_line(
            session,
            run,
            employee,
            structure_as_of=run.structure_as_of,
            component_ids=component_ids,
        )
    run.state = "computed"
    run.computed_by = who
    run.computed_at = datetime.now(timezone.utc)
    session.flush()
    return run


def approve_run(session: Session, run: PayrollRun, *, actor: str) -> PayrollRun:
    """Approve a computed run — and refuse to while any line is incomplete.

    This is where "flagged rather than silently paid a default" is enforced: a line whose day
    could not be priced keeps the run at `computed` until somebody states the roster or the
    calendar, or the line is explained.
    """
    who = _text(actor, "who approved the run")
    if run.state != "computed":
        raise InvalidRunError(
            f"{run.period} is {run.state}; only a computed run is approved"
        )
    incomplete = [line for line in lines_of(session, run) if line.incomplete]
    if incomplete:
        first = incomplete[0]
        raise IncompleteAttendanceError(
            f"{run.period} has {len(incomplete)} line(s) with incomplete attendance, starting"
            f" with {first.employee.number}: {first.flag_reason}"
        )
    run.state = "approved"
    run.approved_by = who
    run.approved_at = datetime.now(timezone.utc)
    session.flush()
    return run


def correct_run(
    session: Session, run: PayrollRun, *, actor: str, reason: str
) -> PayrollRun:
    """Open the next **revision** of a computed or approved run, with who asked and why.

    The superseded revision is left exactly as it was — it is what was paid, and what a payslip
    or a report from that period must keep reproducing — and the new revision is computed from
    today's records. Both the reason and the actor are stored, and the audit trail records the
    act, because "recompute the payroll" without a name and a reason is how a figure nobody can
    explain gets paid.
    """
    who = _text(actor, "who asked for the correction")
    why = _text(reason, "why the run is being corrected")
    if run.state not in ("computed", "approved"):
        raise InvalidRunError(
            f"{run.period} is {run.state}; a draft with no lines needs no correction — compute"
            " it — and a completed run is the only thing there is to correct"
        )
    correction = PayrollRun(
        company_id=run.company_id,
        period=run.period,
        from_date=run.from_date,
        to_date=run.to_date,
        cutoff_date=run.cutoff_date,
        state="draft",
        revision=run.revision + 1,
        supersedes_id=run.id,
        structure_as_of=run.structure_as_of,
        correction_actor=who,
        correction_reason=why,
    )
    session.add(correction)
    session.flush()
    run.superseded_by_id = correction.id
    session.flush()
    return correction


def run_payload(session: Session, run: PayrollRun) -> dict:
    """The run as data: the period, the cutoff, what it was computed with, and its totals."""
    lines = lines_of(session, run)
    return {
        "id": str(run.id),
        "period": run.period,
        "from_date": run.from_date.isoformat(),
        "to_date": run.to_date.isoformat(),
        "cutoff_date": run.cutoff_date.isoformat(),
        "state": run.state,
        "revision": run.revision,
        "supersedes": None if run.supersedes_id is None else str(run.supersedes_id),
        "superseded_by": None if run.superseded_by_id is None else str(run.superseded_by_id),
        "structure_as_of": run.structure_as_of.isoformat(),
        "pack_versions": run.pack_versions,
        "computed_by": run.computed_by,
        "computed_at": None if run.computed_at is None else run.computed_at.isoformat(),
        "approved_by": run.approved_by,
        "approved_at": None if run.approved_at is None else run.approved_at.isoformat(),
        "correction_actor": run.correction_actor,
        "correction_reason": run.correction_reason,
        "employees": len(lines),
        "incomplete_lines": sum(1 for line in lines if line.incomplete),
        "totals": {
            "gross": str(_money(sum(line.gross for line in lines))),
            "deductions": str(_money(sum(line.deductions_total for line in lines))),
            "employer_contributions": str(
                _money(sum(line.employer_contributions_total for line in lines))
            ),
            "net": str(_money(sum(line.net for line in lines))),
        },
    }


def line_payload(session: Session, line: PayrollLine) -> dict:
    """One employee's line, with every figure's component and every day's source record."""
    return {
        "id": str(line.id),
        "employee": line.employee.number,
        "employee_name": line.employee.party.name,
        "contract": None if line.contract_id is None else str(line.contract_id),
        "basic_salary": str(line.basic_salary),
        "daily_rate": str(line.daily_rate),
        "period_days": line.period_days,
        "employed_days": line.employed_days,
        "worked_days": line.worked_days,
        "paid_leave_days": line.paid_leave_days,
        "unpaid_leave_days": line.unpaid_leave_days,
        "holiday_days": line.holiday_days,
        "absent_days": line.absent_days,
        "overtime_minutes": line.overtime_minutes,
        "late_minutes": line.late_minutes,
        "reference_minutes": line.reference_minutes,
        "basic_pay": str(line.basic_pay),
        "gross": str(line.gross),
        "taxable_gross": str(line.taxable_gross),
        "deductions_total": str(line.deductions_total),
        "employer_contributions_total": str(line.employer_contributions_total),
        "net": str(line.net),
        "incomplete": bool(line.incomplete),
        "flag_reason": line.flag_reason,
        "components": [
            {
                "code": row.code,
                "name": row.name,
                "kind": row.kind,
                "basis": row.basis,
                "amount": str(row.amount),
                "source": row.source,
                "order": row.order_no,
                "taxable": bool(row.taxable),
                "account_code": row.account_code,
                "note": row.note,
                "component_id": None if row.component_id is None else str(row.component_id),
            }
            for row in line_components(session, line)
        ],
        "days": [
            {
                "on": row.on_date.isoformat(),
                "kind": row.kind,
                "worked_minutes": row.worked_minutes,
                "scheduled_minutes": row.scheduled_minutes,
                "late_minutes": row.late_minutes,
                "overtime_minutes": row.overtime_minutes,
                "multiplier": None if row.multiplier is None else str(row.multiplier),
                "amount": str(row.amount),
                "source_type": row.source_type,
                "source_id": None if row.source_id is None else str(row.source_id),
            }
            for row in line_inputs(session, line)
        ],
    }
