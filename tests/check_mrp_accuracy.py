"""T-4.MRP.03 check — the net requirements, verified against a hand calculation.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_mrp_accuracy.py

Green on all five:

1. a dataset that exercises **three BOM levels, scrap, partial stock and open supply
   from both sources** (a released work order and an approved purchase order), over a
   four-bucket horizon, produces the rows a planner reads — stated per item per bucket
2. **every field of every row equals the figure calculated by hand** — gross, available,
   supply, net, kind, level, lead time, release date and the shortage mark, compared
   field by field (8 rows × 10 fields): **100.00 % exact**, the metric §6/§4 set
3. the engine's own arithmetic **reconciles row by row**: each net is
   `gross - available - supply`, and each component's gross is the level above's net
   times its BOM quantity up-lifted for scrap — so agreement is not a coincidence of
   two methods reproducing one bug
4. the comparison is **not vacuous**: a single unit of difference on one row is reported
   as a difference, so "100 %" means the engine matched rather than that nothing was
   compared — any difference is a defect for the owning task, not something explained away
5. the one convention the figures rest on is **measured and stated**: the plan reads stock
   as T-1.INV.03's ledger sum as of the horizon's end, so a receipt dated inside the
   horizon covers an earlier bucket too — shown by adding one, rather than left as an
   unstated assumption behind a 100 % figure
"""

from __future__ import annotations

import copy
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
from app.ledger.currency import register_currency  # noqa: E402
from app.manufacturing.bom import add_line, create_bom, release  # noqa: E402
from app.manufacturing.mrp import SALES_ORDERS, plan_of, plan_sorted, run_mrp  # noqa: E402
from app.manufacturing.work_orders import create_work_order  # noqa: E402
from app.procurement.orders import award, submit_order  # noqa: E402
from app.procurement.requisitions import (  # noqa: E402
    create_requisition,
    record_decision,
    submit,
)
from app.procurement.rfq import issue_rfq, record_response  # noqa: E402
from app.procurement.suppliers import add_tax_identifier, create_supplier  # noqa: E402
from app.sales.customers import create_customer  # noqa: E402
from app.sales.fulfilment import Shipment  # noqa: E402,F401 — for its table
from app.sales.orders import confirm_order, convert_quotation_to_order  # noqa: E402
from app.sales.pipeline import Opportunity  # noqa: E402,F401 — for its table
from app.sales.quotations import add_line as quote_line  # noqa: E402
from app.sales.quotations import create_quotation  # noqa: E402
from app.stock.items import create_item  # noqa: E402
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

