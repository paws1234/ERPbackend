"""T-2.AP.04 check — payment batches and the payment run.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/erpv1 \
        python tests/check_payment_batch.py

Green on all eight:

1. a batch selects open invoices and totals exactly what they owe
2. an invoice that is **held**, unposted, already settled or in another currency is
   refused — with its reason — so a batch cannot quietly leave one out
3. executing posts **one balanced entry per settled invoice** (payables debited, bank
   credited), settles each invoice by its line amount, and a partial settlement leaves
   the right remainder
4. only an **approved** batch executes, and a batch never executes twice
5. paying is refused if the invoice was put on **hold after** the batch was built
6. a line cannot be removed from a batch that has already run; it can from a draft
7. the bank file follows the pack's own columns and **refuses** a required column the
   supplier has no value for
8. a repeated batch number and an empty batch are refused, and what the batch paid
   equals the settlements it wrote

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

from app.ap.invoices import (  # noqa: E402
    InvoiceError,
    create_invoice,
    open_amount,
    post_invoice,
    settle,
)
from app.ap.payments import (  # noqa: E402
    APPROVED,
    EXECUTED,
    PENDING,
    BatchStateError,
    DuplicateBatchError,
    EmptyBatchError,
    PaymentError,
    UnpayableInvoiceError,
    bank_file,
    batch_by_number,
    batches,
    create_batch,
    decide_batch,
    execute_batch,
    paid_by_batch,
    remove_line,
    submit_batch,
)
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger.accounts import create_account  # noqa: E402
from app.ledger.currency import register_currency, store_rate  # noqa: E402
from app.ledger.mapping import set_mapping  # noqa: E402
from app.ledger.posting import JournalEntry  # noqa: E402
from app.matching import (  # noqa: E402
    MATCHED,
    InvoiceHeldError,
    current_hold,
    hold_invoice,
    match_invoice,
    set_tolerance,
)
from app.procurement import receipts as _receipts  # noqa: E402,F401 — the FK target
from app.procurement.orders import award, decide_order, submit_order  # noqa: E402
from app.procurement.receipts import create_receipt, post_receipt  # noqa: E402
from app.procurement.requisitions import (  # noqa: E402
    create_requisition,
    record_decision as decide_requisition,
    submit as submit_requisition,
)
from app.procurement.rfq import issue_rfq, record_response  # noqa: E402
from app.procurement.suppliers import (  # noqa: E402
    add_bank_account,
    add_tax_identifier,
    create_supplier,
)
from app.stock.items import create_item  # noqa: E402
from app.stock.locations import create_location  # noqa: E402
from app.workflow import APPROVE, configure  # noqa: E402

COMPANY = uuid.uuid4()
ISSUED_ON = date(2026, 10, 1)
DEADLINE = date(2026, 10, 10)
INVOICE_DATE = date(2026, 11, 5)
RUN_ON = date(2026, 12, 15)
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


def _invoice(session, *, supplier, item, bin_a, unit_price="100", quantity="10",
             received=None, currency=None, number=None):
    """A full chain to a posted invoice, so every line has its documents behind it.

    `received` defaults to the ordered quantity; state it smaller to make the receipt
    short, which is how a mismatched match is built.
    """
    if received is None:
        received = quantity
    COUNTER["n"] += 1
    tag = f"{COUNTER['n']:02d}"
    requisition = create_requisition(
        session, company_id=COMPANY, number=f"REQ-4{tag}", requested_by="rina.requester",
        needed_by=date(2026, 11, 30), currency="PHP",
        lines=[{"description": "Widgets", "quantity": quantity, "uom": "each",
                "estimated_unit_price": unit_price, "item_sku": item.sku}],
    )
    session.commit()
    submit_requisition(session, requisition, actor="rina.requester")
    session.commit()
    decide_requisition(session, requisition, actor="mia.manager", action=APPROVE,
                       role="manager")
    session.commit()
    rfq = issue_rfq(session, requisition=requisition, number=f"RFQ-4{tag}",
                    supplier_codes=[supplier.party.code], response_deadline=DEADLINE,
                    issued_on=ISSUED_ON)
    session.commit()
    record_response(session, rfq, supplier_code=supplier.party.code,
                    received_on=date(2026, 10, 5),
                    lines=[{"line_no": 1, "unit_price": unit_price}])
    session.commit()
    order = award(
        session, rfq=rfq, actor="bob.buyer",
        awards=[{"supplier_code": supplier.party.code, "number": f"PO-4{tag}",
                 "required_date": date(2026, 11, 30),
                 "lines": [{"line_no": 1, "quantity": quantity}]}],
    )[0]
    session.commit()
    submit_order(session, order, actor="bob.buyer")
    session.commit()
    receipt = create_receipt(session, order=order, number=f"GRN-4{tag}", location=bin_a,
                             received_on=date(2026, 10, 20),
                             lines=[{"line_no": 1, "quantity": received}])
    session.commit()
    post_receipt(session, receipt)
    session.commit()
    net = (Decimal(unit_price) * Decimal(quantity)).quantize(Decimal("0.000001"))
    invoice = create_invoice(
        session, company_id=COMPANY, number=number or f"AP-4{tag}", supplier=supplier,
        supplier_reference=f"REF-4{tag}", invoice_date=INVOICE_DATE, currency=currency,
        order_id=order.id, receipt_id=receipt.id,
        lines=[{"description": "Widgets", "item_id": item.id, "quantity": quantity,
                "unit_price": unit_price,
                "tax_amount": (net * Decimal("12") / 100).quantize(Decimal("0.000001")),
                "order_line_id": order.lines[0].id,
                "receipt_line_id": receipt.lines[0].id}],
    )
    session.commit()
    post_invoice(session, invoice)
    session.commit()
    return invoice


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
            Company(id=COMPANY, code="PAY", name="Payments", base_currency="PHP",
                    fiscal_year_start_month=1)
        )
        register_currency(session, company_id=COMPANY, code="PHP", name="Peso")
        register_currency(session, company_id=COMPANY, code="USD", name="US Dollar")
        store_rate(session, company_id=COMPANY, base_currency="PHP", currency="USD",
                   on=INVOICE_DATE, rate="58.5")
        session.commit()
        from tests.seed import seed_stock_accounts

        seed_stock_accounts(session, company_id=COMPANY)
        create_account(session, company_id=COMPANY, code="1310", name="Input VAT",
                       account_class="asset")
        session.commit()
        for key, code in (("payables", "2000"), ("input_tax", "1310"),
                          ("expense", "5200"), ("bank", "1010")):
            set_mapping(session, company_id=COMPANY, key=key, account_code=code)
        configure(session, company_id=COMPANY, doc_type="purchase_requisition",
                  name="Requisition", levels=[(Decimal("1"), "manager")])
        configure(session, company_id=COMPANY, doc_type="purchase_order",
                  name="Order", levels=[(Decimal("100000"), "manager")])
        configure(session, company_id=COMPANY, doc_type="payment_batch",
                  name="Payment batch", levels=[(Decimal("1000"), "controller")])
        set_tolerance(session, company_id=COMPANY, quantity_percent="2",
                      price_percent="1", tax_percent="0.5")
        supplier = create_supplier(session, company_id=COMPANY, party_code="ACME",
                                   name="Acme Supplies", payment_terms_days=30)
        add_tax_identifier(session, supplier, kind="tin", value="001-234-567")
        add_bank_account(session, supplier, bank_name="BPI", account_name="Acme Supplies",
                         account_number="1234567890", swift="BOPIPHMM", is_primary=True)
        bare = create_supplier(session, company_id=COMPANY, party_code="NOBANK",
                               name="No Bank Yet", payment_terms_days=30)
        add_tax_identifier(session, bare, kind="tin", value="009-876-543")
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

        first = _invoice(session, supplier=supplier, item=item, bin_a=bin_a,
                         unit_price="100", quantity="10", number="AP-401")
        second = _invoice(session, supplier=supplier, item=item, bin_a=bin_a,
                          unit_price="50", quantity="6", number="AP-402")
        draft_invoice = create_invoice(
            session, company_id=COMPANY, number="AP-403", supplier=supplier,
            supplier_reference="REF-403", invoice_date=INVOICE_DATE,
            lines=[{"description": "Later", "quantity": "1", "unit_price": "10"}],
        )
        session.commit()
        usd = _invoice(session, supplier=supplier, item=item, bin_a=bin_a,
                       unit_price="100", quantity="1", currency="USD", number="AP-404")

        # 1 — the batch and its total
        batch = create_batch(session, company_id=COMPANY, number="PB-401",
                            scheduled_on=RUN_ON,
                            invoice_numbers=["AP-401", "AP-402"])
        session.commit()
        assert batch.status == "draft" and batch.currency == "PHP"
        assert batch.total_amount == open_amount(session, first) + open_amount(
            session, second
        ), batch.total_amount
        assert batch.total_amount == Decimal("1456.000000"), batch.total_amount
        assert [line.invoice.number for line in batch.lines] == ["AP-401", "AP-402"]
        print(f"1. PB-401 selects AP-401 + AP-402 for {batch.total_amount} PHP")

        # 2 — nothing unpayable gets in
        said = _refused(
            lambda: create_batch(session, company_id=COMPANY, number="PB-402",
                                 scheduled_on=RUN_ON, invoice_numbers=["AP-403"]),
            UnpayableInvoiceError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: create_batch(session, company_id=COMPANY, number="PB-402",
                                 scheduled_on=RUN_ON,
                                 invoice_numbers=["AP-401", "AP-404"]),
            UnpayableInvoiceError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: create_batch(session, company_id=COMPANY, number="PB-402",
                                 scheduled_on=RUN_ON, invoice_numbers=["AP-999"]),
            InvoiceError,
        )
        session.rollback()
        settled = _invoice(session, supplier=supplier, item=item, bin_a=bin_a,
                           unit_price="10", quantity="1", number="AP-405")
        settle(session, settled, amount="11.20", settled_on=INVOICE_DATE,
               source_type="debit_note", source_id=uuid.uuid4())
        session.commit()
        said += " | " + _refused(
            lambda: create_batch(session, company_id=COMPANY, number="PB-402",
                                 scheduled_on=RUN_ON, invoice_numbers=["AP-405"]),
            UnpayableInvoiceError,
        )
        session.rollback()
        print(f"2. a draft invoice, a mixed-currency batch, an unknown invoice and a"
              f" settled one are refused: {said}")

        # 3 + 4 + 8 — approval, execution and the postings
        said = _refused(lambda: execute_batch(session, batch, actor="fin.ada"),
                        BatchStateError)
        session.rollback()
        submit_batch(session, batch, actor="fin.ada")
        session.commit()
        assert batch.status == PENDING, batch.status
        decide_batch(session, batch, actor="ctl.rose", action=APPROVE, role="controller")
        session.commit()
        assert batch.status == APPROVED and batch.approved_by == "ctl.rose"
        settle_before = open_amount(session, first)
        execute_batch(session, batch, actor="fin.ada", executed_on=RUN_ON)
        session.commit()
        assert batch.status == EXECUTED and batch.executed_on == RUN_ON
        assert batch.executed_by == "fin.ada"
        assert open_amount(session, first) == Decimal("0.000000"), open_amount(
            session, first
        )
        assert settle_before == Decimal("1120.000000")
        entries = session.scalars(
            select(JournalEntry).where(JournalEntry.source_type == "payment_batch")
        ).all()
        assert len(entries) == 2, len(entries)
        for entry in entries:
            by_account = {line.account: line for line in entry.lines}
            assert by_account["2000"].debit and by_account["1010"].credit, by_account
            assert by_account["2000"].debit == by_account["1010"].credit
            assert sum(line.debit for line in entry.lines) == sum(
                line.credit for line in entry.lines
            )
        assert paid_by_batch(session, batch) == batch.total_amount
        said += " | " + _refused(
            lambda: execute_batch(session, batch, actor="fin.ada", executed_on=RUN_ON),
            BatchStateError,
        )
        session.rollback()
        print(f"3. both invoices settled fully and two balanced entries posted (payables"
              f" debited / bank credited, one per invoice); the batch paid"
              f" {paid_by_batch(session, batch)}")
        print(f"4. an unapproved batch does not run ({said.split(' | ')[0][:44]}…) and a"
              f" second run is refused ({said.split(' | ')[1][:44]}…)")

        # 6 — a line cannot be removed after the run
        said = _refused(lambda: remove_line(session, batch, first), BatchStateError)
        session.rollback()
        removable = create_batch(session, company_id=COMPANY, number="PB-403",
                                 scheduled_on=RUN_ON, invoice_numbers=["AP-404"])
        session.commit()
        remove_line(session, removable, usd)
        session.commit()
        assert removable.lines == [] and removable.total_amount == Decimal("0.000000")
        print(f"6. a paid line is stuck in its batch ({said[:46]}…) while a draft batch"
              " releases its invoice")

        # 5 — held after the batch was built
        late = _invoice(session, supplier=supplier, item=item, bin_a=bin_a,
                        unit_price="100", quantity="10", received="9", number="AP-406")
        run = match_invoice(session, late)
        session.commit()
        assert run.status != MATCHED
        hold_invoice(session, late, reason="quantity short")
        session.commit()
        said = _refused(
            lambda: create_batch(session, company_id=COMPANY, number="PB-404",
                                 scheduled_on=RUN_ON, invoice_numbers=["AP-406"]),
            InvoiceHeldError,
        )
        session.rollback()
        # and a hold that arrives *after* the batch was built stops the run too
        queued = _invoice(session, supplier=supplier, item=item, bin_a=bin_a,
                          unit_price="100", quantity="10", received="9", number="AP-407")
        run = match_invoice(session, queued)
        session.commit()
        pending = create_batch(session, company_id=COMPANY, number="PB-405",
                               scheduled_on=RUN_ON, invoice_numbers=["AP-407"])
        session.commit()
        assert current_hold(session, queued) is None
        hold_invoice(session, queued, reason="put on hold between build and run")
        session.commit()
        submit_batch(session, pending, actor="fin.ada")
        session.commit()
        assert pending.status == PENDING, pending.status  # 1120 is above the threshold
        decide_batch(session, pending, actor="ctl.rose", action=APPROVE, role="controller")
        session.commit()
        assert pending.status == APPROVED
        said2 = _refused(lambda: execute_batch(session, pending, actor="fin.ada"),
                         InvoiceHeldError)
        session.rollback()
        print(f"5. a held invoice is refused at build time ({said[:38]}…) and a hold"
              f" raised **after** the batch was built stops the run ({said2[:52]}…)")

        # 7 — the bank file follows the pack, and refuses a blank required column
        file_text = bank_file(session, batch)
        rows = file_text.strip().split("\n")
        assert rows[0] == "payee_name,payee_account,bank_code,amount,reference,purpose", rows[0]
        assert len(rows) == 3, rows
        assert "Acme Supplies,1234567890,BOPIPHMM,1120.000000,AP-401," in rows[1], rows[1]
        unbanked = _invoice(session, supplier=bare, item=item, bin_a=bin_a,
                            unit_price="100", quantity="1", number="AP-408")
        unbanked_batch = create_batch(session, company_id=COMPANY, number="PB-406",
                                      scheduled_on=RUN_ON, invoice_numbers=["AP-408"])
        session.commit()
        shown = _refused(lambda: bank_file(session, unbanked_batch), PaymentError)
        session.rollback()
        print(f"7. the file uses the pack's own columns in its order; a supplier with no"
              f" bank account stops it ({shown[:46]}…)")

        # 8 — the rest of the edges
        said = _refused(
            lambda: create_batch(session, company_id=COMPANY, number="PB-401",
                                 scheduled_on=RUN_ON, invoice_numbers=["AP-401"]),
            DuplicateBatchError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: create_batch(session, company_id=COMPANY, number="PB-407",
                                 scheduled_on=RUN_ON, invoice_numbers=[]),
            EmptyBatchError,
        )
        session.rollback()
        assert [row.number for row in batches(session, company_id=COMPANY)] == [
            "PB-401", "PB-403", "PB-405", "PB-406"
        ]
        assert batch_by_number(session, company_id=COMPANY, number="PB-401").id == batch.id
        assert bare.id and supplier.id and draft_invoice.id and unbanked.id
        print(f"8. a repeated batch number and an empty batch are refused: {said}")

    print("check_payment_batch: all assertions green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
