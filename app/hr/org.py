"""T-5.EMP.02 — the reporting hierarchy over employees, and the chart that shows it.

The hierarchy is **history, not a column**. "Who did this person report to in March" is
a question a payroll cut-off, a leave approval and a dispute all ask, so a placement is a
row dated from the day it took effect (:class:`OrgPlacement`) rather than a
``manager_id`` on the employee that the next reorganisation overwrites. The table is
**append-only** (:func:`app.audit.append_only`) — moving somebody is a new row that
supersedes nothing and changes nothing — and a placement's end is *derived* from the next
one, exactly as T-5.EMP.01 derives a contract's window.

Three rules this module is built around:

* **It is a tree, and the structure is checked at entry.** A placement may not name the
  employee themselves, nor anybody below them in the structure the placement would
  create — otherwise the chart has a cycle and no root. The check walks the manager's own
  chain rather than trusting the caller (:func:`place_employee`), and the cycle it refuses
  is the same one the chart could not draw.
* **One root per company.** The criterion is a tree, so exactly one employee tops it: the
  one whose placement names no manager. A second employee asking to top the tree is
  refused by name — while an employee with **no placement at all** is simply not in the
  tree yet, which is a different state and is reported as *unplaced* rather than quietly
  drawn as a second root.
* **The chart is the structure, computed once.** :func:`org_chart` reads every placement
  of the company in one query and answers the whole question — the root, each person's
  depth and direct reports, the department and cost centre they sit in, and who is
  unplaced — so the chart and any other reader cannot disagree about who reports to whom.

What is deliberately *not* here: the employment record itself and its contracts
(T-5.EMP.01), lifecycle movements such as a transfer or an exit (T-5.EMP.03, which
records the *movement* — this module provides the dated placement it moves), and leave
approval (T-5.LEAVE.03), which may consume the chain where it is configured to without
this module knowing about it.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import CheckConstraint, Date, ForeignKey, String, UniqueConstraint, Uuid, select
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.audit import append_only
from app.db import Base
from app.hr.employees import Employee

# The entity the API's field restrictions are stated under when the chart is served.
CHART_ENTITY = "employee"


class OrgError(ValueError):
    """The reporting hierarchy refused what was asked of it."""


class InvalidPlacementError(OrgError):
    """A placement named something that is not a date, a department or a cost centre."""


class PlacementSequenceError(OrgError):
    """The placement would not follow the ones already recorded."""


class ReportingCycleError(OrgError):
    """The placement would put somebody inside their own chain of command."""


class SecondRootError(OrgError):
    """A second employee was asked to top the tree; the structure has one root."""


class UnknownManagerError(OrgError):
    """The manager named is not an employee of this company."""


class OrgPlacement(Base):
    """Where an employee sits from one date: who they report to, and in which group.

    Append-only history (T-0.AUDIT.01): a reorganisation is a new row, and the row it
    follows is never touched. ``manager_id`` is null for the one employee who tops the
    tree; ``department`` and ``cost_centre`` are free text because the plan names the
    grouping but not the vocabulary for it (`departments/cost centres` are "not named in
    the plan beyond the hierarchy"), and a CHECK here would be this module inventing a
    company's org units.
    """

    __tablename__ = "org_placement"
    __table_args__ = (
        # Two placements cannot claim the same start for one employee: the history would
        # be ambiguous about who they reported to on that day.
        UniqueConstraint("employee_id", "effective_from", name="uq_org_placement_start"),
        CheckConstraint(
            "manager_id IS NULL OR manager_id <> employee_id",
            name="ck_org_placement_not_own_manager",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id"), nullable=False, index=True
    )
    # Null exactly for the one employee who tops the tree. The database refuses the
    # degenerate case of somebody managing themselves, whoever writes the row.
    manager_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("employee.id"))
    # Who the employee sits *with*, as free text.
    # ponytail: no org-unit master and no enumerated list — the plan names the grouping
    # (`cost centre/department grouping`) but not a vocabulary for it, so a CHECK here would
    # be this module inventing a company's departments. Upgrade path: a configured list of
    # org units (a master with codes, the way cost centres are reported on) and these two
    # columns become references to it.
    department: Mapped[str | None] = mapped_column(String(64))
    cost_centre: Mapped[str | None] = mapped_column(String(64))
    # The work location, added by T-5.EMP.03, whose lifecycle names a transfer of
    # *department/location/reporting line*: a placement is where somebody is, and all three
    # of those are part of it.
    location: Mapped[str | None] = mapped_column(String(64))
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)

    employee: Mapped[Employee] = relationship(foreign_keys=[employee_id])
    manager: Mapped[Employee | None] = relationship(foreign_keys=[manager_id])


# History is appended, never rewritten (T-0.AUDIT.01): a reorganisation adds a row.
append_only(OrgPlacement.__table__)


def _required(value: Any, what: str) -> str:
    stated = "" if value is None else str(value).strip()
    if not stated:
        raise InvalidPlacementError(f"{what} is required")
    return stated


def _grouping(value: Any, what: str) -> str | None:
    """A department or cost centre as stated, or nothing — an empty string is neither."""
    if value is None:
        return None
    return _required(value, what)


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
            raise InvalidPlacementError(f"not {what}: {value!r}") from exc
    raise InvalidPlacementError(f"{what} is a date, not {value!r}")


def placements_of(session: Session, employee: Employee) -> list[OrgPlacement]:
    """One employee's placements in start order — the history this module reads."""
    return list(
        session.scalars(
            select(OrgPlacement)
            .where(
                OrgPlacement.company_id == employee.company_id,
                OrgPlacement.employee_id == employee.id,
            )
            .order_by(OrgPlacement.effective_from)
        )
    )


def _in_force(history: list[OrgPlacement], *, on: date) -> OrgPlacement | None:
    """The placement whose half-open window contains `on`, or ``None``.

    The end of a placement is the next one's start, derived rather than stored: no row of
    the history is ever updated, which is what the table's append-only rule requires. The
    window is half-open, so the day a reorganisation starts belongs to the new placement
    alone.
    """
    for placement in history:
        following = [
            later.effective_from
            for later in history
            if later.effective_from > placement.effective_from
        ]
        ends = min(following) if following else None
        if placement.effective_from <= on and (ends is None or on < ends):
            return placement
    return None


def latest_placement(session: Session, employee: Employee) -> OrgPlacement | None:
    """The most recent placement on file, or ``None`` when the employee has never been placed.

    What the sequence rule reads. Taken as the maximum start date rather than as the last
    row of a collection, so it is right in the transaction that just wrote one.
    """
    return max(placements_of(session, employee), key=lambda row: row.effective_from, default=None)


def placement_in_force(session: Session, employee: Employee, *, on: date) -> OrgPlacement | None:
    """Where the employee sat on `on`, or ``None`` if they were not placed then."""
    return _in_force(placements_of(session, employee), on=on)


def manager_in_force(session: Session, employee: Employee, *, on: date) -> Employee | None:
    """Who the employee reported to on `on` — ``None`` for the root, and for the unplaced.

    The two are different answers and the caller can tell them apart with
    :func:`placement_in_force`: no manager **with** a placement is the top of the tree,
    no manager **without** one means the employee is not in the structure at all.
    """
    placement = placement_in_force(session, employee, on=on)
    return None if placement is None else placement.manager


def chain_of_command(session: Session, employee: Employee, *, on: date) -> list[Employee]:
    """Everybody above the employee on `on`, nearest first — the escalation path.

    Compiled from the dated placements rather than from a current-state column, so asking
    about a past date answers with the managers *of that date*. The walk is bounded by the
    number of employees in the company: the structure cannot contain a cycle, and a bound
    means a hand-edited database cannot hang a reader either.
    """
    by_employee = _company_placements(session, company_id=employee.company_id)
    chain: list[Employee] = []
    seen = {employee.id}
    current = _in_force(by_employee.get(employee.id, []), on=on)
    for _ in range(len(by_employee) + 1):
        if current is None or current.manager_id is None:
            break
        if current.manager_id in seen:
            break
        seen.add(current.manager_id)
        above = session.get(Employee, current.manager_id)
        if above is None:
            break
        chain.append(above)
        current = _in_force(by_employee.get(above.id, []), on=on)
    return chain


def _company_placements(
    session: Session, *, company_id: uuid.UUID
) -> dict[uuid.UUID, list[OrgPlacement]]:
    """Every placement of one company, grouped by employee, in one query.

    The chart reads the whole structure at once rather than a query per node: an org chart
    is the one screen whose cost would otherwise grow with the size of the company.
    """
    grouped: dict[uuid.UUID, list[OrgPlacement]] = {}
    rows = session.scalars(
        select(OrgPlacement)
        .where(OrgPlacement.company_id == company_id)
        .order_by(OrgPlacement.effective_from)
    )
    for row in rows:
        grouped.setdefault(row.employee_id, []).append(row)
    return grouped


def place_employee(
    session: Session,
    employee: Employee,
    *,
    effective_from: Any,
    manager: Employee | None = None,
    department: str | None = None,
    cost_centre: str | None = None,
    location: str | None = None,
) -> OrgPlacement:
    """Place the employee from `effective_from` under `manager`, in a department.

    Nothing already recorded is touched: the new row is appended, and the placement it
    follows closes by derivation. Three things are refused at entry rather than discovered
    in a chart later:

    * a date that does not follow the placements already recorded, or one before the
      employee was hired;
    * a manager who is the employee themselves, anyone **below** them in the structure this
      placement would create (the cycle that would cost the chart its root), somebody in
      another company, or somebody who has already been retired;
    * a second employee with no manager, because the criterion is one root per company. An
      employee who has never been placed is not a root: they are simply not in the tree,
      and are reported as *unplaced*.
    """
    starts = _date_or_refuse(effective_from, "the date a placement starts")
    if starts < employee.hire_date:
        raise PlacementSequenceError(
            f"a placement cannot start before the employee was hired: {starts} is before"
            f" {employee.hire_date}"
        )
    latest = latest_placement(session, employee)
    if latest is not None and starts <= latest.effective_from:
        raise PlacementSequenceError(
            f"a placement starting {starts} would not follow the one already recorded"
            f" ({latest.effective_from}); a reorganisation supersedes, it does not overlap"
        )

    by_employee = _company_placements(session, company_id=employee.company_id)
    if manager is None:
        other_root = _root_on(session, by_employee=by_employee, on=starts, excluding=employee.id)
        if other_root is not None:
            raise SecondRootError(
                f"{other_root.number!r} already tops the tree on {starts}; one company has one"
                f" root, so {employee.number!r} has to report to somebody — or be left"
                " unplaced until the structure moves"
            )
    else:
        if manager.id == employee.id:
            raise ReportingCycleError(
                f"{employee.number!r} cannot report to themselves; a chain of command ends at"
                " the one employee with no manager"
            )
        if manager.company_id != employee.company_id:
            raise UnknownManagerError(
                f"employee {manager.number!r} belongs to another company; a chain of command"
                " never crosses one"
            )
        if manager.deleted_at is not None:
            raise UnknownManagerError(
                f"employee {manager.number!r} has been retired; somebody who has left cannot be"
                " the manager a placement names — the chart could not draw their reports at all"
            )
        # Walk the manager's own chain: if the employee is in it, this placement would put
        # them inside their own chain of command, and the structure would lose its root.
        if any(above.id == employee.id for above in _above(by_employee, manager, on=starts)):
            raise ReportingCycleError(
                f"{manager.number!r} reports to {employee.number!r} (directly or through"
                f" others), so {employee.number!r} cannot report to {manager.number!r} —"
                " the structure would have a cycle and no root"
            )

    placement = OrgPlacement(
        company_id=employee.company_id,
        employee_id=employee.id,
        manager_id=None if manager is None else manager.id,
        department=_grouping(department, "a department"),
        cost_centre=_grouping(cost_centre, "a cost centre"),
        location=_grouping(location, "a location"),
        effective_from=starts,
    )
    session.add(placement)
    session.flush()
    return placement


def _above(
    by_employee: dict[uuid.UUID, list[OrgPlacement]],
    employee: Employee,
    *,
    on: date,
) -> list[Employee]:
    """Everybody above `employee` on `on`, nearest first, from an already-loaded structure.

    The same walk :func:`chain_of_command` does, without a session: `place_employee` has
    the company's placements in hand and needs to know whether the employee about to be
    placed appears above the manager they are being placed under. Bounded by the number of
    employees, so even a hand-edited cycle terminates the walk rather than hanging it.
    """
    above: list[Employee] = []
    seen = {employee.id}
    current = _in_force(by_employee.get(employee.id, []), on=on)
    for _ in range(len(by_employee) + 1):
        if current is None or current.manager_id is None or current.manager_id in seen:
            break
        seen.add(current.manager_id)
        manager = current.manager
        if manager is None:
            break
        above.append(manager)
        current = _in_force(by_employee.get(manager.id, []), on=on)
    return above


def _root_on(
    session: Session,
    *,
    by_employee: dict[uuid.UUID, list[OrgPlacement]],
    on: date,
    excluding: uuid.UUID,
) -> Employee | None:
    """The employee topping the tree on `on`, ignoring `excluding` — ``None`` if there is none."""
    for employee_id, history in by_employee.items():
        if employee_id == excluding:
            continue
        placement = _in_force(history, on=on)
        if placement is not None and placement.manager_id is None:
            return session.get(Employee, employee_id)
    return None


def org_chart(session: Session, *, company_id: uuid.UUID, on: date) -> dict:
    """The hierarchy on `on`: one root, everybody's place in it, and who is not in it.

    The whole answer from one read of the company's placements and one read of its
    employees — a chart asks about everybody at once, so a query per node would make the
    screen's cost grow with the company. The order is deterministic (children by employee
    number, the unplaced by number), so two renders of the same structure are identical.

    `unplaced` is every live employee the tree cannot draw: one who has never been placed
    (the employee with no manager the criterion names), and one whose recorded manager is no
    longer a live employee. Both are absent from `entries` rather than drawn as extra roots,
    because the criterion is a tree with **one** root per company.
    """
    employees = {
        row.id: row
        for row in session.scalars(
            select(Employee).where(Employee.company_id == company_id)
        )
    }
    by_employee = _company_placements(session, company_id=company_id)

    placed: dict[uuid.UUID, Employee | None] = {}
    unplaced: list[Employee] = []
    for employee_id, employee in employees.items():
        placement = _in_force(by_employee.get(employee_id, []), on=on)
        # A placement naming a manager who is no longer among the company's live employees
        # cannot be drawn: the chain does not reach the root, and calling the employee a
        # root would give the chart two of them. They are reported as unplaced instead.
        if placement is None or (
            placement.manager_id is not None and placement.manager_id not in employees
        ):
            unplaced.append(employee)
        else:
            placed[employee_id] = placement.manager_id

    children: dict[uuid.UUID | None, list[Employee]] = {}
    for employee_id, manager_id in placed.items():
        children.setdefault(manager_id, []).append(employees[employee_id])
    for reports in children.values():
        reports.sort(key=lambda employee: employee.number)

    # Depth-first from the root, so `entries` reads as the chart does: each line carries its
    # depth and how many people report to it directly.
    entries: list[dict] = []
    roots = children.get(None, [])
    frontier = [(employee, 1) for employee in reversed(roots)]
    while frontier:
        employee, depth = frontier.pop()
        direct = children.get(employee.id, [])
        placement = _in_force(by_employee[employee.id], on=on)
        entries.append(
            {
                "number": employee.number,
                "name": employee.party.name if employee.party is not None else None,
                "manager_number": None
                if placement is None or placement.manager is None
                else placement.manager.number,
                "department": None if placement is None else placement.department,
                "cost_centre": None if placement is None else placement.cost_centre,
                "depth": depth,
                "reports": len(direct),
            }
        )
        frontier.extend((report, depth + 1) for report in reversed(direct))

    return {
        "as_of": on.isoformat(),
        "root_number": None if not roots else roots[0].number,
        "entries": entries,
        "unplaced": [
            {
                "number": employee.number,
                "name": employee.party.name if employee.party is not None else None,
            }
            for employee in sorted(unplaced, key=lambda employee: employee.number)
        ],
    }
