"""T-6.ADV.01 check — the constrained plan, the stability of a re-plan, and the same twice.

    DATABASE_URL=postgresql+psycopg://erpv1:erpv1@localhost:5432/erpv1 \
        python tests/check_advanced_planning.py

Green on all eight:

1. **the basic plan violates capacity and the advanced one does not** — two make items
   whose routings both want a 480-minute centre in the same day (800 minutes between
   them) come out one per day, and the loading of every centre in every bucket is at or
   under its capacity, hand-checked against `period_capacity`
2. **the constraint that caused each move is reported** — the centre, the minutes that
   did not fit and the capacity they did not fit into, on the row that moved
3. **a dataset with room reconciles to the basic net requirements**, figure for figure
   and bucket for bucket, with nothing moved: `identical`
4. **changing one item's demand re-plans that item only** — the rows for the untouched
   item come out of the second plan exactly as the first stated them
5. **the same data gives the same plan** — a third run, over unchanged data, produces
   rows equal to the second run's, constraint text included
6. **a horizon that cannot hold the work says so** — the requirement stays where it is
   and is marked `unresolved` with the centre and the over-capacity figure, rather than
   silently overloading or vanishing
7. **an unknown work centre is reported rather than guessed** — a routing naming a
   centre nobody registered states that on the plan
8. **another company's run is refused at the door** — an advanced plan is placed for one
   company's run and for no other's, refused before anything is written
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ar.gateway import GatewayPayment  # noqa: E402,F401 — the exposure's tables
from app.ar.invoices import CustomerInvoice  # noqa: E402,F401 — the exposure's tables
from app.company import Company, set_credit_check_mode  # noqa: E402
from app.db import Base  # noqa: E402
from app.manufacturing.advanced_planning import (  # noqa: E402
    AdvancedPlanRow,
    AdvancedPlanningError,
    advanced_plan_of,
    compare_plans,
    constraints_of,
    reconcile,
    run_advanced_plan,
)
from app.manufacturing.bom import create_bom, release  # noqa: E402
from app.manufacturing.capacity import period_capacity  # noqa: E402
from app.manufacturing.mrp import plan_of, run_mrp  # noqa: E402
from app.manufacturing.routing import add_operation  # noqa: E402
from app.manufacturing.work_centers import create_work_center, work_center_by_code  # noqa: E402
from app.procurement.orders import PurchaseOrderLine  # noqa: E402,F401 — for its table
from app.sales.customers import create_customer  # noqa: E402
from app.sales.fulfilment import Shipment  # noqa: E402,F401 — for its table
from app.sales.orders import confirm_order, convert_quotation_to_order  # noqa: E402
from app.sales.pipeline import Opportunity  # noqa: E402,F401 — for its table
from app.sales.quotations import add_line as quote_line  # noqa: E402
from app.sales.quotations import create_quotation  # noqa: E402
from app.stock.items import create_item  # noqa: E402
from tests.seed import seed_stock_accounts  # noqa: E402

COMPANY = uuid.uuid4()
QUIET = uuid.uuid4()
NARROW = uuid.uuid4()
START = date(2026, 10, 5)
DAY_ONE = date(2026, 10, 5)
DAY_TWO = date(2026, 10, 6)
HORIZON = 7
BUCKET = 1


def _refused(call, expected: type[Exception]) -> str:
    try:
        call()
    except expected as exc:  # noqa: BLE001 — the type and the message are the point
        return str(exc)
    raise AssertionError(f"accepted what it must refuse ({expected.__name__})")


def _row(rows: list[dict], *, item: str) -> dict:
    found = [row for row in rows if row["item"] == item]
    assert len(found) == 1, f"expected one row for {item}, got {len(found)}"
    return found[0]


def _company(session: Session, *, company_id: uuid.UUID, code: str) -> Company:
    company = Company(
        id=company_id,
        code=code,
        name=code,
        base_currency="PHP",
        fiscal_year_start_month=1,
    )
    session.add(company)
    session.commit()
    seed_stock_accounts(session, company_id=company_id)
    set_credit_check_mode(session, company, mode="off")
    session.commit()
    return company


def _made_item(
    session: Session,
    *,
    company_id: uuid.UUID,
    sku: str,
    centre: str,
    minutes: str = "400",
) -> object:
    item = create_item(
        session,
        company_id=company_id,
        sku=sku,
        name=sku.title(),
        base_uom="each",
        traceability_mode="none",
    )
    session.flush()
    bom = create_bom(session, company_id=company_id, item=item)
    add_operation(session, bom, name=f"Make {sku}", run_minutes=minutes, work_center_code=centre)
    release(session, bom)
    session.commit()
    return item


def _order(session: Session, *, company_id: uuid.UUID, customer, item, quantity: str, on: date,
           number: str) -> None:
    quote = create_quotation(
        session,
        company_id=company_id,
        customer_id=customer.id,
        number=f"Q-{number}",
        issued_on=on,
        valid_until=date(2026, 12, 31),
    )
    session.flush()
    quote_line(
        session,
        quote,
        line_no=1,
        description=item.sku,
        quantity=quantity,
        unit_price="100.00",
        uom="each",
        item_id=item.id,
        priced_on=on,
    )
    session.flush()
    order = convert_quotation_to_order(session, quote, number=f"SO-{number}", on=on)
    confirm_order(session, order, actor="maria")
    session.commit()


def _load_by_centre(session: Session, plan) -> dict[tuple[date, str], Decimal]:
    """The minutes the plan places on each centre in each bucket — the figure checked."""
    load: dict[tuple[date, str], Decimal] = {}
    for row in advanced_plan_of(session, plan):
        if not row["work_center"]:
            continue
        key = (row["planned_release_on"], row["work_center"])
        load[key] = load.get(key, Decimal(0)) + row["load_minutes"]
    return load


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
        _company(session, company_id=COMPANY, code="ADV-CHECK")
        # Two centres, each manned 480 minutes a day, none of it lost.
        create_work_center(
            session, company_id=COMPANY, code="CUT", name="Cutting bench",
            capacity_minutes="480", capacity_period="day",
        )
        create_work_center(
            session, company_id=COMPANY, code="WELD", name="Welding bay",
            capacity_minutes="900", capacity_period="day",
        )
        session.commit()
        cut = work_center_by_code(session, company_id=COMPANY, code="CUT")
        # 400 minutes of CUT per unit: two of them do not fit one day.
        widget = _made_item(session, company_id=COMPANY, sku="WIDGET", centre="CUT")
        gadget = _made_item(session, company_id=COMPANY, sku="GADGET", centre="CUT")
        solo = _made_item(session, company_id=COMPANY, sku="SOLO", centre="WELD")
        customer = create_customer(
            session, company_id=COMPANY, party_code="ACME", name="Acme", payment_terms_days=30
        )
        session.commit()
        _order(session, company_id=COMPANY, customer=customer, item=widget, quantity="1",
               on=START, number="W")
        _order(session, company_id=COMPANY, customer=customer, item=gadget, quantity="1",
               on=START, number="G")
        _order(session, company_id=COMPANY, customer=customer, item=solo, quantity="1",
               on=START, number="S")
        session.commit()

        run = run_mrp(
            session, company_id=COMPANY, start=START, horizon_days=HORIZON,
            bucket_days=BUCKET, demand_sources=("sales_orders",), run_on=START,
        )
        session.commit()
        basic = {row["item"]: row for row in plan_of(session, run)}
        capacity = period_capacity(cut, days=BUCKET)
        assert capacity == Decimal("480.000000"), capacity
        # The basic view: both make items want the same centre on the same day.
        assert basic["WIDGET"]["release_on"] == DAY_ONE, basic["WIDGET"]
        assert basic["GADGET"]["release_on"] == DAY_ONE, basic["GADGET"]
        wanted = basic["WIDGET"]["net"] + basic["GADGET"]["net"]
        assert wanted * Decimal("400") == Decimal("800.000000"), wanted

        plan = run_advanced_plan(session, company_id=COMPANY, run=run, run_on=START)
        session.commit()
        rows = advanced_plan_of(session, plan)

        # 1 — the load is placed within capacity, one item a day
        load = _load_by_centre(session, plan)
        assert load[(DAY_ONE, "CUT")] == Decimal("400.000000"), load
        assert load[(DAY_TWO, "CUT")] == Decimal("400.000000"), load
        for (day, centre), minutes in load.items():
            centre_row = work_center_by_code(session, company_id=COMPANY, code=centre)
            assert minutes <= period_capacity(centre_row, days=BUCKET), (day, centre, minutes)
        assert _row(rows, item="GADGET")["planned_release_on"] == DAY_ONE, _row(
            rows, item="GADGET"
        )
        assert _row(rows, item="WIDGET")["planned_release_on"] == DAY_TWO, _row(
            rows, item="WIDGET"
        )
        print(
            f"1. CUT is manned {capacity} minutes a day and the basic plan loads it with"
            f" {wanted * Decimal('400')} on {DAY_ONE}: the advanced plan puts GADGET there"
            f" and WIDGET on {DAY_TWO}, {load[(DAY_ONE, 'CUT')]} minutes a day against"
            f" {capacity} — every centre in every bucket at or under its capacity"
        )

        # 2 — the move carries its cause
        moved = _row(rows, item="WIDGET")
        assert moved["moved_buckets"] == 1, moved
        assert moved["late"] is True, moved
        assert moved["work_center"] == "CUT", moved
        assert moved["load_minutes"] == Decimal("400.000000"), moved
        assert moved["bucket_load_minutes"] == Decimal("800.000000"), moved
        assert moved["capacity_minutes"] == capacity, moved
        assert "moved 1 bucket(s) at 'CUT'" in moved["constraint"], moved["constraint"]
        assert "800.000000 min against 480.000000 min" in moved["constraint"], moved["constraint"]
        assert [row["item"] for row in constraints_of(session, plan)] == ["WIDGET"], (
            constraints_of(session, plan)
        )
        print(
            f"2. the WIDGET row states why it moved: {moved['constraint']} — the centre,"
            " the minutes that did not fit and the capacity they did not fit into"
        )

        # 3 — a dataset with room is the basic plan, unchanged
        _company(session, company_id=QUIET, code="ADV-QUIET")
        create_work_center(
            session, company_id=QUIET, code="ROOM", name="Room to spare",
            capacity_minutes="4800", capacity_period="day",
        )
        session.commit()
        quiet_item = _made_item(
            session, company_id=QUIET, sku="QUIET-1", centre="ROOM", minutes="100"
        )
        quiet_customer = create_customer(
            session, company_id=QUIET, party_code="QUIET", name="Quiet", payment_terms_days=30
        )
        session.commit()
        _order(session, company_id=QUIET, customer=quiet_customer, item=quiet_item,
               quantity="2", on=START, number="Q")
        quiet_run = run_mrp(
            session, company_id=QUIET, start=START, horizon_days=HORIZON,
            bucket_days=BUCKET, demand_sources=("sales_orders",), run_on=START,
        )
        session.commit()
        quiet_plan = run_advanced_plan(session, company_id=QUIET, run=quiet_run, run_on=START)
        session.commit()
        quiet_rows = advanced_plan_of(session, quiet_plan)
        report = reconcile(session, quiet_plan)
        quiet_basic = plan_of(session, quiet_run)
        assert report["identical"] is True, report
        assert report["moved"] == 0 and report["missing"] == [], report
        assert len(quiet_rows) == len(quiet_basic) == 1, (quiet_rows, quiet_basic)
        assert quiet_rows[0]["quantity"] == quiet_basic[0]["net"] == Decimal("2.000000"), (
            quiet_rows[0],
            quiet_basic[0],
        )
        assert quiet_rows[0]["planned_release_on"] == quiet_basic[0]["release_on"], quiet_rows[0]
        assert constraints_of(session, quiet_plan) == [], constraints_of(session, quiet_plan)
        print(
            f"3. a centre with room plans exactly as the basic run does: 2 QUIET-1 wanted"
            f" in {DAY_ONE}, 2 on the advanced plan for the same bucket, no moves — "
            "identical"
        )

        # 4 — one item's demand change moves that item's rows only
        _order(session, company_id=COMPANY, customer=customer, item=solo, quantity="1",
               on=START, number="S2")
        # And a new item's demand, which is the same kind of change from the plan's side:
        # 900 minutes of WELD a day, 2 x 400 from SOLO plus 500 from EXTRA is not a day.
        extra = _made_item(session, company_id=COMPANY, sku="EXTRA", centre="WELD", minutes="500")
        _order(session, company_id=COMPANY, customer=customer, item=extra, quantity="1",
               on=START, number="X")
        session.commit()
        second_run = run_mrp(
            session, company_id=COMPANY, start=START, horizon_days=HORIZON,
            bucket_days=BUCKET, demand_sources=("sales_orders",), run_on=START,
        )
        session.commit()
        second_plan = run_advanced_plan(session, company_id=COMPANY, run=second_run, run_on=START)
        session.commit()
        second_rows = advanced_plan_of(session, second_plan)
        change = compare_plans(rows, second_rows)
        assert change["changed"] == [("SOLO", DAY_ONE)], change
        assert change["added"] == [("EXTRA", DAY_ONE)], change
        assert change["removed"] == [], change
        assert change["unchanged"] == [("GADGET", DAY_ONE), ("WIDGET", DAY_ONE)], change
        assert _row(second_rows, item="SOLO")["quantity"] == Decimal("2.000000"), _row(
            second_rows, item="SOLO"
        )
        solo_row = _row(second_rows, item="SOLO")
        assert solo_row["moved_buckets"] == 1, solo_row
        assert solo_row["planned_release_on"] == DAY_TWO, solo_row
        assert solo_row["bucket_load_minutes"] == Decimal("1300.000000"), solo_row
        assert solo_row["capacity_minutes"] == Decimal("900.000000"), solo_row
        extra_row = _row(second_rows, item="EXTRA")
        assert extra_row["moved_buckets"] == 0, extra_row
        assert extra_row["planned_release_on"] == DAY_ONE, extra_row
        assert _row(second_rows, item="WIDGET")["constraint"] == moved["constraint"], (
            _row(second_rows, item="WIDGET")
        )
        assert _row(second_rows, item="WIDGET")["planned_release_on"] == DAY_TWO, (
            _row(second_rows, item="WIDGET")
        )
        print(
            "4. a second SOLO unit and a new EXTRA item change only their own rows:"
            f" {solo_row['constraint']}, and the WIDGET row still reads"
            f" {moved['planned_release_on']} with the same constraint — GADGET, whose"
            " demand nobody touched, was not rewritten at all"
        )

        # 5 — the same data gives the same plan
        third_run = run_mrp(
            session, company_id=COMPANY, start=START, horizon_days=HORIZON,
            bucket_days=BUCKET, demand_sources=("sales_orders",), run_on=START,
        )
        session.commit()
        third_plan = run_advanced_plan(session, company_id=COMPANY, run=third_run, run_on=START)
        session.commit()
        assert advanced_plan_of(session, third_plan) == second_rows, "the plan moved on its own"
        print(
            f"5. a third run over unchanged data produced the same {len(second_rows)} rows —"
            " dates, constraints and figures — so a plan can be compared with the one"
            " before it rather than trusted"
        )

        # 6 — a horizon with no room says so
        _company(session, company_id=NARROW, code="ADV-NARROW")
        create_work_center(
            session, company_id=NARROW, code="TIGHT", name="One day only",
            capacity_minutes="480", capacity_period="day",
        )
        session.commit()
        tight = _made_item(session, company_id=NARROW, sku="TIGHT-1", centre="TIGHT")
        tight_customer = create_customer(
            session, company_id=NARROW, party_code="TIGHT", name="Tight", payment_terms_days=30
        )
        session.commit()
        _order(session, company_id=NARROW, customer=tight_customer, item=tight,
               quantity="2", on=START, number="T")
        tight_run = run_mrp(
            session, company_id=NARROW, start=START, horizon_days=1, bucket_days=1,
            demand_sources=("sales_orders",), run_on=START,
        )
        session.commit()
        tight_plan = run_advanced_plan(
            session, company_id=NARROW, run=tight_run, run_on=START
        )
        session.commit()
        overflowing = _row(advanced_plan_of(session, tight_plan), item="TIGHT-1")
        assert overflowing["unresolved"] is True, overflowing
        assert overflowing["moved_buckets"] == 0, overflowing
        assert overflowing["planned_release_on"] == DAY_ONE, overflowing
        assert "no bucket in the horizon has room at 'TIGHT'" in overflowing["constraint"], (
            overflowing["constraint"]
        )
        assert "800.000000 min against 480.000000 min" in overflowing["constraint"], (
            overflowing["constraint"]
        )
        print(
            f"6. a one-day horizon cannot hold two TIGHT-1 units and says so:"
            f" {overflowing['constraint']} — the requirement stays where it was wanted"
            " and is marked unresolved rather than overloading in silence"
        )

        # 7 — a centre nobody registered is reported, not invented
        unknown = create_item(
            session, company_id=QUIET, sku="UNKNOWN-CENTRE", name="Unknown centre",
            base_uom="each", traceability_mode="none",
        )
        session.flush()
        bom = create_bom(session, company_id=QUIET, item=unknown)
        add_operation(session, bom, name="Ghost step", run_minutes="10",
                      work_center_code="GHOST")
        release(session, bom)
        session.commit()
        _order(session, company_id=QUIET, customer=quiet_customer, item=unknown,
               quantity="1", on=START, number="Q2")
        ghost_run = run_mrp(
            session, company_id=QUIET, start=START, horizon_days=HORIZON,
            bucket_days=BUCKET, demand_sources=("sales_orders",), run_on=START,
        )
        session.commit()
        ghost_plan = run_advanced_plan(session, company_id=QUIET, run=ghost_run, run_on=START)
        session.commit()
        ghost = _row(advanced_plan_of(session, ghost_plan), item="UNKNOWN-CENTRE")
        assert ghost["work_center"] is None, ghost
        assert ghost["load_minutes"] == Decimal("0.000000"), ghost
        assert "work centre 'GHOST' is named by the routing and registered by nobody" in (
            ghost["constraint"]
        ), ghost["constraint"]
        print(
            f"7. a routing naming a centre nobody registered states it on the plan:"
            f" {ghost['constraint']}"
        )

        # 8 — another company's run never becomes this company's plan
        before = session.scalar(
            select(func.count()).select_from(AdvancedPlanRow)
        )
        message = _refused(
            lambda: run_advanced_plan(session, company_id=NARROW, run=run, run_on=START),
            AdvancedPlanningError,
        )
        session.rollback()
        assert "belongs to another company" in message, message
        after = session.scalar(select(func.count()).select_from(AdvancedPlanRow))
        assert after == before, (before, after)
        print(
            f"8. a run is planned for its own company and no other: {message} — refused"
            f" before a row was written ({after} rows)"
        )

    print("\ncheck_advanced_planning: all assertions green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
