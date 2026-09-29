"""T-2.PROC.03 check — issuing an RFQ and capturing the answers.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/erpv1 \
        python tests/check_rfq_responses.py

Green on all eight:

1. an RFQ is issued against an **approved** requisition, to two suppliers, copying the
   requisition's own lines and keeping the requisition line each stands for
2. an unapproved requisition cannot be asked about: issuing is refused
3. responses are captured **per line per supplier**, and the requisition lines each
   response covers are answerable exactly
4. a response that quotes a line the RFQ never asked about is refused, and so is a
   negative price
5. a **late** response is recorded with `late = true` rather than refused or silently
   accepted, and `late_responses` names it
6. a supplier the RFQ was never issued to cannot answer, and a second answer from the
   same supplier is refused — one supplier, one answer
7. a supplier that did not answer at all is **distinguishable** from one that answered
   and quoted nothing for a line
8. an RFQ to nobody, a deadline before the issue date, a repeated RFQ number and a
   party that is not a supplier are all refused

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api import BASE, app  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger.currency import register_currency, store_rate  # noqa: E402
from app.party import create_party  # noqa: E402
from app.security import assign, define_role, grant  # noqa: E402
from app.procurement.requisitions import (  # noqa: E402
    NotApprovedError,
    create_requisition,
    record_decision,
    submit,
)
from app.procurement.rfq import (  # noqa: E402
    CLOSED,
    DuplicateResponseError,
    DuplicateRfqError,
    IncompleteResponseError,
    NotInvitedError,
    RfqError,
    RfqStateError,
    UnknownRfqLineError,
    close_rfq,
    invited,
    issue_rfq,
    late_responses,
    non_responders,
    quoted_line_numbers,
    record_response,
    requisition_lines_covered,
    responses,
    rfq_by_number,
)
from app.procurement.suppliers import (  # noqa: E402
    NotASupplierError,
    create_supplier,
)
from app.workflow import APPROVE, configure  # noqa: E402

COMPANY = uuid.uuid4()
DEADLINE = date(2026, 10, 10)
ISSUED_ON = date(2026, 10, 1)
LINES = [
    {"description": "Laptops", "quantity": "20", "uom": "each",
     "estimated_unit_price": "1000.00"},
    {"description": "Monitors", "quantity": "40", "uom": "each",
     "estimated_unit_price": "250.00"},
]


def _refused(call, expected: type[Exception] | str) -> str:
    try:
        call()
    except Exception as exc:  # noqa: BLE001 — the type and message are the point
        if isinstance(expected, str):
            assert expected in str(exc), f"unclear refusal: {exc}"
        else:
            assert isinstance(exc, expected), f"refused with {type(exc).__name__}: {exc}"
        return str(exc)
    raise AssertionError("accepted what it must refuse")


def _approved_requisition(session, number: str, amount: str, description: str):
    """A one-line requisition that has finished a one-level chain."""
    requisition = create_requisition(
        session,
        company_id=COMPANY,
        number=number,
        requested_by="rina.requester",
        needed_by=date(2026, 11, 30),
        currency="PHP",
        lines=[{"description": description, "quantity": "1", "uom": "each",
                "estimated_unit_price": amount}],
    )
    session.commit()
    submit(session, requisition, actor="rina.requester")
    session.commit()
    if requisition.status == "pending":
        record_decision(session, requisition, actor="mia.manager", action=APPROVE,
                        role="manager")
        session.commit()
    return requisition


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
            Company(id=COMPANY, code="RFQ-CHECK", name="RFQ check", base_currency="PHP",
                    fiscal_year_start_month=1)
        )
        register_currency(session, company_id=COMPANY, code="PHP", name="Peso")
        register_currency(session, company_id=COMPANY, code="USD", name="US Dollar")
        session.commit()
        configure(session, company_id=COMPANY, doc_type="purchase_requisition",
                  name="Purchase requisition", levels=[(Decimal("1000"), "manager")])
        session.commit()

        # the requisition the RFQ asks about, and the suppliers it is issued to
        requisition = create_requisition(
            session, company_id=COMPANY, number="REQ-100", requested_by="rina.requester",
            needed_by=date(2026, 11, 30), currency="PHP", lines=LINES,
        )
        session.commit()
        submit(session, requisition, actor="rina.requester")
        session.commit()
        assert requisition.status == "pending"
        said = _refused(
            lambda: issue_rfq(session, requisition=requisition, number="RFQ-100",
                              supplier_codes=["ACME"], response_deadline=DEADLINE),
            NotApprovedError,
        )
        session.rollback()
        print(f"2. an unapproved requisition cannot be asked about: {said[:58]}…")

        record_decision(session, requisition, actor="mia.manager", action=APPROVE,
                        role="manager")
        session.commit()
        for code, name in (("ACME", "Acme Supplies"), ("BOREAL", "Boreal Trading"),
                           ("CHIRP", "Chirp Industrial")):
            create_supplier(session, company_id=COMPANY, party_code=code, name=name,
                            payment_terms_days=30)
        create_party(session, company_id=COMPANY, code="NOTSUP", name="Not A Supplier",
                     roles=["customer"])
        session.commit()

        # 1 — issue it
        rfq = issue_rfq(session, requisition=requisition, number="RFQ-100",
                        supplier_codes=["ACME", "BOREAL"], response_deadline=DEADLINE,
                        issued_on=ISSUED_ON)
        session.commit()
        assert rfq.status == "issued" and rfq.currency == "PHP"
        assert [line.description for line in rfq.lines] == ["Laptops", "Monitors"]
        assert [line.line_no for line in rfq.lines] == [1, 2]
        assert {line.requisition_line_id for line in rfq.lines} == {
            line.id for line in requisition.lines
        }
        assert sorted(s.party.code for s in invited(session, rfq)) == ["ACME", "BOREAL"]
        print("1. RFQ-100 issued to ACME and BOREAL over REQ-100's two lines, each line"
              " keeping the requisition line it stands for")

        # 3 — answers, per line, and the coverage is exact
        acme = record_response(
            session, rfq, supplier_code="ACME", received_on=date(2026, 10, 5),
            lines=[{"line_no": 1, "unit_price": "980.00"},
                   {"line_no": 2, "unit_price": "255.50"}],
            lead_time_days=21, valid_until=date(2026, 11, 15),
        )
        session.commit()
        assert quoted_line_numbers(acme) == [1, 2]
        assert acme.lead_time_days == 21 and acme.valid_until == date(2026, 11, 15)
        assert len(acme.lines) == 2 and acme.lines[0].unit_price == Decimal("980.00")
        assert set(requisition_lines_covered(acme)) == {line.id for line in requisition.lines}
        print(f"3. ACME answered on lines {quoted_line_numbers(acme)}, covering both"
              " requisition lines, lead time 21 days")

        # 4 — a line nobody asked about, and a negative price
        said = _refused(
            lambda: record_response(
                session, rfq, supplier_code="BOREAL", received_on=date(2026, 10, 6),
                lines=[{"line_no": 9, "unit_price": "1"}],
            ),
            UnknownRfqLineError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: record_response(
                session, rfq, supplier_code="BOREAL", received_on=date(2026, 10, 6),
                lines=[{"line_no": 1, "unit_price": "-1"}],
            ),
            IncompleteResponseError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: record_response(
                session, rfq, supplier_code="BOREAL", received_on=date(2026, 10, 6),
                lines=[],
            ),
            IncompleteResponseError,
        )
        session.rollback()
        print(f"4. a line the RFQ never asked about and a negative price are refused: {said}")

        # 5 — late is recorded, not swallowed
        late = record_response(
            session, rfq, supplier_code="BOREAL", received_on=date(2026, 10, 14),
            lines=[{"line_no": 1, "unit_price": "1005.00"}], currency="USD",
        )
        session.commit()
        assert late.late is True, "a response after the deadline was not marked late"
        assert late.currency == "USD"
        assert [row.supplier.party.code for row in late_responses(session, rfq)] == ["BOREAL"]
        early = responses(session, rfq)[0]
        assert early.late is False, "an on-time response was marked late"
        print(f"5. BOREAL's answer on {late.received_on} is recorded late=true (deadline"
              f" {DEADLINE}); ACME's is late=false")

        # 6 — invitees only, one answer each
        said = _refused(
            lambda: record_response(
                session, rfq, supplier_code="CHIRP", received_on=date(2026, 10, 6),
                lines=[{"line_no": 1, "unit_price": "900"}],
            ),
            NotInvitedError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: record_response(
                session, rfq, supplier_code="ACME", received_on=date(2026, 10, 7),
                lines=[{"line_no": 1, "unit_price": "970"}],
            ),
            DuplicateResponseError,
        )
        session.rollback()
        print(f"6. a supplier that was not invited and a second answer are refused: {said}")

        # 7 — "did not answer" is not the same as "quoted nothing for a line"
        chirpless = issue_rfq(
            session, requisition=_approved_requisition(session, "REQ-101", "5000",
                                                       "Rags"),
            number="RFQ-101", supplier_codes=["ACME", "CHIRP"],
            response_deadline=DEADLINE, issued_on=ISSUED_ON,
        )
        session.commit()
        record_response(
            session, chirpless, supplier_code="ACME", received_on=date(2026, 10, 5),
            lines=[{"line_no": 1, "unit_price": "0"}],
        )
        session.commit()
        quiet = responses(session, chirpless)[0]
        assert quoted_line_numbers(quiet) == [1] and quiet.lines[0].unit_price == Decimal(0)
        assert [s.party.code for s in non_responders(session, chirpless)] == ["CHIRP"], (
            "a silent supplier is not distinguishable from one that answered"
        )
        assert all(row.supplier.party.code != "CHIRP" for row in responses(session, chirpless))
        assert [s.party.code for s in invited(session, chirpless)] == ["ACME", "CHIRP"]
        print("7. ACME quoted 0 (an answer); CHIRP is a non-responder — the two are told apart")

        # 8 — the rest of the edges
        said = _refused(
            lambda: issue_rfq(
                session, requisition=requisition, number="RFQ-102", supplier_codes=[],
                response_deadline=DEADLINE, issued_on=ISSUED_ON,
            ),
            RfqError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: issue_rfq(
                session, requisition=requisition, number="RFQ-102",
                supplier_codes=["ACME"], response_deadline=date(2026, 9, 30),
                issued_on=ISSUED_ON,
            ),
            RfqError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: issue_rfq(
                session, requisition=requisition, number="RFQ-100", supplier_codes=["ACME"],
                response_deadline=DEADLINE, issued_on=ISSUED_ON,
            ),
            DuplicateRfqError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: issue_rfq(
                session, requisition=requisition, number="RFQ-102",
                supplier_codes=["NOTSUP"], response_deadline=DEADLINE, issued_on=ISSUED_ON,
            ),
            NotASupplierError,
        )
        session.rollback()
        print(f"8. an RFQ to nobody, a deadline before issue, a repeated number and a"
              f" non-supplier are refused: {said}")

        # and a closed RFQ takes no more answers
        close_rfq(session, rfq)
        session.commit()
        assert rfq.status == CLOSED
        said = _refused(
            lambda: record_response(
                session, rfq, supplier_code="CHIRP", received_on=date(2026, 10, 6),
                lines=[{"line_no": 1, "unit_price": "900"}],
            ),
            RfqStateError,
        )
        session.rollback()
        assert rfq_by_number(session, company_id=COMPANY, number="RFQ-100").id == rfq.id
        print(f"   a closed RFQ takes no more answers: {said}")

        # 9 — the same facts through the published API, which is what the
        # comparative statement matrix (T-2.PROC.04) reads: the quotes, the rate
        # each was converted at, and the basis the comparison has to label.
        role = define_role(session, company_id=COMPANY, code="buyer", name="Buyer")
        grant(session, role, "rfq.post", "rfq.read")
        assign(session, company_id=COMPANY, subject="alice", role=role)
        store_rate(session, company_id=COMPANY, base_currency="PHP", currency="USD",
                   on=ISSUED_ON, rate="58.5")
        session.commit()

        client = TestClient(app, raise_server_exceptions=False)
        headers = {"X-Company-Id": str(COMPANY), "X-Actor": "alice"}
        read = client.get(f"{BASE}/rfqs/RFQ-100", headers=headers)
        assert read.status_code == 200, read.text
        body = read.json()
        assert body["requisition"] == "REQ-100" and body["status"] == "closed"
        assert [line["line_no"] for line in body["lines"]] == [1, 2]
        assert body["basis"] == {
            "base_currency": "PHP",
            "fx_on": ISSUED_ON.isoformat(),
            "tax_rule_code": "VAT-IN-12",
            "tax_rate_percent": "12",
            "tax_inclusive": True,
        }, body["basis"]
        by_code = {supplier["code"]: supplier for supplier in body["suppliers"]}
        assert set(by_code) == {"ACME", "BOREAL"}, by_code
        assert by_code["ACME"]["responded"] is True and by_code["ACME"]["late"] is False
        assert by_code["ACME"]["lines"][0]["base_unit_price"] == "980.000000"
        # a USD quote, converted at the issue date's rate — the rate travels with
        # the number, so the basis is visible to the client rather than implied
        usd = by_code["BOREAL"]["lines"][0]
        assert usd["currency"] == "USD" and usd["unit_price"] == "1005.000000", usd
        assert usd["fx_rate"] == "58.5000000000" and usd["base_unit_price"] == "58792.500000", usd
        assert by_code["BOREAL"]["late"] is True
        # a company with no chain of its own is not asked to be readable here
        assert client.get(f"{BASE}/rfqs/RFQ-100",
                          headers={"X-Company-Id": str(COMPANY), "X-Actor": "nobody"}
                          ).status_code == 403
        print("9. the API carries the quotes, the converted base price and the labelled"
              " basis the matrix reads (and refuses an actor without the capability)")

    print("check_rfq_responses: all assertions green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
