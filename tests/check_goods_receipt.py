"""T-2.PROC.07 check — the goods receipt note against a purchase order.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/erpv1 \
        python tests/check_goods_receipt.py

Green on all eight:

1. posting a receipt raises stock at the chosen location, links each line to its
   **order line** and writes the stock ledger entry (and its balanced GL posting)
   with the order's own price — no re-keying
2. the order learns what arrived: `received_quantity` moves and a **partial** receipt
   leaves the remainder receivable
3. over-receipt beyond the ordered quantity is refused without a reason, and accepted
   with one — recorded on the receipt
4. a **rejected** quantity is recorded on the document and never enters stock
5. nothing is received against an **unapproved** order, and a posted receipt is never
   posted twice
6. a **draft** receipt leaves its quantity outstanding, so the order cannot be closed
   while the receipt is open — the rule falls out of `received_quantity` moving only
   on posting
7. the receipt's value is quantity × the ordered price, and it is what the stock
   movement carries — so T-1.INV.04's valuation receives the agreed value
8. an unknown order line, an empty receipt, a repeated number and a location from
   another company are all refused

**Scratch database only**: it drops and recreates the public schema.
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

from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger.currency import register_currency  # noqa: E402
from app.ledger.posting import JournalEntry  # noqa: E402
from app.procurement.orders import (  # noqa: E402
    APPROVED,
    OrderStateError,
    award,
    close_order,
    receipt_progress,
    submit_order,
    decide_order,
)
from app.procurement.receipts import (  # noqa: E402
    DuplicateReceiptError,
    OverReceiptError,
    ReceiptError,
    ReceiptStateError,
    UnknownOrderLineError,
    create_receipt,
    open_receipts,
    post_receipt,
    receipt_by_number,
    receipt_movements,
    receipt_value,
    receipts_for_order,
    rejected_quantity,
)
from app.procurement.requisitions import (  # noqa: E402
    create_requisition,
    record_decision as decide_requisition,
    submit as submit_requisition,
)
from app.procurement.rfq import issue_rfq, record_response  # noqa: E402
from app.procurement.suppliers import create_supplier  # noqa: E402
from app.stock.entries import on_hand  # noqa: E402
from app.stock.items import create_item, item_by_sku  # noqa: E402
from app.stock.locations import create_location, location_by_code  # noqa: E402
from app.workflow import APPROVE, configure  # noqa: E402

COMPANY = uuid.uuid4()
OTHER = uuid.uuid4()
ISSUED_ON = date(2026, 10, 1)
DEADLINE = date(2026, 10, 10)
RECEIVED_ON = date(2026, 10, 20)
REQUIRED_BY = date(2026, 11, 30)


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
        for company_id, code in ((COMPANY, "GRN-CHECK"), (OTHER, "OTHER-CO")):
            session.add(
                Company(id=company_id, code=code, name=f"{code} company",
                        base_currency="PHP", fiscal_year_start_month=1)
            )
            register_currency(session, company_id=company_id, code="PHP", name="Peso")
        session.commit()
        from tests.seed import seed_stock_accounts

        seed_stock_accounts(session, company_id=COMPANY)
        seed_stock_accounts(session, company_id=OTHER)
        session.commit()
        configure(session, company_id=COMPANY, doc_type="purchase_requisition",
                  name="Purchase requisition", levels=[(Decimal("1"), "manager")])
        configure(session, company_id=COMPANY, doc_type="purchase_order",
                  name="Purchase order", levels=[(Decimal("1000"), "manager")])
        create_supplier(session, company_id=COMPANY, party_code="ACME",
                        name="Acme Supplies", payment_terms_days=30)
        item = create_item(session, company_id=COMPANY, sku="WIDGET", name="Widget",
                           base_uom="each", traceability_mode="none")
        warehouse = create_location(session, company_id=COMPANY, code="MAIN",
                                    name="Main warehouse", location_type="warehouse")
        zone = create_location(session, company_id=COMPANY, code="MAIN-Z", name="Zone",
                               location_type="zone", parent_id=warehouse.id)
        aisle = create_location(session, company_id=COMPANY, code="MAIN-A", name="Aisle",
                                location_type="aisle", parent_id=zone.id)
        bin_a = create_location(session, company_id=COMPANY, code="MAIN-B1", name="Bin B1",
                                location_type="bin", parent_id=aisle.id)
        foreign = create_location(session, company_id=OTHER, code="ELSEWHERE",
                                  name="Elsewhere", location_type="warehouse")
        foreign_zone = create_location(session, company_id=OTHER, code="ELSE-Z",
                                       name="Elsewhere zone", location_type="zone",
                                       parent_id=foreign.id)
        foreign_aisle = create_location(session, company_id=OTHER, code="ELSE-A",
                                        name="Elsewhere aisle", location_type="aisle",
                                        parent_id=foreign_zone.id)
        foreign_bin = create_location(session, company_id=OTHER, code="ELSE-B",
                                      name="Elsewhere bin", location_type="bin",
                                      parent_id=foreign_aisle.id)
        session.commit()

        # a real chain: requisition → RFQ → award → approval
        requisition = create_requisition(
            session, company_id=COMPANY, number="REQ-400", requested_by="rina.requester",
            needed_by=REQUIRED_BY, currency="PHP",
            lines=[{"description": "Widgets", "quantity": "10", "uom": "each",
                    "estimated_unit_price": "100", "item_sku": "WIDGET"}],
        )
        session.commit()
        submit_requisition(session, requisition, actor="rina.requester")
        session.commit()
        decide_requisition(session, requisition, actor="mia.manager", action=APPROVE,
                           role="manager")
        session.commit()
        rfq = issue_rfq(session, requisition=requisition, number="RFQ-400",
                        supplier_codes=["ACME"], response_deadline=DEADLINE,
                        issued_on=ISSUED_ON)
        session.commit()
        record_response(session, rfq, supplier_code="ACME", received_on=date(2026, 10, 5),
                        lines=[{"line_no": 1, "unit_price": "100"}])
        session.commit()
        order = award(
            session, rfq=rfq, actor="bob.buyer",
            awards=[{"supplier_code": "ACME", "number": "PO-4001",
                     "required_date": REQUIRED_BY,
                     "lines": [{"line_no": 1, "quantity": "10"}]}],
        )[0]
        session.commit()
        assert order.lines[0].item_id == item.id, "the order line lost its item"

        # 5 (first half) — nothing is received against an unapproved order
        said = _refused(
            lambda: create_receipt(session, order=order, number="GRN-4001",
                                   location=bin_a, received_on=RECEIVED_ON,
                                   lines=[{"line_no": 1, "quantity": "4"}]),
            OrderStateError,
        )
        session.rollback()
        print(f"5a. receiving against an unapproved order is refused: {said[:52]}…")

        submit_order(session, order, actor="bob.buyer")
        session.commit()
        decide_order(session, order, actor="mia.manager", action=APPROVE, role="manager")
        session.commit()
        assert order.status == APPROVED

        # 2 (first half) — a partial receipt
        first = create_receipt(session, order=order, number="GRN-4001", location=bin_a,
                               received_on=RECEIVED_ON,
                               lines=[{"line_no": 1, "quantity": "4",
                                       "rejected_quantity": "1"}])
        session.commit()
        assert first.status == "draft"
        # 6 — a draft receipt leaves the quantity outstanding, so the order cannot close
        assert [row.number for row in open_receipts(session, order)] == ["GRN-4001"]
        assert receipt_progress(session, order)["outstanding"] == Decimal("10.000000")
        said = _refused(lambda: close_order(session, order, actor="mia.manager"),
                        OrderStateError)
        session.rollback()

        post_receipt(session, first)
        session.commit()
        assert first.status == "posted" and first.posted_at is not None
        assert open_receipts(session, order) == []
        assert order.lines[0].received_quantity == Decimal("4.000000")
        assert receipt_progress(session, order)["outstanding"] == Decimal("6.000000")
        print(f"2. a partial receipt of 4 of 10 moved `received_quantity` to 4.000000 and"
              f" left 6.000000 receivable (closed only after posting: {said[:34]}…)")

        # 1 — stock moved, the line links to the order line and to its own movement
        held = on_hand(session, company_id=COMPANY, item_id=item.id, location_id=bin_a.id)
        assert held["quantity"] == Decimal("4.000000"), held
        assert held["value"] == Decimal("400.000000"), held
        movements = receipt_movements(session, first)
        assert len(movements) == 1, movements
        assert movements[0].source_type == "goods_receipt"
        assert movements[0].source_id == first.id
        assert movements[0].value == Decimal("400.000000"), movements[0].value
        assert first.lines[0].movement_id == movements[0].id
        assert first.lines[0].order_line_id == order.lines[0].id
        entry = session.scalar(
            select(JournalEntry).where(JournalEntry.source_id == first.id)
        )
        assert entry is not None, "the receipt wrote no GL entry"
        assert sum(line.debit for line in entry.lines) == sum(
            line.credit for line in entry.lines
        )
        print(f"1. GRN-4001 raised 4 at {bin_a.code} worth 400.000000, linked to PO line 1,"
              f" with movement {movements[0].id} and a balanced GL entry")

        # 7 — the value is quantity × the ordered price
        assert receipt_value(first) == Decimal("400.000000"), receipt_value(first)
        assert first.lines[0].unit_price == order.lines[0].unit_price
        assert receipts_for_order(session, order) == [first]
        print("7. the receipt's value is the ordered price applied to what arrived")

        # 4 — a rejected quantity is recorded and never enters stock
        second = create_receipt(session, order=order, number="GRN-4002", location=bin_a,
                                received_on=RECEIVED_ON,
                                lines=[{"line_no": 1, "quantity": "6",
                                        "rejected_quantity": "3"}])
        session.commit()
        post_receipt(session, second)
        session.commit()
        assert second.lines[0].rejected_quantity == Decimal("3")
        assert on_hand(session, company_id=COMPANY, item_id=item.id,
                       location_id=bin_a.id)["quantity"] == Decimal("10.000000")
        assert rejected_quantity(session, order) == Decimal("4.000000")
        assert receipt_progress(session, order)["outstanding"] == Decimal("0.000000")
        print("4. 3 rejected on the second receipt: on hand stayed 10, the rejection is on"
              " the document (4 counting both)")

        # 3 — over-receipt needs a reason. The draft is fine; **posting** is where the
        # order's own quantity is compared, because that is where stock moves.
        extra = create_receipt(session, order=order, number="GRN-4003", location=bin_a,
                               received_on=RECEIVED_ON,
                               lines=[{"line_no": 1, "quantity": "1"}])
        session.commit()
        said2 = _refused(lambda: post_receipt(session, extra), OverReceiptError)
        session.rollback()
        post_receipt(session, extra, over_receipt_reason="supplier shipped a spare")
        session.commit()
        assert extra.over_receipt_reason == "supplier shipped a spare"
        assert order.lines[0].received_quantity == Decimal("11.000000")
        assert on_hand(session, company_id=COMPANY, item_id=item.id,
                       location_id=bin_a.id)["quantity"] == Decimal("11.000000")
        print(f"3. over-receipt refused ({said2[:56]}…) and accepted only with a recorded"
              " reason")

        # 5 (second half) — posted once
        said = _refused(lambda: post_receipt(session, extra), ReceiptStateError)
        session.rollback()
        print(f"5b. a posted receipt is never posted twice: {said[:56]}…")

        # 8 — the edges
        said = _refused(
            lambda: create_receipt(session, order=order, number="GRN-4004", location=bin_a,
                                   received_on=RECEIVED_ON,
                                   lines=[{"line_no": 9, "quantity": "1"}]),
            UnknownOrderLineError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: create_receipt(session, order=order, number="GRN-4004", location=bin_a,
                                   received_on=RECEIVED_ON, lines=[]),
            ReceiptError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: create_receipt(session, order=order, number="GRN-4001", location=bin_a,
                                   received_on=RECEIVED_ON,
                                   lines=[{"line_no": 1, "quantity": "1"}]),
            DuplicateReceiptError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: create_receipt(session, order=order, number="GRN-4004",
                                   location=foreign_bin, received_on=RECEIVED_ON,
                                   lines=[{"line_no": 1, "quantity": "1"}]),
            ReceiptError,
        )
        session.rollback()
        assert receipt_by_number(session, company_id=COMPANY, number="GRN-4001").id == first.id
        print(f"8. an unknown order line, an empty receipt, a repeated number and a"
              f" foreign location are refused: {said}")

        assert location_by_code(session, company_id=COMPANY, code="MAIN-B1").id == bin_a.id
        assert item_by_sku(session, company_id=COMPANY, sku="WIDGET").id == item.id

    print("check_goods_receipt: all assertions green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
