"""T-5.ATT.01 check — shifts and the roster: one shift a day, dated, with audited overrides.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_shift_roster.py

It fails (non-zero exit) if any of these stops holding:

1. **a day resolves to exactly one shift, or to none** — a roster week comes back day by day
   with the shift in force, the holiday, and the overrides, and a second roster entry
   covering a day already covered is refused (the criterion's "rejected rather than
   ambiguous")
2. **shift definitions may overlap, a roster may not** — a morning and a night shift sharing
   hours coexist, while an entry backdated inside the roster already recorded is refused (two
   shifts claiming one day)
3. **an override records who changed it and why** — the actor and the reason are required and
   stored, one statement per employee per day (so a day still resolves once), and the trail
   keeps what the row said before
4. **the roster respects the holiday calendar** — a day the pack says is not worked resolves
   to no shift, and working it anyway is a deliberate override that names who decided it
5. **a roster change cannot alter a past day** — an entry starting later leaves the days
   before it answering as they did, the database refuses to rewrite a recorded entry, and the
   way to correct a past day is the attributed override
6. **nobody is rostered outside their employment** — a roster entry or an override for a day
   before the hire date (or after an exit) is refused (T-5.EMP.03)

**Scratch database only**: it drops and recreates the schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.audit import AuditLog, set_actor  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.hr.employees import create_employee  # noqa: E402
from app.hr.holidays import seed_calendar  # noqa: E402
from app.hr.movements import record_movement  # noqa: E402
from app.hr.shifts import (  # noqa: E402
    DuplicateShiftError,
    InvalidShiftError,
    NotEmployedError,
    RosterOverlapError,
    ShiftInUseError,
    define_shift,
    override_day,
    resolved_shift,
    retire_shift,
    roster_day,
    roster_employee,
    roster_week,
    shift_by_code,
)
from app.hr.org import place_employee  # noqa: E402,F401 — the phase's own schema, built once
from app.ledger import posting  # noqa: E402,F401 — every check builds the one schema

WEEK = date(2026, 6, 8)  # a Monday; 2026-06-12 is Independence Day in the pack


def _refused(call, expected: str) -> str:
    """The database's message if `call` is refused; fail the check otherwise."""
    try:
        call()
    except DBAPIError as exc:
        message = str(exc.orig).strip()
        assert expected in message, f"unclear database error: {message}"
        return message
    raise AssertionError(f"the database accepted what it must refuse ({expected!r})")


