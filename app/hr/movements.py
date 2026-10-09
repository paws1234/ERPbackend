"""T-5.EMP.03 — the employee lifecycle: joining, transfers, salary revisions and exit.

A movement is **a dated fact about a person, with a reason and an actor**, not an edit to
the employee row. That shape is what makes the two questions an HR record is actually asked
answerable: *what were somebody's terms in March* (the contract T-5.EMP.01 recorded, the
placement T-5.EMP.02 recorded) and *who did this and why* (this row). Nothing already
recorded changes: a movement **appends** — a transfer adds a placement, a salary revision
adds a contract — and the table itself is append-only (T-0.AUDIT.01), so history is the
history.

The four kinds are the four the ledger names for this task — *joining, transfers
(department/location/reporting line), promotion/salary revision and exit* — and each one
records what it changed. What the effects are:

* **joining** records the hire, on the date the employee was hired and no other, once.
* **transfer** appends a placement (T-5.EMP.02) carrying the department, cost centre,
  work location and manager **in force after the move** — so the structure of that date
  is the structure the movement created, and a value it does not state is carried forward
  rather than blanked.
* **promotion** appends a contract (T-5.EMP.01) at the new salary, so a mid-period
  revision pays the old rate for the days before it and the new one after — the correct
  portion of the period, read from the terms in force on each date.
* **exit** ends employment. Nothing further may be recorded for the employee, and from the
  exit date onwards they are **not active**: :func:`active_on` is what the leave accrual
  (T-5.LEAVE.02) stops on and what a payroll run (T-5.PAY.02) reads before it includes
  anybody, and :func:`employment_in` is the portion of a period they were employed for.

What is deliberately *not* here: the payroll arithmetic itself (T-5.PAY.02) and the leave
balances (T-5.LEAVE.02) — this module answers **who was employed, from when to when, and
on what terms**, which is what those two consume.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Numeric,
    String,
    Uuid,
    select,
    text,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.audit import append_only
from app.db import Base
from app.hr.employees import (
    Employee,
    EmploymentContract,
    contract_in_force,
    latest_contract,
    record_contract,
)
from app.hr.org import place_employee, placement_in_force

# The kinds of movement §2.6's lifecycle names: joining, a transfer, a promotion (or any
# salary revision) and an exit. A kind outside this list is refused rather than stored, so
# a typo cannot become a fifth lifecycle step nobody reports on.
MOVEMENT_KINDS = ("joining", "transfer", "promotion", "exit")

# What each kind records. A kind that recorded an effect it does not own would claim a
# change the structure does not show — a promotion stating a manager while placing nobody
# reads as a reorganisation that never happened — so each kind names its own effects and
# anything else is a different movement.
EFFECTS_BY_KIND = {
    "joining": (),
    "transfer": ("department", "cost_centre", "location", "manager"),
    "promotion": ("basic_salary", "contract_type"),
    "exit": (),
}

MONEY = Numeric(20, 6)


class MovementError(ValueError):
    """The lifecycle refused what was asked of it."""


class InvalidMovementError(MovementError):
    """An unknown kind, a missing reason or actor, or effects a kind does not take."""


class MovementSequenceError(MovementError):
    """The movement does not fit the employee's timeline — another exit, or an exit already recorded."""


