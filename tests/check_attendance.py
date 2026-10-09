"""T-5.ATT.02 check — attendance capture: punches appended, hours derived, exceptions reported.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_attendance.py

It fails (non-zero exit) if any of these stops holding:

1. **worked hours derive from the matched in/out pair, against the rostered shift** — a normal
   pair on a rostered day yields the minutes between the punches less the shift's break, and
   the derivation is reported beside the total (each pair with its own minutes, the break that
   was deducted, the shift it came from)
2. **a duplicate punch is not double-counted** — the same employee, instant and direction is
   refused by name and by the database, and the day's hours are unchanged after the attempt
3. **an unmatched punch is reported, not dropped** — an out with no in before it, an in with
   no out after it, and two arrivals in a row each appear as an exception with the reason, and
   none of them contributes hours
4. **a manual correction requires a reason and is audited** — a manual punch without an actor
   or a reason is refused (and the database refuses it too), and with both it is recorded and
   attributable
5. punches are **append-only**: the database refuses to edit or delete a recorded punch, so a
   correction is another punch rather than a rewrite

**Scratch database only**: it drops and recreates the schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date

from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.audit import set_actor  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.hr.attendance import (  # noqa: E402
    DuplicatePunchError,
    InvalidPunchError,
    attendance_day,
    record_punch,
)
from app.hr.employees import create_employee  # noqa: E402
from app.hr.holidays import seed_calendar  # noqa: E402
from app.hr.movements import record_movement  # noqa: E402
from app.hr.shifts import NotEmployedError, define_shift, override_day, roster_employee  # noqa: E402
from app.ledger import posting  # noqa: E402,F401 — every check builds the one schema

DAY = date(2026, 6, 9)  # a Tuesday in the roster week


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
                code="ATT-CHECK",
                name="Attendance check",
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
            number="E-301",
            hire_date="2026-06-08",
            subject="hr",
            name="Ana Reyes",
        )
        leo = create_employee(
            session,
            company_id=company_id,
            party_code="LEO",
            number="E-302",
            hire_date="2026-06-01",
            subject="hr",
            name="Leo Reyes",
        )
        session.commit()
        seed_calendar(session, company_id=company_id, market="philippines", year=2026)
        shift = define_shift(
            session,
            company_id=company_id,
            code="DAY",
            name="Day shift",
            starts_at="09:00",
            ends_at="18:00",
            break_minutes=60,
            late_grace_minutes=10,
        )
        session.commit()
        roster_employee(session, ana, shift=shift, effective_from="2026-06-08")
        override_day(
            session,
            ana,
            on=DAY,
            shift=None,
            actor="scheduling",
            reason="a rest day, so the derivation has a day with no shift",
        )
        session.commit()

        # 1 — a normal pair, with the derivation shown
        record_punch(session, ana, at="2026-06-08T09:02:00+00:00", direction="in", source="biometric device")
        record_punch(session, ana, at="2026-06-08T18:05:00+00:00", direction="out", source="biometric device")
        session.commit()
        day = attendance_day(session, ana, on=date(2026, 6, 8))
        assert day["shift"] == "DAY" and day["status"] == "working"
        assert day["pairs"] == [
            {"in": "2026-06-08T09:02:00+00:00", "out": "2026-06-08T18:05:00+00:00", "minutes": 543}
        ], day["pairs"]
        assert day["break_minutes"] == 60 and day["break_deducted"] == 60
        assert day["worked_minutes"] == 483, day
        assert day["derivation"] == (
            "543 minutes over 1 matched pair(s) minus a 60 minute break from DAY shift = 483"
        ), day["derivation"]
        assert day["exceptions"] == 0
        print(f"the day derives {day['worked_minutes']} minutes: {day['derivation']}")

        # 2 — the same punch arriving twice is one punch, and the hours do not move
        _refuses(
            lambda: record_punch(
                session, ana, at="2026-06-08T09:02:00+00:00", direction="in", source="biometric device"
            ),
            DuplicatePunchError,
            "a punch delivered twice",
        )
        session.rollback()
        assert attendance_day(session, ana, on=date(2026, 6, 8))["worked_minutes"] == 483, (
            "the duplicate attempt changed the day"
        )
        message = _refused(
            lambda: (
                session.execute(
                    text(
                        "INSERT INTO attendance_event"
                        " (id, company_id, employee_id, at, direction, source)"
                        " VALUES (:id, :company, :employee, '2026-06-08T09:02:00+00', 'in',"
                        " 'biometric device')"
                    ),
                    {"id": uuid.uuid4(), "company": company_id, "employee": ana.id},
                ),
                session.commit(),
            ),
            "uq_attendance_event_punch",
        )
        session.rollback()
        print(f"a punch delivered twice is refused by the service and the database ({message.splitlines()[0]})")

        # 3 — the exceptions, each reported with why, none of them counting as hours
        odd = date(2026, 6, 10)
        record_punch(session, leo, at="2026-06-10T08:00:00+00:00", direction="out", source="biometric device")
        record_punch(session, leo, at="2026-06-10T09:05:00+00:00", direction="in", source="biometric device")
        record_punch(session, leo, at="2026-06-10T09:40:00+00:00", direction="in", source="biometric device")
        record_punch(session, leo, at="2026-06-10T17:30:00+00:00", direction="out", source="biometric device")
        session.commit()
        odd_day = attendance_day(session, leo, on=odd)
        reasons = [row["why"] for row in odd_day["unmatched"]]
        assert reasons == [
            "an out with no in before it on this day",
            "an in with no out before the next in",
        ], reasons
        assert [pair["minutes"] for pair in odd_day["pairs"]] == [470], odd_day["pairs"]
        # One pair, no rostered shift (Leo has no roster), so no break is deducted and the
        # unmatched punches add nothing.
        assert odd_day["break_deducted"] == 0 and odd_day["worked_minutes"] == 470, odd_day
        assert odd_day["shift"] is None and odd_day["exceptions"] == 2
        print(
            "the exceptions are reported rather than dropped ("
            + "; ".join(reasons)
            + f") and contribute no hours: {odd_day['worked_minutes']} minutes from the one pair"
        )
        # …and an in with no out at all, at the end of a day.
        record_punch(session, leo, at="2026-06-11T09:00:00+00:00", direction="in", source="manual",
                     actor="leo", reason="forgot to punch out yesterday")
        session.commit()
        tail = attendance_day(session, leo, on=date(2026, 6, 11))
        assert [row["why"] for row in tail["unmatched"]] == [
            "an in with no out at the end of the day"
        ], tail["unmatched"]
        assert tail["worked_minutes"] == 0 and tail["pairs"] == []

        # 4 — a manual punch is a correction: who and why are required, and it is audited
        _refuses(
            lambda: record_punch(
                session, leo, at="2026-06-11T18:00:00+00:00", direction="out", source="manual", actor="leo"
            ),
            InvalidPunchError,
            "a manual punch with no reason",
        )
        session.rollback()
        _refuses(
            lambda: record_punch(
                session, leo, at="2026-06-11T18:00:00+00:00", direction="out"
            ),
            InvalidPunchError,
            "a manual punch that states neither actor nor reason",
        )
        session.rollback()
        message = _refused(
            lambda: (
                session.execute(
                    text(
                        "INSERT INTO attendance_event"
                        " (id, company_id, employee_id, at, direction, source, actor)"
                        " VALUES (:id, :company, :employee, '2026-06-11T18:00:00+00', 'out',"
                        " 'manual', 'leo')"
                    ),
                    {"id": uuid.uuid4(), "company": company_id, "employee": leo.id},
                ),
                session.commit(),
            ),
            "ck_attendance_event_manual_is_attributed",
        )
        session.rollback()
        corrected = record_punch(
            session,
            leo,
            at="2026-06-11T18:00:00+00:00",
            direction="out",
            source="manual",
            actor="leo",
            reason="the device was offline; the supervisor confirmed the time",
        )
        session.commit()
        after = attendance_day(session, leo, on=date(2026, 6, 11))
        assert after["worked_minutes"] == 540 and after["pairs"][0]["minutes"] == 540, after
        assert after["exceptions"] == 0, "the correction did not close the unmatched arrival"
        assert corrected.actor == "leo" and "offline" in corrected.reason
        assert corrected.source == "manual"
        print(
            "a manual correction names who and why (both refused when blank, both enforced by"
            f" the database) and the day now derives {after['worked_minutes']} minutes from"
            " 09:00 to 18:00"
        )

        # 5 — a recorded punch is never rewritten
        message = _refused(
            lambda: (
                session.execute(
                    text("UPDATE attendance_event SET at = '2026-06-11T19:00:00+00' WHERE id = :id"),
                    {"id": corrected.id},
                ),
                session.commit(),
            ),
            "is append-only",
        )
        session.rollback()
        print(f"the punch is append-only: {message.splitlines()[0]}")

        # Outside employment, nothing is captured; a day with no shift derives no break.
        _refuses(
            lambda: record_punch(
                session,
                leo,
                at="2026-05-01T09:00:00+00:00",
                direction="in",
                source="biometric device",
            ),
            NotEmployedError,
            "a punch before the employee was hired",
        )
        session.rollback()
        record_movement(
            session, leo, kind="exit", effective_date="2026-06-30", reason="resigned", actor="hr"
        )
        session.commit()
        _refuses(
            lambda: record_punch(
                session,
                leo,
                at="2026-07-01T09:00:00+00:00",
                direction="in",
                source="biometric device",
            ),
            NotEmployedError,
            "a punch after the exit",
        )
        session.rollback()
        _refuses(
            lambda: record_punch(session, ana, at="2026-06-12T09:00:00+00:00", direction="sideways"),
            InvalidPunchError,
            "a punch that is neither in nor out",
        )
        session.rollback()
        # The rest day (no shift) derives its hours with nothing deducted.
        record_punch(session, ana, at="2026-06-09T09:00:00+00:00", direction="in", source="biometric device")
        record_punch(session, ana, at="2026-06-09T12:00:00+00:00", direction="out", source="biometric device")
        session.commit()
        rest = attendance_day(session, ana, on=DAY)
        assert rest["shift"] is None and rest["rostered_shift"] == "DAY" and rest["overridden"]
        assert rest["break_deducted"] == 0 and rest["worked_minutes"] == 180, rest
        print(
            "a day with no shift still derives what was worked (180 minutes, nothing deducted)"
            " and is reported as the override that made it a rest day"
        )

    engine.dispose()
    print("ok — punches are appended, hours are derived with their workings shown, and every exception is reported")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
