"""T-5.LEAVE.01 check — the holiday calendar: seeded from the pack, resolved once, closed periods refused.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_holiday_calendar.py

It fails (non-zero exit) if any of these stops holding:

1. **the calendar is seeded from the localization pack** — the Philippines pack's own dated
   days (16 of them, `regular` and `special`) land in the calendar, a re-seed adds nothing
   and edits nothing, and a day the company states itself is not overwritten by the pack
2. **a day's status resolves deterministically, once, for attendance, leave and payroll** —
   `day_status` answers `holiday` on a seeded day and `working` on the day after, a region's
   own entry wins over the market's, a region with no entry of its own still gets the
   market's, and a day nobody stated is worked
3. **one day is one entry** — a second entry for the same company, region and day is refused
   by the service *and* by the database (the unique index is `NULLS NOT DISTINCT`, so two
   market-wide rows cannot both exist), and an unknown kind is refused by name
4. **a closed period is not restated** — adding or removing a holiday inside a month the
   ledger has locked (T-1.ACCT.03) is refused, so a reported period cannot be changed by
   editing the calendar underneath it; the same change in an open month is allowed and
   **attributable** on the T-0.AUDIT.02 trail

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
from app.hr.holidays import (  # noqa: E402
    HOLIDAY,
    WORKING,
    ClosedPeriodError,
    DuplicateHolidayError,
    Holiday,
    InvalidHolidayError,
    day_status,
    holiday_on,
    is_working_day,
    remove_holiday,
    seed_calendar,
    state_holiday,
)
from app.ledger import posting  # noqa: E402,F401 — every check builds the one schema
from app.ledger.periods import lock_period  # noqa: E402
from app.localization import holidays  # noqa: E402

# The pack is the source, so the count is the year's own days rather than the whole file:
PACK_YEAR = 2026
HOLIDAYS_IN_PACK = len(holidays("philippines", PACK_YEAR))


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
                code="CAL-CHECK",
                name="Holiday calendar check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()
        set_actor(session, "hr")

        # 1 — the pack is the source, and seeding it twice is seeding it once
        seeded = seed_calendar(session, company_id=company_id, market="philippines", year=2026)
        session.commit()
        assert len(seeded) == HOLIDAYS_IN_PACK, (
            f"the pack carries {HOLIDAYS_IN_PACK} days and {len(seeded)} were seeded"
        )
        assert session.scalar(select(func.count()).select_from(Holiday)) == HOLIDAYS_IN_PACK
        new_year = holiday_on(session, company_id=company_id, on=date(2026, 1, 1))
        assert new_year is not None and new_year.name == "New Year's Day"
        assert new_year.kind == "regular" and new_year.region is None
        again = seed_calendar(session, company_id=company_id, market="philippines", year=2026)
        session.commit()
        assert again == [], "a re-seed added days that were already there"
        assert session.scalar(select(func.count()).select_from(Holiday)) == HOLIDAYS_IN_PACK
        # A day the company renamed is left exactly as it is: a re-seed is not an edit.
        new_year.name = "New Year's Day (company shutdown)"
        session.commit()
        seed_calendar(session, company_id=company_id, market="philippines", year=2026)
        session.commit()
        assert (
            holiday_on(session, company_id=company_id, on=date(2026, 1, 1)).name
            == "New Year's Day (company shutdown)"
        ), "a re-seed overwrote a day somebody had stated"
        print(
            f"{len(seeded)} days seeded from the Philippines pack for 2026, a re-seed adding"
            " nothing and editing nothing"
        )

        # 2 — one resolver, and a region's own day wins over the market's
        assert day_status(session, company_id=company_id, on=date(2026, 1, 1)) == HOLIDAY
        assert day_status(session, company_id=company_id, on=date(2026, 1, 2)) == WORKING
        assert is_working_day(session, company_id=company_id, on=date(2026, 11, 2))
        # A region's own foundation day, on a date the market does not observe.
        state_holiday(
            session,
            company_id=company_id,
            on="2026-02-24",
            name="Cebu Charter Day",
            kind="special",
            region="CEBU",
        )
        session.commit()
        assert day_status(session, company_id=company_id, on=date(2026, 2, 24), region="CEBU") == HOLIDAY
        assert day_status(session, company_id=company_id, on=date(2026, 2, 24)) == WORKING, (
            "a region's own day was read as the whole market's"
        )
        # And a region with no entry of its own still gets the market's.
        assert day_status(session, company_id=company_id, on=date(2026, 12, 25), region="CEBU") == HOLIDAY
        assert holiday_on(
            session, company_id=company_id, on=date(2026, 12, 25), region="CEBU"
        ).region is None
        # A region may not restate a day the market already observes: it is already a
        # non-working day there, and a second entry would only be a second name for it.
        _refuses(
            lambda: state_holiday(
                session,
                company_id=company_id,
                on="2026-12-25",
                name="Pasko sa Cebu",
                kind="regular",
                region="CEBU",
            ),
            DuplicateHolidayError,
            "a region restating a day the market already observes",
        )
        session.rollback()
        print(
            "a day resolves the same way every time, a region's own entry wins where it has"
            " one, and it still gets the market's where it does not"
        )

        # 3 — one day, one entry
        _refuses(
            lambda: state_holiday(
                session, company_id=company_id, on="2026-01-01", name="New Year again"
            ),
            DuplicateHolidayError,
            "a second market-wide entry for one day",
        )
        session.rollback()
        _refuses(
            lambda: state_holiday(
                session,
                company_id=company_id,
                on="2026-01-02",
                name="A day nobody proclaimed",
                kind="public",
            ),
            InvalidHolidayError,
            "a holiday kind nobody defined",
        )
        session.rollback()
        # …and the database holds the same rule, including for the market-wide rows that a
        # plain unique index would let through (NULLs are distinct by default in Postgres).
        message = _refused(
            lambda: (
                session.execute(
                    text(
                        "INSERT INTO holiday (id, company_id, region, holiday_date, name, kind)"
                        " VALUES (:id, :company, NULL, '2026-01-01', 'A second New Year', 'regular')"
                    ),
                    {"id": uuid.uuid4(), "company": company_id},
                ),
                session.commit(),
            ),
            "uq_holiday_company_region_day",
        )
        session.rollback()
        print(f"one day is one entry, by the service and by the database: {message.splitlines()[0]}")

        # 4 — a closed month is not restated by editing the calendar underneath it
        # January is open: the calendar can still be changed, and the change is recorded.
        lock_period(session, company_id=company_id, year=2026, month=1, actor="cfo")
        session.commit()
        _refuses(
            lambda: state_holiday(
                session,
                company_id=company_id,
                on="2026-01-02",
                name="A day added after the month closed",
            ),
            ClosedPeriodError,
            "a holiday added inside a closed month",
        )
        session.rollback()
        january_day = holiday_on(session, company_id=company_id, on=date(2026, 1, 1))
        _refuses(
            lambda: remove_holiday(session, january_day),
            ClosedPeriodError,
            "removing a holiday inside a closed month",
        )
        session.rollback()
        extra = state_holiday(
            session,
            company_id=company_id,
            on="2026-07-15",
            name="A shutdown day decided in the open",
            kind="special",
        )
        session.commit()
        remove_holiday(session, extra)
        session.commit()
        assert holiday_on(session, company_id=company_id, on=date(2026, 7, 15)) is None
        # Both the addition and the removal are on the trail, so the calendar's changes are
        # attributable even though they are not prevented.
        trail = list(
            session.scalars(
                select(AuditLog).where(
                    AuditLog.entity == "holiday", AuditLog.company_id == company_id
                )
            )
        )
        actions = sorted({entry.action for entry in trail})
        assert "insert" in actions and "delete" in actions, f"the calendar's changes: {actions}"
        print(
            "January is locked: a day added or removed inside it is refused, while the same"
            f" change in an open month stands and is on the trail ({', '.join(actions)})"
        )

    engine.dispose()
    print("ok — the calendar comes from the pack, one resolver answers every consumer, and a closed period is not restated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
