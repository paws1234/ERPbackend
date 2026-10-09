"""T-5.ATT.03 check — overtime bands and lateness, from rules that are dated rows.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_overtime.py

It fails (non-zero exit) if any of these stops holding:

1. **overtime is separated into the configured bands, with the band shown per occurrence** — a
   week with ordinary overtime on a working day, all-worked overtime on a rest day and work
   on a holiday comes back banded, each day naming the rule and the multiplier it was priced
   at, and the threshold each band states is applied to its own band
2. **lateness respects the grace and is not an absence** — an arrival inside the grace is not
   late at all, an arrival after it is late by the difference, and either way the day is
   `worked` with its hours counted (nothing here turns lateness into absence)
3. **holiday work is classified according to the calendar** — the pack's Independence Day is a
   `holiday` band even when an override deliberately puts work on it, and it is priced at the
   holiday rate rather than the working one
4. **rule changes are dated, and a past period is not restated** — the multiplier in force on
   a day is read from the rules as of *that* day, a rate raised from a later date leaves June
   exactly as it was, and the database refuses to rewrite a recorded rule
5. **the rules are configuration, not code** — an unknown band, a multiplier that is not a
   positive exact decimal, and a rule that does not follow the band's last one are all refused
   by name; and a band with **no** rule is reported as unclassified rather than paid as
   ordinary time

**Scratch database only**: it drops and recreates the schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.audit import set_actor  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.hr.attendance import record_punch  # noqa: E402
from app.hr.employees import create_employee  # noqa: E402
from app.hr.holidays import seed_calendar  # noqa: E402
from app.hr.overtime import (  # noqa: E402
    InvalidRuleError,
    RuleSequenceError,
    classify_day,
    classify_week,
    rule_in_force,
    state_rule,
)
from app.hr.shifts import (  # noqa: E402
    define_shift,
    override_day,
    resolved_shift,
    roster_employee,
)
from app.ledger import posting  # noqa: E402,F401 — every check builds the one schema

WEEK = date(2026, 6, 8)  # a Monday; 2026-06-12 is Independence Day in the pack
MON, TUE, WED, THU, FRI, SAT, SUN = (WEEK.replace(day=8 + offset) for offset in range(7))


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
                code="OT-CHECK",
                name="Overtime check",
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
            number="E-401",
            hire_date="2026-06-08",
            subject="hr",
            name="Ana Reyes",
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
        # Wednesday and Saturday are overridden: a deliberate rest day, and a holiday worked.
        override_day(
            session, ana, on=WED, shift=None, actor="scheduling", reason="rest day"
        )
        override_day(
            session,
            ana,
            on=FRI,
            shift=shift,
            actor="scheduling",
            reason="year-end close: the store opens on Independence Day",
        )
        session.commit()

        # 5 — the rules are configuration, and each refusal is named
        _refuses(
            lambda: state_rule(
                session, company_id=company_id, day_type="weekend", effective_from="2026-06-01", multiplier="1.3"
            ),
            InvalidRuleError,
            "an overtime band nobody defined",
        )
        session.rollback()
        for bad in ("0", "-1.5", "1.5.2"):
            _refuses(
                lambda bad=bad: state_rule(
                    session, company_id=company_id, day_type="working", effective_from="2026-06-01", multiplier=bad
                ),
                InvalidRuleError,
                f"a multiplier of {bad!r}",
            )
            session.rollback()
        _refuses(
            lambda: state_rule(
                session,
                company_id=company_id,
                day_type="working",
                effective_from="2026-06-01",
                multiplier=1.25,
            ),
            InvalidRuleError,
            "a float multiplier",
        )
        session.rollback()

        # The bands, priced from 2026-06-01 in the market's own numbers (stated, not invented).
        state_rule(
            session,
            company_id=company_id,
            day_type="working",
            effective_from="2026-06-01",
            multiplier="1.250",
            threshold_minutes=0,
            note="ordinary overtime",
        )
        state_rule(
            session,
            company_id=company_id,
            day_type="holiday",
            effective_from="2026-06-01",
            multiplier="1.500",
            threshold_minutes=0,
            note="regular holiday",
        )
        session.commit()
        _refuses(
            lambda: state_rule(
                session,
                company_id=company_id,
                day_type="working",
                effective_from="2026-06-01",
                multiplier="1.300",
            ),
            RuleSequenceError,
            "a rate backdated onto the day the band's rule already took effect",
        )
        session.rollback()
        print("the bands are rows: a working rate of 1.250 and a holiday rate of 1.500 from 2026-06-01")

        # 1 + 2 — a week: ordinary overtime, a late arrival inside and outside the grace,
        # a rest day, and a holiday worked
        punches = {
            MON: ("09:02", "18:35"),  # 573 gross − 60 break = 513 worked: 33 minutes overtime
            # (two minutes late, inside the shift's ten minute grace)
            TUE: ("09:08", "18:00"),  # inside the 10 minute grace: not late
            WED: ("09:00", "13:00"),  # a rest day: 240 worked, all of it overtime
            THU: ("09:25", "18:00"),  # 15 minutes late after the grace
            FRI: ("09:00", "12:30"),  # a holiday worked: 210, at the holiday rate
        }
        for day, (arrived, left) in punches.items():
            record_punch(
                session, ana, at=f"{day.isoformat()}T{arrived}:00+00:00",
                direction="in", source="biometric device",
            )
            record_punch(
                session, ana, at=f"{day.isoformat()}T{left}:00+00:00",
                direction="out", source="biometric device",
            )
        session.commit()

        week = {row["date"]: row for row in classify_week(session, ana, starts_on=WEEK)}
        monday = week[MON.isoformat()]
        assert monday["day_type"] == "working" and monday["worked_minutes"] == 513
        assert monday["scheduled_minutes"] == 480 and monday["overtime_minutes"] == 33
        assert monday["band"] == "working" and monday["multiplier"] == "1.250"
        assert monday["rule_effective_from"] == "2026-06-01"
        assert monday["late_minutes"] == 0, "an arrival two minutes late is inside the grace"
        assert monday["absence"] is False and monday["late_grace_minutes"] == 10

        tuesday = week[TUE.isoformat()]
        assert tuesday["late_minutes"] == 0, "an arrival inside the grace was counted late"
        assert tuesday["worked_minutes"] == 480 - 8, tuesday

        wednesday = week[WED.isoformat()]
        assert wednesday["day_type"] == "rest" and wednesday["shift"] is None
        assert wednesday["scheduled_minutes"] == 0 and wednesday["worked_minutes"] == 240
        assert wednesday["overtime_minutes"] == 0, (
            "a band with no rule in force was paid as overtime anyway"
        )
        assert wednesday["band"] is None and wednesday["unclassified_overtime_minutes"] == 240
        assert "no rest overtime rule is in force" in wednesday["unclassified_reason"]

        thursday = week[THU.isoformat()]
        assert thursday["late_minutes"] == 15, "lateness did not respect the 10 minute grace"
        assert thursday["absence"] is False, "lateness was turned into an absence"
        assert thursday["worked_minutes"] == 455, thursday
        assert thursday["overtime_minutes"] == 0, (
            "an hour late and no overtime: the day is short, not premium"
        )

        friday = week[FRI.isoformat()]
        assert friday["status"] == "holiday" and friday["day_type"] == "holiday"
        assert friday["shift"] == "DAY" and friday["overridden"], friday
        assert friday["worked_minutes"] == 150 and friday["scheduled_minutes"] == 0, friday
        assert friday["overtime_minutes"] == 150, (
            "work on a holiday was measured against an ordinary schedule"
        )
        assert friday["band"] == "holiday" and friday["multiplier"] == "1.500", (
            "work on a holiday was priced at the ordinary rate"
        )
        assert week[SAT.isoformat()]["worked_minutes"] == 0
        print(
            "the week is banded: Mon 33 at 1.250 (2 minutes late, inside the grace), Tue 0 late,"
            " Wed 240 rest-day minutes unclassified (no rule stated yet), Thu 15 late with no"
            " overtime (the day is short, not premium), Fri 150 at 1.500 on the holiday — and not"
            " one of them an absence"
        )

        # The rest band is configuration, so stating it classifies Wednesday without code.
        state_rule(
            session,
            company_id=company_id,
            day_type="rest",
            effective_from="2026-06-01",
            multiplier="1.300",
            threshold_minutes=15,
            note="rest day, after a 15 minute threshold",
        )
        session.commit()
        restated = classify_day(session, ana, on=WED)
        assert restated["band"] == "rest" and restated["multiplier"] == "1.300"
        assert restated["overtime_minutes"] == 225, (
            "the band's own threshold was not applied to its own band"
        )
        assert restated["unclassified_overtime_minutes"] == 0
        print("stating the rest band classifies the same day as 225 minutes at 1.300 (240 less its 15 minute threshold)")

        # 4 — a rate raised later does not re-price June, and a rule is never rewritten
        raised = state_rule(
            session,
            company_id=company_id,
            day_type="working",
            effective_from="2026-07-01",
            multiplier="1.500",
            note="raised for the second half of the year",
        )
        session.commit()
        assert rule_in_force(
            session, company_id=company_id, day_type="working", on=MON
        ).multiplier == Decimal("1.250"), "a later rate applies to an earlier day"
        assert rule_in_force(
            session, company_id=company_id, day_type="working", on=date(2026, 7, 15)
        ).multiplier == Decimal("1.500")
        again = classify_day(session, ana, on=MON)
        assert again["multiplier"] == "1.250" and again["rule_effective_from"] == "2026-06-01", (
            "raising the rate restated a day that had already been classified"
        )
        message = _refused(
            lambda: (
                session.execute(
                    text("UPDATE overtime_rule SET multiplier = 2 WHERE id = :id"),
                    {"id": raised.id},
                ),
                session.commit(),
            ),
            "is append-only",
        )
        session.rollback()
        print(
            "the July rate leaves June's day at 1.250 (its own rule named), and the database"
            f" refuses to rewrite the rule ({message.splitlines()[0]})"
        )

    engine.dispose()
    print("ok — overtime is banded from dated configuration, and lateness respects the grace without becoming absence")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