# ---------------------------------------------------------------------------
# The hand calculation.
#
# Written from the dataset on paper and typed in as figures — never read back from
# the engine, which is the whole point of a verification. The bucket-1 and bucket-2
# figures come out of this arithmetic, in bucket order:
#
#   bucket 1 (10-05 … 10-11)
#     BICYCLE  10 ordered on 10-06, less the 2-unit job already open       = 8
#     FRAME    8 bicycles × 1 frame                                        = 8
#     TUBE     8 frames × 5 tubes × 1.10 scrap up-lift = 44, less 4 on hand = 40
#     WHEEL    8 bicycles × 2 wheels = 16, less 6 on hand                  = 10
#   bucket 2 (10-12 … 10-18)
#     BICYCLE  4 ordered on 10-14 (the open job was spent above)           = 4
#     FRAME    4 × 1                                                       = 4
#     TUBE     4 × 5 × 1.10 = 22, and the 4 on hand are already spent      = 22
#     WHEEL    4 × 2 = 8, less the 5-unit PO landing 10-16                 = 3
#              (the wheel is bought on a one-day lead: order by 10-11)
#
# The two made items are made to order with no lead time, so a component requirement
# sits in the bucket its parent is wanted in; the two bought items carry lead times, so
# the plan states the day each order has to be *placed* — the tube's is 10-02, before the
# horizon opens, which is what the plan says rather than what is convenient. The shortage
# mark says a row cannot be built because its component is short in the bucket that
# component is needed in.
# ---------------------------------------------------------------------------
HAND = (
    {
        "item": "BICYCLE",
        "bucket": B1,
        "level": 0,
        "gross": "10",
        "available": "0",
        "supply": "2",
        "net": "8",
        "kind": "make",
        "lead_time_days": 0,
        "release_on": date(2026, 10, 5),
        "constrained": True,
        "constrained_by": "FRAME, WHEEL",
        "why": "10 ordered less the open 2-unit job",
    },
    {
        "item": "FRAME",
        "bucket": B1,
        "level": 1,
        "gross": "8",
        "available": "0",
        "supply": "0",
        "net": "8",
        "kind": "make",
        "lead_time_days": 0,
        "release_on": date(2026, 10, 5),
        "constrained": True,
        "constrained_by": "TUBE",
        "why": "8 bicycles × 1 frame, none on hand",
    },
    {
        "item": "TUBE",
        "bucket": B1,
        "level": 2,
        "gross": "44",
        "available": "4",
        "supply": "0",
        "net": "40",
        "kind": "buy",
        "lead_time_days": 3,
        "release_on": date(2026, 10, 2),
        "constrained": False,
        "constrained_by": None,
        "why": "8 frames × 5 tubes, +10 % scrap, less the 4 on the shelf",
    },
    {
        "item": "WHEEL",
        "bucket": B1,
        "level": 1,
        "gross": "16",
        "available": "6",
        "supply": "0",
        "net": "10",
        "kind": "buy",
        "lead_time_days": 1,
        "release_on": date(2026, 10, 4),
        "constrained": False,
        "constrained_by": None,
        "why": "8 bicycles × 2 wheels, less the 6 on the shelf; the PO lands later",
    },
    {
        "item": "BICYCLE",
        "bucket": B2,
        "level": 0,
        "gross": "4",
        "available": "0",
        "supply": "0",
        "net": "4",
        "kind": "make",
        "lead_time_days": 0,
        "release_on": date(2026, 10, 12),
        "constrained": True,
        "constrained_by": "FRAME, WHEEL",
        "why": "4 ordered in the second week, nothing left to consume on it",
    },
    {
        "item": "FRAME",
        "bucket": B2,
        "level": 1,
        "gross": "4",
        "available": "0",
        "supply": "0",
        "net": "4",
        "kind": "make",
        "lead_time_days": 0,
        "release_on": date(2026, 10, 12),
        "constrained": True,
        "constrained_by": "TUBE",
        "why": "4 bicycles × 1 frame",
    },
    {
        "item": "TUBE",
        "bucket": B2,
        "level": 2,
        "gross": "22",
        "available": "0",
        "supply": "0",
        "net": "22",
        "kind": "buy",
        "lead_time_days": 3,
        "release_on": date(2026, 10, 9),
        "constrained": False,
        "constrained_by": None,
        "why": "4 frames × 5 tubes, +10 % scrap; the shelf stock went in week 1",
    },
    {
        "item": "WHEEL",
        "bucket": B2,
        "level": 1,
        "gross": "8",
        "available": "0",
        "supply": "5",
        "net": "3",
        "kind": "buy",
        "lead_time_days": 1,
        "release_on": date(2026, 10, 11),
        "constrained": False,
        "constrained_by": None,
        "why": "8 wheels, less the 5 arriving 10-16; placed a day before the week it is wanted",
    },
)

# The fields compared, one by one — every figure and date a planner acts on.
FIELDS = (
    "gross",
    "available",
    "supply",
    "net",
    "kind",
    "level",
    "lead_time_days",
    "release_on",
    "constrained",
    "constrained_by",
)


def _expected(rows) -> dict[tuple[str, date], dict]:
    out = {}
    for row in rows:
        out[(row["item"], row["bucket"])] = {
            field: (Decimal(row[field]).quantize(Decimal("0.000001"))
                    if field in ("gross", "available", "supply", "net")
                    else row[field])
            for field in FIELDS
        }
    return out


def _actual(rows: list[dict]) -> dict[tuple[str, date], dict]:
    return {
        (row["item"], row["bucket_start"]): {field: row[field] for field in FIELDS}
        for row in rows
    }


