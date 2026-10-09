"""T-4.X.GATE check — the Phase 4 exit criteria, verified against the running system.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_phase4_exit.py

§4's Phase 4 exit criterion is: *"Ability to produce finished goods from raw materials
with correct costing."* This walks it through the services the phase built, one document
reached from the one before it — nothing here re-implements a step or re-keys a figure:

1. **the plan drives the jobs**: MRP over the period's sales orders is hand-checked
   against the engine (every field of every row — the §6 metric 4 figure), and its
   suggestions become the two work orders and the requisition for the materials they
   call for, each naming the suggestion it came from and expanding its own multi-level
   BOM (a bicycle takes a frame and two wheels; a frame takes five tubes plus scrap)
2. **raw materials become finished goods, document by document**: the planned materials
   are received, tubes are issued to the frame job, time is booked on the cutting bench,
   the frames are received, the frames and wheels are issued to the bicycle job, time is
   booked on assembly and inspection, the bicycles are received — and the work in
   progress on both jobs clears to zero
3. **the cost is material plus booked time at the dated rates, and equals the finished
   goods' value** — hand-checked on both jobs, with the two costing entries' own lines
   and the accounts they land on read back
4. **scrap is attributed to cost rather than lost**: the frame job's requirement carries
   the BOM's scrap, and the one extra tube the press destroyed is on the record as an
   over-issue with an actor and a reason, inside the material the job cost
5. **a basic capacity view shows overload and underload per period** from the real work
   orders, with the load traceable to the operations that make it up
6. **every posting the cycle wrote balances** — §6 metric 1 over every entry, with at
   least two lines each, read from the ledger rather than from the responses

**Findings are measured, not explained away.** The ceiling this walk exposes — a
sub-assembly's labour reaches the inventory account but is not rolled into the parent's
unit cost (T-4.WO.05) — is asserted with its figures in section 3 rather than passed
over, and the plan's undated stock (T-4.MRP.01) is measured on that task's own check.
The correction this gate had to make first — a job consumes its **own** components, not
every row of its explosion — is in section 1's figures and on the three tasks it
touches.

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ar.gateway import GatewayPayment  # noqa: E402,F401 — the exposure's tables
from app.ar.invoices import CustomerInvoice  # noqa: E402,F401 — the exposure's tables
from app.company import Company, set_credit_check_mode  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger.accounts import create_account  # noqa: E402
from app.ledger.currency import register_currency  # noqa: E402
from app.ledger.mapping import set_mapping  # noqa: E402
from app.ledger.posting import JournalEntry, JournalLine  # noqa: E402
from app.manufacturing.bom import add_line, create_bom, release  # noqa: E402
from app.manufacturing.capacity import capacity_profile  # noqa: E402
from app.manufacturing.costing import (  # noqa: E402
    LABOUR_KEY,
    VARIANCE_KEY,
    cost_summary,
    cost_work_order,
    finished_goods_value,
    labour_breakdown,
)
from app.manufacturing.issues import (  # noqa: E402
    WIP_KEY,
    issue_material,
    issued_quantity,
    issued_value,
)
from app.manufacturing.job_cards import book_time, close_card, open_card  # noqa: E402
from app.manufacturing.mrp import SALES_ORDERS, plan_of, plan_sorted, run_mrp  # noqa: E402
from app.manufacturing.mrp_output import (  # noqa: E402
    convert_suggestion,
    raise_suggestions,
    suggestions_of,
)
from app.manufacturing.receipts import (  # noqa: E402
    receive_finished_goods,
    received_quantity,
    wip_balance,
)
from app.manufacturing.routing import add_operation  # noqa: E402
from app.manufacturing.work_centers import create_work_center, set_rate  # noqa: E402
from app.manufacturing.work_orders import (  # noqa: E402
    IN_PROGRESS,
    RELEASED,
    advance,
    direct_requirements,
    requirements_of,
    work_order_by_number,
)
from app.procurement.orders import PurchaseOrderLine  # noqa: E402,F401 — for its table
from app.procurement.requisitions import create_requisition, record_decision, submit  # noqa: E402
from app.sales.customers import create_customer  # noqa: E402
from app.sales.fulfilment import Shipment  # noqa: E402,F401 — for its table
from app.sales.orders import confirm_order, convert_quotation_to_order  # noqa: E402
from app.sales.pipeline import Opportunity  # noqa: E402,F401 — for its table
from app.sales.quotations import add_line as quote_line  # noqa: E402
from app.sales.quotations import create_quotation  # noqa: E402
from app.stock.items import Item, create_item  # noqa: E402
from app.stock.locations import create_location  # noqa: E402
from app.stock.transactions import receive  # noqa: E402
from app.workflow import APPROVE, configure  # noqa: E402
from tests.seed import seed_stock_accounts  # noqa: E402

COMPANY = uuid.uuid4()
START = date(2026, 10, 5)
HORIZON = 28
BUCKET = 7
B1 = date(2026, 10, 5)
B2 = date(2026, 10, 12)

# The rates the jobs are costed at, in force from the start of the year.
CUT_RATE = Decimal("300.00")
ASM_RATE = Decimal("400.00")
QC_RATE = Decimal("600.00")

# What the two made items are judged against (the variance's standard).
FRAME_STANDARD = Decimal("50")
BICYCLE_STANDARD = Decimal("250")

# The hand calculation of the plan, worked from the dataset in bucket order:
#
#   bucket 1 (10-05 … 10-11)
#     BICYCLE  10 ordered on 10-05, nothing on hand or on order            = 10
#     FRAME    10 bicycles × 1 frame                                       = 10
#     TUBE     10 frames × 5 tubes × 1.10 scrap = 55, less the 40 on hand  = 15
#     WHEEL    10 bicycles × 2 wheels = 20, less the 5 on hand             = 15
#   bucket 2 (10-12 … 10-18)
#     BICYCLE  6 ordered on 10-12                                          = 6
#     FRAME    6 × 1                                                       = 6
#     TUBE     6 × 5 × 1.10 = 33, and the 40 on hand went in week 1        = 33
#     WHEEL    6 × 2 = 12, and the 5 on hand went in week 1                = 12
#
# Release dates apply each item's lead time to the bucket it is wanted in: the made
# items are made to order, the tube is bought on three days and the wheel on one.
HAND = (
    {"item": "BICYCLE", "bucket": B1, "level": 0, "lead_time_days": 0, "gross": "10",
     "available": "0", "supply": "0", "net": "10", "kind": "make", "release_on": B1,
     "constrained": True, "constrained_by": "FRAME, WHEEL"},
    {"item": "FRAME", "bucket": B1, "level": 1, "lead_time_days": 0, "gross": "10",
     "available": "0", "supply": "0", "net": "10", "kind": "make", "release_on": B1,
     "constrained": True, "constrained_by": "TUBE"},
    {"item": "TUBE", "bucket": B1, "level": 2, "lead_time_days": 3, "gross": "55",
     "available": "40", "supply": "0", "net": "15", "kind": "buy",
     "release_on": date(2026, 10, 2), "constrained": False, "constrained_by": None},
    {"item": "WHEEL", "bucket": B1, "level": 1, "lead_time_days": 1, "gross": "20",
     "available": "5", "supply": "0", "net": "15", "kind": "buy",
     "release_on": date(2026, 10, 4), "constrained": False, "constrained_by": None},
    {"item": "BICYCLE", "bucket": B2, "level": 0, "lead_time_days": 0, "gross": "6",
     "available": "0", "supply": "0", "net": "6", "kind": "make", "release_on": B2,
     "constrained": True, "constrained_by": "FRAME, WHEEL"},
    {"item": "FRAME", "bucket": B2, "level": 1, "lead_time_days": 0, "gross": "6",
     "available": "0", "supply": "0", "net": "6", "kind": "make", "release_on": B2,
     "constrained": True, "constrained_by": "TUBE"},
    {"item": "TUBE", "bucket": B2, "level": 2, "lead_time_days": 3, "gross": "33",
     "available": "0", "supply": "0", "net": "33", "kind": "buy",
     "release_on": date(2026, 10, 9), "constrained": False, "constrained_by": None},
    {"item": "WHEEL", "bucket": B2, "level": 1, "lead_time_days": 1, "gross": "12",
     "available": "0", "supply": "0", "net": "12", "kind": "buy",
     "release_on": date(2026, 10, 11), "constrained": False, "constrained_by": None},
)
FIELDS = ("level", "lead_time_days", "gross", "available", "supply", "net", "kind",
          "release_on", "constrained", "constrained_by")
MONEY_FIELDS = ("gross", "available", "supply", "net")


def _expected() -> dict[tuple[str, date], dict]:
    return {
        (row["item"], row["bucket"]): {
            field: (Decimal(row[field]).quantize(Decimal("0.000001"))
                    if field in MONEY_FIELDS else row[field])
            for field in FIELDS
        }
        for row in HAND
    }


def _actual(rows: list[dict]) -> dict[tuple[str, date], dict]:
    return {(row["item"], row["bucket_start"]): {field: row[field] for field in FIELDS}
            for row in rows}


def _compare(expected: dict, actual: dict) -> list[str]:
    """Every field of every row that differs — the comparison the figure is counted from."""
    differences = []
    for key in sorted(expected, key=lambda row: (row[1], row[0])):
        if key not in actual:
            differences.append(f"{key[1]} {key[0]}: expected, the plan has no row")
            continue
        for field in FIELDS:
            want, got = expected[key][field], actual[key][field]
            if want != got:
                differences.append(f"{key[1]} {key[0]} {field}: expected {want!r}, got {got!r}")
    for key in sorted(set(actual) - set(expected), key=lambda row: (row[1], row[0])):
        differences.append(f"{key[1]} {key[0]}: the plan has a row nothing expected")
    return differences


def _lines(session: Session, entry: JournalEntry) -> list[dict]:
    rows = session.scalars(
        select(JournalLine).where(JournalLine.entry_id == entry.id).order_by(JournalLine.line_no)
    )
    return [
        {
            "account": row.account,
            "debit": Decimal(row.debit).quantize(Decimal("0.000001")),
            "credit": Decimal(row.credit).quantize(Decimal("0.000001")),
        }
        for row in rows
    ]


def _balance(session: Session, code: str) -> Decimal:
    total = sum(
        (
            Decimal(line.debit) - Decimal(line.credit)
            for line in session.scalars(select(JournalLine))
            if line.account == code
        ),
        Decimal(0),
    )
    return total.quantize(Decimal("0.000001"))


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
        company = Company(id=COMPANY, code="P4-GATE", name="Phase 4 gate",
                          base_currency="PHP", fiscal_year_start_month=1)
        session.add(company)
        session.commit()
        seed_stock_accounts(session, company_id=COMPANY)
        for code, name, account_class in (
            ("1230", "Work in Progress", "asset"),
            ("2150", "Labour Applied", "liability"),
            ("5950", "Production Variance", "expense"),
        ):
            create_account(session, company_id=COMPANY, code=code, name=name,
                           account_class=account_class)
        set_mapping(session, company_id=COMPANY, key=WIP_KEY, account_code="1230")
        set_mapping(session, company_id=COMPANY, key=LABOUR_KEY, account_code="2150")
        set_mapping(session, company_id=COMPANY, key=VARIANCE_KEY, account_code="5950")
        register_currency(session, company_id=COMPANY, code="PHP", name="Peso")
        set_credit_check_mode(session, company, mode="off")
        configure(session, company_id=COMPANY, doc_type="purchase_requisition",
                  name="Purchase requisition", levels=[(Decimal(0), "manager")])
        session.commit()

        # Three centres: the cutting bench, the assembly bench, and inspection. The
        # assembly bench is small on purpose — a capacity view has to be able to show a
        # period that is over what the centre can do.
        cutting = create_work_center(session, company_id=COMPANY, code="CUT",
                                    name="Cutting bench", capacity_minutes="480",
                                    capacity_period="day", downtime_percent="0")
        assembly = create_work_center(session, company_id=COMPANY, code="ASM",
                                      name="Assembly bench", capacity_minutes="120",
                                      capacity_period="day", downtime_percent="0")
        inspection = create_work_center(session, company_id=COMPANY, code="QC",
                                       name="Inspection", capacity_minutes="480",
                                       capacity_period="day", downtime_percent="0")
        set_rate(session, cutting, effective_from=date(2026, 1, 1), hourly_rate=CUT_RATE)
        set_rate(session, assembly, effective_from=date(2026, 1, 1), hourly_rate=ASM_RATE)
        set_rate(session, inspection, effective_from=date(2026, 1, 1), hourly_rate=QC_RATE)

        bicycle = create_item(session, company_id=COMPANY, sku="BICYCLE", name="Bicycle",
                              base_uom="each", traceability_mode="none",
                              standard_cost=BICYCLE_STANDARD)
        frame = create_item(session, company_id=COMPANY, sku="FRAME", name="Frame",
                            base_uom="each", traceability_mode="none",
                            standard_cost=FRAME_STANDARD)
        tube = create_item(session, company_id=COMPANY, sku="TUBE", name="Tube",
                           base_uom="each", traceability_mode="none", lead_time_days=3)
        wheel = create_item(session, company_id=COMPANY, sku="WHEEL", name="Wheel",
                            base_uom="each", traceability_mode="none", lead_time_days=1)
        session.commit()
        bicycle_bom = create_bom(session, company_id=COMPANY, item=bicycle)
        add_line(session, bicycle_bom, item=frame, quantity="1")
        add_line(session, bicycle_bom, item=wheel, quantity="2")
        add_operation(session, bicycle_bom, name="Assemble", work_center_code="ASM",
                      setup_minutes="20", run_minutes="12")
        add_operation(session, bicycle_bom, name="Inspect", work_center_code="QC",
                      setup_minutes="5", run_minutes="3")
        release(session, bicycle_bom)
        frame_bom = create_bom(session, company_id=COMPANY, item=frame)
        add_line(session, frame_bom, item=tube, quantity="5", scrap_percent="10")
        add_operation(session, frame_bom, name="Cut", work_center_code="CUT",
                      setup_minutes="10", run_minutes="5")
        release(session, frame_bom)
        warehouse = create_location(session, company_id=COMPANY, code="MAIN", name="Main",
                                    location_type="warehouse")
        zone = create_location(session, company_id=COMPANY, code="MAIN-Z", name="Zone",
                              location_type="zone", parent_id=warehouse.id)
        aisle = create_location(session, company_id=COMPANY, code="MAIN-Z-1", name="Aisle",
                               location_type="aisle", parent_id=zone.id)
        raw_bin = create_location(session, company_id=COMPANY, code="MAIN-Z-1-A",
                                  name="Raw store", location_type="bin", parent_id=aisle.id)
        goods_bin = create_location(session, company_id=COMPANY, code="MAIN-Z-1-B",
                                    name="Finished store", location_type="bin",
                                    parent_id=aisle.id)
        session.commit()
        # What is on the shelf before the period: 40 tubes at ten, 5 wheels at twenty —
        # both single receipts, so the moving average is exactly that unit price.
        receive(session, item=tube, location=raw_bin, uom="each", quantity="40",
                value=Decimal("400"), currency="PHP", source_type="goods_receipt",
                source_id=uuid.uuid4(), posting_date=date(2026, 10, 1))
        receive(session, item=wheel, location=raw_bin, uom="each", quantity="5",
                value=Decimal("100"), currency="PHP", source_type="goods_receipt",
                source_id=uuid.uuid4(), posting_date=date(2026, 10, 1))
        session.commit()

        customer = create_customer(session, company_id=COMPANY, party_code="ACME",
                                   name="Acme Cycles", payment_terms_days=30)
        session.commit()
        for number, quantity, placed in (("Q-1", "10", B1), ("Q-2", "6", B2)):
            quote = create_quotation(session, company_id=COMPANY, customer_id=customer.id,
                                     number=number, issued_on=B1,
                                     valid_until=date(2026, 12, 31))
            session.flush()
            quote_line(session, quote, line_no=1, description="Bicycle", quantity=quantity,
                       unit_price="1000.00", uom="each", item_id=bicycle.id, priced_on=placed)
            session.flush()
            order = convert_quotation_to_order(session, quote, number=f"SO-{number[2:]}",
                                               on=placed)
            confirm_order(session, order, actor="maria")
        session.commit()

        # 1 — the plan, hand-checked, and the jobs it raises
        run = run_mrp(session, company_id=COMPANY, start=START, horizon_days=HORIZON,
                      bucket_days=BUCKET, demand_sources=(SALES_ORDERS,), run_on=START)
        session.commit()
        plan = plan_sorted(plan_of(session, run))
        differences = _compare(_expected(), _actual(plan))
        assert len(plan) == 8, [row["item"] for row in plan]
        assert not differences, "the plan differs from the hand calculation: " + "; ".join(
            differences
        )
        compared = len(HAND) * len(FIELDS)
        print(
            f"1a. the plan's {len(plan)} rows over {2} levels match the hand calculation"
            f" field for field — {compared} of {compared} comparisons, 100.00 % (§6 metric 4)"
            f" — including the scrap inside the tube requirement (8 × 5 × 1.10 = 55) and the"
            " stock each week consumed"
        )
        made = raise_suggestions(session, run)
        session.commit()
        picks = {(row.item.sku, row.needed_by): row for row in made}
        bike_job = convert_suggestion(session, picks[("BICYCLE", B1)], actor="maria",
                                      on=START, number="WO-BIKE-1")
        frame_job = convert_suggestion(session, picks[("FRAME", B1)], actor="maria",
                                       on=START, number="WO-FRAME-1")
        tube_buy = convert_suggestion(session, picks[("TUBE", B1)], actor="mia.buyer",
                                      on=START, number="REQ-TUBE", estimated_unit_price="10")
        session.commit()
        job = work_order_by_number(session, company_id=COMPANY, number=bike_job["number"])
        frame_order = work_order_by_number(
            session, company_id=COMPANY, number=frame_job["number"]
        )
        assert job.source == "mrp" and frame_order.source == "mrp", (job.source, frame_order.source)
        assert (job.quantity, job.due_on) == (Decimal("10.000000"), B1), (job.quantity, job.due_on)
        # The requirement list is the whole explosion the order was raised from — the
        # bicycle job also names the tubes its frames are made of — while what the job
        # itself draws is its own components, level 1 (T-4.WO.01).
        expanded = {
            session.get(Item, row.item_id).sku: Decimal(row.quantity_required)
            for row in requirements_of(session, job)
        }
        assert expanded == {
            "FRAME": Decimal("10.000000"),
            "WHEEL": Decimal("20.000000"),
            "TUBE": Decimal("55.000000"),
        }, expanded
        drawn = {
            session.get(Item, row.item_id).sku: Decimal(row.quantity_required)
            for row in direct_requirements(session, job)
        }
        assert drawn == {"FRAME": Decimal("10.000000"), "WHEEL": Decimal("20.000000")}, drawn
        frame_needs = {
            session.get(Item, row.item_id).sku: Decimal(row.quantity_required)
            for row in requirements_of(session, frame_order)
        }
        assert frame_needs == {"TUBE": Decimal("55.000000")}, frame_needs
        print(
            f"1b. the plan's suggestions became the two jobs and the purchase: {job.number}"
            f" ({job.quantity} bicycles, sourced {job.source!r}) carries the explosion"
            f" {expanded} and draws its own {drawn}; {frame_order.number} needs"
            f" {frame_needs} (the 10 % scrap already inside); and the tube suggestion"
            f" became requisition {tube_buy['number']} for"
            f" {picks[('TUBE', B1)].quantity} — nothing was re-keyed to raise them"
        )

        # The procurement the plan asked for lands, then the walk begins.
        receive(session, item=tube, location=raw_bin, uom="each", quantity="16",
                value=Decimal("160"), currency="PHP", source_type="goods_receipt",
                source_id=uuid.uuid4(), posting_date=date(2026, 10, 2))
        receive(session, item=wheel, location=raw_bin, uom="each", quantity="15",
                value=Decimal("300"), currency="PHP", source_type="goods_receipt",
                source_id=uuid.uuid4(), posting_date=date(2026, 10, 3))
        session.commit()

        # 2 — raw materials to finished goods, document by document
        for order in (frame_order, job):
            advance(session, order, status=RELEASED)
            advance(session, order, status=IN_PROGRESS)
        session.commit()
        tube_issue = issue_material(session, frame_order, item=tube, location=raw_bin,
                                    quantity="55", on=B1, actor="ana")
        session.commit()
        scrap_issue = issue_material(
            session, frame_order, item=tube, location=raw_bin, quantity="1", on=B1,
            actor="ana", override=True,
            override_reason="a tube buckled in the press when the blade slipped",
        )
        session.commit()
        cut_card = open_card(session, frame_order, operation_sequence=1, operator="ana", on=B1)
        book_time(session, cut_card, setup_minutes="10", run_minutes="45", produced_quantity="10",
                  recorded_by="ana", booked_at=datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc))
        close_card(session, cut_card, actor="ana")
        session.commit()
        frame_receipt = receive_finished_goods(session, frame_order, location=goods_bin,
                                               quantity="10", on=B1, actor="ana")
        session.commit()
        assert wip_balance(session, frame_order) == Decimal("0.000000"), wip_balance(
            session, frame_order
        )
        assert received_quantity(session, frame_order) == Decimal("10.000000"), (
            received_quantity(session, frame_order)
        )
        frame_issue = issue_material(session, job, item=frame, location=goods_bin,
                                     quantity="10", on=B1, actor="ben")
        wheel_issue = issue_material(session, job, item=wheel, location=raw_bin,
                                     quantity="20", on=B1, actor="ben")
        session.commit()
        asm_card = open_card(session, job, operation_sequence=1, operator="ben", on=B1)
        book_time(session, asm_card, setup_minutes="20", run_minutes="100", produced_quantity="10",
                  recorded_by="ben", booked_at=datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc))
        close_card(session, asm_card, actor="ben")
        qc_card = open_card(session, job, operation_sequence=2, operator="cleo", on=B1)
        book_time(session, qc_card, setup_minutes="5", run_minutes="25", produced_quantity="10",
                  recorded_by="cleo", booked_at=datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc))
        close_card(session, qc_card, actor="cleo")
        session.commit()
        bike_receipt = receive_finished_goods(session, job, location=goods_bin,
                                              quantity="10", on=B1, actor="ben")
        session.commit()
        assert wip_balance(session, job) == Decimal("0.000000"), wip_balance(session, job)
        assert received_quantity(session, job) == Decimal("10.000000"), received_quantity(
            session, job
        )
        # Every document names the one before it: the movement names the issue or the
        # receipt that wrote it, and each card names its job.
        assert tube_issue.movement_id and frame_receipt.movement_id and bike_receipt.movement_id
        assert cut_card.work_order_id == frame_order.id and qc_card.work_order_id == job.id
        assert frame_receipt.completes is True and bike_receipt.completes is True
        # Each job consumed its own components and nobody else's: the 55 tubes are the
        # frame job's draw, and the bicycle job's tube row was not drawn at all — the
        # materials of a component that is itself built belong to that component's job.
        bike_tube = [row for row in requirements_of(session, job) if row.item_id == tube.id][0]
        assert issued_quantity(session, bike_tube) == Decimal("0.000000"), bike_tube
        assert issued_quantity(session, [row for row in requirements_of(session, frame_order)
                                         if row.item_id == tube.id][0]) == Decimal("56.000000")
        print(
            f"2. the cycle ran document by document: {tube_issue.quantity} tubes issued to"
            f" {frame_order.number} (plus {scrap_issue.quantity} scrapped),"
            f" {Decimal('55.000000')} minutes booked on CUT and the card closed,"
            f" {frame_receipt.quantity} frames received; then {frame_issue.quantity} frames"
            f" and {wheel_issue.quantity} wheels issued to {job.number}, both route steps"
            f" ({', '.join(card.operation.name for card in (asm_card, qc_card))}) carded and"
            f" closed, {bike_receipt.quantity} bicycles received — and both jobs' work in"
            " progress cleared to 0.000000"
        )

        # 3 — the cost, hand-checked, and the value the goods carry
        frame_cost = cost_work_order(session, frame_order)
        session.commit()
        # 55 tubes the BOM asked for, plus the one the press destroyed, at the ten a unit
        # the shelf carries them at: 560. The 55 minutes on the cutting bench are 55/60 of
        # 300 an hour = 275.
        frames = cost_summary(session, frame_order)
        assert frames["material"] == Decimal("560.000000"), frames
        assert frames["labour"] == Decimal("275.000000"), frames
        assert frames["total"] == Decimal("835.000000"), frames
        assert frames["expected"] == Decimal("500.000000"), frames
        assert frames["variance"] == Decimal("335.000000"), frames
        assert frames["finished_goods"] == Decimal("835.000000"), frames
        bike_cost = cost_work_order(session, job)
        session.commit()
        # The frames leave stock at the ten a unit the *ledger* carries them at (560 for
        # the lot — their material only, see the finding below) and the wheels at twenty:
        # 960 of material. Assembly 120 minutes at 400 = 800, inspection 30 at 600 = 300.
        bikes = cost_summary(session, job)
        assert bikes["material"] == Decimal("960.000000"), bikes
        assert bikes["labour"] == Decimal("1100.000000"), bikes
        assert bikes["total"] == Decimal("2060.000000"), bikes
        assert bikes["expected"] == Decimal("2500.000000"), bikes
        assert bikes["variance"] == Decimal("-440.000000"), bikes
        assert finished_goods_value(session, job) == bikes["total"] == Decimal("2060.000000"), (
            finished_goods_value(session, job),
            bikes,
        )
        rows = {row["work_center"]: row for row in bikes["labour_rows"]}
        assert (rows["ASM"]["minutes"], rows["ASM"]["hourly_rate"], rows["ASM"]["cost"]) == (
            Decimal("120.000000"), ASM_RATE, Decimal("800.000000")
        ), rows["ASM"]
        assert (rows["QC"]["minutes"], rows["QC"]["hourly_rate"], rows["QC"]["cost"]) == (
            Decimal("30.000000"), QC_RATE, Decimal("300.000000")
        ), rows["QC"]
        print(
            f"3a. the frame job cost {frames['total']} — {frames['material']} of tubes (56 at"
            f" ten) plus {frames['labour']} of cutting (55 minutes at {CUT_RATE}) — and the"
            f" bicycle job cost {bikes['total']}: {bikes['material']} of material plus"
            f" {bikes['labour']} of time ({rows['ASM']['minutes']} minutes on ASM at"
            f" {ASM_RATE} = {rows['ASM']['cost']}, {rows['QC']['minutes']} on QC at"
            f" {QC_RATE} = {rows['QC']['cost']}), which is what the finished bicycles carry"
            " — the criterion's own figures, hand-checked"
        )
        entries = {
            "frame": _lines(session, session.get(JournalEntry, frame_cost.entry_id)),
            "bike": _lines(session, session.get(JournalEntry, bike_cost.entry_id)),
        }
        # The frame job: inventory takes the 275 of labour the receipts could not know;
        # the applied account is debited 60, because the standard allowed the output 500
        # while the job had already used 560 of material; the variance is credited 335.
        assert entries["frame"] == [
            {"account": "1200", "debit": Decimal("275.000000"), "credit": Decimal("0.000000")},
            {"account": "2150", "debit": Decimal("60.000000"), "credit": Decimal("0.000000")},
            {"account": "5950", "debit": Decimal("0.000000"), "credit": Decimal("335.000000")},
        ], entries["frame"]
        # The bicycle job: inventory takes its 1100 of labour; the applied account is
        # credited 1540 (the standard's 2500 less the 960 of material), and the 440 the
        # job came in under is debited to the variance.
        assert entries["bike"] == [
            {"account": "1200", "debit": Decimal("1100.000000"), "credit": Decimal("0.000000")},
            {"account": "2150", "debit": Decimal("0.000000"), "credit": Decimal("1540.000000")},
            {"account": "5950", "debit": Decimal("440.000000"), "credit": Decimal("0.000000")},
        ], entries["bike"]
        inventory = _balance(session, "1200")
        # By hand: 400 + 100 of opening raw materials, 160 + 300 of planned purchases,
        # 560 of tubes out to the frame job and 560 of frames back in, 560 of frames and
        # 400 of wheels out to the bicycle job, 960 of bicycles in, then the two jobs'
        # 275 + 1100 of labour.
        assert inventory == Decimal("2335.000000"), inventory
        assert _balance(session, "1230") == Decimal("0.000000"), _balance(session, "1230")
        print(
            f"3b. the two costing entries post what the goods are worth: inventory reads"
            f" {inventory} by hand (the raw materials in, the material through both jobs,"
            f" and the {frames['labour'] + bikes['labour']} of labour), work in progress is"
            f" {_balance(session, '1230')} because both jobs cleared, and the variance"
            f" accounts hold {_balance(session, '5950')} — 335 credited on the frame job,"
            " 440 debited on the bicycle job"
        )
        # The ceiling this measures: a sub-assembly's labour reaches the inventory account
        # as a total, and the stock ledger's moving average for the frames carries their
        # material only, so the frames leave stock at 56 a unit. The account reconciles —
        # both jobs' cost is in it — but a stock report of the items does not agree with
        # it by the 275 the frame job spent on time.
        # ponytail: sub-assembly labour is not rolled into the parent's unit cost. The
        # inventory account and the item ledger disagree by the labour of any intermediate
        # that was costed and then consumed; rolling it up needs a stock revaluation.
        frames_issued_value = issued_value(session, job)
        assert frames_issued_value == Decimal("960.000000"), frames_issued_value
        assert inventory - bikes["total"] == frames["labour"], (
            inventory - bikes["total"], frames["labour"]
        )
        print(
            f"3c. measured, not passed over: the frames left stock at 56 a unit"
            f" ({frame_receipt.value} of material over 10) because the ledger's moving"
            f" average does not carry the frame job's {frames['labour']} of labour, so the"
            f" inventory account holds {inventory} while the bicycle job's own cost is"
            f" {bikes['total']} — the difference being exactly that labour, still in the"
            " account and attributed to no unit (a finding against T-4.WO.05's roll-up)"
        )

        # 4 — scrap attributed to cost rather than lost
        requirement = [row for row in requirements_of(session, frame_order)
                       if row.item_id == tube.id][0]
        assert Decimal(requirement.quantity_required) == Decimal("55.000000"), requirement
        assert issued_quantity(session, requirement) == Decimal("56.000000"), (
            issued_quantity(session, requirement)
        )
        assert scrap_issue.overridden is True, scrap_issue
        assert scrap_issue.override_actor == "ana", scrap_issue.override_actor
        assert "buckled" in (scrap_issue.override_reason or ""), scrap_issue.override_reason
        # 55 tubes would have been 550: the 560 the job cost is the loss carried by the
        # goods rather than written off anywhere.
        assert frames["material"] - Decimal("550") == Decimal("10.000000"), frames
        print(
            f"4. the press destroyed one tube: the requirement said {requirement.quantity_required}"
            f" (the BOM's 10 % scrap already inside) and the job consumed"
            f" {issued_quantity(session, requirement)}, the extra one on the record as an"
            f" OverIssue override naming {scrap_issue.override_actor} and why — so the"
            f" {frames['material']} of material is 550 + that one at ten, carried by the"
            " goods, and nothing was written off silently"
        )

        # 5 — the capacity view, from the real work orders
        profile = capacity_profile(session, company_id=COMPANY, start=B1,
                                   end=date(2026, 10, 18), bucket_days=1)
        loaded = [row for row in profile["periods"] if row["load_minutes"] > 0]
        over = [row for row in profile["periods"] if row["overloaded"]]
        # Both jobs are dated 10-05 by the plan, and the load is the route's own minutes
        # for the quantity ordered: CUT 10 + 5 × 10 = 60, ASM 20 + 12 × 10 = 140,
        # QC 5 + 3 × 10 = 35.
        assert profile["overloaded_periods"] == 1, over
        assert [(row["work_center"], row["load_minutes"], row["capacity_minutes"])
                for row in over] == [("ASM", Decimal("140.000000"), Decimal("120.000000"))], over
        assert {(row["work_center"], row["load_minutes"]) for row in loaded} == {
            ("CUT", Decimal("60.000000")),
            ("ASM", Decimal("140.000000")),
            ("QC", Decimal("35.000000")),
        }, loaded
        assert all(row["period_start"] == B1 for row in loaded), loaded
        named = {order["work_order"] for row in loaded for order in row["orders"]}
        assert named == {"WO-BIKE-1", "WO-FRAME-1"}, named
        idle = [row for row in profile["periods"]
                if row["period_start"] == date(2026, 10, 6) and row["load_minutes"] == 0]
        assert len(idle) == 3, idle
        by_center = {row["work_center"]: row["load_minutes"] for row in loaded}
        print(
            f"5. the capacity view over {B1}…2026-10-18 shows"
            f" {profile['overloaded_periods']} overloaded period and"
            f" {len([row for row in profile['periods'] if row['load_minutes'] == 0])} idle"
            f" ones: ASM carries {over[0]['load_minutes']} minutes on {over[0]['period_start']}"
            f" against a {over[0]['capacity_minutes']} minute day"
            f" (utilisation {over[0]['utilisation']}), while CUT's {by_center['CUT']} and"
            f" QC's {by_center['QC']} sit inside their own days — every figure traceable to"
            f" the two work orders either centre is loading ({sorted(named)})"
        )

        # 6 — every posting the cycle wrote balances
        entries = list(session.scalars(select(JournalEntry).where(JournalEntry.company_id
                                                                  == COMPANY)))
        assert entries, "the cycle wrote no postings"
        for entry in entries:
            lines = list(session.scalars(
                select(JournalLine).where(JournalLine.entry_id == entry.id)
            ))
            assert len(lines) >= 2, (entry.id, len(lines))
            debits = sum((Decimal(line.debit) for line in lines), Decimal(0))
            credits = sum((Decimal(line.credit) for line in lines), Decimal(0))
            assert debits == credits, (entry.source_type, debits, credits)
        totals = session.execute(
            select(JournalLine.debit, JournalLine.credit)
            .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
            .where(JournalEntry.company_id == COMPANY)
        ).all()
        debits = sum((Decimal(row[0]) for row in totals), Decimal(0))
        credits = sum((Decimal(row[1]) for row in totals), Decimal(0))
        assert debits == credits, (debits, credits)
        sources = sorted({entry.source_type for entry in entries})
        print(
            f"6. all {len(entries)} postings the cycle wrote balance — every entry has at"
            f" least two lines and equal debits and credits, and the company's whole ledger"
            f" nets to zero ({debits} either side) — across {sources} (§6 metric 1)"
        )

    print("\ncheck_phase4_exit: all assertions green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
