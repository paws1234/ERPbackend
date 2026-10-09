"""T-5.LEAVE.02 — leave types, the entries they accrue, and the balance those entries make.

The balance is **not a column**: it is the sum of the entries an employee has, read for a
date. That is the whole design — a stored balance is a second copy of the truth that drifts
the first time a job half-runs, and the criterion says so ("a balance is always derivable
from accruals plus approved leave").

Three rules this module is built around:

* **An accrual is idempotent per period.** Every entry names the **period** it belongs to, and
  one employee, one type and one period is one accrual — enforced by the database, not by
  hoping a job runs once. Re-running a cycle therefore adds nothing, which is what makes a
  scheduled job safe to retry.
* **A carry-forward is capped, and its expiry is recorded.** At the year boundary the balance
  is carried into the new year up to the type's cap, as an entry that **states the date it
  expires** (the caller states it: the ledger names a carry-forward cap and no expiry policy,
  so none is invented). An expired entry stops counting for a date after it.
* **Paid and unpaid are different facts.** A type is `paid` or it is not, and payroll reads
  that rather than guessing from a name — an unpaid leave type is what makes the difference
  between a day paid and a day not, which is T-5.PAY.02's to consume and this module's to
  state.

What is deliberately *not* here: applying for leave, the approval chain and the effect of an
approval on attendance (T-5.LEAVE.03, which posts its own entries through
:func:`record_entry`), and every monetary consequence (payroll).
"""

from __future__ import annotations

import calendar
import re
import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    select,
    text,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.audit import SoftDeleteMixin, append_only, deny_hard_delete
from app.db import Base
from app.hr.employees import Employee
from app.hr.movements import active_on

# The cadences the ledger names for an accrual rule (`Monthly | Annual | Per-cycle`), as the
# two this module can post for plus the one it reports but does not schedule: a "per-cycle"
# type accrues with whatever cycle the caller states, which is what the period key already
# allows.
ACCRUAL_CADENCES = ("monthly", "annual", "per_cycle")

# The kinds of entry a balance is made of. An accrual is earned, a carry-forward is brought
# into a year (with an expiry), leave taken is what LEAVE.03 posts, and an adjustment is the
# deliberate correction — all of them rows, so the balance is a sum and nothing else.
ENTRY_KINDS = ("accrual", "carry_forward", "leave_taken", "adjustment")

# Days, at four decimal places: half days and quarter days are real, and a balance must add
# up exactly (DOMAIN-MODELS §2's rule, applied to days rather than money).
DAYS = Numeric(10, 4)

_MONTH = re.compile(r"^\d{4}-\d{2}$")
_YEAR = re.compile(r"^\d{4}$")


class LeaveError(ValueError):
    """Leave management refused what was asked of it."""


class InvalidLeaveError(LeaveError):
    """A cadence, a period, a date or an amount failed validation at entry."""


class DuplicateLeaveTypeError(LeaveError):
    """That leave type code is already used in this company."""


class UnknownLeaveTypeError(LeaveError):
    """A lookup named a leave type this company does not have."""