def _compare(expected: dict, actual: dict) -> list[str]:
    """Field-by-field differences between what was calculated and what the engine said.

    Reports a missing row, an extra row, and every field that differs — the comparison
    the accuracy figure is counted from.
    """
    differences: list[str] = []
    for key in sorted(expected, key=lambda row: (row[1], row[0])):
        if key not in actual:
            differences.append(f"{key[1]} {key[0]}: expected, the engine has no row")
            continue
        for field in FIELDS:
            want, got = expected[key][field], actual[key][field]
            if want != got:
                differences.append(
                    f"{key[1]} {key[0]} {field}: expected {want!r}, engine {got!r}"
                )
    for key in sorted(set(actual) - set(expected), key=lambda row: (row[1], row[0])):
        differences.append(f"{key[1]} {key[0]}: the engine has a row nothing expected")
    return differences


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
        company = Company(id=COMPANY, code="MRPA-CHECK", name="MRP accuracy check",
                          base_currency="PHP", fiscal_year_start_month=1)
        session.add(company)
        session.commit()
        seed_stock_accounts(session, company_id=COMPANY)
        register_currency(session, company_id=COMPANY, code="PHP", name="Peso")
        set_credit_check_mode(session, company, mode="off")
        for doc_type, name in (("purchase_requisition", "Purchase requisition"),
                               ("purchase_order", "Purchase order")):
            configure(session, company_id=COMPANY, doc_type=doc_type, name=name,
                      levels=[(Decimal(0), "manager")])
        session.commit()

        # A bicycle is a frame and two wheels; a frame is five tubes with 10 % scrap;
        # tubes are bought on a three-day lead time, wheels on one.
        bicycle = create_item(session, company_id=COMPANY, sku="BICYCLE", name="Bicycle",
                              base_uom="each", traceability_mode="none")
        frame = create_item(session, company_id=COMPANY, sku="FRAME", name="Frame",
                            base_uom="each", traceability_mode="none")
        tube = create_item(session, company_id=COMPANY, sku="TUBE", name="Tube",
                           base_uom="each", traceability_mode="none", lead_time_days=3)
        wheel = create_item(session, company_id=COMPANY, sku="WHEEL", name="Wheel",
                            base_uom="each", traceability_mode="none", lead_time_days=1)
        session.commit()
        bicycle_bom = create_bom(session, company_id=COMPANY, item=bicycle)
        add_line(session, bicycle_bom, item=frame, quantity="1")
        add_line(session, bicycle_bom, item=wheel, quantity="2")
        release(session, bicycle_bom)
        frame_bom = create_bom(session, company_id=COMPANY, item=frame)
        add_line(session, frame_bom, item=tube, quantity="5", scrap_percent="10")
        release(session, frame_bom)
        warehouse = create_location(session, company_id=COMPANY, code="MAIN", name="Main",
                                    location_type="warehouse")
        zone = create_location(session, company_id=COMPANY, code="MAIN-Z", name="Zone",
                               location_type="zone", parent_id=warehouse.id)
        aisle = create_location(session, company_id=COMPANY, code="MAIN-Z-1", name="Aisle",
                               location_type="aisle", parent_id=zone.id)
        bin_a = create_location(session, company_id=COMPANY, code="MAIN-Z-1-A", name="Bin A",
                                location_type="bin", parent_id=aisle.id)
        session.commit()
        # Partial stock: four tubes and six wheels, on the shelf before the horizon.
        for item, quantity in ((tube, "4"), (wheel, "6")):
            receive(session, item=item, location=bin_a, uom="each", quantity=quantity,
                    value=Decimal(quantity) * Decimal("10"), currency="PHP",
                    source_type="goods_receipt", source_id=uuid.uuid4(),
                    posting_date=date(2026, 10, 1))
        session.commit()

        customer = create_customer(session, company_id=COMPANY, party_code="ACME",
                                   name="Acme Cycles", payment_terms_days=30)
        session.commit()
        # One order placed in week 1 for ten, one in week 2 for four: two quotations,
        # because a quotation is converted once.
        for number, quantity, placed in (("Q-1", "10", START), ("Q-2", "4", date(2026, 10, 14))):
            quote = create_quotation(session, company_id=COMPANY, customer_id=customer.id,
                                     number=number, issued_on=START,
                                     valid_until=date(2026, 12, 31))
            session.flush()
            quote_line(session, quote, line_no=1, description="Bicycle", quantity=quantity,
                       unit_price="100.00", uom="each", item_id=bicycle.id, priced_on=placed)
            session.flush()
            order = convert_quotation_to_order(session, quote, number=f"SO-{number[2:]}", on=placed)
            confirm_order(session, order, actor="maria")
        session.commit()

        # Open supply, both sources: a job for two bicycles due in week 1, and an
        # approved purchase order for five wheels required in week 2.
        create_work_order(session, company_id=COMPANY, item=bicycle, quantity="2",
                          number="WO-OPEN", created_on=START,
                          due_on=date(2026, 10, 7))
        session.commit()
        supplier = create_supplier(session, company_id=COMPANY, party_code="TUBCO",
                                   name="Tube & Co", payment_terms_days=30)
        add_tax_identifier(session, supplier, kind="tin", value="001-234-567")
        session.commit()
        requisition = create_requisition(
            session, company_id=COMPANY, number="REQ-WHEELS", requested_by="rina.requester",
            needed_by=date(2026, 10, 16), currency="PHP",
            lines=[{"description": "Wheel", "quantity": "5", "uom": "each",
                    "estimated_unit_price": "10", "item_sku": "WHEEL"}],
        )
        session.commit()
        submit(session, requisition, actor="rina.requester")
        record_decision(session, requisition, actor="mia.manager", action=APPROVE,
                        role="manager")
        session.commit()
        rfq = issue_rfq(session, requisition=requisition, number="RFQ-1",
                        supplier_codes=["TUBCO"], response_deadline=date(2026, 10, 2),
                        issued_on=date(2026, 10, 1))
        session.commit()
        record_response(session, rfq, supplier_code="TUBCO", received_on=date(2026, 10, 2),
                        lines=[{"line_no": 1, "unit_price": "10"}])
        session.commit()
        order = award(
            session, rfq=rfq, actor="bob.buyer",
            awards=[{"supplier_code": "TUBCO", "number": "PO-1",
                     "required_date": date(2026, 10, 16),
                     "lines": [{"line_no": 1, "quantity": "5"}]}],
        )[0]
        session.commit()
        # A draft order is not supply; approving it is what makes it one.
        assert order.status == "draft", order.status
        submit_order(session, order, actor="bob.buyer")
        session.commit()
        assert order.status in ("pending", "approved"), order.status

        run = run_mrp(session, company_id=COMPANY, start=START, horizon_days=HORIZON,
                      bucket_days=BUCKET, demand_sources=(SALES_ORDERS,), run_on=START)
        session.commit()
        plan = plan_sorted(plan_of(session, run))

        # 1 — the rows, stated per item per bucket
        assert len(plan) == 8, [row["item"] for row in plan]
        for row in plan:
            derived = next(
                entry["why"] for entry in HAND
                if entry["item"] == row["item"] and entry["bucket"] == row["bucket_start"]
            )
            print(
                f"   {row['bucket_start']} {row['item']:<8} gross {row['gross']:>10}"
                f" available {row['available']:>10} supply {row['supply']:>10}"
                f" net {row['net']:>10} {row['kind']:<4} release {row['release_on']}"
                f" | {derived}"
            )
        print(
            f"1. {len(plan)} rows over {HORIZON // BUCKET} buckets and {3} levels —"
            f" {sorted({row['item'] for row in plan})} — with the stock on hand, the open"
            " job and the approved purchase order each stated on the row that consumed them"
        )

        # 2 — the hand calculation, compared field by field
        expected = _expected(HAND)
        actual = _actual(plan)
        differences = _compare(expected, actual)
        assert not differences, "net requirements differ from the hand calculation: " + "; ".join(
            differences
        )
        compared = len(expected) * len(FIELDS)
        exact = compared - len(differences)
        exact_rate = (Decimal(exact) / Decimal(compared)) * Decimal(100)
        print(
            f"2. the plan matches the hand calculation exactly — {exact} of {compared}"
            f" field comparisons ({len(expected)} rows × {len(FIELDS)} fields),"
            f" {exact_rate.quantize(Decimal('0.01'))} % — so §6 metric 4's target of 100 %"
            " exact holds on this dataset"
        )

        # 3 — and the engine's own arithmetic reconciles, row by row
        for row in plan:
            # net = gross - available - open supply, floored at zero
            assert row["net"] == max(
                Decimal(0), row["gross"] - row["available"] - row["supply"]
            ), row
        rows = {(row["item"], row["bucket_start"]): row for row in plan}
        # Each child's gross is the parent's net × BOM quantity × its scrap up-lift.
        assert rows[("FRAME", B1)]["gross"] == rows[("BICYCLE", B1)]["net"] * 1, rows
        assert rows[("WHEEL", B1)]["gross"] == rows[("BICYCLE", B1)]["net"] * 2, rows
        assert rows[("TUBE", B1)]["gross"] == rows[("FRAME", B1)]["net"] * 5 * Decimal("1.1"), rows
        assert rows[("FRAME", B2)]["gross"] == rows[("BICYCLE", B2)]["net"] * 1, rows
        assert rows[("WHEEL", B2)]["gross"] == rows[("BICYCLE", B2)]["net"] * 2, rows
        assert rows[("TUBE", B2)]["gross"] == rows[("FRAME", B2)]["net"] * 5 * Decimal("1.1"), rows
        # The stock was spent once and only once, and the PO's five wheels once.
        assert sum(rows[("TUBE", bucket)]["available"] for bucket in (B1, B2)) == Decimal(
            "4.000000"
        ), rows
        assert sum(rows[("WHEEL", bucket)]["available"] for bucket in (B1, B2)) == Decimal(
            "6.000000"
        ), rows
        assert sum(rows[("WHEEL", bucket)]["supply"] for bucket in (B1, B2)) == Decimal(
            "5.000000"
        ), rows
        print(
            "3. every row reconciles with the arithmetic the plan documents — each net is"
            " its gross less the stock and open supply it consumed, each component's gross"
            " is the level above's net times its BOM quantity up-lifted for scrap (8 × 5 ×"
            " 1.10 = 44), the four tubes on hand were spent in week 1 and not again, and the"
            " purchase order's five wheels were claimed once"
        )

        # 4 — the comparison is not vacuous
        wrong = copy.deepcopy(HAND)
        wrong[0]["net"] = "7"  # one unit out, on the first row only
        caught = _compare(_expected(wrong), actual)
        assert len(caught) == 1, caught
        assert caught[0].startswith(f"{B1} BICYCLE net: expected"), caught
        assert _compare(_expected(HAND), actual) == [], "the comparison is unstable"
        print(
            f"4. the same comparison reports a difference the moment one is there — a single"
            f" unit on one row gives exactly one finding ({caught[0]}) — so the 100 % above is"
            " the engine agreeing with the hand calculation rather than nothing being compared"
        )

        # 5 — the convention behind the figures, stated rather than assumed
        # The plan treats stock as the ledger sum as of the horizon's end (T-1.INV.03),
        # so a receipt dated *inside* the horizon is available to a bucket before it
        # lands. That is what the module documents; measuring it here means the 100 %
        # above is not resting on an unstated assumption. Dating stock inside the
        # horizon would be a change to T-4.MRP.01, not something this check can assume.
        # ponytail: stock is not dated within the horizon — a receipt landing in week 3
        # covers a shortage in week 1, which over-states how soon the shelf is stocked.
        receive(session, item=tube, location=bin_a, uom="each", quantity="4",
                value=Decimal("40"), currency="PHP", source_type="goods_receipt",
                source_id=uuid.uuid4(), posting_date=date(2026, 10, 20))
        session.commit()
        later = run_mrp(session, company_id=COMPANY, start=START, horizon_days=HORIZON,
                        bucket_days=BUCKET, demand_sources=(SALES_ORDERS,), run_on=START)
        session.commit()
        moved = _actual(plan_sorted(plan_of(session, later)))
        assert moved[("TUBE", B1)]["available"] == Decimal("8.000000"), moved[("TUBE", B1)]
        assert moved[("TUBE", B1)]["net"] == Decimal("36.000000"), moved[("TUBE", B1)]
        # It went to the earliest bucket that needs it, and nothing else moved.
        assert moved[("TUBE", B2)]["available"] == Decimal("0.000000"), moved[("TUBE", B2)]
        assert moved[("TUBE", B2)]["net"] == Decimal("22.000000"), moved[("TUBE", B2)]
        touched = [key for key in actual if moved[key] != actual[key]]
        assert touched == [("TUBE", B1)], touched
        print(
            "5. the one convention the figures rest on is measured: four tubes received"
            " inside the horizon (10-20, two buckets after they were wanted) moved bucket"
            " 1's available stock from 4 to 8 and its net from 40 to 36 — the plan reads"
            " stock as the ledger sum as of the horizon's end, so it is not dated within"
            " the horizon — and that was the only row that changed"
        )

    print("\ncheck_mrp_accuracy: all assertions green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
