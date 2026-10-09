"""T-4.MRP.02 check — the plan's suggestions, their conversion, and a stale one refused.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_mrp_output.py

Green on all five:

1. **every net requirement produces exactly one suggestion**, with the requirement's own
   quantity, its type (produce or purchase) and the date it is needed, **listed in the
   plan's own order** — and asking the plan for suggestions twice does not double them
2. a suggestion the plan has **moved past is refused** before conversion, and converting
   it anyway needs a reason: the stock that arrived is what moved it, and with a reason
   it becomes a **purchase requisition that earns its approval** through the configured
   chain rather than being waved through
3. the **newest run's own suggestion converts cleanly**, at the figure the plan states
   now rather than the one it asked for before the stock arrived
4. a **make suggestion becomes a work order** sourced `mrp`, due when the plan says, with
   its components expanded — and converting it a second time is refused
5. the run's **summary states each suggestion's state**, the document it became and
   whether it is still flagged stale
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ar.gateway import GatewayPayment  # noqa: E402,F401 — the exposure's tables
from app.ar.invoices import CustomerInvoice  # noqa: E402,F401 — the exposure's tables
from app.company import Company, set_credit_check_mode  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger.currency import register_currency  # noqa: E402
from app.manufacturing.bom import add_line, create_bom, release  # noqa: E402
from app.manufacturing.mrp import (  # noqa: E402
    SALES_ORDERS,
    plan_of,
    plan_sorted,
    run_mrp,
)
from app.manufacturing.mrp_output import (  # noqa: E402
    CONVERTED,
    PURCHASE,
    AlreadyConvertedError,
    StaleSuggestionError,
    convert_suggestion,
    is_stale,
    latest_plan,
    open_suggestions,
    raise_suggestions,
    suggestions_of,
    summary,
)
from app.manufacturing.work_orders import (  # noqa: E402
    WorkOrder,
    create_work_order,
    missing_route,
    requirements_of,
)
from app.procurement.orders import PurchaseOrderLine  # noqa: E402,F401 — for its table
from app.procurement.requisitions import Requisition  # noqa: E402
from app.sales.customers import create_customer  # noqa: E402
from app.sales.fulfilment import Shipment  # noqa: E402,F401 — for its table
from app.sales.orders import confirm_order, convert_quotation_to_order  # noqa: E402
from app.sales.pipeline import Opportunity  # noqa: E402,F401 — for its table
from app.sales.quotations import add_line as quote_line  # noqa: E402
from app.sales.quotations import create_quotation  # noqa: E402
from app.stock.items import Item, create_item  # noqa: E402
from app.stock.locations import create_location  # noqa: E402
from app.stock.transactions import receive  # noqa: E402
from app.workflow import configure  # noqa: E402
from tests.seed import seed_stock_accounts  # noqa: E402

COMPANY = uuid.uuid4()
START = date(2026, 10, 5)
HORIZON = 28
BUCKET = 7
APPROVER = "procurement.head"


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
        company = Company(id=COMPANY, code="MRPO-CHECK", name="MRP output check",
                          base_currency="PHP", fiscal_year_start_month=1)
        session.add(company)
        session.commit()
        seed_stock_accounts(session, company_id=COMPANY)
        register_currency(session, company_id=COMPANY, code="PHP", name="Peso")
        set_credit_check_mode(session, company, mode="off")
        # Every requisition this company raises needs a level's approval: converting a
        # suggestion must not be a way around that.
        configure(session, company_id=COMPANY, doc_type="purchase_requisition",
                  name="Purchase requisition", levels=[(0, APPROVER)])
        session.commit()

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

        # Four widgets are already being made: that is supply, so the plan asks for six.
        create_work_order(session, company_id=COMPANY, item=widget, quantity="4",
                          number="WO-1", created_on=START, due_on=date(2026, 10, 8))
        session.commit()

        run = run_mrp(session, company_id=COMPANY, start=START, horizon_days=HORIZON,
                      bucket_days=BUCKET, demand_sources=(SALES_ORDERS,), run_on=START)
        session.commit()

        # 1 — one suggestion per net requirement, at its own quantity
        made = raise_suggestions(session, run)
        session.commit()
        again = raise_suggestions(session, run)
        assert [row.id for row in again] == [row.id for row in made], "suggestions doubled"
        assert len(made) == 2, made
        by_item = {row.item.sku: row for row in made}
        assert (by_item["WIDGET"].kind, by_item["WIDGET"].quantity) == (
            "produce",
            Decimal("6.000000"),
        ), by_item["WIDGET"]
        assert (by_item["BLANK"].kind, by_item["BLANK"].quantity) == (
            PURCHASE,
            Decimal("7.000000"),
        ), by_item["BLANK"]
        assert by_item["BLANK"].needed_by == START, by_item["BLANK"].needed_by
        assert by_item["BLANK"].release_on == date(2026, 10, 2), by_item["BLANK"].release_on
        assert len(plan_sorted(plan_of(session, run))) == 2, plan_of(session, run)
        print(
            f"1. the plan raised {len(made)} suggestions from its"
            f" {len(plan_of(session, run))} requirement rows: produce"
            f" {by_item['WIDGET'].quantity} widgets and purchase"
            f" {by_item['BLANK'].quantity} blanks (order by"
            f" {by_item['BLANK'].release_on}) — each exactly its net requirement, and"
            " asking twice returned the same two"
        )

        # 2 — the plan moves, and the suggestion it has overtaken is refused
        receive(session, item=blank, location=bin_a, uom="each", quantity="5",
                value=Decimal("50"), currency="PHP", source_type="goods_receipt",
                source_id=uuid.uuid4(), posting_date=date(2026, 10, 3))
        session.commit()
        moved = run_mrp(session, company_id=COMPANY, start=START, horizon_days=HORIZON,
                        bucket_days=BUCKET, demand_sources=(SALES_ORDERS,), run_on=START)
        session.commit()
        raise_suggestions(session, moved)
        session.commit()
        target = {row.item.sku: row for row in suggestions_of(session, run)}["BLANK"]
        assert target.quantity == Decimal("7.000000"), target.quantity
        assert is_stale(session, target) is True, "the overtaken suggestion is not stale"
        assert latest_plan(session, target) == Decimal("2.000000"), latest_plan(session, target)
        said_stale = _refused(
            lambda: convert_suggestion(session, target, actor="maria", on=START,
                                       number="REQ-STALE"),
            StaleSuggestionError,
        )
        session.rollback()
        said_unreasoned = _refused(
            lambda: convert_suggestion(session, target, actor="maria", on=START,
                                       number="REQ-1", allow_stale=True),
            "needs a reason",
        )
        session.rollback()
        target = {row.item.sku: row for row in suggestions_of(session, run)}["BLANK"]
        outcome = convert_suggestion(
            session, target, actor="maria", on=START, number="REQ-1",
            estimated_unit_price="5", allow_stale=True,
            stale_reason="the supplier's minimum order is the larger lot",
        )
        session.commit()
        requisition = session.get(Requisition, target.converted_id)
        assert requisition is not None, outcome
        assert requisition.number == "REQ-1", requisition.number
        assert [line.item_id for line in requisition.lines] == [blank.id], requisition.lines
        assert requisition.lines[0].quantity == Decimal("7.000000"), requisition.lines[0]
        # Needed when the plan said to place the order, not when the stock is wanted.
        assert requisition.needed_by == date(2026, 10, 2), requisition.needed_by
        assert target.state == CONVERTED, target.state
        assert requisition.status == "pending", requisition.status
        assert requisition.approval_request_id is not None, requisition.approval_request_id
        assert requisition.approval_request_id != requisition.id, "no approval was raised"
        print(
            f"2. five more blanks arriving moved that bucket from {target.quantity} to"
            f" {latest_plan(session, target)}: the overtaken suggestion was refused"
            f" ({said_stale[:42]}…), refusing to convert it anyway without a reason"
            f" ({said_unreasoned[:42]}…), and with one it became requisition"
            f" {requisition.number} for {requisition.lines[0].quantity} blanks needed by"
            f" {requisition.needed_by} — {requisition.status!r} on the approval chain,"
            " raised for approval rather than waved through"
        )

        # 3 — the newest run's own suggestion is fresh, and converts at its own figure
        fresh = {row.item.sku: row for row in suggestions_of(session, moved)}["BLANK"]
        assert fresh.quantity == Decimal("2.000000"), fresh.quantity
        assert is_stale(session, fresh) is False, "the newest run's own suggestion is stale"
        convert_suggestion(session, fresh, actor="maria", on=START, number="REQ-2",
                           estimated_unit_price="5")
        session.commit()
        still_open = open_suggestions(session, moved)
        assert [
            (row.item.sku, row.quantity) for row in still_open
        ] == [("WIDGET", Decimal("6.000000"))], still_open
        print(
            f"3. the newest run's own suggestion converted cleanly as REQ-2 — {fresh.quantity}"
            f" blanks, the figure the plan states now, not the {target.quantity} it asked"
            " for before the stock arrived; its widget row is still open, waiting on the"
            " planner the same way"
        )

        # 4 — a make suggestion becomes a work order, once
        produced = convert_suggestion(session, by_item["WIDGET"], actor="maria", on=START,
                                      number="WO-MRP-1")
        session.commit()
        job = session.scalar(select(WorkOrder).where(WorkOrder.number == "WO-MRP-1"))
        assert job is not None, produced
        assert job.source == "mrp", job.source
        assert job.quantity == Decimal("6.000000"), job.quantity
        # Wanted in the bucket the plan named, not on the day of conversion.
        assert job.due_on == by_item["WIDGET"].needed_by == START, job.due_on
        assert [
            (session.get(Item, row.item_id).sku, row.quantity_required)
            for row in requirements_of(session, job)
        ] == [("BLANK", Decimal("12.000000"))], "the job did not expand its own components"
        assert missing_route(session, job) is True, "the plan invented a route"
        said_again = _refused(
            lambda: convert_suggestion(session, by_item["WIDGET"], actor="maria", on=START,
                                       number="WO-MRP-2"),
            AlreadyConvertedError,
        )
        session.rollback()
        print(
            f"4. the make suggestion became {job.number} — sourced {job.source!r},"
            f" {job.quantity} widgets due {job.due_on}, its"
            f" {len(requirements_of(session, job))} component requirement expanded from the"
            f" BOM — and converting it again was refused ({said_again[:40]}…) rather than"
            " scheduling the same six units twice"
        )

        # 5 — the run's summary says where every suggestion ended up
        rows = summary(session, run)["suggestions"]
        # The plan's own canonical order: by bucket, then item (T-4.MRP.01).
        assert [row["item"] for row in rows] == ["BLANK", "WIDGET"], rows
        assert [row["state"] for row in rows] == [CONVERTED, CONVERTED], rows
        assert rows[0]["converted"] == {
            "type": "purchase_requisition",
            "number": "REQ-1",
            "status": "pending",
        }, rows[0]
        assert rows[0]["stale"] is True, rows[0]
        assert rows[1]["converted"] == {
            "type": "work_order",
            "number": "WO-MRP-1",
            "status": job.status,
            "source": "mrp",
        }, rows[1]
        print(
            f"5. the run's summary reports both of its suggestions"
            f" {[row['state'] for row in rows]}, in the plan's own order — the blank as"
            f" {rows[0]['converted']['number']} ({rows[0]['converted']['status']}) and the"
            f" widget as {rows[1]['converted']['number']}"
            f" ({rows[1]['converted']['status']}) — and the blank is still flagged"
            f" stale={rows[0]['stale']}, so the override stays visible in the record"
        )

    print("\ncheck_mrp_output: all assertions green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