class LeaveType(SoftDeleteMixin, Base):
    """One kind of leave, and how it accrues."""

    __tablename__ = "leave_type"
    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_leave_type_company_code"),
        CheckConstraint(
            "cadence IN (" + ", ".join(f"'{c}'" for c in ACCRUAL_CADENCES) + ")",
            name="ck_leave_type_cadence",
        ),
        CheckConstraint("accrual_days >= 0", name="ck_leave_type_accrual"),
        CheckConstraint(
            "carry_forward_cap IS NULL OR carry_forward_cap >= 0",
            name="ck_leave_type_carry_forward",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    # Whether the days are paid. Stated per type and never inferred from the name: payroll
    # (T-5.PAY.02) distinguishes an unpaid day from a paid one, and a type called "unpaid" is
    # not evidence of anything.
    paid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    cadence: Mapped[str] = mapped_column(String(16), nullable=False)
    # What one cadence unit earns. 1.25 days a month is a real rule and must add up exactly,
    # so it is an exact decimal like every other amount in the platform.
    accrual_days: Mapped[Decimal] = mapped_column(DAYS, nullable=False, default=Decimal(0))
    # How much of a year's unused balance may be brought forward. Null is **uncapped**, which
    # is a different answer from 0 (nothing may be carried).
    carry_forward_cap: Mapped[Decimal | None] = mapped_column(DAYS)


class LeaveEntry(Base):
    """One movement of a balance: earned, carried, taken or adjusted.

    Append-only by construction: nothing edits an entry, so a balance is the sum of what was
    recorded and never a figure somebody typed. `period` is what makes an accrual idempotent
    — one employee, one type, one kind, one period is one row, enforced by the database.
    """

    __tablename__ = "leave_entry"
    __table_args__ = (
        CheckConstraint(
            "kind IN (" + ", ".join(f"'{kind}'" for kind in ENTRY_KINDS) + ")",
            name="ck_leave_entry_kind",
        ),
        # A balance is a sum of movements: a period-scoped movement happens once, whatever
        # re-runs the job that produced it. Ad-hoc movements (leave taken, an adjustment)
        # carry no period and are deliberately not covered by this index.
        Index(
            "uq_leave_entry_period",
            "employee_id",
            "leave_type_id",
            "kind",
            "period",
            unique=True,
            postgresql_where=text("period IS NOT NULL"),
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
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # Signed: earned and carried are positive, leave taken is negative, an adjustment either.
    days: Mapped[Decimal] = mapped_column(DAYS, nullable=False)
    on_date: Mapped[date] = mapped_column(Date, nullable=False)
    # The period this movement belongs to, for the idempotence above — `2026-06` for a monthly
    # accrual, `2026` for an annual one, null for a movement that is not periodic.
    period: Mapped[str | None] = mapped_column(String(9))
    # When the days stop counting. Stated on a carry-forward (the ledger names a cap and no
    # expiry policy, so the policy is somebody's to state) and null when they never lapse.
    expires_on: Mapped[date | None] = mapped_column(Date)
    # What caused it — the request that was approved (T-5.LEAVE.03) or the job that accrued.
    source: Mapped[str | None] = mapped_column(String(64))

    employee: Mapped[Employee] = relationship()
    leave_type: Mapped[LeaveType] = relationship()


# A type is a master: retired by marking (T-0.AUDIT.01). An entry is a movement: appended,
# never rewritten — a balance must be re-derivable for any date that was ever reported.
deny_hard_delete(LeaveType.__table__)
append_only(LeaveEntry.__table__)


def _required(value: Any, what: str) -> str:
    stated = "" if value is None else str(value).strip()
    if not stated:
        raise InvalidLeaveError(f"{what} is required")
    return stated


def _date_or_refuse(value: Any, what: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError as exc:
            raise InvalidLeaveError(f"not {what}: {value!r}") from exc
    raise InvalidLeaveError(f"{what} is a date, not {value!r}")


def _days(value: Any, what: str) -> Decimal:
    """A number of days as an exact decimal — a float would not add up."""
    if isinstance(value, float):
        raise InvalidLeaveError(
            f"{what} is an exact decimal or a string, not the float {value!r}"
        )
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, AttributeError, ValueError) as exc:
        raise InvalidLeaveError(f"not {what}: {value!r}") from exc
    if not amount.is_finite():
        raise InvalidLeaveError(f"{what} is a finite number of days, not {amount}")
    return amount


def _period_end(period: str) -> date:
    """The last day of a period key — the date an accrual for it is posted on.

    Derived rather than read from a clock: an accrual for `2026-06` is dated `2026-06-30`
    whether it is posted in June or re-run in December, which is what makes a late re-run
    land on the day the period ended rather than on the day somebody noticed.
    """
    if _MONTH.match(period):
        year, month = (int(part) for part in period.split("-"))
        if not 1 <= month <= 12:
            raise InvalidLeaveError(f"not a period: {period!r} (a month is 01–12)")
        return date(year, month, calendar.monthrange(year, month)[1])
    if _YEAR.match(period):
        return date(int(period), 12, 31)
    raise InvalidLeaveError(
        f"not a period: {period!r}; a calendar period is `YYYY-MM` (monthly) or `YYYY` (annual)"
    )


def define_leave_type(
    session: Session,
    *,
    company_id: uuid.UUID,
    code: str,
    name: str,
    cadence: str,
    accrual_days: Any = 0,
    paid: bool = True,
    carry_forward_cap: Any = None,
) -> LeaveType:
    """Define a leave type and its accrual rule."""
    stated = _required(code, "a leave type code")
    if session.scalar(
        select(LeaveType).where(LeaveType.company_id == company_id, LeaveType.code == stated)
    ) is not None:
        raise DuplicateLeaveTypeError(f"leave type {stated!r} already exists in this company")
    wanted = _required(cadence, "an accrual cadence").lower().replace("-", "_")
    if wanted not in ACCRUAL_CADENCES:
        raise InvalidLeaveError(
            f"unknown accrual cadence {cadence!r}; a type accrues"
            f" {', '.join(ACCRUAL_CADENCES)}"
        )
    accrual = _days(accrual_days, "an accrual")
    if accrual < 0:
        raise InvalidLeaveError(f"an accrual is not negative: {accrual}")
    cap = None if carry_forward_cap is None else _days(carry_forward_cap, "a carry-forward cap")
    if cap is not None and cap < 0:
        raise InvalidLeaveError(
            f"a carry-forward cap is not negative: {cap}; null means uncapped, 0 means nothing"
        )
    leave_type = LeaveType(
        company_id=company_id,
        code=stated,
        name=_required(name, "a leave type name"),
        paid=bool(paid),
        cadence=wanted,
        accrual_days=accrual,
        carry_forward_cap=cap,
    )
    session.add(leave_type)
    session.flush()
    return leave_type


def leave_type_by_code(
    session: Session, *, company_id: uuid.UUID, code: str
) -> LeaveType:
    """The live leave type a request names by its code, or a refusal."""
    leave_type = session.scalar(
        select(LeaveType).where(
            LeaveType.company_id == company_id, LeaveType.code == str(code).strip()
        )
    )
    if leave_type is None:
        raise UnknownLeaveTypeError(
            f"no leave type {code!r} in this company; define it first (T-5.LEAVE.02)"
        )
    return leave_type


def entries_of(
    session: Session, employee: Employee, *, leave_type: LeaveType
) -> list[LeaveEntry]:
    """One employee's movements for one type, oldest first — the balance's own record."""
    return list(
        session.scalars(
            select(LeaveEntry)
            .where(
                LeaveEntry.company_id == employee.company_id,
                LeaveEntry.employee_id == employee.id,
                LeaveEntry.leave_type_id == leave_type.id,
            )
            .order_by(LeaveEntry.on_date, LeaveEntry.kind)
        )
    )


def balance(
    session: Session,
    employee: Employee,
    *,
    leave_type: LeaveType,
    on: date,
) -> Decimal:
    """The balance **derived** from the entries, as of `on` — never a stored figure.

    Only what has happened by `on` counts, and an entry that had expired by then counts for
    nothing: a balance is a reading of the record, so a past date answers as it did then.
    """
    total = Decimal(0)
    for entry in entries_of(session, employee, leave_type=leave_type):
        if entry.on_date > on:
            continue
        if entry.expires_on is not None and entry.expires_on < on:
            continue
        total += Decimal(entry.days)
    return total


def balances(session: Session, employee: Employee, *, on: date) -> dict[str, Decimal]:
    """Every type the employee has entries for, with the balance of each on `on`."""
    rows = list(
        session.scalars(
            select(LeaveType).where(LeaveType.company_id == employee.company_id)
        )
    )
    return {
        leave_type.code: balance(session, employee, leave_type=leave_type, on=on)
        for leave_type in sorted(rows, key=lambda row: row.code)
    }


def record_entry(
    session: Session,
    employee: Employee,
    *,
    leave_type: LeaveType,
    kind: str,
    days: Any,
    on: date,
    period: str | None = None,
    expires_on: Any = None,
    source: str | None = None,
) -> LeaveEntry:
    """Record one movement of a balance — the only way a balance ever changes.

    Refused when the type belongs to another company, when the day is outside the employee's
    employment, and when the movement is already recorded for its period (**idempotence**: a
    re-run of the job that produced it adds nothing rather than doubling the entitlement).
    """
    wanted = _required(kind, "an entry kind").lower()
    if wanted not in ENTRY_KINDS:
        raise InvalidLeaveError(
            f"unknown entry kind {kind!r}; a balance moves by {', '.join(ENTRY_KINDS)}"
        )
    if leave_type.company_id != employee.company_id:
        raise InvalidLeaveError(
            f"leave type {leave_type.code!r} belongs to another company; a balance never"
            " crosses one"
        )
    if not active_on(session, employee, on=on):
        raise InvalidLeaveError(
            f"{employee.number!r} was not employed on {on}, so nothing accrues or is taken for"
            " that day (T-5.EMP.03)"
        )
    amount = _days(days, "an amount of days")
    if wanted in ("accrual", "carry_forward") and amount < 0:
        raise InvalidLeaveError(f"a {wanted} is not negative: {amount}")
    if wanted == "leave_taken" and amount > 0:
        raise InvalidLeaveError(
            f"leave taken is recorded as a negative movement, not {amount}; the balance is a sum"
        )
    if period is not None:
        _period_end(period)
        existing = session.scalar(
            select(LeaveEntry).where(
                LeaveEntry.employee_id == employee.id,
                LeaveEntry.leave_type_id == leave_type.id,
                LeaveEntry.kind == wanted,
                LeaveEntry.period == period,
            )
        )
        if existing is not None:
            raise InvalidLeaveError(
                f"{employee.number!r} already has a {wanted} of {existing.days} days for"
                f" {period}: a period is accrued once, so re-running the job adds nothing"
            )
    entry = LeaveEntry(
        company_id=employee.company_id,
        employee_id=employee.id,
        leave_type_id=leave_type.id,
        kind=wanted,
        days=amount,
        on_date=on,
        period=period,
        expires_on=None if expires_on is None else _date_or_refuse(expires_on, "an expiry date"),
        source=source,
    )
    session.add(entry)
    session.flush()
    return entry


def accrue_period(session: Session, *, company_id: uuid.UUID, period: str) -> list[LeaveEntry]:
    """Accrue one period for every type and every employee whose cadence it matches.

    Monthly types accrue on a `YYYY-MM` period, annual ones on `YYYY`; a type whose cadence
    the period does not match is simply not this period's. Only employees **active** on the
    day the period ends accrue — somebody who has left stops accruing (T-5.EMP.03's exit is
    what that reads from). Re-running it adds nothing, because the entry names its period.
    """
    ends = _period_end(period)
    monthly = _MONTH.match(period) is not None
    types = list(
        session.scalars(select(LeaveType).where(LeaveType.company_id == company_id))
    )
    employees = list(
        session.scalars(select(Employee).where(Employee.company_id == company_id))
    )
    accrued: list[LeaveEntry] = []
    for leave_type in sorted(types, key=lambda row: row.code):
        if leave_type.accrual_days <= 0:
            continue
        if (leave_type.cadence == "monthly") != monthly and leave_type.cadence != "per_cycle":
            continue
        for employee in sorted(employees, key=lambda row: row.number):
            if not active_on(session, employee, on=ends):
                continue
            existing = session.scalar(
                select(LeaveEntry).where(
                    LeaveEntry.employee_id == employee.id,
                    LeaveEntry.leave_type_id == leave_type.id,
                    LeaveEntry.kind == "accrual",
                    LeaveEntry.period == period,
                )
            )
            if existing is not None:
                continue
            accrued.append(
                record_entry(
                    session,
                    employee,
                    leave_type=leave_type,
                    kind="accrual",
                    days=leave_type.accrual_days,
                    on=ends,
                    period=period,
                    source=f"accrual {period}",
                )
            )
    session.flush()
    return accrued


def carry_forward(
    session: Session,
    *,
    company_id: uuid.UUID,
    year: int,
    expires_on: Any = None,
) -> list[LeaveEntry]:
    """Close `year` and bring what may be carried into the next, with its expiry recorded.

    At the boundary the year's remaining balance **lapses** and what the type's cap allows is
    carried into the new year as its own entry — two movements rather than one, because that
    is what actually happened and because a balance is a sum: without the lapse the old year's
    accruals would keep counting for ever, and "the cap was enforced" would be a claim the
    record did not support. The cap is the type's own (`carry_forward_cap`); **null means
    uncapped** and 0 means nothing is carried, which is a rule rather than an absence of one.
    The carried days state the date they expire (the caller states it — the ledger names a cap
    and no expiry policy, so none is invented), and an expired entry stops counting.

    Idempotent like an accrual: the lapse is keyed to the year and the carry to the year it
    enters, so re-running the year end adds nothing.
    """
    boundary = date(int(year), 12, 31)
    carried_on = date(int(year) + 1, 1, 1)
    stated_expiry = None
    if expires_on is not None:
        stated_expiry = _date_or_refuse(expires_on, "an expiry date")
        if stated_expiry < carried_on:
            raise InvalidLeaveError(
                f"days carried into {carried_on.year} cannot expire on {stated_expiry}, which is"
                " before they were carried"
            )
    types = list(session.scalars(select(LeaveType).where(LeaveType.company_id == company_id)))
    employees = list(session.scalars(select(Employee).where(Employee.company_id == company_id)))
    moved: list[LeaveEntry] = []
    for leave_type in sorted(types, key=lambda row: row.code):
        for employee in sorted(employees, key=lambda row: row.number):
            if not active_on(session, employee, on=boundary):
                # Somebody who leaves during the year is settled by the exit, not carried.
                continue
            if _year_settled(session, employee, leave_type=leave_type, year=year):
                continue
            remaining = balance(session, employee, leave_type=leave_type, on=boundary)
            if remaining <= 0:
                continue
            cap = leave_type.carry_forward_cap
            carried = remaining if cap is None else min(remaining, Decimal(cap))
            moved.append(
                record_entry(
                    session,
                    employee,
                    leave_type=leave_type,
                    kind="adjustment",
                    days=-remaining,
                    on=boundary,
                    period=str(year),
                    source=f"year-end lapse {year}",
                )
            )
            if carried > 0:
                moved.append(
                    record_entry(
                        session,
                        employee,
                        leave_type=leave_type,
                        kind="carry_forward",
                        days=carried,
                        on=carried_on,
                        period=str(carried_on.year),
                        expires_on=stated_expiry,
                        source=f"carried from {year}"
                        + ("" if cap is None else f" (cap {cap})"),
                    )
                )
    session.flush()
    return moved


def _year_settled(
    session: Session, employee: Employee, *, leave_type: LeaveType, year: int
) -> bool:
    """Whether `year` has already been closed for this employee and type."""
    return (
        session.scalar(
            select(LeaveEntry).where(
                LeaveEntry.employee_id == employee.id,
                LeaveEntry.leave_type_id == leave_type.id,
                LeaveEntry.kind == "adjustment",
                LeaveEntry.period == str(year),
            )
        )
        is not None
    )
