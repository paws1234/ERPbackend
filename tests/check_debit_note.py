"""T-2.AP.03 check — debit notes for returns and adjustments.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/erpv1 \
        python tests/check_debit_note.py

Green on all eight:

1. an **adjustment** posts the invoice's entry with the sides swapped — payables
   debited, the cost accounts and input tax credited — and it balances
2. it **reduces what the invoice is owed** by exactly its gross amount, through a
   settlement row, so aging and the control-account reconciliation see it without
   knowing debit notes exist
3. a **return** posts the same money reversal **and** takes the stock out of the named
   location at what it cost — one economic event, one GL entry, no second posting
   crediting inventory for the same goods
4. a return refuses a location that does not hold the quantity, and refuses a line with
   no stock item (that is an adjustment)
5. a note for more than the invoice's open amount is refused without an override, and
   accepted with one — recorded on the note
6. a debit note against a **draft** invoice is refused, and a posted note is never
   posted twice
7. an unknown kind, a return with no location, an adjustment with one, a repeated
   number and an empty note are refused
8. the note keeps the invoice it reverses, and the invoice's history shows it

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

from app.ap.debit_notes import (  # noqa: E402
    POSTED,
    DebitNoteError,
    DebitNoteStateError,
    DuplicateDebitNoteError,
    OverNoteError,
    create_debit_note,
    debit_note_by_number,
    debit_notes_for_invoice,
    noted_amount,
    post_debit_note,
)
from app.ap.invoices import (  # noqa: E402
    create_invoice,
    open_amount,
    post_invoice,
)
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger.accounts import create_account  # noqa: E402
from app.ledger.currency import register_currency  # noqa: E402
from app.ledger.mapping import set_mapping  # noqa: E402
from app.ledger.posting import JournalEntry  # noqa: E402
from app.procurement import receipts as _receipts  # noqa: E402,F401 — the FK target
from app.procurement.orders import award, decide_order, submit_order  # noqa: E402
from app.procurement.receipts import create_receipt, post_receipt  # noqa: E402
from app.procurement.requisitions import (  # noqa: E402
    create_requisition,
    record_decision as decide_requisition,
    submit as submit_requisition,
)
from app.procurement.rfq import issue_rfq, record_response  # noqa: E402
from app.procurement.suppliers import add_tax_identifier, create_supplier  # noqa: E402
from app.stock.entries import StockLedgerEntry, on_hand  # noqa: E402
from app.stock.items import create_item  # noqa: E402
from app.stock.locations import create_location  # noqa: E402
from app.workflow import APPROVE, configure  # noqa: E402

COMPANY = uuid.uuid4()
ISSUED_ON = date(2026, 10, 1)
DEADLINE = date(2026, 10, 10)
INVOICE_DATE = date(2026, 11, 5)


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
            Company(id=COMPANY, code="DN-CHECK", name="Debit note check",
                    base_currency="PHP", fiscal_year_start_month=1)
        )
        register_currency(session, company_id=COMPANY, code="PHP", name="Peso")
        session.commit()
        from tests.seed import seed_stock_accounts

        seed_stock_accounts(session, company_id=COMPANY)
        create_account(session, company_id=COMPANY, code="1310", name="Input VAT",
                       account_class="asset")
        session.commit()
        set_mapping(session, company_id=COMPANY, key="payables", account_code="2000")
        set_mapping(session, company_id=COMPANY, key="input_tax", account_code="1310")
        set_mapping(session, company_id=COMPANY, key="expense", account_code="5200")
        session.commit()
        configure(session, company_id=COMPANY, doc_type="purchase_requisition",
                  name="Requisition", levels=[(Decimal("1"), "manager")])
        configure(session, company_id=COMPANY, doc_type="purchase_order",
                  name="Order", levels=[(Decimal("100000"), "manager")])
        supplier = create_supplier(session, company_id=COMPANY, party_code="ACME",
                                   name="Acme Supplies", payment_terms_days=30)
        add_tax_identifier(session, supplier, kind="tin", value="001-234-567")
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
        empty_bin = create_location(session, company_id=COMPANY, code="MAIN-B2",
                                    name="Empty bin", location_type="bin",
                                    parent_id=aisle.id)
        session.commit()

        # a real chain so the stock and the invoice both exist
        requisition = create_requisition(
            session, company_id=COMPANY, number="REQ-700", requested_by="rina.requester",
            needed_by=date(2026, 11, 30), currency="PHP",
            lines=[{"description": "Widgets", "quantity": "10", "uom": "each",
                    "estimated_unit_price": "100", "item_sku": "WIDGET"}],
        )
        session.commit()
        submit_requisition(session, requisition, actor="rina.requester")
        session.commit()
        decide_requisition(session, requisition, actor="mia.manager", action=APPROVE,
                           role="manager")
        session.commit()
        rfq = issue_rfq(session, requisition=requisition, number="RFQ-700",
                        supplier_codes=["ACME"], response_deadline=DEADLINE,
                        issued_on=ISSUED_ON)
        session.commit()
        record_response(session, rfq, supplier_code="ACME", received_on=date(2026, 10, 5),
                        lines=[{"line_no": 1, "unit_price": "100"}])
        session.commit()
        order = award(
            session, rfq=rfq, actor="bob.buyer",
            awards=[{"supplier_code": "ACME", "number": "PO-7001",
                     "required_date": date(2026, 11, 30),
                     "lines": [{"line_no": 1, "quantity": "10"}]}],
        )[0]
        session.commit()
        submit_order(session, order, actor="bob.buyer")
        session.commit()
        receipt = create_receipt(session, order=order, number="GRN-7001", location=bin_a,
                                 received_on=date(2026, 10, 20),
                                 lines=[{"line_no": 1, "quantity": "10"}])
        session.commit()
        post_receipt(session, receipt)
        session.commit()
        assert on_hand(session, company_id=COMPANY, item_id=item.id,
                       location_id=bin_a.id)["quantity"] == Decimal("10.000000")

        invoice = create_invoice(
            session, company_id=COMPANY, number="AP-7001", supplier=supplier,
            supplier_reference="ACME-7001", invoice_date=INVOICE_DATE,
            order_id=order.id, receipt_id=receipt.id,
            lines=[{"description": "Widgets", "item_id": item.id, "quantity": "10",
                    "unit_price": "100", "tax_amount": "120.00",
                    "order_line_id": order.lines[0].id,
                    "receipt_line_id": receipt.lines[0].id}],
        )
        session.commit()
        post_invoice(session, invoice)
        session.commit()
        assert open_amount(session, invoice) == Decimal("1120.000000")
        draft_invoice = create_invoice(
            session, company_id=COMPANY, number="AP-7002", supplier=supplier,
            supplier_reference="ACME-7002", invoice_date=INVOICE_DATE,
            lines=[{"description": "Widgets", "quantity": "1", "unit_price": "100"}],
        )
        session.commit()

        # 6 (first half) + 7 — the edges that need no posting
        said = _refused(
            lambda: create_debit_note(session, number="DN-7000", invoice=draft_invoice,
                                      note_date=INVOICE_DATE, kind="adjustment",
                                      lines=[{"description": "x", "quantity": "1",
                                              "unit_price": "1"}]),
            DebitNoteStateError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: create_debit_note(session, number="DN-7000", invoice=invoice,
                                      note_date=INVOICE_DATE, kind="write-off",
                                      lines=[{"description": "x", "quantity": "1",
                                              "unit_price": "1"}]),
            DebitNoteError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: create_debit_note(session, number="DN-7000", invoice=invoice,
                                      note_date=INVOICE_DATE, kind="return",
                                      lines=[{"description": "x", "quantity": "1",
                                              "unit_price": "1"}]),
            DebitNoteError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: create_debit_note(session, number="DN-7000", invoice=invoice,
                                      note_date=INVOICE_DATE, kind="adjustment",
                                      location=bin_a,
                                      lines=[{"description": "x", "quantity": "1",
                                              "unit_price": "1"}]),
            DebitNoteError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: create_debit_note(session, number="DN-7000", invoice=invoice,
                                      note_date=INVOICE_DATE, kind="adjustment",
                                      lines=[]),
            DebitNoteError,
        )
        session.rollback()
        print(f"6a/7. a draft invoice, an unknown kind, a return with no location, an"
              f" adjustment with one and an empty note are refused: {said}")

        # 1 + 2 — an adjustment reverses the invoice and reduces what is owed
        adjustment = create_debit_note(
            session, number="DN-7001", invoice=invoice, note_date=date(2026, 11, 20),
            kind="adjustment",
            lines=[{"description": "Price adjustment on widgets", "item_id": item.id,
                    "quantity": "10", "uom": "each", "unit_price": "5",
                    "tax_amount": "6.00"}],
        )
        session.commit()
        assert adjustment.net_amount == Decimal("50.000000"), adjustment.net_amount
        assert adjustment.tax_amount == Decimal("6.000000")
        assert adjustment.gross_amount == Decimal("56.000000"), adjustment.gross_amount
        entry = post_debit_note(session, adjustment)
        session.commit()
        by_account = {line.account: line for line in entry.lines}
        assert by_account["2000"].debit == Decimal("56.000000"), by_account["2000"]
        assert by_account["1200"].credit == Decimal("50.000000"), by_account["1200"]
        assert by_account["1310"].credit == Decimal("6.000000"), by_account["1310"]
        assert sum(line.debit for line in entry.lines) == sum(
            line.credit for line in entry.lines
        )
        assert open_amount(session, invoice) == Decimal("1064.000000"), open_amount(
            session, invoice
        )
        assert noted_amount(session, invoice) == Decimal("56.000000")
        print(f"1. DN-7001 posted the invoice's entry reversed — payables debited"
              f" 56.000000, inventory credited 50.000000, input tax 6.000000 — balanced")
        print(f"2. the invoice now owes 1064.000000 (was 1120.000000), through a"
              " settlement row naming DN-7001")

        # 4 + 5 — the ceilings
        said = _refused(
            lambda: post_debit_note(
                session,
                create_debit_note(session, number="DN-7002", invoice=invoice,
                                  note_date=date(2026, 11, 21), kind="adjustment",
                                  lines=[{"description": "Too much", "quantity": "1",
                                          "unit_price": "2000"}]),
            ),
            OverNoteError,
        )
        session.rollback()
        over = create_debit_note(session, number="DN-7002", invoice=invoice,
                                 note_date=date(2026, 11, 21), kind="adjustment",
                                 lines=[{"description": "Too much", "quantity": "1",
                                         "unit_price": "2000"}])
        session.commit()
        post_debit_note(session, over, over_note_reason="supplier agreed a full credit")
        session.commit()
        assert over.over_note_reason == "supplier agreed a full credit"
        assert open_amount(session, invoice) == Decimal("0.000000")
        print(f"5. a note beyond the open amount is refused ({said[:52]}…) and accepted"
              f" with a recorded reason; the invoice is now fully settled")

        # 3 — a return: the money reversal **and** the stock
        big = create_invoice(
            session, company_id=COMPANY, number="AP-7003", supplier=supplier,
            supplier_reference="ACME-7003", invoice_date=INVOICE_DATE,
            lines=[{"description": "Widgets", "item_id": item.id, "quantity": "10",
                    "unit_price": "100", "tax_amount": "120.00"}],
        )
        session.commit()
        post_invoice(session, big)
        session.commit()
        before = on_hand(session, company_id=COMPANY, item_id=item.id,
                         location_id=bin_a.id)
        returned = create_debit_note(
            session, number="DN-7003", invoice=big, note_date=date(2026, 11, 25),
            kind="return", location=bin_a,
            lines=[{"description": "Widgets returned", "item_id": item.id,
                    "quantity": "4", "uom": "each", "unit_price": "100",
                    "tax_amount": "48.00"}],
        )
        session.commit()
        # a return from a location that does not hold the goods is refused
        not_there = create_debit_note(
            session, number="DN-7004", invoice=big, note_date=date(2026, 11, 26),
            kind="return", location=empty_bin,
            lines=[{"description": "Not there", "item_id": item.id, "quantity": "1",
                    "uom": "each", "unit_price": "100"}],
        )
        session.commit()
        held_elsewhere = _refused(lambda: post_debit_note(session, not_there),
                                  DebitNoteError)
        session.rollback()

        entry = post_debit_note(session, returned)
        session.commit()
        after = on_hand(session, company_id=COMPANY, item_id=item.id,
                        location_id=bin_a.id)
        assert after["quantity"] == before["quantity"] - Decimal("4.000000"), (before, after)
        assert after["value"] < before["value"], (before, after)
        assert returned.lines[0].movement_id is not None
        movements = session.scalars(
            select(StockLedgerEntry).where(StockLedgerEntry.source_id == returned.id)
        ).all()
        assert len(movements) == 1, movements
        assert movements[0].quantity == Decimal("-4.000000"), movements[0].quantity
        assert movements[0].source_type == "debit_note"
        # exactly one journal entry for the note: the stock movement brought none
        entries_for_note = session.scalars(
            select(JournalEntry).where(JournalEntry.source_id == returned.id)
        ).all()
        assert len(entries_for_note) == 1, entries_for_note
        assert len(entry.lines) == 3, entry.lines  # inventory + input tax + payables
        print(f"3. DN-7003 took 4 back out of {bin_a.code}"
              f" ({before['quantity']} → {after['quantity']}, value {before['value']} →"
              f" {after['value']}) with exactly **one** GL entry and one movement"
              f" ({held_elsewhere[:44]}… from the empty bin)")

        # 4 (second half) — a service line is an adjustment, not a return
        service = create_invoice(
            session, company_id=COMPANY, number="AP-7004", supplier=supplier,
            supplier_reference="ACME-7004", invoice_date=INVOICE_DATE,
            lines=[{"description": "Delivery", "quantity": "1", "unit_price": "10"}],
        )
        session.commit()
        post_invoice(session, service)
        session.commit()
        said = _refused(
            lambda: post_debit_note(
                session,
                create_debit_note(session, number="DN-7005", invoice=service,
                                  note_date=date(2026, 11, 26), kind="return",
                                  location=bin_a,
                                  lines=[{"description": "Delivery", "quantity": "1",
                                          "unit_price": "10"}]),
            ),
            DebitNoteError,
        )
        session.rollback()
        print(f"4. a line with no stock item cannot be returned: {said[:60]}…")

        # 6 (second half) + 8 — posted once, and the history is on the invoice
        said = _refused(lambda: post_debit_note(session, returned), DebitNoteStateError)
        session.rollback()
        said += " | " + _refused(
            lambda: create_debit_note(session, number="DN-7001", invoice=invoice,
                                      note_date=INVOICE_DATE, kind="adjustment",
                                      lines=[{"description": "x", "quantity": "1",
                                              "unit_price": "1"}]),
            DuplicateDebitNoteError,
        )
        session.rollback()
        on_big = debit_notes_for_invoice(session, big)
        assert [note.number for note in on_big] == ["DN-7003", "DN-7004"], on_big
        # the refused return stayed a draft and moved nothing
        assert on_big[1].status == "draft" and on_big[1].journal_entry_id is None
        assert debit_note_by_number(session, company_id=COMPANY,
                                    number="DN-7001").id == adjustment.id
        assert adjustment.status == POSTED and returned.invoice_id == big.id
        print(f"6b/8. a posted note is never posted twice and a repeated number is refused"
              f" ({said}); the notes stay attached to their invoices")

    print("check_debit_note: all assertions green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