class EmployeeMovement(Base):
    """One dated lifecycle fact, with who recorded it and why.

    The effect columns state **what this movement changed** — a transfer's new department,
    a promotion's new salary — and are null where the movement left that alone. They are
    not a second copy of the current state: what is in force on a date is read from the
    contract and the placement that date falls in.
    """

    __tablename__ = "employee_movement"
    __table_args__ = (
        CheckConstraint(
            "kind IN (" + ", ".join(f"'{kind}'" for kind in MOVEMENT_KINDS) + ")",
            name="ck_employee_movement_kind",
        ),
        # A movement nobody can attribute is not a record: both are required, and stated.
        CheckConstraint("char_length(reason) > 0", name="ck_employee_movement_reason"),
        CheckConstraint("char_length(actor) > 0", name="ck_employee_movement_actor"),
        # Somebody joins once and leaves once, enforced by the database rather than by
        # hoping two writers agree — the same shape as the customer's primary contact.
        Index(
            "uq_employee_movement_joining",
            "employee_id",
            unique=True,
            postgresql_where=text("kind = 'joining'"),
        ),
        Index(
            "uq_employee_movement_exit",
            "employee_id",
            unique=True,
            postgresql_where=text("kind = 'exit'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    # Who made the change (the person, not the session) and why, in their words. Both are
    # required: "transferred" with no reason is exactly the row an audit cannot use.
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)

    # What this movement changed, stated only where it changed it.
    department: Mapped[str | None] = mapped_column(String(64))
    cost_centre: Mapped[str | None] = mapped_column(String(64))
    # The work location. Free text: the plan names a transfer of location but no work-site
    # master (the `location` table is the warehouse hierarchy, T-1.INV.02, and belongs to
    # stock).
    # ponytail: no work-site master. Ceiling: a typo in a location is stored, and two
    # spellings are two sites as far as a report is concerned. Upgrade path: when a market's
    # pack or a company master names work sites, this column becomes a reference to it.
    location: Mapped[str | None] = mapped_column(String(64))
    manager_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("employee.id"))
    basic_salary: Mapped[Decimal | None] = mapped_column(MONEY)
    contract_type: Mapped[str | None] = mapped_column(String(32))

    employee: Mapped[Employee] = relationship(foreign_keys=[employee_id])
    manager: Mapped[Employee | None] = relationship(foreign_keys=[manager_id])


# A movement is a record of something that happened: appended, never rewritten.
append_only(EmployeeMovement.__table__)


def _required(value: Any, what: str) -> str:
    stated = "" if value is None else str(value).strip()
    if not stated:
        raise InvalidMovementError(f"{what} is required")
    return stated


def _date_or_refuse(value: Any, what: str) -> date:
    """A calendar date from a date, a datetime or the ISO string the boundary carries."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError as exc:
            raise InvalidMovementError(f"not {what}: {value!r}") from exc
    raise InvalidMovementError(f"{what} is a date, not {value!r}")


def _grouping(value: Any, what: str) -> str | None:
    """A department, cost centre or location as stated, or nothing — empty is neither."""
    return None if value is None else _required(value, what)


def movements_of(session: Session, employee: Employee) -> list[EmployeeMovement]:
    """One employee's movements, oldest first — the life as recorded."""
    return list(
        session.scalars(
            select(EmployeeMovement)
            .where(
                EmployeeMovement.company_id == employee.company_id,
                EmployeeMovement.employee_id == employee.id,
            )
            .order_by(EmployeeMovement.effective_date, EmployeeMovement.kind)
        )
    )


def exit_of(session: Session, employee: Employee) -> date | None:
    """The date employment ended, or ``None`` while the employee is still here."""
    return next(
        (
            movement.effective_date
            for movement in movements_of(session, employee)
            if movement.kind == "exit"
        ),
        None,
    )


def active_on(session: Session, employee: Employee, *, on: date) -> bool:
    """Whether the employee was employed on `on` — what leave accrual and payroll read.

    Half-open at the exit: the day somebody leaves is a day they are no longer employed,
    so a month's accrual stops the day before it and a payroll run for the period pays the
    days up to it. The date is stated rather than read from the clock, because a past
    period must answer the same today as it did then.
    """
    left = exit_of(session, employee)
    if on < employee.hire_date:
        return False
    return left is None or on < left


def employment_in(
    session: Session, employee: Employee, *, since: date, until: date
) -> tuple[date, date] | None:
    """The half-open window inside `[since, until)` the employee was employed, or ``None``.

    The portion of a period payroll may pay for, and the reason a mid-period joiner is not
    paid a full period and a mid-period leaver is not paid for days they were gone: the
    window is clipped by the hire date at the front and the exit date at the back, and the
    caller counts days from what is returned rather than from the period it asked about.
    """
    if until <= since:
        raise InvalidMovementError(f"a period ends after it starts: {since}…{until}")
    starts = max(since, employee.hire_date)
    left = exit_of(session, employee)
    ends = until if left is None else min(until, left)
    return None if ends <= starts else (starts, ends)


def record_movement(
    session: Session,
    employee: Employee,
    *,
    kind: str,
    effective_date: Any,
    reason: str,
    actor: str,
    department: str | None = None,
    cost_centre: str | None = None,
    location: str | None = None,
    manager: Employee | None = None,
    basic_salary: Any = None,
    contract_type: str | None = None,
) -> EmployeeMovement:
    """Record what happened to the employee on `effective_date`, and the effect it has.

    The movement row is written with its effects in **one transaction**, so a transfer
    whose placement is refused leaves no movement behind claiming it happened. What each
    kind takes is stated rather than inferred: a joining carries no effects (the terms
    belong to a contract, T-5.EMP.01), an exit carries none, a transfer must change
    something and a promotion must state the salary it revises to. Anything else is refused
    by name — a movement that changes nothing is not a movement.
    """
    wanted = _required(kind, "a movement kind").lower()
    if wanted not in MOVEMENT_KINDS:
        raise InvalidMovementError(
            f"unknown movement {kind!r}; the lifecycle is {', '.join(MOVEMENT_KINDS)}"
        )
    when = _date_or_refuse(effective_date, "an effective date")
    if when < employee.hire_date:
        raise MovementSequenceError(
            f"a movement cannot be dated before the employee was hired: {when} is before"
            f" {employee.hire_date}"
        )
    what = _required(reason, "a reason")
    who = _required(actor, "the actor recording the movement")

    left = exit_of(session, employee)
    if left is not None and wanted != "exit" and when >= left:
        raise MovementSequenceError(
            f"employment ended on {left}; nothing can be recorded from that date onwards"
            " — a rehire is a new employment, not a movement on this one"
        )
    stated = {
        "department": _grouping(department, "a department"),
        "cost_centre": _grouping(cost_centre, "a cost centre"),
        "location": _grouping(location, "a location"),
        "manager": manager,
        "basic_salary": basic_salary,
        "contract_type": _grouping(contract_type, "a contract type"),
    }
    effects = {name: value for name, value in stated.items() if value is not None}

    allowed = EFFECTS_BY_KIND[wanted]
    wrong = sorted(name for name in effects if name not in allowed)
    if wrong:
        raise InvalidMovementError(
            f"a {wanted} does not record {', '.join(wrong)}: it records"
            + (f" {', '.join(allowed)}" if allowed else " the fact and nothing else")
            + " — what a kind does not own is a different movement"
        )
    if wanted == "joining":
        if when != employee.hire_date:
            raise MovementSequenceError(
                f"the employee was hired on {employee.hire_date}; a joining movement is that"
                f" date or it is not a joining ({when} stated)"
            )
        if any(movement.kind == "joining" for movement in movements_of(session, employee)):
            raise MovementSequenceError(
                f"{employee.number!r} has already been recorded as joining"
            )
    if wanted == "exit":
        if left is not None:
            raise MovementSequenceError(
                f"employment already ended on {left}; an exit happens once"
            )
    if wanted == "transfer" and not effects:
        raise InvalidMovementError(
            "a transfer has to change something: state a department, a cost centre, a"
            " location or a manager"
        )
    if wanted == "promotion" and basic_salary is None:
        raise InvalidMovementError(
            "a promotion states the salary it revises to; a title change with no new terms"
            " is not recorded here"
        )

    movement = EmployeeMovement(
        company_id=employee.company_id,
        employee_id=employee.id,
        kind=wanted,
        effective_date=when,
        actor=who,
        reason=what,
        department=stated["department"],
        cost_centre=stated["cost_centre"],
        location=stated["location"],
        manager_id=None if manager is None else manager.id,
        # The salary is left to the contract that carries it: `record_contract` is what
        # validates it as an exact decimal (DOMAIN-MODELS §2), and the movement records the
        # value that was actually recorded rather than a second reading of the argument.
        basic_salary=None,
        contract_type=stated["contract_type"],
    )

    if wanted == "transfer":
        _transfer(session, employee, movement=movement, when=when, stated=stated)
    elif wanted == "promotion":
        movement.basic_salary = _revise_terms(
            session, employee, when=when, stated=stated, actor=who
        ).basic_salary

    session.add(movement)
    session.flush()
    return movement


def _transfer(
    session: Session,
    employee: Employee,
    *,
    movement: EmployeeMovement,
    when: date,
    stated: dict,
) -> None:
    """Append the placement a transfer puts the employee in, carrying forward what it keeps.

    A placement states the whole position, so a value the movement does not change is taken
    from the day before rather than blanked — a transfer to another department must not
    make it look as though nobody manages the employee. The manager is read from the day
    before the movement because the placement that applied then is the one these terms
    replace.
    """
    before = placement_in_force(session, employee, on=when - timedelta(days=1))
    if before is not None and before.manager_id is not None and before.manager is None:
        raise MovementError(
            f"{employee.number!r} was placed under somebody who is no longer an employee;"
            " state who they report to now rather than carrying the manager forward"
        )
    manager = stated["manager"]
    if manager is None and before is not None:
        manager = before.manager
    if manager is None and before is None:
        raise MovementError(
            f"{employee.number!r} has no reporting line yet, so a transfer cannot infer one:"
            " state the manager they report to (T-5.EMP.02)"
        )
    place_employee(
        session,
        employee,
        effective_from=when,
        manager=manager,
        department=stated["department"] or (None if before is None else before.department),
        cost_centre=stated["cost_centre"] or (None if before is None else before.cost_centre),
        location=stated["location"] or (None if before is None else before.location),
    )


def _revise_terms(
    session: Session,
    employee: Employee,
    *,
    when: date,
    stated: dict,
    actor: str,
) -> EmploymentContract:
    """Append the contract a promotion puts in force, carrying forward what it does not change.

    The contract in force the day before is what is being revised: its currency and, unless
    the movement states otherwise, its kind of engagement. The actor recording the movement
    is the **subject** of the write, so a salary a role may not write is refused here rather
    than found out in a payroll run (T-0.SEC.01).
    """
    before = contract_in_force(employee, on=when - timedelta(days=1)) or latest_contract(employee)
    if before is None:
        raise MovementError(
            f"{employee.number!r} has no contract to revise; record the terms of employment"
            " first (T-5.EMP.01)"
        )
    return record_contract(
        session,
        employee,
        subject=actor,
        effective_from=when,
        basic_salary=stated["basic_salary"],
        contract_type=stated["contract_type"] or before.contract_type,
        transaction_currency=before.transaction_currency,
    )
