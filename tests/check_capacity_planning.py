"""T-4.WC.02 check — the load chart, one overloaded period and one that is not.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_capacity_planning.py

Green on all five:

1. **the load equals the operation times of the orders assigned to the centre**,
   hand-checked against the source work orders, with one period **overloaded** and the
   next **underloaded** against the same capacity
2. capacity is stated **per period and prorated to the bucket** — a week-rated centre
   read over one day gets a seventh of its week, and the report says so
3. an **unassigned operation** and one naming a **centre nobody registered** are both
   reported with their minutes rather than quietly left off the chart
4. a **completed order stops loading** its centre: the chart is of work still to do
5. the calculation is **reproducible** from the work orders — the same horizon answers
   the same figures, twice
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
from app.manufacturing.bom import add_line, create_bom, release  # noqa: E402
from app.manufacturing.capacity import capacity_profile, load_for  # noqa: E402
from app.manufacturing.routing import add_operation  # noqa: E402
from app.manufacturing.work_centers import create_work_center  # noqa: E402
from app.manufacturing.work_orders import (  # noqa: E402
    CLOSED,
    COMPLETED,
    IN_PROGRESS,
    RELEASED,
    advance,
    create_work_order,
)
from app.stock.items import create_item  # noqa: E402

COMPANY = uuid.uuid4()
DAY_ONE = date(2026, 9, 10)
DAY_TWO = date(2026, 9, 11)
DAY_THREE = date(2026, 9, 12)


def _row(profile: dict, *, center: str, on: date) -> dict:
    rows = [
        row
        for row in profile["periods"]
        if row["work_center"] == center and row["period_start"] == on
    ]
    assert rows, f"no row for {center} on {on}"
    return rows[0]


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
                code="CAP-CHECK",
                name="Capacity check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()

        # A cutting bench manned 480 minutes a day and 10 % of it down: 432 minutes.
        create_work_center(
            session, company_id=COMPANY, code="CUT", name="Cutting bench",
            capacity_minutes="480", capacity_period="day", downtime_percent="10",
        )
        # A bay manned 1400 minutes a week with no downtime: 200 minutes a day.
        create_work_center(
            session, company_id=COMPANY, code="WELD", name="Welding bay",
            capacity_minutes="1400", capacity_period="week",
        )
        widget = create_item(
            session, company_id=COMPANY, sku="WIDGET", name="Widget",
            base_uom="each", traceability_mode="none",
        )
        blank = create_item(
            session, company_id=COMPANY, sku="BLANK", name="Blank",
            base_uom="each", traceability_mode="none",
        )
        raw = create_item(
            session, company_id=COMPANY, sku="RAW", name="Raw stock",
            base_uom="each", traceability_mode="none",
        )
        session.commit()
        bom = create_bom(session, company_id=COMPANY, item=widget)
        add_line(session, bom, item=blank, quantity="1")
        # Step 1 cuts (15 minutes to set up, 10 a unit); step 2 welds (15 + 3 a unit).
        add_operation(session, bom, name="Cut", work_center_code="CUT",
                      setup_minutes="15", run_minutes="10")
        add_operation(session, bom, name="Weld", work_center_code="WELD",
                      setup_minutes="15", run_minutes="3")
        release(session, bom)
        session.commit()

        def order(number: str, quantity: str, due: date):
            made = create_work_order(
                session, company_id=COMPANY, item=widget, quantity=quantity,
                number=number, created_on=date(2026, 9, 1), due_on=due,
            )
            session.commit()
            return made

        # Two orders land on the 10th and one on the 11th.
        first = order("WO-A", "20", DAY_ONE)
        second = order("WO-B", "40", DAY_ONE)
        third = order("WO-C", "20", DAY_TWO)
        profile = capacity_profile(
            session, company_id=COMPANY, start=DAY_ONE, end=DAY_THREE, bucket_days=1
        )

        # 1 — the load is the orders' own operation times, hand-checked
        # CUT on the 10th: (15 + 10×20) + (15 + 10×40) = 215 + 415 = 630 against 432.
        # CUT on the 11th: 15 + 10×20 = 215 against 432. WELD on the 10th:
        # (15 + 3×20) + (15 + 3×40) = 75 + 135 = 210 against a seventh of 1400 = 200.
        overloaded = _row(profile, center="CUT", on=DAY_ONE)
        underloaded = _row(profile, center="CUT", on=DAY_TWO)
        welded = _row(profile, center="WELD", on=DAY_ONE)
        assert overloaded["load_minutes"] == Decimal("630.000000"), overloaded
        assert overloaded["capacity_minutes"] == Decimal("432.000000"), overloaded
        assert overloaded["overloaded"] is True, overloaded
        assert underloaded["load_minutes"] == Decimal("215.000000"), underloaded
        assert underloaded["overloaded"] is False, underloaded
        assert welded["load_minutes"] == Decimal("210.000000"), welded
        assert sorted(row["work_order"] for row in overloaded["orders"]) == ["WO-A", "WO-B"], (
            overloaded["orders"]
        )
        assert all(row["dated_by"] == "due_on" for row in overloaded["orders"]), overloaded
        assert profile["overloaded_periods"] == 2, profile["overloaded_periods"]
        print(
            f"1. CUT on {DAY_ONE} carries {overloaded['load_minutes']} minutes against"
            f" {overloaded['capacity_minutes']} — overloaded by"
            f" {overloaded['load_minutes'] - overloaded['capacity_minutes']}, and by hand"
            f" (15+10×20) + (15+10×40) — while {DAY_TWO} carries"
            f" {underloaded['load_minutes']} and is not; WELD on {DAY_ONE} takes"
            f" {welded['load_minutes']}"
        )

        # 2 — a period longer than the bucket is prorated, and says where it came from
        assert welded["capacity_period"] == "week", welded
        assert welded["capacity_gross_minutes"] == Decimal("1400.000000"), welded
        assert welded["capacity_minutes"] == Decimal("200.000000"), welded
        weekly = capacity_profile(
            session, company_id=COMPANY, start=DAY_ONE, end=DAY_ONE, bucket_days=7
        )
        week_row = _row(weekly, center="WELD", on=DAY_ONE)
        assert week_row["days"] == 1, week_row
        assert week_row["capacity_minutes"] == Decimal("200.000000"), week_row
        print(
            f"2. WELD is manned {welded['capacity_gross_minutes']} minutes a"
            f" {welded['capacity_period']} and contributes"
            f" {welded['capacity_minutes']} to a one-day bucket — a seventh of the week,"
            " stated with the gross figure and the period it came from rather than"
            " implied"
        )

        # 3 — an operation with nowhere to load is reported, not dropped
        loose = create_bom(session, company_id=COMPANY, item=blank)
        add_line(session, loose, item=raw, quantity="1")
        add_operation(session, loose, name="Inspect", setup_minutes="5", run_minutes="1")
        add_operation(session, loose, name="Ghost step", work_center_code="GHOST",
                      setup_minutes="0", run_minutes="1")
        release(session, loose)
        session.commit()
        stray = create_work_order(
            session, company_id=COMPANY, item=blank, quantity="10", number="WO-D",
            created_on=date(2026, 9, 1), due_on=DAY_ONE,
        )
        session.commit()
        profile = capacity_profile(
            session, company_id=COMPANY, start=DAY_ONE, end=DAY_THREE, bucket_days=1
        )
        assert [row["work_order"] for row in profile["unassigned_operations"]] == ["WO-D"], (
            profile["unassigned_operations"]
        )
        assert profile["unassigned_operations"][0]["minutes"] == Decimal("15.000000"), (
            profile["unassigned_operations"]
        )
        assert [(row["work_order"], row["work_center"]) for row in profile["unknown_work_centers"]] == [
            ("WO-D", "GHOST")
        ], profile["unknown_work_centers"]
        # ...and neither of them was loaded onto a centre that does exist.
        assert _row(profile, center="CUT", on=DAY_ONE)["load_minutes"] == Decimal("630.000000"), (
            _row(profile, center="CUT", on=DAY_ONE)
        )
        # 630 (CUT, the 10th) + 210 (WELD, the 10th) + 215 (CUT, the 11th) + 75
        # (WELD, the 11th) — every placed minute, and neither of WO-D's.
        assert profile["load_minutes"] == Decimal("1130.000000"), profile["load_minutes"]
        print(
            f"3. WO-D's unassigned step ({profile['unassigned_operations'][0]['minutes']}"
            f" minutes) and its step naming GHOST are both reported with their minutes;"
            f" the figure the centres carry is {profile['load_minutes']}, so nothing was"
            " silently loaded onto a bench that does not do it"
        )

        # 4 — a completed order is not work still to do
        for status in (RELEASED, IN_PROGRESS, COMPLETED, CLOSED):
            advance(session, first, status=status)
            session.commit()
        settled = capacity_profile(
            session, company_id=COMPANY, start=DAY_ONE, end=DAY_THREE, bucket_days=1
        )
        assert _row(settled, center="CUT", on=DAY_ONE)["load_minutes"] == Decimal("415.000000"), (
            _row(settled, center="CUT", on=DAY_ONE)
        )
        assert _row(settled, center="CUT", on=DAY_ONE)["overloaded"] is False, (
            _row(settled, center="CUT", on=DAY_ONE)
        )
        print(
            f"4. WO-A closed and CUT on {DAY_ONE} fell from"
            f" {overloaded['load_minutes']} to"
            f" {_row(settled, center='CUT', on=DAY_ONE)['load_minutes']} — no longer"
            " overloaded, because a finished job is not work still to do"
        )

        # 5 — reproducible, and one cell is a call away
        again = capacity_profile(
            session, company_id=COMPANY, start=DAY_ONE, end=DAY_THREE, bucket_days=1
        )
        assert again == settled, "the same horizon answered different figures"
        assert load_for(session, company_id=COMPANY, code="CUT", on=DAY_TWO) == Decimal(
            "215.000000"
        ), load_for(session, company_id=COMPANY, code="CUT", on=DAY_TWO)
        assert stray.number == "WO-D", stray.number
        assert sorted(row["work_order"] for row in settled["periods"][0]["orders"] if row["work_center"] == "CUT") == [
            "WO-B"
        ], settled["periods"][0]["orders"]
        print(
            f"5. the same horizon answered the same figures twice ({len(settled['periods'])}"
            f" centre-periods), and one cell reads {load_for(session, company_id=COMPANY, code='CUT', on=DAY_TWO)}"
            " minutes — the chart is the work orders, so it can be recomputed from them"
        )

    print("\ncheck_capacity_planning: all assertions green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
