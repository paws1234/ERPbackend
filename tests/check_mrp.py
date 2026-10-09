"""T-4.MRP.01 check — the net requirement, hand-checked, and the same twice.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_mrp.py

Green on all five:

1. a two-level dataset explodes to the **hand-calculated net requirements**: the sales
   order's 10 widgets against 4 already on order, and the 12 blanks they need against
   the 5 on the shelf — bucket by bucket, at every level
2. a **shortage at a sub-level propagates to the parent requirement**: the widget row
   names the blank it is short of, because a component that is not there is a parent
   that cannot be built
3. **lead times are respected**: the blank's order has to be placed three days before
   the bucket it is needed in, and the plan states the date
4. the run **states its inputs** on every plan, the second demand feed really is one
   (the open work order's components), and a feed this system does not have is refused
5. **running MRP twice on unchanged data produces identical results** — bucket by
   bucket, figure for figure
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

from app.ar.gateway import GatewayPayment  # noqa: E402,F401 — the exposure's tables
from app.ar.invoices import CustomerInvoice  # noqa: E402,F401 — the exposure's tables
from app.company import Company, set_credit_check_mode  # noqa: E402
from app.db import Base  # noqa: E402
from app.manufacturing.bom import add_line, create_bom, release  # noqa: E402
from app.manufacturing.mrp import (  # noqa: E402
    SALES_ORDERS,
    WORK_ORDERS,
    UnknownDemandSource,
    inputs_of,
    plan_of,
    plan_sorted,
    run_mrp,
)
from app.manufacturing.work_orders import create_work_order  # noqa: E402
from app.procurement.orders import PurchaseOrderLine  # noqa: E402,F401 — for its table
from app.sales.customers import create_customer  # noqa: E402
from app.sales.orders import confirm_order, convert_quotation_to_order  # noqa: E402
from app.sales.fulfilment import Shipment  # noqa: E402,F401 — for its table
from app.sales.pipeline import Opportunity  # noqa: E402,F401 — for its table
from app.sales.quotations import add_line as quote_line  # noqa: E402
from app.sales.quotations import create_quotation  # noqa: E402
from app.stock.items import create_item  # noqa: E402
from app.stock.locations import create_location  # noqa: E402
from app.stock.transactions import receive  # noqa: E402
from tests.seed import seed_stock_accounts  # noqa: E402

COMPANY = uuid.uuid4()
START = date(2026, 10, 5)
BUCKET = 7
HORIZON = 28
BUCKET_ONE = date(2026, 10, 5)
BUCKET_TWO = date(2026, 10, 12)


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


def _row(plan: list[dict], *, item: str, bucket: date) -> dict:
    rows = [row for row in plan if row["item"] == item and row["bucket_start"] == bucket]
    assert rows, f"no row for {item} in {bucket}: {[r['item'] for r in plan]}"
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
        company = Company(id=COMPANY, code="MRP-CHECK", name="MRP check",
                          base_currency="PHP", fiscal_year_start_month=1)
        session.add(company)
        session.commit()
        seed_stock_accounts(session, company_id=COMPANY)
        set_credit_check_mode(session, company, mode="off")
        session.commit()

        # A widget is two blanks; the blank is bought with a three-day lead time.
        widget = create_item(session, company_id=COMPANY, sku="WIDGET", name="Widget",
                             base_uom="each", traceability_mode="none")
        blank = create_item(session, company_id=COMPANY, sku="BLANK", name="Blank",
                            base_uom="each", traceability_mode="none", lead_time_days=3)
        session.commit()
        bom = create_bom(session, company_id=COMPANY, item=widget)
        add_line(session, bom, item=blank, quantity="2")
        release(session, bom)
        warehouse = create_location(session, company_id=COMPANY, code="MAIN", name="Main",
                                    location_type="warehouse")
        zone = create_location(session, company_id=COMPANY, code="MAIN-Z", name="Zone",
                               location_type="zone", parent_id=warehouse.id)
        aisle = create_location(session, company_id=COMPANY, code="MAIN-Z-1", name="Aisle",
                                location_type="aisle", parent_id=zone.id)
        bin_a = create_location(session, company_id=COMPANY, code="MAIN-Z-1-A", name="Bin A",
                                location_type="bin", parent_id=aisle.id)
        session.commit()
        receive(session, item=blank, location=bin_a, uom="each", quantity="5",
                value=Decimal("50"), currency="PHP", source_type="goods_receipt",
                source_id=uuid.uuid4(), posting_date=date(2026, 10, 1))
        session.commit()

        customer = create_customer(session, company_id=COMPANY, party_code="ACME",
                                   name="Acme", payment_terms_days=30)
        session.commit()
        quote = create_quotation(session, company_id=COMPANY, customer_id=customer.id,
                                 number="Q-1", issued_on=START,
                                 valid_until=date(2026, 12, 31))
        session.flush()
        quote_line(session, quote, line_no=1, description="Widget", quantity="10",
                   unit_price="100.00", uom="each", item_id=widget.id, priced_on=START)
        session.flush()
        order = convert_quotation_to_order(session, quote, number="SO-1", on=START)
        confirm_order(session, order, actor="maria")
        session.commit()

        # Four widgets are already being made, wanted in the first bucket: that is supply.
        open_job = create_work_order(session, company_id=COMPANY, item=widget, quantity="4",
                                     number="WO-1", created_on=START,
                                     due_on=date(2026, 10, 8))
        session.commit()

        # 1 — the net requirement, by hand
        run = run_mrp(session, company_id=COMPANY, start=START, horizon_days=HORIZON,
                      bucket_days=BUCKET, demand_sources=(SALES_ORDERS,),
                      run_on=START)
        session.commit()
        plan = plan_sorted(plan_of(session, run))
        made = _row(plan, item="WIDGET", bucket=BUCKET_ONE)
        # 10 wanted, 4 already on order, nothing on the shelf: 6 still to make.
        assert (made["gross"], made["supply"], made["net"]) == (
            Decimal("10.000000"),
            Decimal("4.000000"),
            Decimal("6.000000"),
        ), made
        assert made["kind"] == "make" and made["level"] == 0, made
        # 6 widgets take 12 blanks; five are on the shelf: 7 to buy, three days earlier.
        bought = _row(plan, item="BLANK", bucket=BUCKET_ONE)
        assert (bought["gross"], bought["available"], bought["net"]) == (
            Decimal("12.000000"),
            Decimal("5.000000"),
            Decimal("7.000000"),
        ), bought
        assert bought["kind"] == "buy" and bought["level"] == 1, bought
        assert len(plan) == 2, plan
        print(
            f"1. bucket 1: {made['gross']} widgets wanted ({made['supply']} already being"
            f" made) leave a net {made['net']} to produce; those take {bought['gross']}"
            f" blanks, {bought['available']} on the shelf, so {bought['net']} to buy —"
            " the level below the level above, hand-checked"
        )

        # 2 — the sub-level shortage reaches the parent
        assert made["constrained"] is True, made
        assert made["constrained_by"] == "BLANK", made
        assert bought["constrained"] is False, bought
        print(
            f"2. the widget row names what it is short of"
            f" ({made['constrained_by']}): seven blanks missing is a widget that cannot"
            " be built, and the plan says so on the parent's line rather than leaving it"
            " to be inferred from the level below"
        )

        # 3 — the lead time, stated as a date
        assert bought["lead_time_days"] == 3, bought
        assert bought["release_on"] == date(2026, 10, 2), bought["release_on"]
        assert made["release_on"] == BUCKET_ONE, made
        print(
            f"3. the blank is needed in the week of {BUCKET_ONE} and must be ordered by"
            f" {bought['release_on']} — its {bought['lead_time_days']}-day lead time"
            " applied to the bucket it is wanted in"
        )

        # 4 — the inputs, the second feed, and a feed that does not exist
        assert inputs_of(run) == {
            "start": START,
            "horizon_days": HORIZON,
            "bucket_days": BUCKET,
            "demand_sources": (SALES_ORDERS,),
            "run_on": START,
        }, inputs_of(run)
        from_work = run_mrp(session, company_id=COMPANY, start=START, horizon_days=HORIZON,
                           bucket_days=BUCKET, demand_sources=(WORK_ORDERS,), run_on=START)
        session.commit()
        work_plan = plan_sorted(plan_of(session, from_work))
        # The open job still owes 8 blanks (4 widgets × 2): five on the shelf leave 3.
        drawn = _row(work_plan, item="BLANK", bucket=BUCKET_ONE)
        assert (drawn["gross"], drawn["available"], drawn["net"]) == (
            Decimal("8.000000"),
            Decimal("5.000000"),
            Decimal("3.000000"),
        ), drawn
        assert all(row["item"] != "WIDGET" for row in work_plan), work_plan
        said_unknown = _refused(
            lambda: run_mrp(session, company_id=COMPANY, start=START,
                            horizon_days=HORIZON, bucket_days=BUCKET,
                            demand_sources=("forecasts",), run_on=START),
            UnknownDemandSource,
        )
        session.rollback()
        print(
            f"4. the run states its inputs ({inputs_of(from_work)['demand_sources']} for"
            f" the second run); that feed really is demand — the open job owes"
            f" {drawn['gross']} blanks, {drawn['available']} on the shelf, net"
            f" {drawn['net']} — and a feed this system does not have is refused"
            f" ({said_unknown[:42]}…)"
        )

        # 5 — the same data, the same plan
        again = run_mrp(session, company_id=COMPANY, start=START, horizon_days=HORIZON,
                        bucket_days=BUCKET, demand_sources=(SALES_ORDERS,), run_on=START)
        session.commit()
        assert plan_sorted(plan_of(session, again)) == plan, (
            plan_sorted(plan_of(session, again)),
            plan,
        )
        assert again.id != run.id, "the second run is the first"
        print(
            f"5. a second run over unchanged data produced the identical plan —"
            f" {len(plan)} rows, the same quantities, dates and constraints — so a plan"
            " can be compared with the one before it rather than trusted"
        )

    print("\ncheck_mrp: all assertions green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
