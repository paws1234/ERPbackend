"""T-4.WC.01 check — two work centres, their downtime, and a dated rate change.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_work_centers.py

Green on all five:

1. **capacity is per period and the period is stated** — a day-rated centre and a
   week-rated one report different figures *and* the unit each is in
2. **downtime reduces the effective capacity** everything downstream loads (10 % of a
   480-minute day is 432 minutes of usable time)
3. **a rate change is dated and does not restate a past job's cost**: an hour on the
   March rate is still worth what it was worth after a July rate is added, and a
   second rate for the same date is refused rather than overwriting the first
4. **a zero capacity and a zero rate are refused**, each naming the field — an
   unstated figure is not a price of nothing
5. a centre nobody had rated yet has **no cost to state** rather than a guessed one,
   and the dated history is readable
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.manufacturing.work_centers import (  # noqa: E402
    RateAlreadyDatedError,
    WorkCenterError,
    capacity_of,
    create_work_center,
    effective_capacity_minutes,
    rate_history,
    rate_on,
    set_rate,
    work_center_by_code,
)

COMPANY = uuid.uuid4()
JANUARY = date(2026, 1, 1)
MARCH = date(2026, 3, 15)
JULY = date(2026, 7, 1)
AUGUST = date(2026, 8, 1)


def _refused(call, expected: type[Exception] | str) -> str:
    try:
        call()
    except Exception as exc:  # noqa: BLE001 — the type and the message are the point
        if isinstance(expected, str):
            assert expected in str(exc), f"unclear refusal: {exc}"
        else:
            assert isinstance(exc, expected), f"refused with {type(exc).__name__}: {exc}"
        return str(exc)
    raise AssertionError("accepted what it must refuse")


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

    with Session(engine) as session:
        session.add(
            Company(
                id=COMPANY,
                code="WC-CHECK",
                name="Work centre check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()

        # 1 — two centres, different capacities, stated per different periods
        cutting = create_work_center(
            session, company_id=COMPANY, code="CUT", name="Cutting bench",
            capacity_minutes="480", capacity_period="day", downtime_percent="10",
        )
        welding = create_work_center(
            session, company_id=COMPANY, code="WELD", name="Welding bay",
            capacity_minutes="2400", capacity_period="week", downtime_percent="0",
        )
        session.commit()
        cut_view = capacity_of(session, cutting, on=MARCH)
        weld_view = capacity_of(session, welding, on=MARCH)
        assert cut_view["capacity_period"] == "day", cut_view
        assert weld_view["capacity_period"] == "week", weld_view
        assert (cut_view["capacity_minutes"], weld_view["capacity_minutes"]) == (
            Decimal("480.000000"),
            Decimal("2400.000000"),
        ), (cut_view, weld_view)
        assert welding.capacity_minutes != cutting.capacity_minutes, "the two are the same centre"
        print(
            f"1. CUT is {cut_view['capacity_minutes']} minutes a"
            f" {cut_view['capacity_period']} and WELD is {weld_view['capacity_minutes']}"
            f" a {weld_view['capacity_period']} — the figure is reported with the unit it"
            " is in, so neither is read as the other"
        )

        # 2 — downtime is what downstream loads
        assert effective_capacity_minutes(cutting) == Decimal("432.000000"), (
            effective_capacity_minutes(cutting)
        )
        assert effective_capacity_minutes(welding) == Decimal("2400.000000"), (
            effective_capacity_minutes(welding)
        )
        assert cut_view["effective_capacity_minutes"] == Decimal("432.000000"), cut_view
        print(
            f"2. a {cut_view['downtime_percent']} % downtime allowance leaves CUT"
            f" {cut_view['effective_capacity_minutes']} minutes of the"
            f" {cut_view['capacity_minutes']} it is manned — the figure WC.02 loads, and"
            " WELD, which loses none, is unchanged"
        )

        # 3 — a dated rate change, and the past it does not touch
        set_rate(session, cutting, effective_from=JANUARY, hourly_rate="250.00")
        set_rate(session, welding, effective_from=JANUARY, hourly_rate="410.00")
        session.commit()
        march_hours = Decimal("2")
        job_before = (march_hours * rate_on(session, cutting, on=MARCH)).quantize(
            Decimal("0.000001")
        )
        said_twice = _refused(
            lambda: set_rate(session, cutting, effective_from=JANUARY, hourly_rate="260.00"),
            RateAlreadyDatedError,
        )
        session.rollback()
        cutting = work_center_by_code(session, company_id=COMPANY, code="CUT")
        set_rate(session, cutting, effective_from=JULY, hourly_rate="310.00")
        session.commit()
        cutting = work_center_by_code(session, company_id=COMPANY, code="CUT")
        job_after = (march_hours * rate_on(session, cutting, on=MARCH)).quantize(
            Decimal("0.000001")
        )
        assert job_before == job_after == Decimal("500.000000"), (job_before, job_after)
        assert rate_on(session, cutting, on=AUGUST) == Decimal("310.000000"), (
            rate_on(session, cutting, on=AUGUST)
        )
        assert rate_on(session, welding, on=MARCH) == Decimal("410.000000"), (
            rate_on(session, welding, on=MARCH)
        )
        print(
            f"3. CUT was rated 250.00 from {JANUARY} and 310.00 from {JULY}; the"
            f" {march_hours}-hour job finished in March is worth {job_after} at both"
            f" moments, work after July is priced at"
            f" {rate_on(session, cutting, on=AUGUST)}, and a second rate for a date"
            f" already rated was refused ({said_twice[:44]}…)"
        )

        # 4 — an unstated figure is not a figure of nothing
        said_capacity = _refused(
            lambda: create_work_center(
                session, company_id=COMPANY, code="EMPTY", name="Unmanned",
                capacity_minutes="0", capacity_period="day",
            ),
            WorkCenterError,
        )
        session.rollback()
        said_rate = _refused(
            lambda: set_rate(
                session, work_center_by_code(session, company_id=COMPANY, code="CUT"),
                effective_from=date(2027, 1, 1), hourly_rate="0",
            ),
            WorkCenterError,
        )
        session.rollback()
        said_period = _refused(
            lambda: create_work_center(
                session, company_id=COMPANY, code="VAGUE", name="Unstated period",
                capacity_minutes="100", capacity_period="fortnight",
            ),
            WorkCenterError,
        )
        session.rollback()
        assert "nobody can load" in said_capacity, said_capacity
        assert "above zero" in said_rate, said_rate
        assert "per one of day, week, month" in said_period, said_period
        print(
            f"4. a capacity of zero ({said_capacity[:40]}…), a rate of zero"
            f" ({said_rate[:40]}…) and a period nobody recognises"
            f" ({said_period[:40]}…) are each refused, so an unstated figure cannot pass"
            " for a decision"
        )

        # 5 — nothing stated yet is nothing to state, and the history is readable
        fresh = create_work_center(
            session, company_id=COMPANY, code="PAINT", name="Paint booth",
            capacity_minutes="300", capacity_period="day",
        )
        session.commit()
        assert rate_on(session, fresh, on=MARCH) is None, rate_on(session, fresh, on=MARCH)
        assert capacity_of(session, fresh)["hourly_rate"] is None, capacity_of(session, fresh)
        cutting = work_center_by_code(session, company_id=COMPANY, code="CUT")
        history = rate_history(session, cutting)
        assert history == [
            {"effective_from": JANUARY, "hourly_rate": Decimal("250.000000")},
            {"effective_from": JULY, "hourly_rate": Decimal("310.000000")},
        ], history
        print(
            f"5. a centre nobody has rated yet states no cost rather than a guessed one,"
            f" and CUT's"
            f" history reads {[row['effective_from'].isoformat() for row in history]} —"
            " one rate per date, both still on the record"
        )

    print("\ncheck_work_centers: all assertions green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
