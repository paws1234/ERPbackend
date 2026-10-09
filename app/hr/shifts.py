"""T-5.ATT.01 — shifts and the roster: who works when, on which day.

A **shift** is a definition — when it starts, when it ends, how long its break is and how
late somebody may be before it counts (T-5.ATT.03 reads that grace). A **roster** is a
dated statement that an employee works a shift from a day onwards, and a **roster override**
is a statement about one day.

Four rules this module is built around:

* **A day resolves to exactly one shift, or to none.** :func:`resolved_shift` is the only
  answer to "what does this person work on this day": an override for that day wins, then
  the roster entry in force, then nothing. Two roster entries covering one day would make
  that answer ambiguous, so an overlapping roster is **refused** — the criterion's "rejected
  or flagged", answered by refusing. Shift *definitions* may overlap (a morning and a night
  shift share hours by nature); a roster may not.
* **The roster is history.** A roster entry is appended and never rewritten — a new entry
  supersedes the one before it and the previous one closes by derivation, exactly as a
  contract (T-5.EMP.01) and a placement (T-5.EMP.02) do — so a roster change **cannot**
  alter what somebody was rostered for last Tuesday. Changing a day that has already happened
  is the **override**, which names who changed it and why: the audited correction the
  criterion asks for, rather than a silent rewrite.
* **The holiday calendar closes the roster.** A day the calendar says is not worked resolves
  to no shift (T-5.LEAVE.01's one resolver is what this reads), so nobody is rostered on a
  day the company does not work — and working one anyway is a deliberate, named act: an
  **override** stating the shift, the actor and the reason.
* **Nobody is rostered outside their employment.** A roster entry or an override for a day
  the employee was not employed on is refused (T-5.EMP.03's `active_on`), so a roster cannot
  quietly claim attendance for somebody who had not joined or had already left.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Integer,
    String,
    Time,
    UniqueConstraint,
    Uuid,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.audit import SoftDeleteMixin, append_only, deny_hard_delete, soft_delete
from app.db import Base
from app.hr.employees import Employee
from app.hr.holidays import WORKING, day_status
from app.hr.movements import active_on


class ShiftError(ValueError):
    """The roster refused what was asked of it."""


class InvalidShiftError(ShiftError):
    """A shift definition, a date or a reason failed validation at entry."""


class DuplicateShiftError(ShiftError):
    """That shift code is already used in this company."""


class ShiftInUseError(ShiftError):
    """A shift somebody is still rostered on cannot be retired."""


class RosterOverlapError(ShiftError):
    """The roster would cover a day twice, so the day's shift would be ambiguous."""


class NotEmployedError(ShiftError):
    """The day is outside the employee's employment — there is nothing to roster."""


