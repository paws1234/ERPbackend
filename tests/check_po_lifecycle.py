"""T-2.PROC.06 check — the purchase order's lifecycle: approve, amend, close.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/erpv1 \
        python tests/check_po_lifecycle.py

Green on all seven:

1. an order above the configured threshold is routed to its approval chain and
   **cannot be received against** until the chain approves it; once approved the gate
   opens
2. an order below every configured level is approved on submission — the engine's own
   answer, not a second rule
3. the engine's own rules hold: a decision from the wrong role is refused, and a
   **return** leaves the order returned rather than approved
4. amending an approved order raises a **new draft revision** over the same lines with
   the change applied, while the **original keeps its approval** and the chain is
   readable
5. amending a draft, and raising a second revision of one order, are both refused
6. an order still expecting goods cannot be closed — "no open receipts" — and closes
   only with a stated short-close reason, which is recorded
7. a draft order cannot be closed, and every transition is on the audit trail with its
   **actor and time**

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

from app.audit import set_actor  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger.currency import register_currency  # noqa: E402
from app.procurement.orders import (  # noqa: E402
    APPROVED,
    CLOSED,
    DRAFT,
    PENDING,
    OrderError,
    OrderStateError,
    amend,
    award,
    close_order,
    order_total,
    receipt_progress,
    require_approved,
    revisions,
    status_trail,
    submit_order,
    decide_order,
)
from app.procurement.requisitions import (  # noqa: E402
    create_requisition,
    record_decision as decide_requisition,
    submit as submit_requisition,
)
from app.procurement.rfq import issue_rfq, record_response  # noqa: E402
from app.procurement.suppliers import add_tax_identifier, create_supplier  # noqa: E402
from app.workflow import APPROVE, RETURN, WrongApprover, WorkflowError, configure  # noqa: E402

COMPANY = uuid.uuid4()
ISSUED_ON = date(2026, 10, 1)
DEADLINE = date(2026, 10, 10)
REQUIRED_BY = date(2026, 11, 30)
LIMIT = Decimal("100")


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


def _order(session, *, number: str, requisition_number: str, unit_price: str,
           quantity: str = "1", order_number: str):
    """A purchase order generated the real way: requisition → RFQ → award."""
    requisition = create_requisition(
        session, company_id=COMPANY, number=requisition_number,
        requested_by="rina.requester", needed_by=REQUIRED_BY, currency="PHP",
        lines=[{"description": "Brackets", "quantity": quantity, "uom": "each",
                "estimated_unit_price": unit_price}],
    )
    session.commit()
    submit_requisition(session, requisition, actor="rina.requester")
    session.commit()
    if requisition.status == "pending":
        decide_requisition(session, requisition, actor="mia.manager", action=APPROVE,
                           role="manager")
        session.commit()
    rfq = issue_rfq(session, requisition=requisition, number=f"RFQ-{number}",
                    supplier_codes=["ACME"], response_deadline=DEADLINE,
                    issued_on=ISSUED_ON)
    session.commit()
    record_response(session, rfq, supplier_code="ACME", received_on=date(2026, 10, 5),
                    lines=[{"line_no": 1, "unit_price": unit_price}])
    session.commit()
    return award(
        session, rfq=rfq, actor="bob.buyer",
        awards=[{"supplier_code": "ACME", "number": order_number,
                 "required_date": REQUIRED_BY,
                 "lines": [{"line_no": 1, "quantity": quantity}]}],
    )[0]


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
            Company(id=COMPANY, code="PO-LIFE", name="PO lifecycle", base_currency="PHP",
                    fiscal_year_start_month=1)
        )
        register_currency(session, company_id=COMPANY, code="PHP", name="Peso")
        session.commit()
        configure(session, company_id=COMPANY, doc_type="purchase_requisition",
                  name="Purchase requisition", levels=[(Decimal("1"), "manager")])
        configure(session, company_id=COMPANY, doc_type="purchase_order",
                  name="Purchase order", levels=[(LIMIT, "manager"), (Decimal("10000"), "director")])
        supplier = create_supplier(session, company_id=COMPANY, party_code="ACME",
                                   name="Acme Supplies", payment_terms_days=30)
        add_tax_identifier(session, supplier, kind="tin", value="001-234-567")
        session.commit()

        # 1 — above the threshold: routed, and nothing may be received yet
        big = _order(session, number="1", requisition_number="REQ-300",
                     unit_price="5000", quantity="4", order_number="PO-3001")
        assert big.status == DRAFT and order_total(big) == Decimal("20000.000000")
        set_actor(session, "bob.buyer")
        submit_order(session, big, actor="bob.buyer")
        session.commit()
        assert big.status == PENDING, big.status
        said = _refused(lambda: require_approved(session, big), OrderStateError)
        session.rollback()

        # 3 (first half) — the engine's own rules: level 1 is the manager's, so a
        # director deciding it is refused
        wrong = _refused(
            lambda: decide_order(session, big, actor="dan.director", action=APPROVE,
                                 role="director"),
            WrongApprover,
        )
        session.rollback()

        set_actor(session, "mia.manager")
        decide_order(session, big, actor="mia.manager", action=APPROVE, role="manager")
        session.commit()
        assert big.status == PENDING, "one level of two finished the chain"
        decide_order(session, big, actor="dan.director", action=APPROVE, role="director")
        session.commit()
        assert big.status == APPROVED and big.approved_by == "dan.director"
        assert require_approved(session, big) is big
        print(f"1. PO-3001 routed on 20000.000000, refused receiving while pending"
              f" ({said[:44]}…), approved after two levels")

        returned = _order(session, number="2", requisition_number="REQ-301",
                          unit_price="9000", quantity="2", order_number="PO-3002")
        submit_order(session, returned, actor="bob.buyer")
        session.commit()
        said = _refused(
            lambda: decide_order(session, returned, actor="mia.manager", action=RETURN,
                                 role="manager"),
            WorkflowError,
        )
        session.rollback()
        decide_order(session, returned, actor="mia.manager", action=RETURN, role="manager",
                     reason="attach the signed quotation")
        session.commit()
        assert returned.status == "returned", returned.status
        print(f"3. a decision from the wrong role refused ({wrong[:46]}…), a reasonless"
              f" return refused, and PO-3002 {returned.status} with its reason recorded")

        # 2 — below every configured level
        small = _order(session, number="3", requisition_number="REQ-302",
                       unit_price="50", quantity="1", order_number="PO-3003")
        submit_order(session, small, actor="bob.buyer")
        session.commit()
        assert small.status == APPROVED and small.approval_request_id is None, small.status
        assert require_approved(session, small) is small
        print("2. PO-3003 (50.000000, below every level) approved on submission, with no"
              " approval request at all")

        # 4 — an amendment is a new revision, and the original keeps its approval
        revised = amend(session, big, number="PO-3001-R2", actor="bob.buyer",
                        lines=[{"line_no": 1, "quantity": "5"}])
        session.commit()
        assert revised.status == DRAFT and revised.revision_no == 2
        assert revised.revision_of_id == big.id
        assert revised.lines[0].quantity == Decimal("5")
        assert revised.lines[0].unit_price == big.lines[0].unit_price
        assert order_total(revised) == Decimal("25000.000000")
        session.refresh(big)
        assert big.status == APPROVED and big.lines[0].quantity == Decimal("4"), (
            "the approved order was edited by the amendment"
        )
        assert [row.number for row in revisions(session, revised)] == [
            "PO-3001", "PO-3001-R2"
        ]
        print("4. PO-3001-R2 raised as a draft over the same lines with the quantity"
              " changed; PO-3001 still approved with its original 4")

        # 5 — a draft is not amended, and one order has one revision
        said = _refused(
            lambda: amend(session, revised, number="PO-3001-R3", actor="bob.buyer"),
            OrderStateError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: amend(session, big, number="PO-3001-R3", actor="bob.buyer"),
            OrderStateError,
        )
        session.rollback()
        print(f"5. a draft is not revised and a second revision is refused: {said}")

        # 6 — closing with open receipts
        assert receipt_progress(session, big)["outstanding"] == Decimal("4.000000")
        said = _refused(lambda: close_order(session, big, actor="mia.manager"),
                        OrderStateError)
        session.rollback()
        close_order(session, big, actor="mia.manager",
                    short_close_reason="supplier discontinued the part")
        session.commit()
        assert big.status == CLOSED and big.closed_at is not None
        assert big.close_reason == "supplier discontinued the part"
        print(f"6. closing with 4.000000 outstanding refused ({said[:58]}…), then closed"
              " short with the reason recorded")

        # 7 — a draft is not closed, and the trail carries actor and time
        said = _refused(lambda: close_order(session, revised, actor="mia.manager"),
                        OrderStateError)
        session.rollback()
        set_actor(session, "mia.manager")
        close_order(session, small, actor="mia.manager",
                    short_close_reason="never needed after all")
        session.commit()
        trail = status_trail(session, small)
        assert trail, "no trail was recorded for the order"
        assert any(row.action == "insert" for row in trail)
        assert any(row.action == "soft_delete" for row in trail) or any(
            row.action == "update" for row in trail
        )
        assert all(row.occurred_at is not None and row.actor for row in trail)
        print(f"7. a draft order is not closed ({said[:44]}…); PO-3003's trail has"
              f" {len(trail)} entries, each with an actor and a time"
              f" ({sorted({row.action for row in trail})})")
        assert small.status == CLOSED

    print("check_po_lifecycle: all assertions green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
