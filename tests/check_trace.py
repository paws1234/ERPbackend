"""T-6.TRACE.03 check — the trace: backward to the supplier, forward to the customer.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_trace.py

It fails (non-zero exit) if any of these stops holding:

1. **backward from a sold batch reaches its origin** — the finished lot traces to the work
   order that produced it, through that order to the raw material it consumed, and from there
   to the **supplier's goods receipt** that brought it in, each hop naming its document
2. **forward from a received batch reaches everything that used it** — the raw lot traces to
   the work order that consumed it, to the finished lot that work order produced, and to the
   **customer** the finished lot was shipped to: the chain crosses the production step rather
   than stopping at it
3. **the hops are ordered and labelled** — depth increases through the chain, every hop names a
   document and its type and id, and a hop that names a party or a work order names the right one
4. **a recall is the same walk, counted** — the customers, suppliers, work orders, locations and
   quantities the identity (or what it became) reached, and it is **not** limited to a period:
   a window narrows the report, never the chain
5. **the report exports** — `trace_csv` writes a row per hop with the document each hop came
   from, and a second batch of the same item traces separately (identity, not item)
6. **units trace too** — a serial-tracked item received from the supplier and shipped to the
   customer traces backward to the receipt and forward to the shipment, so the trace is not a
   batch-only report

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import csv
import io
import os
import sys
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.audit import set_actor  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base, scope_to_company  # noqa: E402
from app.ledger.accounts import create_account  # noqa: E402
from app.ledger.currency import register_currency  # noqa: E402
from app.ledger.mapping import set_mapping  # noqa: E402
from app.manufacturing.bom import add_line, create_bom, release  # noqa: E402
from app.manufacturing.issues import issue_material  # noqa: E402
from app.manufacturing.receipts import receive_finished_goods  # noqa: E402
from app.manufacturing.work_orders import (  # noqa: E402
    IN_PROGRESS,
    RELEASED,
    advance,
    create_work_order,
)
from app.procurement.orders import award, decide_order, submit_order  # noqa: E402
from app.procurement.receipts import create_receipt, post_receipt  # noqa: E402
from app.procurement.requisitions import (  # noqa: E402
    create_requisition,
    record_decision as decide_requisition,
    submit as submit_requisition,
)
from app.procurement.rfq import issue_rfq, record_response  # noqa: E402
from app.procurement.suppliers import add_tax_identifier, create_supplier  # noqa: E402
from app.sales.customers import create_customer  # noqa: E402
from app.sales.pipeline import Opportunity  # noqa: E402,F401 — the quotation's FK target
from app.sales.fulfilment import generate_pick_list, record_picked, ship_order  # noqa: E402
from app.sales.orders import confirm_order, convert_quotation_to_order  # noqa: E402
from app.sales.quotations import add_line as add_quote_line, create_quotation  # noqa: E402
from app.stock.items import create_item  # noqa: E402
from app.stock.locations import create_location  # noqa: E402
from app.stock.trace import TraceError, recall, trace, trace_csv  # noqa: E402
from app.workflow import APPROVE  # noqa: E402
from tests.seed import seed_stock_accounts  # noqa: E402

COMPANY = uuid.uuid4()
DAY = date(2026, 9, 17)
DEADLINE = date(2026, 9, 25)


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
            Company(id=COMPANY, code="TRACE", name="Trace", base_currency="PHP",
                    fiscal_year_start_month=1)
        )
        session.commit()
        scope_to_company(session, COMPANY)
        set_actor(session, "mia")
        seed_stock_accounts(session, company_id=COMPANY)
        register_currency(session, company_id=COMPANY, code="PHP", name="Peso")
        for key, code in (("receivables", "1100"), ("revenue", "4000"),
                          ("output_tax", "2200"), ("input_tax", "1300"),
                          ("work_in_progress", "1210"), ("finished_goods", "1220")):
            if code in ("2200", "1300", "1220"):
                create_account(session, company_id=COMPANY, code=code, name=f"Account {code}",
                               account_class="asset")
            set_mapping(session, company_id=COMPANY, key=key, account_code=code)
        from app.workflow import configure

        configure(session, company_id=COMPANY, doc_type="purchase_requisition",
                  name="Purchase requisition", levels=[(0, "manager")])
        configure(session, company_id=COMPANY, doc_type="purchase_order",
                  name="Purchase order", levels=[(0, "manager")])
        session.commit()

        # The identities: a raw lot received from a supplier, a finished lot produced from it,
        # and a unit that is received and shipped as one piece.
        milk = create_item(session, company_id=COMPANY, sku="MILK", name="Milk",
                           base_uom="litre", traceability_mode="batch_lot")
        yoghurt = create_item(session, company_id=COMPANY, sku="YOG", name="Yoghurt",
                              base_uom="tub", traceability_mode="batch_lot")
        gadget = create_item(session, company_id=COMPANY, sku="GADGET", name="Gadget",
                             base_uom="each", traceability_mode="serial")
        store = create_location(session, company_id=COMPANY, code="WH1", name="Main",
                                location_type="warehouse")
        zone = create_location(session, company_id=COMPANY, code="WH1-Z", name="Zone",
                               location_type="zone", parent_id=store.id)
        aisle = create_location(session, company_id=COMPANY, code="WH1-Z-A", name="Aisle",
                                location_type="aisle", parent_id=zone.id)
        raw_bin = create_location(session, company_id=COMPANY, code="RAW", name="Raw",
                                  location_type="bin", parent_id=aisle.id)
        finished_bin = create_location(session, company_id=COMPANY, code="FG", name="Finished",
                                       location_type="bin", parent_id=aisle.id)
        supplier = create_supplier(session, company_id=COMPANY, party_code="BOREAL",
                                   name="Boreal Supplies", payment_terms_days=30)
        add_tax_identifier(session, supplier, kind="tin", value="009-876-543")
        customer = create_customer(session, company_id=COMPANY, party_code="ACME",
                                   name="Acme Retail", payment_terms_days=30)
        session.commit()

        # A purchase order for both materials, and the receipt that brings them in: the milk as
        # batch RAW-1 (the supplier's lot) and the gadget as unit SER-1.
        requisition = create_requisition(
            session, company_id=COMPANY, number="REQ-1", requested_by="rina",
            needed_by=DEADLINE, currency="PHP",
            lines=[{"description": "Milk", "quantity": "100", "uom": "litre",
                    "estimated_unit_price": "1.00", "item_sku": "MILK"},
                   {"description": "Gadgets", "quantity": "2", "uom": "each",
                    "estimated_unit_price": "50.00", "item_sku": "GADGET"}],
        )
        session.commit()
        submit_requisition(session, requisition, actor="rina")
        session.commit()
        decide_requisition(session, requisition, actor="mia", action=APPROVE, role="manager")
        session.commit()
        rfq = issue_rfq(session, requisition=requisition, number="RFQ-1",
                        supplier_codes=["BOREAL"], response_deadline=date(2026, 11, 30))
        session.commit()
        record_response(session, rfq, supplier_code="BOREAL", received_on=DAY,
                        lines=[{"line_no": 1, "unit_price": "1.00"},
                               {"line_no": 2, "unit_price": "50.00"}])
        session.commit()
        order = award(
            session, rfq=rfq, actor="mia",
            awards=[{"supplier_code": "BOREAL", "number": "PO-1", "required_date": DEADLINE,
                     "lines": [{"line_no": 1, "quantity": "100"},
                               {"line_no": 2, "quantity": "2"}]}],
        )[0]
        session.commit()
        submit_order(session, order, actor="mia")
        session.commit()
        decide_order(session, order, actor="mia", action=APPROVE, role="manager")
        session.commit()
        receipt = create_receipt(
            session, order=order, number="GRN-1", location=raw_bin, received_on=DAY,
            lines=[{"line_no": 1, "quantity": "100", "batch_code": "RAW-1"},
                   {"line_no": 2, "quantity": "1", "serial_code": "SER-1"}],
        )
        session.commit()
        post_receipt(session, receipt)
        session.commit()

        # The production step: the raw lot is consumed by a work order, which produces a
        # finished lot of its own.
        bom = create_bom(session, company_id=COMPANY, item=yoghurt)
        add_line(session, bom, item=milk, quantity="2")
        release(session, bom)
        session.commit()
        work_order = create_work_order(session, company_id=COMPANY, item=yoghurt,
                                       quantity="40", number="WO-1", created_on=DAY,
                                       due_on=DEADLINE)
        session.commit()
        advance(session, work_order, status=RELEASED, actor="mia")
        session.commit()
        advance(session, work_order, status=IN_PROGRESS, actor="mia")
        session.commit()
        issue_material(session, work_order, item=milk, location=raw_bin, quantity="20",
                       on=DAY, actor="mia", batch_code="RAW-1")
        session.commit()
        receive_finished_goods(session, work_order, location=finished_bin, quantity="10",
                               on=DAY, actor="mia", batch_code="FG-1")
        session.commit()

        # The sale: the finished lot and the unit are shipped to a customer.
        quote = create_quotation(session, company_id=COMPANY, customer_id=customer.id,
                                number="Q-1", issued_on=DAY,
                                valid_until=date(2026, 12, 31))
        session.flush()
        add_quote_line(session, quote, line_no=1, description="Yoghurt", quantity="4",
                       unit_price="6.00", uom="tub", item_id=yoghurt.id, priced_on=DAY)
        add_quote_line(session, quote, line_no=2, description="Gadget", quantity="1",
                       unit_price="90.00", uom="each", item_id=gadget.id, priced_on=DAY)
        session.flush()
        sales_order = convert_quotation_to_order(session, quote, number="SO-1", on=DAY)
        session.commit()
        from app.company import Company as _Company, set_credit_check_mode

        set_credit_check_mode(session, session.get(_Company, COMPANY), mode="block")
        session.commit()
        confirm_order(session, sales_order, exposure="0", actor="mia")
        session.commit()
        pick = generate_pick_list(session, sales_order, number="PL-1", on=DAY)
        session.commit()
        record_picked(session, pick, line_no=1, quantity="4")
        record_picked(session, pick, line_no=2, quantity="1")
        session.commit()
        shipment = ship_order(session, sales_order, number="SH-1", warehouse=finished_bin,
                              lines=[(1, "4", "FG-1")], on=DAY)
        session.commit()
        assert shipment.number == "SH-1", shipment
        # The unit came in at the raw bin, so it leaves from there: one shipment per location.
        unit_shipment = ship_order(session, sales_order, number="SH-2", warehouse=raw_bin,
                                   lines=[(2, "1", None, "SER-1")], on=DAY)
        session.commit()
        assert unit_shipment.number == "SH-2", unit_shipment

        # 1 — backward from a sold batch reaches its origin
        sold = trace(session, company_id=COMPANY, batch="FG-1", item="YOG")
        assert sold.kind == "batch" and sold.item == "YOG", sold
        documents = [hop.document for hop in sold.backward]
        assert any("work order WO-1" in one for one in documents), documents
        assert any("goods receipt GRN-1 from BOREAL" in one for one in documents), documents
        assert any(hop.batch == "RAW-1" for hop in sold.backward), [
            (hop.batch, hop.document) for hop in sold.backward
        ]
        assert [step["work_order"] for step in sold.steps] == ["WO-1"], sold.steps
        assert sold.suppliers() == ["BOREAL"], sold.suppliers()
        materials = sorted({hop.batch for hop in sold.backward if hop.batch and hop.batch != "FG-1"})
        assert materials == ["RAW-1"], materials
        print(
            f"1. FG-1 traces back through {len(sold.backward)} hop(s): "
            f"{' → '.join(dict.fromkeys(hop.document for hop in sold.backward))} — the finished lot"
            f" to the order that made it, to the raw lot {materials} that order consumed and to"
            f" the supplier's own receipt"
        )

        # 2 — forward from a received batch reaches everything that used it
        raw = trace(session, company_id=COMPANY, batch="RAW-1", item="MILK")
        forward = [hop.document for hop in raw.forward]
        assert any("consumed by work order WO-1" in one for one in forward), forward
        assert any("SH-1 to ACME" in one for one in forward), forward
        assert "FG-1" in {hop.batch for hop in raw.forward}, {
            hop.batch for hop in raw.forward
        }
        # The finished lot's own receipt is a hop *back* — where FG-1 came from — and it is in
        # the chain because the walk crossed the work order, which is the whole point.
        produced = [hop for hop in raw.backward if "produced by work order WO-1" in hop.document]
        assert produced and produced[0].batch == "FG-1", [hop.document for hop in raw.backward]
        assert produced[0].depth >= 1, produced[0]
        assert raw.customers() == ["ACME"], raw.customers()
        print(
            f"2. RAW-1 traces forward over {len(raw.forward)} hop(s): "
            f"{' → '.join(dict.fromkeys(forward))} — the raw lot to the order that consumed it,"
            f" through that order to the finished lot {sorted({hop.batch for hop in raw.forward if hop.batch})}"
            f" at depth {produced[0].depth} ({produced[0].document}), and to {raw.customers()} who"
            " bought it"
        )

        # 3 — the hops are ordered and labelled
        for hop in raw.hops:
            assert hop.document and hop.document_type and hop.document_id, hop
            assert hop.item and hop.location and hop.quantity, hop
        assert [hop.depth for hop in raw.forward] == sorted(hop.depth for hop in raw.forward), (
            [hop.depth for hop in raw.forward]
        )
        deepest = max(hop.depth for hop in raw.forward)
        assert deepest >= 1, [hop.depth for hop in raw.forward]
        assert all(
            hop.work_order == "WO-1" for hop in raw.forward if hop.work_order is not None
        ), [hop.work_order for hop in raw.forward]
        print(
            f"3. every hop names its document ({sorted({hop.document_type for hop in raw.hops})})"
            f" and its depth, ordered from the lot outwards to depth {deepest}, with the work"
            " order named on the hops that went through it"
        )

        # 4 — a recall is the same walk, counted, and a period narrows only the report
        impact = recall(session, company_id=COMPANY, batch="RAW-1", item="MILK")
        assert impact["customers"] == ["ACME"], impact
        assert impact["suppliers"] == ["BOREAL"], impact
        assert impact["work_orders"] == ["WO-1"], impact
        assert impact["locations"] == ["FG", "RAW"], impact
        assert Decimal(impact["quantities"]["in"]) == Decimal("100"), impact
        assert Decimal(impact["quantities"]["out"]) == Decimal("20"), impact
        windowed = trace(session, company_id=COMPANY, batch="RAW-1", item="MILK",
                         start=DAY, end=DAY)
        assert len(windowed.hops) == len(raw.hops), (windowed.hops, raw.hops)
        narrow = trace(session, company_id=COMPANY, batch="RAW-1", item="MILK",
                       start=date(2026, 10, 1), end=date(2026, 10, 31))
        assert narrow.hops == [], narrow.hops
        still = recall(session, company_id=COMPANY, batch="RAW-1", item="MILK")
        assert still["customers"] == ["ACME"], still
        print(
            f"4. the recall of RAW-1 names {impact['customers']} as customers,"
            f" {impact['work_orders']} as the work orders that used it, {impact['locations']}"
            f" as the locations it stood in, {impact['quantities']['in']} in and"
            f" {impact['quantities']['out']} out of {impact['hops']} hops; a window outside"
            f" every posting empties the *report*"
            f" ({len(narrow.hops)} rows) while the recall still names {still['customers']}"
        )

        # 5 — the report exports, and identity is not the item
        exported = trace_csv(raw)
        rows = list(csv.reader(io.StringIO(exported)))
        assert rows[0][0].startswith("trace of batch RAW-1"), rows[0]
        assert rows[1][0] == "direction", rows[1]
        assert len(rows) - 2 == len(raw.hops), len(rows)
        assert any("goods receipt GRN-1" in row[9] for row in rows[2:]), exported
        # A second lot of the same item is its own chain: the item is not the identity.
        receipt_two = create_receipt(
            session, order=order, number="GRN-2", location=raw_bin, received_on=DAY,
            lines=[{"line_no": 1, "quantity": "5", "batch_code": "RAW-2"}],
        )
        session.commit()
        post_receipt(session, receipt_two, over_receipt_reason="a second delivery of the same lot")
        session.commit()
        other = trace(session, company_id=COMPANY, batch="RAW-2", item="MILK")
        assert other.forward == [], other.forward
        assert other.backward[0].batch == "RAW-2", other.backward
        assert recall(session, company_id=COMPANY, batch="RAW-2", item="MILK")["customers"] == [], (
            "an unused lot reached a customer"
        )
        print(
            f"5. the export is {len(rows) - 2} rows under a header naming the batch, the item and"
            f" the period, with each row's document and type; a second lot of the same item"
            f" ({other.backward[0].batch}) traces to {len(other.backward)} receipt and"
            f" {len(other.forward)} issue of its own — the lot is the identity, not the item"
        )

        # 6 — units trace too
        unit = trace(session, company_id=COMPANY, serial="SER-1", item="GADGET")
        assert unit.kind == "serial" and unit.item == "GADGET", unit
        assert any("GRN-1" in hop.document for hop in unit.backward), [
            hop.document for hop in unit.backward
        ]
        assert any("SH-2 to ACME" in hop.document for hop in unit.forward), [
            hop.document for hop in unit.forward
        ]
        assert [hop.serial for hop in unit.hops] == ["SER-1"] * len(unit.hops), [
            hop.serial for hop in unit.hops
        ]
        said = _refused(
            lambda: trace(session, company_id=COMPANY, batch="NOSUCH", item="MILK"),
            "has no batch",
        )
        both = _refused(
            lambda: trace(session, company_id=COMPANY, batch="RAW-1", serial="SER-1"),
            TraceError,
        )
        print(
            f"6. the unit SER-1 traces back to {unit.backward[0].document} and forward to"
            f" {unit.forward[0].document}; an identity nobody recorded is refused"
            f" ({said[:44]}…) and asking for a batch *and* a unit is refused too ({both[:36]}…)"
        )

    print("\ncheck_trace: all assertions green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
