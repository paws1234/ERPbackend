"""T-2.PROC.09 check — supplier performance scored from recorded documents.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/erpv1 \
        python tests/check_supplier_scorecard.py

Green on all seven:

1. a supplier's scorecard is built from its **posted receipts**: punctuality against
   the order's required date, quantity delivered against ordered, quality from the
   rejected/accepted split, and price from the awarded against the estimated price
2. every input **names its documents** — order number, receipt number, line number and
   the figures each came from
3. the **weights are shown**, defaults are equal, a caller may state its own, and a
   zero weight drops a metric while a negative one is refused
4. a supplier with **no receipts is unrated**, with no score at all — not zero
5. two suppliers with different performance score differently, and the better one
   scores higher
6. a window narrows the scorecard to the receipts inside it
7. a **draft** receipt is not counted: the scorecard reads posted documents only

**Scratch database only**: it drops and recreates the public schema.
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
from app.ledger.currency import register_currency  # noqa: E402
from app.procurement.orders import (  # noqa: E402
    award,
    decide_order,
    submit_order,
)
from app.procurement.receipts import create_receipt, post_receipt  # noqa: E402
from app.procurement.requisitions import (  # noqa: E402
    create_requisition,
    record_decision as decide_requisition,
    submit as submit_requisition,
)
from app.procurement.rfq import issue_rfq, record_response  # noqa: E402
from app.procurement.scoring import (  # noqa: E402
    METRICS,
    ScoringError,
    on_time_rate,
    scorecard,
    scorecards,
    scoring_weights,
)
from app.procurement.suppliers import create_supplier  # noqa: E402
from app.stock.items import create_item  # noqa: E402
from app.stock.locations import create_location  # noqa: E402
from app.workflow import APPROVE, configure  # noqa: E402

COMPANY = uuid.uuid4()
ISSUED_ON = date(2026, 10, 1)
DEADLINE = date(2026, 10, 10)
REQUIRED_BY = date(2026, 11, 30)
COUNTER = {"n": 0}


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


def _order(session, *, supplier_code: str, estimated: str, awarded: str,
           quantity: str, required_date: date, item_sku: str, location):
    """A real chain up to an approved purchase order, then a receipt to post or leave."""
    COUNTER["n"] += 1
    tag = COUNTER["n"]
    requisition = create_requisition(
        session, company_id=COMPANY, number=f"REQ-5{tag:02d}",
        requested_by="rina.requester", needed_by=required_date, currency="PHP",
        lines=[{"description": "Widgets", "quantity": quantity, "uom": "each",
                "estimated_unit_price": estimated, "item_sku": item_sku}],
    )
    session.commit()
    submit_requisition(session, requisition, actor="rina.requester")
    session.commit()
    decide_requisition(session, requisition, actor="mia.manager", action=APPROVE,
                       role="manager")
    session.commit()
    rfq = issue_rfq(session, requisition=requisition, number=f"RFQ-5{tag:02d}",
                    supplier_codes=[supplier_code], response_deadline=DEADLINE,
                    issued_on=ISSUED_ON)
    session.commit()
    record_response(session, rfq, supplier_code=supplier_code,
                    received_on=date(2026, 10, 5),
                    lines=[{"line_no": 1, "unit_price": awarded}])
    session.commit()
    order = award(
        session, rfq=rfq, actor="bob.buyer",
        awards=[{"supplier_code": supplier_code, "number": f"PO-5{tag:02d}",
                 "required_date": required_date,
                 "lines": [{"line_no": 1, "quantity": quantity}]}],
    )[0]
    session.commit()
    submit_order(session, order, actor="bob.buyer")
    session.commit()
    if order.status == "pending":
        decide_order(session, order, actor="mia.manager", action=APPROVE, role="manager")
        session.commit()
    return order


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
            Company(id=COMPANY, code="SCORE", name="Scoring", base_currency="PHP",
                    fiscal_year_start_month=1)
        )
        register_currency(session, company_id=COMPANY, code="PHP", name="Peso")
        session.commit()
        from tests.seed import seed_stock_accounts

        seed_stock_accounts(session, company_id=COMPANY)
        session.commit()
        configure(session, company_id=COMPANY, doc_type="purchase_requisition",
                  name="Requisition", levels=[(Decimal("1"), "manager")])
        configure(session, company_id=COMPANY, doc_type="purchase_order",
                  name="Order", levels=[(Decimal("100000"), "manager")])
        for code, name in (("GOOD", "Good Supplier"), ("POOR", "Poor Supplier"),
                           ("NEW", "Never Bought From")):
            create_supplier(session, company_id=COMPANY, party_code=code, name=name,
                            payment_terms_days=30)
        create_item(session, company_id=COMPANY, sku="WIDGET", name="Widget",
                    base_uom="each", traceability_mode="none")
        warehouse = create_location(session, company_id=COMPANY, code="MAIN",
                                    name="Main", location_type="warehouse")
        zone = create_location(session, company_id=COMPANY, code="MAIN-Z", name="Zone",
                               location_type="zone", parent_id=warehouse.id)
        aisle = create_location(session, company_id=COMPANY, code="MAIN-A", name="Aisle",
                                location_type="aisle", parent_id=zone.id)
        bin_a = create_location(session, company_id=COMPANY, code="MAIN-B1", name="Bin",
                                location_type="bin", parent_id=aisle.id)
        session.commit()

        from app.procurement.suppliers import supplier_by_code

        good_supplier = supplier_by_code(session, company_id=COMPANY, code="GOOD")
        poor_supplier = supplier_by_code(session, company_id=COMPANY, code="POOR")
        new_supplier = supplier_by_code(session, company_id=COMPANY, code="NEW")

        # GOOD: on time, complete, nothing rejected, at the estimated price
        order = _order(session, supplier_code="GOOD", estimated="100", awarded="100",
                       quantity="10", required_date=date(2026, 11, 30),
                       item_sku="WIDGET", location=bin_a)
        receipt = create_receipt(session, order=order, number="GRN-501", location=bin_a,
                                 received_on=date(2026, 11, 25),
                                 lines=[{"line_no": 1, "quantity": "10"}])
        session.commit()
        post_receipt(session, receipt)
        session.commit()

        # POOR: late, short, and 4 of 10 rejected, at 25 % over the estimate
        poor_order = _order(session, supplier_code="POOR", estimated="100", awarded="125",
                            quantity="10", required_date=date(2026, 11, 30),
                            item_sku="WIDGET", location=bin_a)
        poor_receipt = create_receipt(session, order=poor_order, number="GRN-502",
                                      location=bin_a, received_on=date(2026, 12, 20),
                                      lines=[{"line_no": 1, "quantity": "6",
                                              "rejected_quantity": "4"}])
        session.commit()
        post_receipt(session, poor_receipt, over_receipt_reason=None)
        session.commit()

        # a third receipt left as a draft: it must not count
        draft_order = _order(session, supplier_code="GOOD", estimated="100", awarded="100",
                             quantity="5", required_date=date(2026, 11, 30),
                             item_sku="WIDGET", location=bin_a)
        draft = create_receipt(session, order=draft_order, number="GRN-503",
                               location=bin_a, received_on=date(2026, 11, 20),
                               lines=[{"line_no": 1, "quantity": "5"}])
        session.commit()

        # 1 + 2 — the figures, and every input naming its documents
        card = scorecard(session, supplier=good_supplier)
        assert card["rated"] is True
        assert card["metrics"]["on_time"] == Decimal("100.000000"), card["metrics"]
        assert card["metrics"]["quantity"] == Decimal("100.000000"), card["metrics"]
        assert card["metrics"]["quality"] == Decimal("100.000000"), card["metrics"]
        assert card["metrics"]["price"] == Decimal("100.000000"), card["metrics"]
        assert card["score"] == Decimal("100.000000"), card["score"]
        assert [row["receipt"] for row in card["inputs"]] == ["GRN-501"], card["inputs"]
        assert card["inputs"][0]["order"] == order.number
        assert card["inputs"][0]["punctual"] is True
        print(f"1. GOOD scores {card['score']} from {card['inputs'][0]['order']}/"
              f"{card['inputs'][0]['receipt']} — on time, complete, nothing rejected,"
              " at the estimated price")

        # 7 — the draft receipt is not counted
        assert card["metrics"]["quantity"] == Decimal("100.000000"), (
            "the draft receipt was counted"
        )
        assert all(row["receipt"] != "GRN-503" for row in card["inputs"])
        print("7. GRN-503 (draft, unposted) is not in the scorecard at all")

        # 5 — a worse supplier scores worse
        poor = scorecard(session, supplier=poor_supplier)
        assert poor["rated"] is True
        assert poor["metrics"]["on_time"] == Decimal("0.000000"), poor["metrics"]
        assert poor["metrics"]["quantity"] == Decimal("60.000000"), poor["metrics"]
        assert poor["metrics"]["quality"] == Decimal("60.000000"), poor["metrics"]
        assert poor["metrics"]["price"] == Decimal("75.000000"), poor["metrics"]
        assert poor["score"] < card["score"], (poor["score"], card["score"])
        assert len(poor["inputs"]) == 1
        print(f"5. POOR scores {poor['score']} ({poor['metrics']}) — lower than GOOD's"
              f" {card['score']}")

        # 4 — no receipts, no score
        unrated = scorecard(session, supplier=new_supplier)
        assert unrated["rated"] is False and "score" not in unrated
        assert unrated["reason"] and "unrated" in unrated["reason"]
        assert all(value is None for value in unrated["metrics"].values())
        print(f"4. NEW is unrated: {unrated['reason']}")

        # 3 — the weights are shown, and they are the caller's
        assert card["weights"] == {metric: Decimal(1) for metric in METRICS}
        punctuality = scoring_weights(on_time=Decimal(3), price=Decimal(0))
        weighted = scorecard(session, supplier=poor_supplier, weights=punctuality)
        assert weighted["weights"] == punctuality
        assert weighted["score"] == Decimal("24.000000"), weighted["score"]
        assert weighted["score"] != poor["score"]
        said = _refused(lambda: scoring_weights(on_time="-1"), ScoringError)
        said += " | " + _refused(lambda: scoring_weights(punctuality="1"), ScoringError)
        said += " | " + _refused(
            lambda: scorecard(session, supplier=poor_supplier, weights={"on_time": Decimal(1)}),
            ScoringError,
        )
        print(f"3. equal weights by default, a 3/0/1/1 weighting changing the score to"
              f" {weighted['score']}, and a negative weight/unknown metric/incomplete set"
              f" refused: {said}")

        # 6 — a window narrows it
        narrowed = scorecard(session, supplier=good_supplier,
                             start=date(2026, 11, 26), end=date(2026, 12, 1))
        assert narrowed["rated"] is False, "the window did not exclude GRN-501"
        inside = scorecard(session, supplier=good_supplier,
                           start=date(2026, 11, 1), end=date(2026, 11, 30))
        assert inside["rated"] is True and len(inside["inputs"]) == 1
        print("6. a window excluding the receipt makes GOOD unrated again; one covering it"
              " keeps the score")

        both = scorecards(session, suppliers=[good_supplier, poor_supplier])
        assert [row["supplier"] for row in both] == ["GOOD", "POOR"]
        assert on_time_rate(session, supplier=poor_supplier) == Decimal("0.000000")
        assert on_time_rate(session, supplier=new_supplier) == Decimal("0.000000")

    print("check_supplier_scorecard: all assertions green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