class Shift(SoftDeleteMixin, Base):
    """One shift as defined: windows, break and grace."""

    __tablename__ = "shift"
    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_shift_company_code"),
        CheckConstraint("break_minutes >= 0", name="ck_shift_break_minutes"),
        CheckConstraint("late_grace_minutes >= 0", name="ck_shift_late_grace_minutes"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    # When the shift starts and ends, as clock times. An end at or before the start means it
    # runs past midnight (`22:00`–`06:00` is a night shift, not a negative one), which is the
    # one convention that makes an overnight shift storable without inventing a second date.
    starts_at: Mapped[time] = mapped_column(Time, nullable=False)
    ends_at: Mapped[time] = mapped_column(Time, nullable=False)
    break_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # How late somebody may be before it counts (T-5.ATT.03 classifies it). Stated per shift
    # because that is what the ledger's `late_grace_minutes` is a property of, with no
    # default invented here: zero is a real answer meaning "any lateness counts".
    late_grace_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def spans_midnight(self) -> bool:
        """Whether the shift runs past midnight — the rule above, stated once."""
        return self.ends_at <= self.starts_at


class RosterEntry(Base):
    """A dated statement that an employee works a shift from one day onwards.

    Append-only history: a roster change is a new row, and the entry it replaces closes by
    derivation (its window ends where the next one starts). Nothing updates a row, so what
    somebody was rostered for on a past day cannot change after the fact.
    """

    __tablename__ = "roster_entry"
    __table_args__ = (
        UniqueConstraint("employee_id", "effective_from", name="uq_roster_entry_start"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id"), nullable=False, index=True
    )
    shift_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("shift.id"), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    # The cycle the roster repeats in, as the company states it (`roster_cycle` has no
    # default in the ledger). Free text for that reason: a pattern language nobody has
    # specified is not something this module should invent. The roster's *effect* is told by
    # the dated entries, which is what every reader resolves from.
    # ponytail: no cycle engine. Ceiling: a repeating pattern is expressed by the entries a
    # caller writes, not by a rule evaluated here. Upgrade path: when a company states its
    # cycle (`roster_cycle`), a weekly/monthly pattern expands into entries at entry time.
    cycle: Mapped[str | None] = mapped_column(String(32))

    employee: Mapped[Employee] = relationship()
    shift: Mapped[Shift] = relationship()


class RosterOverride(Base):
    """One day, stated deliberately — the audited correction to the roster.

    The row is **updated in place** when the same day is overridden again, because an
    override is a claim about one day that has to be able to be wrong and be fixed; what
    makes that safe is that it names **who** changed it and **why**, and that T-0.AUDIT.02
    records what it said before.
    """

    __tablename__ = "roster_override"
    __table_args__ = (
        # One statement per employee per day: two would make the day ambiguous, which is the
        # property the whole resolution rests on.
        UniqueConstraint("employee_id", "on_date", name="uq_roster_override_day"),
        CheckConstraint("char_length(reason) > 0", name="ck_roster_override_reason"),
        CheckConstraint("char_length(actor) > 0", name="ck_roster_override_actor"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id"), nullable=False, index=True
    )
    on_date: Mapped[date] = mapped_column(Date, nullable=False)
    # Null is a real answer: "not working that day" — a day off, which is not the same as an
    # override nobody wrote.
    shift_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("shift.id"))
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)

    employee: Mapped[Employee] = relationship()
    shift: Mapped[Shift | None] = relationship()


# A shift is a master: retired by marking, never removed (T-0.AUDIT.01). A roster entry is
# history: appended, never rewritten. An override is neither — see its own note.
deny_hard_delete(Shift.__table__)
append_only(RosterEntry.__table__)


def _required(value: Any, what: str) -> str:
    stated = "" if value is None else str(value).strip()
    if not stated:
        raise InvalidShiftError(f"{what} is required")
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
            raise InvalidShiftError(f"not {what}: {value!r}") from exc
    raise InvalidShiftError(f"{what} is a date, not {value!r}")


def _clock(value: Any, what: str) -> time:
    """A clock time from a `time`, a `datetime` or the `HH:MM[:SS]` string a client sends."""
    if isinstance(value, datetime):
        return value.time().replace(microsecond=0)
    if isinstance(value, time):
        return value.replace(microsecond=0)
    if isinstance(value, str):
        try:
            return time.fromisoformat(value.strip())
        except ValueError as exc:
            raise InvalidShiftError(f"not {what}: {value!r}") from exc
    raise InvalidShiftError(f"{what} is a clock time, not {value!r}")


def _minutes(value: Any, what: str) -> int:
    if value is None:
        return 0
    try:
        stated = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidShiftError(f"{what} is a number of minutes, not {value!r}") from exc
    if stated < 0:
        raise InvalidShiftError(f"{what} is not negative: {stated}")
    return stated


def _employed(session: Session, employee: Employee, *, on: date, what: str) -> None:
    """Refuse a roster statement about a day the employee was not employed on."""
    if not active_on(session, employee, on=on):
        raise NotEmployedError(
            f"{employee.number!r} was not employed on {on}, so {what} cannot be stated for"
            " it (T-5.EMP.03 answers who is active, and on which dates)"
        )


def define_shift(
    session: Session,
    *,
    company_id: uuid.UUID,
    code: str,
    name: str,
    starts_at: Any,
    ends_at: Any,
    break_minutes: Any = 0,
    late_grace_minutes: Any = 0,
) -> Shift:
    """Define a shift. Definitions may overlap; a roster may not (see the module docstring)."""
    stated = _required(code, "a shift code")
    if session.scalar(
        select(Shift).where(Shift.company_id == company_id, Shift.code == stated)
    ) is not None:
        raise DuplicateShiftError(
            f"shift {stated!r} already exists in this company; a code identifies one shift"
        )
    shift = Shift(
        company_id=company_id,
        code=stated,
        name=_required(name, "a shift name"),
        starts_at=_clock(starts_at, "a shift start"),
        ends_at=_clock(ends_at, "a shift end"),
        break_minutes=_minutes(break_minutes, "a break"),
        late_grace_minutes=_minutes(late_grace_minutes, "a late grace"),
    )
    session.add(shift)
    session.flush()
    return shift


def shift_by_code(session: Session, *, company_id: uuid.UUID, code: str) -> Shift:
    """The live shift a roster names by its code, or a refusal."""
    shift = session.scalar(
        select(Shift).where(Shift.company_id == company_id, Shift.code == str(code).strip())
    )
    if shift is None:
        raise InvalidShiftError(
            f"no shift {code!r} in this company; define it first (T-5.ATT.01)"
        )
    return shift


def roster_entries(session: Session, employee: Employee) -> list[RosterEntry]:
    """One employee's roster, oldest first — the history this module resolves from."""
    return list(
        session.scalars(
            select(RosterEntry)
            .where(
                RosterEntry.company_id == employee.company_id,
                RosterEntry.employee_id == employee.id,
            )
            .order_by(RosterEntry.effective_from)
        )
    )


def latest_entry(session: Session, employee: Employee) -> RosterEntry | None:
    """The most recent roster entry on file, or ``None`` — what a new one supersedes.

    Taken as the maximum start date rather than as the last row of a collection, so it is
    right in the transaction that just wrote one. This is the ordering the roster's windows
    rest on: an entry's end is the next entry's start, so the starts are what must advance.
    """
    return max(roster_entries(session, employee), key=lambda row: row.effective_from, default=None)


def _entry_in_force(session: Session, employee: Employee, *, on: date) -> RosterEntry | None:
    """The roster entry whose half-open window contains `on`, or ``None``.

    An entry's window ends where the next one starts, **derived** from the roster rather than
    stored on the row: nothing updates a roster entry, which is what the table's append-only
    rule requires and what makes a past day's answer stable.
    """
    entries = roster_entries(session, employee)
    for entry in entries:
        following = [
            later.effective_from
            for later in entries
            if later.effective_from > entry.effective_from
        ]
        ends = min(following) if following else None
        if entry.effective_from <= on and (ends is None or on < ends):
            return entry
    return None


def roster_employee(
    session: Session,
    employee: Employee,
    *,
    shift: Shift,
    effective_from: Any,
    cycle: str | None = None,
) -> RosterEntry:
    """Put the employee on the shift from `effective_from` onwards.

    Refused when the day would **overlap** the roster already recorded — an entry that does
    not start after the last one is exactly two shifts claiming one day, so it is rejected
    rather than left to a reader to disambiguate — when the shift belongs to another company,
    when the shift has been retired, and when the day is outside the employee's employment. A
    change to a day that has already happened is not an entry: it is an override, which names
    who and why.

    The entry carries no actor of its own — the change is on the T-0.AUDIT.02 trail with the
    session's actor like any other, and the per-day *correction* which does need a name is
    the override.
    """
    starts = _date_or_refuse(effective_from, "a roster start date")
    if shift.company_id != employee.company_id:
        raise InvalidShiftError(
            f"shift {shift.code!r} belongs to another company; a roster never crosses one"
        )
    if shift.deleted_at is not None:
        raise InvalidShiftError(
            f"shift {shift.code!r} has been retired; roster the shift that replaced it"
        )
    _employed(session, employee, on=starts, what=f"a roster entry from {starts}")
    previous = latest_entry(session, employee)
    if previous is not None and starts <= previous.effective_from:
        raise RosterOverlapError(
            f"a roster entry starting {starts} would not follow the one already recorded"
            f" ({previous.shift.code!r} from {previous.effective_from}); the roster supersedes,"
            " it does not overlap — a day that has already been rostered is corrected with an"
            " override, which says who and why"
        )
    entry = RosterEntry(
        company_id=employee.company_id,
        employee_id=employee.id,
        shift_id=shift.id,
        effective_from=starts,
        cycle=None if cycle is None else _required(cycle, "a roster cycle"),
    )
    session.add(entry)
    session.flush()
    return entry


def override_day(
    session: Session,
    employee: Employee,
    *,
    on: Any,
    shift: Shift | None,
    actor: str,
    reason: str,
) -> RosterOverride:
    """State what one day actually is — a day off, another shift, or a day worked on a holiday.

    This is where a past day is corrected, and it is **attributed**: the actor and the reason
    are required, stored on the row, and the trail keeps what the override said before. A day
    already overridden is updated in place rather than accumulated, so a day still resolves
    once.
    """
    when = _date_or_refuse(on, "an override date")
    who = _required(actor, "the actor overriding the day")
    why = _required(reason, "a reason")
    _employed(session, employee, on=when, what=f"an override for {when}")
    if shift is not None and shift.company_id != employee.company_id:
        raise InvalidShiftError(
            f"shift {shift.code!r} belongs to another company; an override never crosses one"
        )
    existing = session.scalar(
        select(RosterOverride).where(
            RosterOverride.company_id == employee.company_id,
            RosterOverride.employee_id == employee.id,
            RosterOverride.on_date == when,
        )
    )
    if existing is not None:
        existing.shift_id = None if shift is None else shift.id
        existing.actor = who
        existing.reason = why
        session.flush()
        return existing
    override = RosterOverride(
        company_id=employee.company_id,
        employee_id=employee.id,
        on_date=when,
        shift_id=None if shift is None else shift.id,
        actor=who,
        reason=why,
    )
    session.add(override)
    session.flush()
    return override


def override_on(session: Session, employee: Employee, *, on: date) -> RosterOverride | None:
    """The statement made about that day, or ``None`` — never a guess."""
    return session.scalar(
        select(RosterOverride).where(
            RosterOverride.company_id == employee.company_id,
            RosterOverride.employee_id == employee.id,
            RosterOverride.on_date == on,
        )
    )


def resolved_shift(session: Session, employee: Employee, *, on: date) -> Shift | None:
    """The shift the employee works on `on`, or ``None`` — **one** answer, or none.

    An override for the day wins (it may say "no shift"), then a holiday resolves to no shift
    at all, then the roster entry in force. No combination of these can produce two shifts:
    the roster refuses to cover a day twice, and an override is unique per day.
    """
    override = override_on(session, employee, on=on)
    if override is not None:
        return override.shift
    if day_status(session, company_id=employee.company_id, on=on) != WORKING:
        return None
    entry = _entry_in_force(session, employee, on=on)
    return None if entry is None else entry.shift


def roster_day(session: Session, employee: Employee, *, on: date) -> dict:
    """One day as the roster resolves it: the status, the shift, and why.

    What a timesheet, an attendance capture (T-5.ATT.02) and an overtime classification
    (T-5.ATT.03) all read, so they cannot disagree about what somebody was rostered for.
    """
    override = override_on(session, employee, on=on)
    entry = _entry_in_force(session, employee, on=on)
    shift = resolved_shift(session, employee, on=on)
    return {
        "date": on.isoformat(),
        "status": day_status(session, company_id=employee.company_id, on=on),
        "shift": None if shift is None else shift.code,
        "shift_name": None if shift is None else shift.name,
        "late_grace_minutes": None if shift is None else shift.late_grace_minutes,
        "rostered_shift": None if entry is None else entry.shift.code,
        "overridden": override is not None,
        "override_actor": None if override is None else override.actor,
        "override_reason": None if override is None else override.reason,
    }


def roster_week(session: Session, employee: Employee, *, starts_on: date) -> list[dict]:
    """Seven days from `starts_on`, resolved — "the resolved shift per day", as one read."""
    return [
        roster_day(session, employee, on=starts_on + timedelta(days=offset))
        for offset in range(7)
    ]


def retire_shift(session: Session, shift: Shift, *, at: datetime | None = None) -> Shift:
    """Retire a shift by marking it — refused while somebody is still rostered on it.

    The row has to stay for the roster entries and the attendance that name it, so the mark
    is what "no longer used" means; a shift nobody is rostered on any more can go.
    """
    still_rostered = list(
        session.scalars(select(RosterEntry).where(RosterEntry.shift_id == shift.id))
    )
    if still_rostered:
        raise ShiftInUseError(
            f"shift {shift.code!r} is still on {len(still_rostered)} roster"
            f" {'entry' if len(still_rostered) == 1 else 'entries'}; a shift somebody is"
            " rostered on cannot be retired"
        )
    return soft_delete(session, shift, at=at)
