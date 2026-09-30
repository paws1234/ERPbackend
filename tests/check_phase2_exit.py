"""T-2.X.GATE — the Phase 2 exit check.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/erpv1 \
        python tests/check_phase2_exit.py

The plan's Phase 2 exit criterion, verbatim: **"End-to-end purchase-to-pay cycle with
3-way match."** This file does not add a feature; it drives one whole cycle through the
modules every earlier task built and checks the five things the ledger's gate names:

1. a requisition runs through approval → RFQ → comparative statement → automated PO →
   GRN → supplier invoice → 3-way match → payment batch → settlement with **no manual
   re-keying**: every figure in the chain is carried from the document that decided it,
   and the comparative statement is read back through the published route the frontend
   repository renders it from before the award consumes the prices it states
2. the 3-way match rate is **measured on the exercised dataset** and reported against
   the > 95 % target — including that a window containing only clean matches does reach
   the target, so the metric can be met and not merely computed
3. **every step's postings balance** (§6 metric 1 — the ledger gate over the stored
   ledger) and the **AP subledger equals the payables control account** (§6 metric 2)
4. a **failed match blocks payment** until an authorised, audited release
5. **supplier tax is applied per the localization pack** on every document in the cycle

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api import BASE, app  # noqa: E402
from app.ap.aging import aging  # noqa: E402
from app.ap.invoices import create_invoice, open_amount, post_invoice  # noqa: E402
from app.ap.payments import (  # noqa: E402
    EXECUTED,
    bank_file,
    create_batch,
    decide_batch,
    execute_batch,
    submit_batch,
)
from app.ap.reconciliation import reconcile  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger.accounts import create_account  # noqa: E402
from app.ledger.currency import register_currency  # noqa: E402
from app.ledger.mapping import set_mapping  # noqa: E402
from app.ledger.posting import JournalEntry, JournalLine  # noqa: E402
from app.matching import (  # noqa: E402
    MATCHED,
    InvoiceHeldError,
    hold_invoice,
    match_invoice,
    match_rate,
    override_count,
    release,
    require_not_held,
    set_tolerance,
)
from app.procurement import receipts as _receipts  # noqa: E402,F401 — the FK target
from app.procurement.orders import (  # noqa: E402
    APPROVED,
    award,
    decide_order,
    order_total,
    orders_for_rfq,
    remaining_awardable,
    submit_order,
)
from app.procurement.receipts import (  # noqa: E402
    create_receipt,
    post_receipt,
    receipt_value,
    rejected_quantity,
)
from app.procurement.requisitions import (  # noqa: E402
    APPROVED as REQ_APPROVED,
    create_requisition,
    record_decision as decide_requisition,
    require_sourceable,
    requisition_total,
    submit as submit_requisition,
)
from app.procurement.rfq import (  # noqa: E402
    issue_rfq,
    late_responses,
    non_responders,
    quoted_line_numbers,
    record_response,
)
from app.procurement.scoring import scorecard  # noqa: E402
from app.procurement.suppliers import add_bank_account, create_supplier  # noqa: E402
from app.procurement.tax import (  # noqa: E402
    PROCUREMENT_DOCUMENTS,
    active_market,
    procurement_rule,
    require_supplier_tax,
    tax_on,
)
from app.security import assign, define_role, grant  # noqa: E402
from app.stock.entries import on_hand  # noqa: E402
from app.stock.items import create_item  # noqa: E402
from app.stock.locations import create_location  # noqa: E402
from app.workflow import APPROVE, configure  # noqa: E402

COMPANY = uuid.uuid4()
ISSUED_ON = date(2026, 10, 1)
DEADLINE = date(2026, 10, 10)
NEEDED_BY = date(2026, 11, 30)
NOVEMBER = date(2026, 11, 5)
DECEMBER = date(2026, 12, 5)
RUN_ON = date(2026, 12, 20)


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
        # --- the company, its books, its chains ----------------------------
        session.add(
            Company(id=COMPANY, code="GATE2", name="Phase 2 gate", base_currency="PHP",
                    fiscal_year_start_month=1)
        )
        register_currency(session, company_id=COMPANY, code="PHP", name="Peso")
        session.commit()
        from tests.seed import seed_stock_accounts

        # The seed maps `stock_receipt` to the pack's own **2010 Goods Received Not
        # Invoiced**: a receipt credits 2010 and the supplier invoice's received line
        # debits it back (T-2.AP.01 posts that line through the same key), so a receipt
        # never touches the payables control account and the control account agrees
        # with the AP subledger in step 3 below. The gate installs **no** mapping of its
        # own here — it uses the seed's, so step 3 is evidence about the default rather
        # than about a substitution the gate made for itself.
        seed_stock_accounts(session, company_id=COMPANY)
        create_account(session, company_id=COMPANY, code="1310", name="Input VAT",
                       account_class="asset")
        session.commit()
        for key, code in (("payables", "2000"), ("input_tax", "1310"),
                          ("expense", "5200"), ("bank", "1010")):
            set_mapping(session, company_id=COMPANY, key=key, account_code=code)
        configure(session, company_id=COMPANY, doc_type="purchase_requisition",
                  name="Requisition", levels=[(Decimal("1000"), "manager")])
        configure(session, company_id=COMPANY, doc_type="purchase_order",
                  name="Order", levels=[(Decimal("10000"), "director")])
        configure(session, company_id=COMPANY, doc_type="payment_batch",
                  name="Payment batch", levels=[(Decimal("20000"), "controller")])
        set_tolerance(session, company_id=COMPANY, quantity_percent="2",
                      price_percent="1", tax_percent="0.5")
        controller = define_role(session, company_id=COMPANY, code="controller",
                                 name="Controller")
        grant(session, controller, "match.override", "payment.run")
        assign(session, company_id=COMPANY, subject="fin.ada", role=controller)
        # The comparative statement is read through the published route in step 1c, so
        # the buyer needs a role that may read an RFQ at all.
        buyer = define_role(session, company_id=COMPANY, code="buyer", name="Buyer")
        grant(session, buyer, "rfq.read")
        assign(session, company_id=COMPANY, subject="bob.buyer", role=buyer)
        acme = create_supplier(session, company_id=COMPANY, party_code="ACME",
                               name="Acme Supplies", payment_terms_days=30)
        add_bank_account(session, acme, bank_name="BPI", account_name="Acme Supplies",
                         account_number="1234567890", swift="BOPIPHMM", is_primary=True)
        from app.procurement.suppliers import add_tax_identifier

        add_tax_identifier(session, acme, kind="tin", value="001-234-567")
        boreal = create_supplier(session, company_id=COMPANY, party_code="BOREAL",
                                 name="Boreal Trading", payment_terms_days=30)
        add_bank_account(session, boreal, bank_name="BDO", account_name="Boreal Trading",
                         account_number="9988776655", swift="BNORPHMM", is_primary=True)
        add_tax_identifier(session, boreal, kind="tin", value="009-876-543")
        chirp = create_supplier(session, company_id=COMPANY, party_code="CHIRP",
                                name="Chirp Industrial", payment_terms_days=30)
        item = create_item(session, company_id=COMPANY, sku="WIDGET", name="Widget",
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

        # --- 1. requisition → approval -------------------------------------
        requisition = create_requisition(
            session, company_id=COMPANY, number="REQ-G1", requested_by="rina.requester",
            needed_by=NEEDED_BY, currency="PHP",
            lines=[
                {"description": "Widgets", "quantity": "100", "uom": "each",
                 "estimated_unit_price": "100", "item_sku": "WIDGET"},
                {"description": "Spare parts", "quantity": "20", "uom": "each",
                 "estimated_unit_price": "50", "item_sku": "WIDGET"},
            ],
        )
        session.commit()
        assert requisition_total(requisition) == Decimal("11000.000000")
        submit_requisition(session, requisition, actor="rina.requester")
        session.commit()
        assert requisition.status == "pending"
        _refused(lambda: require_sourceable(session, requisition), Exception)
        session.rollback()
        decide_requisition(session, requisition, actor="mia.manager", action=APPROVE,
                           role="manager")
        session.commit()
        assert requisition.status == REQ_APPROVED
        print(f"1a. REQ-G1 approved for {requisition_total(requisition)} through its chain")

        # --- 1. RFQ → comparative statement → award ------------------------
        rfq = issue_rfq(session, requisition=requisition, number="RFQ-G1",
                        supplier_codes=["ACME", "BOREAL", "CHIRP"],
                        response_deadline=DEADLINE, issued_on=ISSUED_ON)
        session.commit()
        record_response(session, rfq, supplier_code="ACME", received_on=date(2026, 10, 5),
                        lines=[{"line_no": 1, "unit_price": "100"},
                               {"line_no": 2, "unit_price": "50"}],
                        lead_time_days=21)
        record_response(session, rfq, supplier_code="BOREAL",
                        received_on=date(2026, 10, 14),
                        lines=[{"line_no": 1, "unit_price": "98"},
                               {"line_no": 2, "unit_price": "48"}])
        session.commit()
        assert [row.supplier.party.code for row in late_responses(session, rfq)] == [
            "BOREAL"
        ]
        assert [row.party.code for row in non_responders(session, rfq)] == ["CHIRP"]
        quoted = {row.supplier.party.code: quoted_line_numbers(row)
                  for row in session.scalars(
                      select(__import__("app.procurement.rfq",
                                        fromlist=["RfqResponse"]).RfqResponse)
                  )}
        assert quoted == {"ACME": [1, 2], "BOREAL": [1, 2]}, quoted
        basis = tax_on(Decimal("11000"))
        assert basis["rule_code"] == procurement_rule()["code"]
        print(f"1b. RFQ-G1 issued to three: ACME quoted lines 1–2, BOREAL both lines"
              f" (late), CHIRP silent — the comparison states its basis"
              f" ({basis['rule_code']} at {basis['rate_percent']}%)")

        # --- 1. the comparative statement, read through the published route --------
        # The matrix T-2.PROC.04 renders in the frontend repository reads exactly this
        # payload (`GET {BASE}/rfqs/{{number}}`), so this is the statement the buyer
        # decides on — and the prices it states are the ones `award` below reads.
        client = TestClient(app, raise_server_exceptions=False)
        read = client.get(
            f"{BASE}/rfqs/RFQ-G1",
            headers={"X-Company-Id": str(COMPANY), "X-Actor": "bob.buyer"},
        )
        assert read.status_code == 200, read.text
        statement = read.json()
        assert statement["basis"]["base_currency"] == "PHP", statement["basis"]
        assert statement["basis"]["tax_rule_code"] == basis["rule_code"], statement["basis"]
        assert Decimal(statement["basis"]["tax_rate_percent"]) == Decimal(
            str(basis["rate_percent"])
        ), statement["basis"]
        stated = {supplier["code"]: supplier for supplier in statement["suppliers"]}
        assert set(stated) == {"ACME", "BOREAL", "CHIRP"}, stated
        assert stated["CHIRP"]["responded"] is False and stated["CHIRP"]["lines"] == [], stated
        assert stated["BOREAL"]["late"] is True and stated["ACME"]["late"] is False, stated
        quoted_price = {
            (code, line["line_no"]): Decimal(line["unit_price"])
            for code, supplier in stated.items()
            for line in supplier["lines"]
        }
        assert quoted_price[("ACME", 1)] == Decimal("100.000000"), quoted_price
        assert quoted_price[("ACME", 2)] == Decimal("50.000000"), quoted_price
        assert quoted_price[("BOREAL", 2)] == Decimal("48.000000"), quoted_price
        print(f"1c. the comparative statement states all three invitees side by side on"
              f" its labelled basis ({statement['basis']['tax_rule_code']} at"
              f" {statement['basis']['tax_rate_percent']}%): ACME 100/50, BOREAL 48 on"
              f" line 2, CHIRP blank")

        orders = award(
            session, rfq=rfq, actor="bob.buyer",
            awards=[
                {"supplier_code": "ACME", "number": "PO-G1", "required_date": NEEDED_BY,
                 "lines": [{"line_no": 1, "quantity": "100"},
                           {"line_no": 2, "quantity": "15"}]},
                {"supplier_code": "BOREAL", "number": "PO-G2", "required_date": NEEDED_BY,
                 "lines": [{"line_no": 2, "quantity": "5"}]},
            ],
        )
        session.commit()
        acme_order, boreal_order = orders
        assert order_total(acme_order) == Decimal("10750.000000")
        assert acme_order.lines[0].unit_price == Decimal("100.00")
        # the hand-off, stated rather than assumed: every awarded price is the winning
        # supplier's own quoted price in the statement above — nothing re-keyed. A PO
        # line numbers itself, so the link back to the quote is `rfq_line_id`.
        rfq_line_no = {line.id: line.line_no for line in rfq.lines}
        for order, code in ((acme_order, "ACME"), (boreal_order, "BOREAL")):
            assert order.supplier.party.code == code, order.supplier.party.code
            for line in order.lines:
                from_rfq = rfq_line_no[line.rfq_line_id]
                assert line.unit_price == quoted_price[(code, from_rfq)], (
                    code, from_rfq, line.unit_price
                )
        assert remaining_awardable(session, rfq) == {
            1: Decimal("0.000000"), 2: Decimal("0.000000")
        }, remaining_awardable(session, rfq)
        assert order_total(boreal_order) == Decimal("240.000000")
        assert len(orders_for_rfq(session, rfq)) == 2
        print(f"1d. award generated PO-G1 ({order_total(acme_order)} from ACME's own"
              f" quote) and PO-G2 ({order_total(boreal_order)} from BOREAL's) with no"
              " re-keying — every line's price is the one the statement carries")

        for order in (acme_order, boreal_order):
            submit_order(session, order, actor="bob.buyer")
            session.commit()
            if order.status == "pending":
                decide_order(session, order, actor="dan.director", action=APPROVE,
                             role="director")
                session.commit()
        assert acme_order.status == APPROVED and acme_order.approved_by == "dan.director"
        print(f"1e. both orders approved through their own chain"
              f" ({acme_order.status}/{boreal_order.status})")

        # --- 1. GRN --------------------------------------------------------
        good_receipt = create_receipt(session, order=acme_order, number="GRN-G1",
                                      location=bin_a, received_on=date(2026, 10, 20),
                                      lines=[{"line_no": 1, "quantity": "100"},
                                             {"line_no": 2, "quantity": "15"}])
        short_receipt = create_receipt(session, order=boreal_order, number="GRN-G2",
                                       location=bin_a, received_on=date(2026, 10, 21),
                                       lines=[{"line_no": 1, "quantity": "5",
                                               "rejected_quantity": "1"}])
        session.commit()
        post_receipt(session, good_receipt)
        post_receipt(session, short_receipt)
        session.commit()
        assert receipt_value(good_receipt) == Decimal("10750.000000")
        assert rejected_quantity(session, boreal_order) == Decimal("1.000000")
        assert on_hand(session, company_id=COMPANY, item_id=item.id,
                       location_id=bin_a.id)["quantity"] == Decimal("120.000000")
        print(f"1e. GRN-G1 received all 115 for {receipt_value(good_receipt)}; GRN-G2"
              f" received 5 with 1 rejected — the rejection is on the document")

        # --- 1 + 5. supplier invoices, tax per the pack --------------------
        rates = {doc: tax_on(Decimal("10750"), document_type=doc)["rule_code"]
                 for doc in PROCUREMENT_DOCUMENTS}
        assert len(set(rates.values())) == 1, rates
        for invoice_supplier in (acme, boreal):
            require_supplier_tax(session, invoice_supplier, document_type="supplier_invoice")
        clean_tax = tax_on(Decimal("10750"))["tax"]
        assert clean_tax == Decimal("1290.000000"), clean_tax

        clean = create_invoice(
            session, company_id=COMPANY, number="AP-G1", supplier=acme,
            supplier_reference="ACME-G1", invoice_date=NOVEMBER,
            order_id=acme_order.id, receipt_id=good_receipt.id,
            lines=[
                {"description": "Widgets", "item_id": item.id, "quantity": "100",
                 "unit_price": "100", "tax_amount": "1200.00",
                 "order_line_id": acme_order.lines[0].id,
                 "receipt_line_id": good_receipt.lines[0].id},
                {"description": "Spare parts", "item_id": item.id, "quantity": "15",
                 "unit_price": "50", "tax_amount": "90.00",
                 "order_line_id": acme_order.lines[1].id,
                 "receipt_line_id": good_receipt.lines[1].id},
            ],
        )
        session.commit()
        post_invoice(session, clean)
        session.commit()
        assert clean.tax_amount == clean_tax, (clean.tax_amount, clean_tax)
        broken = create_invoice(
            session, company_id=COMPANY, number="AP-G2", supplier=boreal,
            supplier_reference="BOREAL-G2", invoice_date=DECEMBER,
            order_id=boreal_order.id, receipt_id=short_receipt.id,
            lines=[{"description": "Spares, invoiced for one more than arrived",
                    "item_id": item.id, "quantity": "6", "unit_price": "48",
                    "tax_amount": "34.56",
                    "order_line_id": boreal_order.lines[0].id,
                    "receipt_line_id": short_receipt.lines[0].id}],
        )
        session.commit()
        post_invoice(session, broken)
        session.commit()
        print(f"1f. AP-G1 and AP-G2 posted; the 12 % input tax is the pack's own rule"
              f" ({rates}) and {clean.tax_amount} on 10750.000000")

        # --- 2. the 3-way match, and the rate ------------------------------
        clean_run = match_invoice(session, clean)
        broken_run = match_invoice(session, broken)
        session.commit()
        assert clean_run.status == MATCHED, clean_run.details
        assert broken_run.status != MATCHED, broken_run.details
        november = match_rate(session, company_id=COMPANY, start=date(2026, 11, 1),
                              end=date(2026, 11, 30))
        assert november["considered"] == 1 and november["rate_percent"] == Decimal(
            "100.0000"
        ), november
        assert november["met"] is True, november
        both = match_rate(session, company_id=COMPANY, start=date(2026, 11, 1),
                          end=date(2026, 12, 31))
        assert both["considered"] == 2 and both["rate_percent"] == Decimal("50.0000"), both
        assert both["target_percent"] == Decimal("95") and both["met"] is False, both
        print(f"2. the rate is measured on the dataset: 100.0000 % over the clean month"
              f" (target met) and {both['rate_percent']} % over both, reported against"
              f" the {both['target_percent']} % target")

        # --- 4. a failed match blocks payment ------------------------------
        hold = hold_invoice(session, broken, reason="invoiced for six but five arrived")
        session.commit()
        said = _refused(lambda: require_not_held(session, broken), InvoiceHeldError)
        session.rollback()
        blocked = _refused(
            lambda: create_batch(session, company_id=COMPANY, number="PB-G0",
                                 scheduled_on=RUN_ON,
                                 invoice_numbers=["AP-G1", "AP-G2"]),
            InvoiceHeldError,
        )
        session.rollback()
        release(session, hold, actor="fin.ada",
                reason="the 1 rejected unit was credited on a debit note",
                on=date(2026, 12, 18))
        session.commit()
        assert override_count(session, company_id=COMPANY, start=date(2026, 12, 1),
                             end=date(2026, 12, 31)) == 1
        print(f"4. the failed match blocked payment ({said[:40]}… / {blocked[:40]}…)"
              " until an authorised, recorded release")

        # --- 1. payment batch → settlement ---------------------------------
        batch = create_batch(session, company_id=COMPANY, number="PB-G1",
                             scheduled_on=RUN_ON,
                             invoice_numbers=["AP-G1", "AP-G2"])
        session.commit()
        assert batch.total_amount == Decimal("12362.560000"), batch.total_amount
        # T-2.AP.02's criterion on this cycle: the aging report's own total, stated
        # against the payables control account, is what the batch is about to settle.
        # The receipts are not in it — they sit in GRNI 2010, clear of the control
        # account, which is the whole point of the seed's mapping.
        aged = aging(session, company_id=COMPANY, as_of=RUN_ON)
        assert aged.total == batch.total_amount, (aged.total, batch.total_amount)
        assert aged.control == {"PHP": aged.total}, aged.control
        assert aged.balanced is True, aged.difference
        print(f"    the aging report reads {aged.total} — the control account's own"
              f" balance (difference {aged.difference['PHP']}), which is what the batch"
              " settles")
        submit_batch(session, batch, actor="fin.ada")
        session.commit()
        if batch.status == "pending":
            decide_batch(session, batch, actor="rose.ctl", action=APPROVE,
                         role="controller")
            session.commit()
        execute_batch(session, batch, actor="fin.ada", executed_on=RUN_ON)
        session.commit()
        assert batch.status == EXECUTED
        assert open_amount(session, clean) == Decimal("0.000000")
        assert open_amount(session, broken) == Decimal("0.000000")
        assert "payee_account" in bank_file(session, batch).split("\n")[0]
        print(f"1g. PB-G1 paid {batch.total_amount} across both invoices and settled"
              " them; the bank file follows the pack")

        # --- 3. the ledger and the control account -------------------------
        from tests.check_ledger_integrity import ledger_gate

        with engine.connect() as connection:
            assert ledger_gate(connection) == 0, "a posting in the cycle does not balance"
        report = reconcile(session, company_id=COMPANY, as_of=RUN_ON)
        assert report["balanced"] is True, report
        assert report["currencies"][0]["subledger"] == Decimal("0.000000"), report
        assert report["currencies"][0]["control"] == Decimal("0.000000"), report
        # the receipts' counterpart sits where it belongs, waiting to be invoiced
        grni = session.scalar(
            select(func.coalesce(func.sum(JournalLine.credit - JournalLine.debit), 0))
            .select_from(JournalLine)
            .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
            .where(
                JournalEntry.company_id == COMPANY,
                JournalLine.account == "2010",
                JournalEntry.currency == "PHP",
            )
        )
        assert Decimal(grni) == Decimal("-48.000000"), grni
        print("3. every entry in the cycle balances (the T-0.CORE.02 gate over the stored"
              f" ledger) and the AP subledger equals the payables control account"
              f" ({report['currencies'][0]['control']}), with the 48.000000 overbilling"
              f" debit retained in GRNI account 2010 ({grni}) — the seed's own mapping,"
              " installed with no substitution by this check")

        # --- the cycle is complete, and nothing was re-keyed ---------------
        card = scorecard(session, supplier=acme)
        assert card["rated"] is True and card["metrics"]["on_time"] == Decimal("100.000000")
        assert card["inputs"][0]["receipt"] == "GRN-G1"
        assert active_market()
        print(f"   ACME's scorecard reads {card['score']} over"
              f" {len(card['inputs'])} received line(s), every input naming its"
              " documents")
        print("check_phase2_exit: the purchase-to-pay cycle runs end to end — all five"
              " gate criteria hold")

    return 0


if __name__ == "__main__":
    sys.exit(main())
