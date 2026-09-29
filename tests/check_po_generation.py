"""T-2.PROC.05 check — awarding RFQ lines and generating the purchase order.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/erpv1 \
        python tests/check_po_generation.py

Green on all seven:

1. awarding a full line generates a purchase order whose line carries the **quoted**
   price, the supplier, the requisition reference and the required date, with no
   re-keying — and whose total is exact
2. a supplier that never answered cannot be awarded, and neither can a line that
   supplier was silent about: there is no price to invent
3. ordering **more than the requisition asked for** is refused without an explicit
   override, and accepted with one — the reason recorded on the order
4. a **partial** award leaves the rest of the line genuinely awardable
   (`remaining_awardable`), and a later award can take it
5. a repeated order number, a non-positive quantity, an award that names no lines and
   an award against an **unapproved** requisition are all refused
6. the generated lines keep the RFQ line and the requisition line behind them — what
   T-2.MATCH.01 compares an invoice against later
7. an award states who made it

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
    NotAwardableError,
    DuplicateOrderError,
    OrderError,
    OverAwardError,
    award,
    order_by_number,
    order_total,
    orders_for_rfq,
    remaining_awardable,
)
from app.procurement.requisitions import (  # noqa: E402
    NotApprovedError,
    create_requisition,
    record_decision,
    submit,
)
from app.procurement.rfq import Rfq, RfqLine, issue_rfq, record_response  # noqa: E402
from app.procurement.suppliers import create_supplier  # noqa: E402
from app.workflow import APPROVE, configure  # noqa: E402

COMPANY = uuid.uuid4()
ISSUED_ON = date(2026, 10, 1)
DEADLINE = date(2026, 10, 10)
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
        session.add(
            Company(id=COMPANY, code="PO-CHECK", name="PO check", base_currency="PHP",
                    fiscal_year_start_month=1)
        )
        register_currency(session, company_id=COMPANY, code="PHP", name="Peso")
        session.commit()
        configure(session, company_id=COMPANY, doc_type="purchase_requisition",
                  name="Purchase requisition", levels=[(Decimal("1000"), "manager")])
        session.commit()

        for code, name in (("ACME", "Acme Supplies"), ("BOREAL", "Boreal Trading"),
                           ("CHIRP", "Chirp Industrial")):
            create_supplier(session, company_id=COMPANY, party_code=code, name=name,
                            payment_terms_days=30)
        requisition = create_requisition(
            session, company_id=COMPANY, number="REQ-200", requested_by="rina.requester",
            needed_by=REQUIRED_BY, currency="PHP",
            lines=[
                {"description": "Laptops", "quantity": "20", "uom": "each",
                 "estimated_unit_price": "1000.00"},
                {"description": "Monitors", "quantity": "40", "uom": "each",
                 "estimated_unit_price": "250.00"},
            ],
        )
        session.commit()
        submit(session, requisition, actor="rina.requester")
        session.commit()

        # 5 (first half) — an **unapproved** requisition cannot be awarded against.
        # An RFQ cannot normally exist for one (issue_rfq would refuse too), so the
        # document is built by hand here: the point is that the guard lives in
        # `award` as well, not only at the door of the RFQ.
        pending = create_requisition(
            session, company_id=COMPANY, number="REQ-201", requested_by="rina.requester",
            needed_by=REQUIRED_BY, currency="PHP",
            lines=[{"description": "Mice", "quantity": "5", "uom": "each",
                    "estimated_unit_price": "2000"}],
        )
        session.commit()
        submit(session, pending, actor="rina.requester")
        session.commit()
        assert pending.status == "pending"
        hand_made = Rfq(
            company_id=COMPANY, number="RFQ-201", requisition_id=pending.id,
            currency="PHP", issued_on=ISSUED_ON, response_deadline=DEADLINE,
            status="issued",
        )
        session.add(hand_made)
        session.flush()
        hand_made.lines.append(
            RfqLine(company_id=COMPANY, rfq_id=hand_made.id, line_no=1,
                    requisition_line_id=pending.lines[0].id, description="Mice",
                    quantity=Decimal("5"), uom="each")
        )
        session.commit()
        said = _refused(
            lambda: award(
                session, rfq=hand_made, actor="bob.buyer",
                awards=[{"supplier_code": "ACME", "number": "PO-2000",
                         "required_date": REQUIRED_BY,
                         "lines": [{"line_no": 1, "quantity": "1"}]}],
            ),
            NotApprovedError,
        )
        session.rollback()
        print(f"5a. an award against an unapproved requisition is refused: {said[:52]}…")

        record_decision(session, requisition, actor="mia.manager", action=APPROVE,
                        role="manager")
        session.commit()
        rfq = issue_rfq(session, requisition=requisition, number="RFQ-200",
                        supplier_codes=["ACME", "BOREAL", "CHIRP"],
                        response_deadline=DEADLINE, issued_on=ISSUED_ON)
        session.commit()
        # ACME quotes both lines; BOREAL quotes only line 1; CHIRP never answers
        record_response(session, rfq, supplier_code="ACME",
                        received_on=date(2026, 10, 5),
                        lines=[{"line_no": 1, "unit_price": "980.00"},
                               {"line_no": 2, "unit_price": "255.50"}],
                        lead_time_days=21)
        record_response(session, rfq, supplier_code="BOREAL",
                        received_on=date(2026, 10, 6),
                        lines=[{"line_no": 1, "unit_price": "1005.00"}])
        session.commit()
        assert remaining_awardable(session, rfq) == {
            1: Decimal("20.000000"), 2: Decimal("40.000000")
        }

        # 4 (first half) — a partial award
        partial = award(
            session, rfq=rfq, actor="bob.buyer",
            awards=[{"supplier_code": "ACME", "number": "PO-2001",
                     "required_date": REQUIRED_BY,
                     "lines": [{"line_no": 1, "quantity": "12"},
                               {"line_no": 2, "quantity": "40"}]}],
        )
        session.commit()
        assert len(partial) == 1
        order = partial[0]
        assert remaining_awardable(session, rfq) == {
            1: Decimal("8.000000"), 2: Decimal("0.000000")
        }, remaining_awardable(session, rfq)

        # 1 — the line carries what was quoted, and the total is exact
        assert order.supplier.party.code == "ACME"
        assert order.requisition.number == "REQ-200" and order.rfq.number == "RFQ-200"
        assert order.required_date == REQUIRED_BY and order.currency == "PHP"
        assert order.status == "draft"
        first, second = order.lines
        assert (first.unit_price, second.unit_price) == (
            Decimal("980.00"), Decimal("255.50")
        ), (first.unit_price, second.unit_price)
        assert first.quantity == Decimal("12") and second.quantity == Decimal("40")
        assert order_total(order) == Decimal("21980.000000"), order_total(order)
        assert first.rfq_line_id == rfq.lines[0].id
        assert first.requisition_line_id == rfq.lines[0].requisition_line_id
        print(f"1. PO-2001 awarded from ACME's own quote: 12 × 980.00 + 40 × 255.50 ="
              f" {order_total(order)}")

        # 6 — the chain back to the requisition is intact
        assert {line.requisition_line_id for line in order.lines} == {
            line.requisition_line_id for line in rfq.lines
        }
        assert len(orders_for_rfq(session, rfq)) == 1
        print("6. every ordered line keeps its RFQ line and the requisition line behind it")

        # 4 (second half) — the rest of line 1 is still awardable, to somebody else
        rest = award(
            session, rfq=rfq, actor="bob.buyer",
            awards=[{"supplier_code": "BOREAL", "number": "PO-2002",
                     "required_date": REQUIRED_BY,
                     "lines": [{"line_no": 1, "quantity": "8"}]}],
        )
        session.commit()
        assert rest[0].lines[0].unit_price == Decimal("1005.00")
        assert remaining_awardable(session, rfq) == {
            1: Decimal("0.000000"), 2: Decimal("0.000000")
        }
        assert [row.number for row in orders_for_rfq(session, rfq)] == ["PO-2001", "PO-2002"]
        print("4. a partial award left the remaining 8 of line 1 awardable, and BOREAL took"
              " it at its own quoted price")

        # 2 — nothing may be awarded that was not quoted
        said = _refused(
            lambda: award(
                session, rfq=rfq, actor="bob.buyer",
                awards=[{"supplier_code": "CHIRP", "number": "PO-2003",
                         "required_date": REQUIRED_BY,
                         "lines": [{"line_no": 1, "quantity": "1"}]}],
            ),
            NotAwardableError,
        )
        session.rollback()
        silent = _refused(
            lambda: award(
                session, rfq=rfq, actor="bob.buyer",
                awards=[{"supplier_code": "BOREAL", "number": "PO-2004",
                         "required_date": REQUIRED_BY,
                         "lines": [{"line_no": 2, "quantity": "1"}]}],
            ),
            NotAwardableError,
        )
        session.rollback()
        print(f"2. a non-responder is refused ({said[:52]}…) and so is a line they were"
              f" silent about ({silent[:52]}…)")

        # 3 — the requisitioned quantity is the ceiling
        over = _refused(
            lambda: award(
                session, rfq=rfq, actor="bob.buyer",
                awards=[{"supplier_code": "ACME", "number": "PO-2005",
                         "required_date": REQUIRED_BY,
                         "lines": [{"line_no": 1, "quantity": "1"}]}],
            ),
            OverAwardError,
        )
        session.rollback()
        overridden = award(
            session, rfq=rfq, actor="bob.buyer", override_reason="urgent spares, approved by CFO",
            awards=[{"supplier_code": "ACME", "number": "PO-2005",
                     "required_date": REQUIRED_BY,
                     "lines": [{"line_no": 1, "quantity": "1"}]}],
        )
        session.commit()
        assert overridden[0].over_award_reason == "urgent spares, approved by CFO"
        print(f"3. over-awarding refused ({over[:56]}…) and accepted only with a recorded"
              " reason")

        # 5 — the rest of the edges
        said = _refused(
            lambda: award(
                session, rfq=rfq, actor="bob.buyer",
                awards=[{"supplier_code": "ACME", "number": "PO-2001",
                         "required_date": REQUIRED_BY,
                         "lines": [{"line_no": 2, "quantity": "1"}]}],
            ),
            DuplicateOrderError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: award(
                session, rfq=rfq, actor="bob.buyer", override_reason="because",
                awards=[{"supplier_code": "ACME", "number": "PO-2006",
                         "required_date": REQUIRED_BY,
                         "lines": [{"line_no": 2, "quantity": "0"}]}],
            ),
            OrderError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: award(
                session, rfq=rfq, actor="bob.buyer",
                awards=[{"supplier_code": "ACME", "number": "PO-2007",
                         "required_date": REQUIRED_BY, "lines": []}],
            ),
            OrderError,
        )
        session.rollback()
        # 7 — an award states who made it
        said += " | " + _refused(
            lambda: award(
                session, rfq=rfq, actor="  ",
                awards=[{"supplier_code": "ACME", "number": "PO-2008",
                         "required_date": REQUIRED_BY,
                         "lines": [{"line_no": 2, "quantity": "1"}]}],
            ),
            OrderError,
        )
        session.rollback()
        assert order_by_number(session, company_id=COMPANY, number="PO-2001").id == order.id
        print(f"5/7. a repeated number, a zero quantity, an empty award and an unstated"
              f" actor are refused: {said}")

    print("check_po_generation: all assertions green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
