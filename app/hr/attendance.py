"""T-5.ATT.02 — capturing attendance: punches in, worked hours out.

A punch is an **event that happened**, so the table is append-only: an in, an out, when, and
where it came from. Nothing is edited, and a wrong or missing punch is answered by another
punch — a **manual** one, which must say who entered it and why. That is the criterion's
"manual correction requires a reason and is audited", and the same shape the ledger uses for
a correction: append the correcting record rather than rewrite the wrong one.

Three rules this module is built around:

* **Worked hours are derived, and the derivation is shown.** :func:`attendance_day` pairs the
  day's punches in time order — an `in` with the next `out` — subtracts the rostered shift's
  break **once** for the day, and reports every pair with its own minutes beside the total, so
  a reader can see where the number came from instead of being told it.
* **Nothing is dropped.** A punch with no partner — an `out` with no `in` before it, an `in`
  with no `out` after — is **reported** as unmatched, with the reason, and contributes no
  hours: silently ignoring it is how a day quietly loses half its work, and silently counting
  it is how a day gains hours nobody worked.
* **A punch cannot be counted twice.** The same employee, instant and direction is one row,
  refused by the service by name and by the database's unique constraint — which is what the
  device feed will lean on in Phase 6, where a re-pulled window arrives twice as a matter of
  course (T-6.OFFLINE.02).

What is deliberately *not* here: classifying what the day means — overtime bands, lateness
against the shift's grace, holiday premiums (T-5.ATT.03) and what payroll pays for any of it
(T-5.PAY.02). This module establishes **what was worked and how that was derived**.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    Uuid,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.audit import append_only
from app.db import Base
from app.hr.employees import Employee
from app.hr.leave_requests import leave_on
from app.hr.movements import active_on
from app.hr.shifts import NotEmployedError, resolved_shift, roster_day

# The origins a punch may have. `manual` is what a person enters; `biometric device` is what
# the integration boundary will carry in Phase 6 — the ledger's own two values for
# `attendance_source`, named here once so a third cannot appear by typo.
PUNCH_SOURCES = ("manual", "biometric device")

# A punch is either somebody arriving or somebody leaving. Named, not numbered, so a report
# and a device feed speak the same words.
DIRECTIONS = ("in", "out")

IN = "in"
OUT = "out"


class AttendanceError(ValueError):
    """The attendance log refused what was asked of it."""


class InvalidPunchError(AttendanceError):
    """A direction, a source, an instant or a reason failed validation at entry."""


class DuplicatePunchError(AttendanceError):
    """That employee already has a punch of that direction at that instant."""


class AttendanceEvent(Base):
    """One punch: who, when, which way — and, if a person entered it, who and why."""

    __tablename__ = "attendance_event"
    __table_args__ = (
        CheckConstraint(
            "direction IN (" + ", ".join(f"'{direction}'" for direction in DIRECTIONS) + ")",
            name="ck_attendance_event_direction",
        ),
        CheckConstraint(
            "source IN (" + ", ".join(f"'{source}'" for source in PUNCH_SOURCES) + ")",
            name="ck_attendance_event_source",
        ),
        # A manual punch is a correction somebody made, so it says who and why; a device
        # punch is a reading, and has neither. Stated as a CHECK so a writer cannot leave a
        # manual punch unattributed.
        CheckConstraint(
            "source <> 'manual' OR (actor IS NOT NULL AND reason IS NOT NULL)",
            name="ck_attendance_event_manual_is_attributed",
        ),
        CheckConstraint(
            "source <> 'manual' OR (char_length(actor) > 0 AND char_length(reason) > 0)",
            name="ck_attendance_event_manual_is_stated",
        ),
        # One punch per employee, instant and direction: the same event arriving twice is a
        # re-pull or a double tap, never a second arrival.
        UniqueConstraint(
            "company_id", "employee_id", "at", "direction", name="uq_attendance_event_punch"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id"), nullable=False, index=True
    )
    # The instant of the punch, with its zone. A timestamp rather than a time plus a date,
    # because that is what a device reports and what an overnight shift needs: pairing by
    # instant is correct across midnight without a convention of its own.
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    actor: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(String(255))

    employee: Mapped[Employee] = relationship()


# An event that happened is appended, never rewritten (T-0.AUDIT.01): the correcting punch is
# the correction, exactly as the ledger corrects by posting.
append_only(AttendanceEvent.__table__)


def _required(value: Any, what: str) -> str:
    stated = "" if value is None else str(value).strip()
    if not stated:
        raise InvalidPunchError(f"{what} is required")
    return stated


def _instant(value: Any, what: str) -> datetime:
    """A moment in time from a `datetime`, an ISO string, or a date (midnight, local to UTC).

    Naive instants are read as UTC rather than as the database's zone: a punch that does not
    state its zone must not be interpreted differently on two machines.
    """
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, date):
        moment = datetime(value.year, value.month, value.day)
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise InvalidPunchError(f"not {what}: {value!r}") from exc
    else:
        raise InvalidPunchError(f"{what} is an instant, not {value!r}")
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def record_punch(
    session: Session,
    employee: Employee,
    *,
    at: Any,
    direction: str,
    source: str = "manual",
    actor: str | None = None,
    reason: str | None = None,
) -> AttendanceEvent:
    """Record one punch — refused when it duplicates one already recorded.

    The same employee, instant and direction is one event: a device re-pull or a double tap
    arrives twice and is refused by name here and by the table's unique constraint
    regardless, so no derivation can ever count it twice. A punch is also refused outside the
    employee's employment (T-5.EMP.03): nobody arrived before they joined or after they left.
    """
    wanted = _required(direction, "a direction").lower()
    if wanted not in DIRECTIONS:
        raise InvalidPunchError(
            f"unknown direction {direction!r}; a punch is {', '.join(DIRECTIONS)}"
        )
    origin = _required(source, "a source").lower()
    if origin not in PUNCH_SOURCES:
        raise InvalidPunchError(
            f"unknown source {source!r}; attendance comes from {', '.join(PUNCH_SOURCES)}"
        )
    moment = _instant(at, "a punch instant")
    who = None if actor is None else _required(actor, "the actor of a manual punch")
    why = None if reason is None else _required(reason, "the reason for a manual punch")
    if origin == "manual" and (who is None or why is None):
        raise InvalidPunchError(
            "a manual punch is a correction somebody made: state who entered it and why"
        )
    if not active_on(session, employee, on=moment.date()):
        raise NotEmployedError(
            f"{employee.number!r} was not employed on {moment.date()}, so a punch cannot be"
            " recorded for it (T-5.EMP.03)"
        )

    existing = session.scalar(
        select(AttendanceEvent).where(
            AttendanceEvent.company_id == employee.company_id,
            AttendanceEvent.employee_id == employee.id,
            AttendanceEvent.at == moment,
            AttendanceEvent.direction == wanted,
        )
    )
    if existing is not None:
        raise DuplicatePunchError(
            f"{employee.number!r} already has a {wanted} punch at {moment.isoformat()}; the"
            " same event is one row, whatever delivered it"
        )

    event = AttendanceEvent(
        company_id=employee.company_id,
        employee_id=employee.id,
        at=moment,
        direction=wanted,
        source=origin,
        actor=who,
        reason=why,
    )
    session.add(event)
    session.flush()
    return event


def punches_on(session: Session, employee: Employee, *, on: date) -> list[AttendanceEvent]:
    """The day's punches, earliest first — what the derivation reads.

    A day is taken in **UTC**, the zone the punches are stored in, so "the day's punches" is
    the same set on every machine. A site in another zone is Phase 6's problem and would be
    stated here, not assumed.
    """
    starts = datetime(on.year, on.month, on.day, tzinfo=timezone.utc)
    return list(
        session.scalars(
            select(AttendanceEvent)
            .where(
                AttendanceEvent.company_id == employee.company_id,
                AttendanceEvent.employee_id == employee.id,
                AttendanceEvent.at >= starts,
                AttendanceEvent.at < starts + timedelta(days=1),
            )
            .order_by(AttendanceEvent.at, AttendanceEvent.direction)
        )
    )


def attendance_day(session: Session, employee: Employee, *, on: date) -> dict:
    """One day derived: the shift, the pairs, the unmatched punches, and the hours.

    The derivation is the point — every pair is reported with its own minutes beside the
    total, the break that was deducted is named with the shift it came from, and every punch
    that could not be paired is listed with why. `worked_minutes` is the sum of the pairs
    minus the break, and it is **zero** when nothing paired: an unmatched punch is a question
    for a person, not hours credited to an employee.
    """
    shift = resolved_shift(session, employee, on=on)
    punches = punches_on(session, employee, on=on)
    pairs: list[dict] = []
    unmatched: list[dict] = []
    open_punch: AttendanceEvent | None = None

    for punch in punches:
        if open_punch is None:
            if punch.direction == OUT:
                unmatched.append(
                    {
                        "at": punch.at.isoformat(),
                        "direction": punch.direction,
                        "why": "an out with no in before it on this day",
                    }
                )
                continue
            open_punch = punch
            continue
        if punch.direction == IN:
            # Two arrivals in a row: the earlier one is the question, and the day goes on.
            unmatched.append(
                {
                    "at": open_punch.at.isoformat(),
                    "direction": open_punch.direction,
                    "why": "an in with no out before the next in",
                }
            )
            open_punch = punch
            continue
        minutes = int((punch.at - open_punch.at).total_seconds() // 60)
        pairs.append(
            {
                "in": open_punch.at.isoformat(),
                "out": punch.at.isoformat(),
                "minutes": minutes,
            }
        )
        open_punch = None

    if open_punch is not None:
        unmatched.append(
            {
                "at": open_punch.at.isoformat(),
                "direction": open_punch.direction,
                "why": "an in with no out at the end of the day",
            }
        )

    gross = sum(pair["minutes"] for pair in pairs)
    break_minutes = 0 if shift is None else shift.break_minutes
    # The shift's break is deducted once for the day, and only when there is something to
    # deduct it from: a day with no matched pair owes no break.
    deducted = min(break_minutes, gross) if pairs else 0
    day = roster_day(session, employee, on=on)
    # Approved leave (T-5.LEAVE.03) is part of what the day *was*: hours worked without it
    # are a contradiction somebody has to look at, and payroll needs to know the day was
    # leave as well as whether it is paid.
    leave = leave_on(session, employee, on=on)
    return {
        **day,
        "leave": None
        if leave is None
        else {
            "request": str(leave.id),
            "leave_type": leave.leave_type.code,
            "paid": bool(leave.leave_type.paid),
        },
        "break_minutes": break_minutes,
        "pairs": pairs,
        "unmatched": unmatched,
        "punches": len(punches),
        "gross_minutes": gross,
        "break_deducted": deducted,
        "worked_minutes": gross - deducted,
        "derivation": (
            f"{gross} minutes over {len(pairs)} matched pair(s) minus a {deducted} minute break"
            f" from {shift.code if shift else 'no'} shift = {gross - deducted}"
        ),
        "exceptions": len(unmatched),
    }