def _refuses(call, expected: type, what: str) -> Exception:
    """The refusal `call` raises; fail the check if it does not refuse."""
    try:
        call()
    except expected as exc:
        return exc
    raise AssertionError(f"{what} was accepted")


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is required (a scratch Postgres)", file=sys.stderr)
        return 2

    engine = create_engine(url)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    Base.metadata.create_all(engine)

    company_id = uuid.uuid4()
    with Session(engine) as session:
        session.add(
            Company(
                id=company_id,
                code="ROSTER-CHECK",
                name="Shift roster check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()
        set_actor(session, "scheduling")

        ana = create_employee(
            session,
            company_id=company_id,
            party_code="ANA",
            number="E-201",
            hire_date="2026-06-08",
            subject="hr",
            name="Ana Reyes",
        )
        ben = create_employee(
            session,
            company_id=company_id,
            party_code="BEN",
            number="E-202",
            hire_date="2026-06-01",
            subject="hr",
            name="Ben Reyes",
        )
        session.commit()
        seed_calendar(session, company_id=company_id, market="philippines", year=2026)
        session.commit()

        # 2 — the definitions may overlap; the codes are what identify them
        day = define_shift(
            session,
            company_id=company_id,
            code="DAY",
            name="Day shift",
            starts_at="09:00",
            ends_at="18:00",
            break_minutes=60,
            late_grace_minutes=10,
        )
        night = define_shift(
            session,
            company_id=company_id,
            code="NIGHT",
            name="Night shift",
            starts_at="22:00",
            ends_at="06:00",
        )
        session.commit()
        assert night.spans_midnight(), "22:00–06:00 is not read as an overnight shift"
        assert not day.spans_midnight()
        _refuses(
            lambda: define_shift(
                session,
                company_id=company_id,
                code="DAY",
                name="Another day shift",
                starts_at="08:00",
                ends_at="17:00",
            ),
            DuplicateShiftError,
            "a second shift with one code",
        )
        session.rollback()
        _refuses(
            lambda: define_shift(
                session,
                company_id=company_id,
                code="NEG",
                name="A shift with a negative break",
                starts_at="09:00",
                ends_at="18:00",
                break_minutes=-5,
            ),
            InvalidShiftError,
            "a negative break",
        )
        session.rollback()
        print("DAY (09:00–18:00) and NIGHT (22:00–06:00) coexist: definitions may overlap")

        # 1 + 5 — a roster entry is dated, and a later one does not reach back
        roster_employee(session, ana, shift=day, effective_from="2026-06-08", cycle="weekly")
        session.commit()
        assert resolved_shift(session, ana, on=date(2026, 6, 10)).code == "DAY"
        _refuses(
            lambda: roster_employee(session, ben, shift=day, effective_from="2026-05-01"),
            NotEmployedError,
            "a roster entry before the employee was hired",
        )
        session.rollback()
        # The successor: from 2026-06-15 the roster is the night shift, and 2026-06-10 still
        # answers DAY because the entry that applied then is untouched.
        entry = roster_employee(session, ana, shift=night, effective_from="2026-06-15")
        session.commit()
        assert resolved_shift(session, ana, on=date(2026, 6, 14)).code == "DAY"
        assert resolved_shift(session, ana, on=date(2026, 6, 15)).code == "NIGHT"
        # An entry *backdated* inside the roster already recorded would claim a day twice:
        # rejected, not left for a reader to disambiguate.
        _refuses(
            lambda: roster_employee(session, ana, shift=day, effective_from="2026-06-10"),
            RosterOverlapError,
            "a roster entry backdated inside the one already recorded",
        )
        session.rollback()
        message = _refused(
            lambda: (
                session.execute(
                    text("UPDATE roster_entry SET effective_from = '2026-06-01' WHERE id = :id"),
                    {"id": entry.id},
                ),
                session.commit(),
            ),
            "is append-only",
        )
        session.rollback()
        print(
            "the roster is dated history: an entry from 2026-06-15 leaves 2026-06-10 answering"
            f" DAY, and the database refuses to rewrite a recorded entry ({message.splitlines()[0]})"
        )

        # 3 + 4 — the week, with the holiday and the attributed overrides
        _refuses(
            lambda: override_day(
                session, ana, on="2026-06-11", shift=night, actor="scheduling", reason=" "
            ),
            InvalidShiftError,
            "an override with no reason",
        )
        session.rollback()
        worked_holiday = override_day(
            session,
            ana,
            on="2026-06-12",
            shift=day,
            actor="scheduling",
            reason="year-end close: the store opens on Independence Day",
        )
        rest_day = override_day(
            session,
            ana,
            on="2026-06-13",
            shift=None,
            actor="scheduling",
            reason="rest day after the night roster",
        )
        cover = override_day(
            session,
            ana,
            on="2026-06-11",
            shift=night,
            actor="scheduling",
            reason="covering the night shift for a colleague on leave",
        )
        session.commit()
        week = roster_week(session, ana, starts_on=WEEK)
        assert [row["date"] for row in week] == [
            "2026-06-08",
            "2026-06-09",
            "2026-06-10",
            "2026-06-11",
            "2026-06-12",
            "2026-06-13",
            "2026-06-14",
        ]
        assert [row["shift"] for row in week[:4]] == ["DAY", "DAY", "DAY", "NIGHT"], week
        assert week[0]["rostered_shift"] == "DAY" and week[6]["rostered_shift"] == "DAY"
        assert week[3]["overridden"] and week[3]["override_reason"] == cover.reason
        assert week[4]["status"] == "holiday" and week[4]["shift"] == "DAY", (
            "the holiday either lost the deliberate override or was not reported as a holiday"
        )
        assert week[4]["rostered_shift"] == "DAY", (
            "the holiday erased the roster assignment rather than the day's work"
        )
        # A holiday nobody overrode: the roster assignment is still on file and the day is
        # not worked, which is the "respects the holiday calendar" half of the criterion.
        heroes = roster_day(session, ana, on=date(2026, 8, 31))
        assert heroes["status"] == "holiday" and heroes["shift"] is None, heroes
        assert heroes["rostered_shift"] == "NIGHT" and not heroes["overridden"], heroes
        assert week[5]["shift"] is None and week[5]["overridden"], "the rest day did not resolve"
        assert week[6]["shift"] == "DAY" and week[6]["status"] == "working", week[6]
        print(
            "the week resolves day by day: DAY, DAY, DAY, NIGHT (override), Independence Day"
            " worked by decision, rest day, DAY — and every override names who and why"
        )

        # An override for one day is one statement: overriding it again updates it rather
        # than making the day ambiguous, and the trail keeps what it said before.
        again = override_day(
            session,
            ana,
            on="2026-06-13",
            shift=day,
            actor="scheduling",
            reason="rest day cancelled: month-end close",
        )
        session.commit()
        assert again.id == rest_day.id, "a second override for one day was stored beside the first"
        assert session.scalar(select(func.count()).select_from(
            text("roster_override")
        )) == 3, "one day holds more than one override"
        assert resolved_shift(session, ana, on=date(2026, 6, 13)).code == "DAY"
        trail = list(
            session.scalars(
                select(AuditLog).where(
                    AuditLog.entity == "roster_override", AuditLog.company_id == company_id
                )
            )
        )
        assert any(entry.action == "update" for entry in trail), (
            f"the correction of an override is not on the trail: {[e.action for e in trail]}"
        )
        print(
            "overriding a day twice updates the one statement (the day still resolves once)"
            " and the change is on the audit trail"
        )

        # 6 — and a day outside employment cannot be overridden either
        _refuses(
            lambda: override_day(
                session,
                ana,
                on="2026-06-07",
                shift=day,
                actor="scheduling",
                reason="the day before the hire",
            ),
            NotEmployedError,
            "an override before the employee was hired",
        )
        session.rollback()
        record_movement(
            session,
            ben,
            kind="exit",
            effective_date="2026-06-30",
            reason="resigned",
            actor="hr",
        )
        session.commit()
        _refuses(
            lambda: roster_employee(session, ben, shift=day, effective_from="2026-07-01"),
            NotEmployedError,
            "a roster entry after the exit",
        )
        session.rollback()
        print("a roster entry or an override outside the employment is refused (T-5.EMP.03)")

        # A shift somebody is rostered on cannot be retired; one nobody is, can.
        _refuses(
            lambda: (retire_shift(session, day), session.commit()),
            ShiftInUseError,
            "retiring a shift somebody is rostered on",
        )
        session.rollback()
        spare = define_shift(
            session,
            company_id=company_id,
            code="SPARE",
            name="Shift nobody works",
            starts_at="06:00",
            ends_at="14:00",
        )
        session.commit()
        retire_shift(session, spare)
        session.commit()
        _refuses(
            lambda: roster_employee(session, ben, shift=spare, effective_from="2026-06-15"),
            InvalidShiftError,
            "rostering a retired shift",
        )
        session.rollback()
        assert shift_by_code(session, company_id=company_id, code="DAY").code == "DAY"
        print("a shift in use cannot be retired, and a retired one cannot be rostered")

    engine.dispose()
    print("ok — one shift a day, dated, with the holiday and every correction attributed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
