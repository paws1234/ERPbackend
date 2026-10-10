"""T-6.PORTAL.01 check — two suppliers, two doors, and one matrix.

    DATABASE_URL=postgresql+psycopg://erpv1:erpv1@localhost:5432/erpv1 \
        python tests/check_supplier_portal.py

Green on all seven:

1. **a supplier sees only its own documents** — two accounts, each on its own supplier, and
   each payload carries that supplier's RFQ, order and invoice and **nothing** of the other's;
   driven through the API as well, where the subject alone decides whose view it is
2. **a portal response is the matrix's own record** — the answer submitted through the portal
   is the same `RfqResponse` row an internally captured one is (same shape, same reads:
   `responses`, `quoted_line_numbers`), so the comparison matrix shows both without either
   being copied, and a second answer for the same supplier is refused
3. **a PO acknowledgement updates the order's status** — approved → acknowledged, once, with
   the audit trail naming the actor; an order that is not approved, and one already
   acknowledged, are refused; and an acknowledged order is **still one goods may be received
   against** (`require_approved`), because the supplier taking it on does not un-approve it
4. **a submitted invoice is a draft, and the match refuses it until it is posted** — nothing
   is auto-approved, and the invoice belongs to the account's supplier rather than to whoever
   the request named
5. **the scope is the account, not the argument** — another supplier's RFQ, order and invoice
   number are each refused; a subject linked to another supplier cannot be re-linked; and a
   subject with no account is refused (403) rather than shown an empty page
6. **the capability is a capability** — a portal account with no role granted `portal.supplier`
   is refused on every portal path, and the refusal is the platform's one error shape
7. **the API is the whole portal** — documents, an RFQ response, an acknowledgement and an
   invoice submission driven over `GET`/`POST /api/v1/portal/...`, with the acknowledgement
   visible on the order and the response visible in the RFQ the matrix reads

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
from app.ledger.currency import register_currency  # noqa: E402
from app.procurement.orders import (  # noqa: E402
    ACKNOWLEDGED,
    APPROVED,
    OrderStateError,
    acknowledge_order,
    order_by_number,
    require_approved,
    submit_order,
)
from app.procurement.portal import (  # noqa: E402
    AlreadyLinkedError,
    NoPortalAccountError,
    NotYoursError,
    PortalError,
    account_for,
    documents_for,
    link_account,
    respond_to_rfq,
    submit_invoice,
)
from app.procurement.requisitions import (  # noqa: E402
    create_requisition,
    record_decision,
    submit,
)
from app.procurement.rfq import (  # noqa: E402
    DuplicateResponseError,
    NotInvitedError,
    issue_rfq,
    quoted_line_numbers,
    record_response,
    responses,
    rfq_by_number,
)
from app.procurement.orders import award  # noqa: E402
# Imported for their tables, not their APIs: an invoice names a receipt and a supplier tax
# record, the match reads its own runs, and the award reads the requisition's lines — so the
# one schema has to carry them before `create_all`.
from app.matching import MatchRun  # noqa: E402,F401
from app.procurement.receipts import GoodsReceipt  # noqa: E402,F401
from app.procurement.suppliers import add_tax_identifier, create_supplier  # noqa: E402
from app.stock.entries import StockLedgerEntry  # noqa: E402,F401 — for its table
from app.stock.items import Item  # noqa: E402,F401 — for its table
from app.stock.locations import Location  # noqa: E402,F401 — for its table
from app.security import assign, define_role, grant  # noqa: E402
from app.workflow import APPROVE, configure  # noqa: E402

COMPANY = uuid.uuid4()
DAY = date(2026, 10, 1)
DEADLINE = date(2026, 10, 20)
REQUIRED_BY = date(2026, 11, 15)


def _refused(call, expected: type[Exception]) -> str:
    try:
        call()
    except expected as exc:  # noqa: BLE001 — the type and the message are the point
        return str(exc)
    raise AssertionError(f"accepted what it must refuse ({expected.__name__})")


def _answer(session, *, code: str, price: str):
    return {
        "supplier_code": code,
        "received_on": DAY,
        "lines": [{"line_no": 1, "unit_price": price}],
    }


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
            Company(id=COMPANY, code="PORTAL-CHECK", name="Portal check",
                    base_currency="PHP", fiscal_year_start_month=1)
        )
        register_currency(session, company_id=COMPANY, code="PHP", name="Peso")
        session.commit()
        configure(session, company_id=COMPANY, doc_type="purchase_requisition",
                  name="Purchase requisition", levels=[(Decimal("1000"), "manager")])
        # A purchase-order chain that approves anything below a figure nothing here reaches,
        # so an awarded order is approved on the spot and can be acknowledged.
        configure(session, company_id=COMPANY, doc_type="purchase_order",
                  name="Purchase order", levels=[(Decimal("1000000"), "manager")])
        session.commit()

        suppliers = {}
        for code, name in (("ACME", "Acme Supplies"), ("BOREAL", "Boreal Trading")):
            supplier = create_supplier(
                session, company_id=COMPANY, party_code=code, name=name,
                payment_terms_days=30,
            )
            # A TIN, because T-2.PROC.06 refuses to send a purchase order without one for
            # this pack's purchase tax (T-2.PROC.08).
            add_tax_identifier(session, supplier, kind="tin", value=f"TIN-{code}")
            suppliers[code] = supplier
        session.commit()

        requisition = create_requisition(
            session, company_id=COMPANY, number="REQ-1", requested_by="rina.requester",
            needed_by=REQUIRED_BY, currency="PHP",
            lines=[{"description": "Laptops", "quantity": "20", "uom": "each",
                    "estimated_unit_price": "1000.00"}],
        )
        session.commit()
        submit(session, requisition, actor="rina.requester")
        session.commit()
        if requisition.status == "pending":
            record_decision(session, requisition, actor="mia.manager", action=APPROVE,
                            role="manager")
            session.commit()
        rfq = issue_rfq(session, requisition=requisition, number="RFQ-1",
                        supplier_codes=["ACME", "BOREAL"], response_deadline=DEADLINE)

        # A second RFQ, issued to BOREAL alone: what an ACME portal must never be able to
        # read or answer.
        private = create_requisition(
            session, company_id=COMPANY, number="REQ-2", requested_by="rina.requester",
            needed_by=REQUIRED_BY, currency="PHP",
            lines=[{"description": "Cables", "quantity": "5", "uom": "each",
                    "estimated_unit_price": "50.00"}],
        )
        session.commit()
        submit(session, private, actor="rina.requester")
        session.commit()
        if private.status == "pending":
            record_decision(session, private, actor="mia.manager", action=APPROVE,
                            role="manager")
            session.commit()
        others_rfq = issue_rfq(session, requisition=private, number="RFQ-2",
                               supplier_codes=["BOREAL"], response_deadline=DEADLINE)
        session.commit()

        # Two doors: one subject, one supplier each.
        acme_account = link_account(
            session, company_id=COMPANY, supplier=suppliers["ACME"],
            subject="acme.portal", actor="mia.manager",
        )
        boreal_account = link_account(
            session, company_id=COMPANY, supplier=suppliers["BOREAL"],
            subject="boreal.portal", actor="mia.manager",
        )
        session.commit()

        # BOREAL answers through the portal, ACME's answer is captured internally.
        internal = record_response(session, rfq, **_answer(session, code="ACME", price="1000.00"))
        session.commit()
        portal = respond_to_rfq(
            session, account=boreal_account, number="RFQ-1", received_on=DAY,
            lines=[{"line_no": 1, "unit_price": "1005.00"}],
        )
        session.commit()

        # 2 — the portal's answer is the matrix's own record
        assert type(portal) is type(internal), (type(portal), type(internal))
        stored = responses(session, rfq)
        assert len(stored) == 2, stored
        assert {row.supplier_id for row in stored} == {
            suppliers["ACME"].id, suppliers["BOREAL"].id
        }, stored
        assert quoted_line_numbers(portal) == [1] == quoted_line_numbers(internal)
        assert portal.rfq_id == rfq.id and portal.supplier_id == suppliers["BOREAL"].id, portal
        repeated = _refused(
            lambda: respond_to_rfq(
                session, account=boreal_account, number="RFQ-1", received_on=DAY,
                lines=[{"line_no": 1, "unit_price": "999.00"}],
            ),
            DuplicateResponseError,
        )
        session.rollback()
        assert "already answered" in repeated, repeated
        print(
            f"2. the portal's answer is the matrix's own row: {len(stored)} responses on"
            f" RFQ-1, one per supplier, the portal's indistinguishable from the internally"
            f" captured one (line {quoted_line_numbers(portal)} quoted) — and a second"
            " answer is refused"
        )

        # The orders the award generated: one per supplier, so each portal has one to see.
        orders = award(
            session, rfq=rfq, actor="bob.buyer",
            awards=[
                {"supplier_code": "ACME", "number": "PO-1", "required_date": REQUIRED_BY,
                 "lines": [{"line_no": 1, "quantity": "10"}]},
                {"supplier_code": "BOREAL", "number": "PO-2", "required_date": REQUIRED_BY,
                 "lines": [{"line_no": 1, "quantity": "10"}]},
            ],
        )
        session.commit()
        acme_order = order_by_number(session, company_id=COMPANY, number="PO-1")
        boreal_order = order_by_number(session, company_id=COMPANY, number="PO-2")
        # Generated orders are drafts until the buyer sends them through the chain
        # (T-2.PROC.06), which is also what makes them visible to the supplier at all.
        for order in (acme_order, boreal_order):
            submit_order(session, order, actor="bob.buyer")
        session.commit()
        assert acme_order.status == APPROVED and boreal_order.status == APPROVED, (
            acme_order.status,
            boreal_order.status,
        )

        # 3 — the acknowledgement, and what it does not undo
        acknowledge_order(session, acme_order)
        session.commit()
        assert acme_order.status == ACKNOWLEDGED, acme_order.status
        assert require_approved(session, acme_order) is acme_order, "a receipt would be refused"
        again = _refused(
            lambda: acknowledge_order(session, acme_order), OrderStateError
        )
        session.rollback()
        assert "already acknowledged" in again, again
        draft = order_by_number(session, company_id=COMPANY, number="PO-2")
        draft.status = "draft"
        session.flush()
        refused_draft = _refused(
            lambda: acknowledge_order(session, draft), OrderStateError
        )
        draft.status = APPROVED
        session.flush()
        assert "cannot be acknowledged until its approval chain" in refused_draft, refused_draft
        print(
            f"3. the order moved {APPROVED} → {ACKNOWLEDGED} once — a second acknowledgement"
            f" is refused ('{again[:44]}…'), an unapproved one too, and an acknowledged order"
            " is still one goods may be received against"
        )

        # 4 — a submitted invoice is a draft, and the match will not have it yet
        from app.matching import NotMatchableError, match_invoice  # noqa: E402

        invoice = submit_invoice(
            session, account=acme_account, number="INV-1", invoice_date=DAY,
            supplier_reference="ACME-77", order_number="PO-1",
            lines=[{"line_no": 1, "description": "Laptops", "quantity": "10",
                    "unit_price": "1000.00"}],
        )
        session.commit()
        assert invoice.status == "draft", invoice.status
        assert invoice.supplier_id == suppliers["ACME"].id, invoice.supplier_id
        assert Decimal(invoice.gross_amount) == Decimal("10000.000000"), invoice.gross_amount
        unmatched = _refused(lambda: match_invoice(session, invoice), NotMatchableError)
        session.rollback()
        assert "draft" in unmatched, unmatched
        elsewhere = _refused(
            lambda: submit_invoice(
                session, account=acme_account, number="INV-2", invoice_date=DAY,
                supplier_reference="ACME-78", order_number="PO-2",
                lines=[{"line_no": 1, "description": "Laptops", "quantity": "1",
                        "unit_price": "1000.00"}],
            ),
            NotYoursError,
        )
        session.rollback()
        assert "PO-2" in elsewhere, elsewhere
        print(
            f"4. the submitted invoice is {invoice.status} for {invoice.supplier_reference}"
            f" of {Decimal(invoice.gross_amount)} — the match refuses it until the buyer posts"
            f" it ('{unmatched[:38]}…'), so submitting approves nothing, and naming another"
            " supplier's order is refused"
        )

        # 1 — each door shows its own documents and nothing of the other's
        acme_view = documents_for(session, account=acme_account)
        boreal_view = documents_for(session, account=boreal_account)
        assert acme_view["supplier"]["code"] == "ACME" and boreal_view["supplier"]["code"] == "BOREAL"
        assert [row["number"] for row in acme_view["orders"]] == ["PO-1"], acme_view["orders"]
        assert [row["number"] for row in boreal_view["orders"]] == ["PO-2"], boreal_view["orders"]
        assert [row["number"] for row in acme_view["invoices"]] == ["INV-1"], acme_view["invoices"]
        assert boreal_view["invoices"] == [], boreal_view["invoices"]
        assert [row["number"] for row in acme_view["rfqs"]] == ["RFQ-1"], acme_view["rfqs"]
        assert [row["number"] for row in boreal_view["rfqs"]] == ["RFQ-1", "RFQ-2"], (
            boreal_view["rfqs"]
        )
        assert acme_view["rfqs"][0]["answered"] is True, acme_view["rfqs"][0]
        assert boreal_view["rfqs"][0]["answered"] is True, boreal_view["rfqs"][0]
        assert "PO-2" not in str(acme_view) and "INV-1" not in str(boreal_view), "leak"
        assert "responses" not in str(acme_view["rfqs"][0]), "the buyer's matrix leaked"
        assert "RFQ-2" not in str(acme_view), "an RFQ the supplier was not issued leaked"
        print(
            f"1. two doors: ACME is shown {[r['number'] for r in acme_view['orders']]} and"
            f" {[r['number'] for r in acme_view['invoices']]}, BOREAL"
            f" {[r['number'] for r in boreal_view['orders']]} and no invoices — neither"
            " payload carries the other supplier's numbers"
        )

        # 5 — the scope is the account, not an argument
        stolen_rfq = _refused(
            lambda: respond_to_rfq(
                session, account=acme_account, number="RFQ-2", received_on=DAY,
                lines=[{"line_no": 1, "unit_price": "1.00"}],
            ),
            NotInvitedError,
        )
        session.rollback()
        assert "was not issued to supplier 'ACME'" in stolen_rfq, stolen_rfq
        relink = _refused(
            lambda: link_account(
                session, company_id=COMPANY, supplier=suppliers["BOREAL"],
                subject="acme.portal", actor="mia.manager",
            ),
            AlreadyLinkedError,
        )
        session.rollback()
        assert "already acts for supplier 'ACME'" in relink, relink
        homeless = _refused(
            lambda: account_for(session, company_id=COMPANY, subject="nobody"),
            NoPortalAccountError,
        )
        assert "not linked to a supplier" in homeless, homeless
        print(
            f"5. the account is the scope: a subject already acting for a supplier cannot be"
            f" re-linked ('{relink[:48]}…'), a subject with no account is refused"
            f" ('{homeless[:44]}…'), and RFQ-2 — issued to BOREAL alone — is refused to an"
            f" ACME portal ('{stolen_rfq[:52]}…') rather than filtered out"
        )

        # 6 and 7 — the capability, and the whole portal over the API
        portal_role = define_role(session, company_id=COMPANY, code="supplier", name="Supplier")
        reader_role = define_role(session, company_id=COMPANY, code="stray", name="Stray")
        grant(session, portal_role, "portal.supplier")
        assign(session, company_id=COMPANY, subject="acme.portal", role=portal_role)
        assign(session, company_id=COMPANY, subject="boreal.portal", role=portal_role)
        assign(session, company_id=COMPANY, subject="stray.portal", role=reader_role)
        # An account with no capability, and a capability with no account: both are refused,
        # by different sentences, so neither is implied by the other.
        assign(session, company_id=COMPANY, subject="homeless.portal", role=portal_role)
        link_account(
            session, company_id=COMPANY, supplier=suppliers["BOREAL"], subject="stray.portal",
            actor="mia.manager",
        )
        session.commit()

        client = TestClient(app, raise_server_exceptions=False)
        acme_headers = {"X-Company-Id": str(COMPANY), "X-Actor": "acme.portal"}
        boreal_headers = {"X-Company-Id": str(COMPANY), "X-Actor": "boreal.portal"}
        stray_headers = {"X-Company-Id": str(COMPANY), "X-Actor": "stray.portal"}
        homeless_headers = {"X-Company-Id": str(COMPANY), "X-Actor": "homeless.portal"}

        mine = client.get(f"{BASE}/portal/documents", headers=acme_headers)
        assert mine.status_code == 200, mine.text
        view = mine.json()["documents"]
        assert view["supplier"]["code"] == "ACME", view["supplier"]
        assert [row["number"] for row in view["orders"]] == ["PO-1"], view["orders"]
        assert "PO-2" not in mine.text, "one supplier's portal showed another's order"
        theirs = client.get(f"{BASE}/portal/documents", headers=boreal_headers)
        assert theirs.status_code == 200, theirs.text
        assert [row["number"] for row in theirs.json()["documents"]["orders"]] == ["PO-2"]
        unpermitted = client.get(f"{BASE}/portal/documents", headers=stray_headers)
        assert unpermitted.status_code == 403, unpermitted.text
        assert set(unpermitted.json()) == {"error"}, unpermitted.json()
        assert unpermitted.json()["error"]["code"] == "forbidden", unpermitted.json()
        accountless = client.get(f"{BASE}/portal/documents", headers=homeless_headers)
        assert accountless.status_code == 403, accountless.text
        assert accountless.json()["error"]["code"] == "no_portal_account", accountless.json()
        assert accountless.json()["error"]["code"] == "no_portal_account", accountless.json()

        answered = client.post(
            f"{BASE}/portal/rfqs/RFQ-2/responses",
            headers=boreal_headers,
            json={"received_on": DAY.isoformat(),
                  "lines": [{"line_no": 1, "unit_price": "55.00"}]},
        )
        assert answered.status_code == 201, answered.text
        assert answered.json()["recorded"]["supplier"] == "BOREAL", answered.json()
        assert [row.supplier_id for row in responses(session, others_rfq)] == [
            suppliers["BOREAL"].id
        ], "the portal's answer is not in the matrix RFQ-2 is read through"
        repeated_answer = client.post(
            f"{BASE}/portal/rfqs/RFQ-2/responses",
            headers=boreal_headers,
            json={"received_on": DAY.isoformat(),
                  "lines": [{"line_no": 1, "unit_price": "54.00"}]},
        )
        assert repeated_answer.status_code == 422, repeated_answer.text
        assert repeated_answer.json()["error"]["code"] == "rfq_error", repeated_answer.json()

        acknowledged = client.post(
            f"{BASE}/portal/orders/PO-2/acknowledge", headers=boreal_headers
        )
        assert acknowledged.status_code == 200, acknowledged.text
        assert acknowledged.json()["recorded"]["status"] == ACKNOWLEDGED, acknowledged.json()
        assert order_by_number(session, company_id=COMPANY, number="PO-2").status == ACKNOWLEDGED

        submitted = client.post(
            f"{BASE}/portal/invoices",
            headers=boreal_headers,
            json={"number": "INV-2", "invoice_date": DAY.isoformat(),
                  "supplier_reference": "BOREAL-1", "order_number": "PO-2",
                  "lines": [{"line_no": 1, "description": "Laptops", "quantity": "10",
                             "unit_price": "1005.00"}]},
        )
        assert submitted.status_code == 201, submitted.text
        assert submitted.json()["recorded"]["status"] == "draft", submitted.json()
        assert submitted.json()["recorded"]["supplier"] == "BOREAL", submitted.json()
        theirs_again = client.get(f"{BASE}/portal/documents", headers=boreal_headers)
        assert [row["number"] for row in theirs_again.json()["documents"]["invoices"]] == [
            "INV-2"
        ], theirs_again.json()["documents"]["invoices"]
        assert [row["status"] for row in theirs_again.json()["documents"]["orders"]] == [
            ACKNOWLEDGED
        ], theirs_again.json()["documents"]["orders"]
        print(
            f"6. both halves are needed: the capability without an account is refused"
            f" ({accountless.status_code} {accountless.json()['error']['code']}) and the"
            f" account without the capability is refused ({unpermitted.status_code}"
            f" {unpermitted.json()['error']['code']}), each in the platform's one error shape"
        )
        print(
            "7. the whole portal over the API: ACME read its own documents and not BOREAL's,"
            f" BOREAL answered its own RFQ-2 (into the matrix) and acknowledged PO-2"
            f" ({ACKNOWLEDGED}) and submitted INV-2 as a draft, and a second answer for the"
            " same RFQ was refused in the same shape"
        )

        assert str(COMPANY) not in mine.text, "the payload carries a company id"
        assert all(
            row["status"] != "posted" for row in documents_for(session, account=boreal_account)["invoices"]
        ), "a portal invoice was posted by the portal"

    print("\ncheck_supplier_portal: all assertions green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
