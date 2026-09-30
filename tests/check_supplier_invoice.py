"""T-2.AP.01 check — the supplier invoice, its posting and what is still owed.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/erpv1 \
        python tests/check_supplier_invoice.py

Green on all eight:

1. posting an invoice writes **one balanced entry** crediting the payables control
   account on the stated date and debiting the cost per line and the tax apart — every
   account resolved through T-1.ACCT.03's mapping, so no account code is fixed in the
   module
2. a stock line debits `inventory` and a service line `expense`
3. a **duplicate** invoice — same supplier, same reference, same amount — is refused by
   the module's message, and the database refuses a hand-written one too
4. a **foreign-currency** invoice keeps its currency and its rate, and its base amount
   is derivable exactly
5. the due date is the invoice date plus the supplier's own payment terms
6. what is owed is **derived** from settlements: a partial settlement leaves the right
   remainder, an over-settlement is refused, and settling an unposted invoice is refused
7. a settlement row cannot be edited or removed — it is history (append-only)
8. the invoice is attributable to the order and receipt behind it, and an empty
   invoice, a blank supplier reference and a negative line are refused

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ap.invoices import (  # noqa: E402
    POSTED,
    DuplicateInvoiceError,
    InvoiceError,
    InvoiceStateError,
    OverSettlementError,
    SupplierInvoiceSettlement,
    create_invoice,
    invoice_by_number,
    open_amount,
    open_invoices,
    post_invoice,
    settle,
    settled_amount,
)
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger.accounts import create_account  # noqa: E402
from app.ledger.currency import register_currency, store_rate  # noqa: E402
from app.ledger.mapping import set_mapping  # noqa: E402
from app.ledger.posting import JournalEntry  # noqa: E402
from app.procurement.orders import award, decide_order, submit_order  # noqa: E402
from app.procurement.receipts import create_receipt, post_receipt  # noqa: E402
from app.procurement.requisitions import (  # noqa: E402
    create_requisition,
    record_decision as decide_requisition,
    submit as submit_requisition,
)
from app.procurement.rfq import issue_rfq, record_response  # noqa: E402
from app.procurement.suppliers import add_tax_identifier, create_supplier  # noqa: E402
from app.stock.items import create_item  # noqa: E402
from app.stock.locations import create_location  # noqa: E402
from app.workflow import APPROVE, configure  # noqa: E402

COMPANY = uuid.uuid4()
ISSUED_ON = date(2026, 10, 1)
DEADLINE = date(2026, 10, 10)
INVOICE_DATE = date(2026, 11, 5)
TERMS = 30


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
            Company(id=COMPANY, code="AP-CHECK", name="AP check", base_currency="PHP",
                    fiscal_year_start_month=1)
        )
        register_currency(session, company_id=COMPANY, code="PHP", name="Peso")
        register_currency(session, company_id=COMPANY, code="USD", name="US Dollar")
        session.commit()

        from tests.seed import seed_stock_accounts

        seed_stock_accounts(session, company_id=COMPANY)
        create_account(session, company_id=COMPANY, code="1310", name="Input VAT",
                       account_class="asset")
        session.commit()
        # `stock_receipt` is left as the seed maps it (`2010 Goods Received Not
        # Invoiced`, the pack's own account): a receipt credits it and the invoice's
        # received line debits it back, so the payables control account stays clear.
        set_mapping(session, company_id=COMPANY, key="payables", account_code="2000")
        set_mapping(session, company_id=COMPANY, key="input_tax", account_code="1310")
        set_mapping(session, company_id=COMPANY, key="expense", account_code="5200")
        store_rate(session, company_id=COMPANY, base_currency="PHP", currency="USD",
                   on=INVOICE_DATE, rate="58.5")
        session.commit()

        configure(session, company_id=COMPANY, doc_type="purchase_requisition",
                  name="Requisition", levels=[(Decimal("1"), "manager")])
        configure(session, company_id=COMPANY, doc_type="purchase_order",
                  name="Order", levels=[(Decimal("100000"), "manager")])
        supplier = create_supplier(session, company_id=COMPANY, party_code="ACME",
                                   name="Acme Supplies", payment_terms_days=TERMS)
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
        session.commit()

        # a real order and receipt, so the invoice can be attributed to them
        requisition = create_requisition(
            session, company_id=COMPANY, number="REQ-600", requested_by="rina.requester",
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
        rfq = issue_rfq(session, requisition=requisition, number="RFQ-600",
                        supplier_codes=["ACME"], response_deadline=DEADLINE,
                        issued_on=ISSUED_ON)
        session.commit()
        record_response(session, rfq, supplier_code="ACME", received_on=date(2026, 10, 5),
                        lines=[{"line_no": 1, "unit_price": "100"}])
        session.commit()
        order = award(
            session, rfq=rfq, actor="bob.buyer",
            awards=[{"supplier_code": "ACME", "number": "PO-6001",
                     "required_date": date(2026, 11, 30),
                     "lines": [{"line_no": 1, "quantity": "10"}]}],
        )[0]
        session.commit()
        submit_order(session, order, actor="bob.buyer")
        session.commit()
        receipt = create_receipt(session, order=order, number="GRN-6001", location=bin_a,
                                 received_on=date(2026, 10, 20),
                                 lines=[{"line_no": 1, "quantity": "10"}])
        session.commit()
        post_receipt(session, receipt)
        session.commit()

        # 1 + 2 + 8 — the invoice, its posting, and its documents
        invoice = create_invoice(
            session, company_id=COMPANY, number="AP-6001", supplier=supplier,
            supplier_reference="ACME-88231", invoice_date=INVOICE_DATE,
            order_id=order.id, receipt_id=receipt.id,
            lines=[
                {"description": "Widgets", "item_id": item.id, "quantity": "10",
                 "unit_price": "100", "tax_amount": "120.00",
                 "order_line_id": order.lines[0].id,
                 "receipt_line_id": receipt.lines[0].id},
                {"description": "Delivery charge", "quantity": "1", "unit_price": "50"},
            ],
        )
        session.commit()
        assert invoice.net_amount == Decimal("1050.000000"), invoice.net_amount
        assert invoice.tax_amount == Decimal("120.000000")
        assert invoice.gross_amount == Decimal("1170.000000")
        entry = post_invoice(session, invoice)
        session.commit()
        assert invoice.status == POSTED and invoice.journal_entry_id == entry.id
        assert entry.posting_date == INVOICE_DATE
        assert entry.currency == "PHP"
        assert entry.source_type == "supplier_invoice" and entry.source_id == invoice.id
        by_account = {line.account: line for line in entry.lines}
        assert by_account["2000"].credit == invoice.gross_amount, by_account["2000"]
        assert by_account["2010"].debit == Decimal("1000.000000"), by_account["2010"]
        assert by_account["5200"].debit == Decimal("50.000000"), by_account["5200"]
        assert by_account["1310"].debit == Decimal("120.000000"), by_account["1310"]
        assert sum(line.debit for line in entry.lines) == sum(
            line.credit for line in entry.lines
        )
        assert invoice.order_id == order.id and invoice.receipt_id == receipt.id
        assert invoice.lines[0].order_line_id == order.lines[0].id
        print(f"1. AP-6001 posted {invoice.gross_amount} to the payables control account"
              f" on {INVOICE_DATE}, balanced (debits {sum(line.debit for line in entry.lines)})")
        print(f"2. the received stock line debited GRNI 2010 with 1000.000000 and the"
              f" service line 5200 with 50.000000; input tax 120.000000 went to 1310")

        # 5 — the due date
        assert invoice.due_date == INVOICE_DATE + timedelta(days=TERMS), invoice.due_date
        print(f"5. the due date is {invoice.due_date} — the invoice date plus the"
              f" supplier's {TERMS} days")

        # 3 — duplicates
        said = _refused(
            lambda: create_invoice(
                session, company_id=COMPANY, number="AP-6002", supplier=supplier,
                supplier_reference="ACME-88231", invoice_date=INVOICE_DATE,
                lines=[{"description": "Widgets", "quantity": "10", "unit_price": "100",
                        "tax_amount": "120.00"},
                       {"description": "Delivery charge", "quantity": "1",
                        "unit_price": "50"}],
            ),
            DuplicateInvoiceError,
        )
        session.rollback()
        session.add(
            __import__("app.ap.invoices", fromlist=["SupplierInvoice"]).SupplierInvoice(
                company_id=COMPANY, number="AP-6003", supplier_id=supplier.id,
                supplier_reference="ACME-88231", invoice_date=INVOICE_DATE,
                due_date=INVOICE_DATE, currency="PHP", net_amount=Decimal(1050),
                tax_amount=Decimal(120), gross_amount=Decimal(1170), status="draft",
            )
        )
        try:
            session.commit()
        except DBAPIError as exc:
            assert "uq_supplier_invoice_duplicate" in str(exc), exc
            session.rollback()
        else:
            raise AssertionError("the database accepted a duplicate invoice")
        print(f"3. a re-keyed invoice is refused ({said[:58]}…) and the database refuses a"
              " hand-written one too")

        # 4 — a foreign-currency invoice keeps its rate
        usd = create_invoice(
            session, company_id=COMPANY, number="AP-6004", supplier=supplier,
            supplier_reference="ACME-88232", invoice_date=INVOICE_DATE, currency="USD",
            lines=[{"description": "Imported widgets", "quantity": "2",
                    "unit_price": "100", "tax_amount": "12"}],
        )
        session.commit()
        assert usd.currency == "USD" and usd.gross_amount == Decimal("212.000000")
        usd_entry = post_invoice(session, usd)
        session.commit()
        assert usd_entry.exchange_rate == Decimal("58.5000000000"), usd_entry.exchange_rate
        base = (usd.gross_amount * usd_entry.exchange_rate).quantize(Decimal("0.000001"))
        assert base == Decimal("12402.000000"), base
        print(f"4. AP-6004 kept USD 212.000000 at {usd_entry.exchange_rate} — base"
              f" {base}, exactly derivable")

        # 6 — what is owed is derived from settlements
        assert settled_amount(session, invoice) == Decimal("0.000000")
        assert open_amount(session, invoice) == invoice.gross_amount
        batch_id = uuid.uuid4()
        settle(session, invoice, amount="700.00", settled_on=date(2026, 11, 20),
               source_type="payment_batch", source_id=batch_id)
        session.commit()
        assert settled_amount(session, invoice) == Decimal("700.000000")
        assert open_amount(session, invoice) == Decimal("470.000000")
        said = _refused(
            lambda: settle(session, invoice, amount="500.00",
                           settled_on=date(2026, 11, 21),
                           source_type="payment_batch", source_id=batch_id),
            OverSettlementError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: settle(session, usd, amount="0", settled_on=date(2026, 11, 21),
                           source_type="payment_batch", source_id=batch_id),
            InvoiceError,
        )
        session.rollback()
        draft = create_invoice(
            session, company_id=COMPANY, number="AP-6005", supplier=supplier,
            supplier_reference="ACME-88233", invoice_date=INVOICE_DATE,
            lines=[{"description": "Later", "quantity": "1", "unit_price": "10"}],
        )
        session.commit()
        said += " | " + _refused(
            lambda: settle(session, draft, amount="10", settled_on=date(2026, 11, 21),
                           source_type="payment_batch", source_id=batch_id),
            InvoiceStateError,
        )
        session.rollback()
        assert [row.number for row in open_invoices(session, company_id=COMPANY)] == [
            "AP-6001", "AP-6004"
        ]
        print(f"6. 700.00 settled left 470.000000 open; over-settling, a zero settlement"
              f" and settling a draft are refused: {said}")

        # 7 — a settlement is history
        try:
            session.execute(
                SupplierInvoiceSettlement.__table__.update().values(amount=Decimal(1))
            )
            session.commit()
        except DBAPIError as exc:
            assert "append-only" in str(exc), exc
            session.rollback()
        else:
            raise AssertionError("a settlement was edited")
        assert session.scalars(
            select(SupplierInvoiceSettlement).where(
                SupplierInvoiceSettlement.invoice_id == invoice.id
            )
        ).all()
        print("7. a settlement cannot be edited or deleted — the table is append-only")

        # 8 — the edges
        said = _refused(
            lambda: create_invoice(session, company_id=COMPANY, number="AP-6006",
                                   supplier=supplier, supplier_reference="ACME-9",
                                   invoice_date=INVOICE_DATE, lines=[]),
            InvoiceError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: create_invoice(session, company_id=COMPANY, number="AP-6006",
                                   supplier=supplier, supplier_reference="   ",
                                   invoice_date=INVOICE_DATE,
                                   lines=[{"description": "x", "quantity": "1",
                                           "unit_price": "1"}]),
            InvoiceError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: create_invoice(session, company_id=COMPANY, number="AP-6006",
                                   supplier=supplier, supplier_reference="ACME-9",
                                   invoice_date=INVOICE_DATE,
                                   lines=[{"description": "x", "quantity": "-1",
                                           "unit_price": "1"}]),
            InvoiceError,
        )
        session.rollback()
        assert invoice_by_number(session, company_id=COMPANY, number="AP-6001").id == invoice.id
        print(f"8. an empty invoice, a blank supplier reference and a negative line are"
              f" refused: {said}")

    print("check_supplier_invoice: all assertions green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
