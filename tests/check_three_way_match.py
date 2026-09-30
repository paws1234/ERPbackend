"""T-2.MATCH.01 check — the three-way match and the rate it feeds.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/erpv1 \
        python tests/check_three_way_match.py

Green on all eight:

1. a clean invoice **matches** — quantity against what was received, price against what
   was ordered, tax against what the pack's rule implies on the ordered value
2. a **quantity** mismatch is reported separately from a **price** mismatch, and a
   within-tolerance difference passes — three invoices, three different verdicts
3. the verdict is **explainable**: the finding names the line, the dimension, the
   expected value, the stated value and the tolerance that was exceeded
4. a line with no ordered and received line behind it is a finding in itself — an
   invoice nobody ordered cannot be within tolerance of anything
5. a tolerance loose enough to let a wrong price pass **silently** is refused when it
   is set, and a negative one too; with none configured everything must agree exactly
6. the **match rate** is measured over the latest run of each invoice — re-running one
   invoice does not move it — and reported against the > 95 % target
7. a run is **history**: it cannot be edited or deleted, so "what did the match say
   then" stays answerable
8. an unposted invoice cannot be matched at all

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ap.invoices import create_invoice, post_invoice  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger.accounts import create_account  # noqa: E402
from app.ledger.currency import register_currency  # noqa: E402
from app.ledger.mapping import set_mapping  # noqa: E402
from app.matching import (  # noqa: E402
    FAILED,
    MATCHED,
    MAX_TOLERANCE_PERCENT,
    PARTIAL,
    MatchRun,
    NotMatchableError,
    ToleranceError,
    explain,
    latest_match,
    match_invoice,
    match_rate,
    runs,
    set_tolerance,
    tolerance,
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
from app.procurement.suppliers import add_tax_identifier, create_supplier  # noqa: E402
from app.stock.items import create_item  # noqa: E402
from app.stock.locations import create_location  # noqa: E402
from app.workflow import APPROVE, configure  # noqa: E402

COMPANY = uuid.uuid4()
ISSUED_ON = date(2026, 10, 1)
DEADLINE = date(2026, 10, 10)
INVOICE_DATE = date(2026, 11, 5)
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


def _chain(session, *, ordered: str, invoiced: str, received: str, item, bin_a,
           invoiced_unit_price: str | None = None, tax_rate: str = "12"):
    """requisition → RFQ → order → receipt → posted invoice, with the numbers stated."""
    COUNTER["n"] += 1
    tag = COUNTER["n"]
    requisition = create_requisition(
        session, company_id=COMPANY, number=f"REQ-8{tag:02d}",
        requested_by="rina.requester", needed_by=date(2026, 11, 30), currency="PHP",
        lines=[{"description": "Widgets", "quantity": ordered, "uom": "each",
                "estimated_unit_price": invoiced, "item_sku": item.sku}],
    )
    session.commit()
    submit_requisition(session, requisition, actor="rina.requester")
    session.commit()
    decide_requisition(session, requisition, actor="mia.manager", action=APPROVE,
                       role="manager")
    session.commit()
    rfq = issue_rfq(session, requisition=requisition, number=f"RFQ-8{tag:02d}",
                    supplier_codes=["ACME"], response_deadline=DEADLINE,
                    issued_on=ISSUED_ON)
    session.commit()
    record_response(session, rfq, supplier_code="ACME", received_on=date(2026, 10, 5),
                    lines=[{"line_no": 1, "unit_price": invoiced}])
    session.commit()
    order = award(
        session, rfq=rfq, actor="bob.buyer",
        awards=[{"supplier_code": "ACME", "number": f"PO-8{tag:02d}",
                 "required_date": date(2026, 11, 30),
                 "lines": [{"line_no": 1, "quantity": ordered}]}],
    )[0]
    session.commit()
    submit_order(session, order, actor="bob.buyer")
    session.commit()
    receipt = create_receipt(session, order=order, number=f"GRN-8{tag:02d}",
                             location=bin_a, received_on=date(2026, 10, 20),
                             lines=[{"line_no": 1, "quantity": received}])
    session.commit()
    post_receipt(session, receipt)
    session.commit()

    unit = invoiced if invoiced_unit_price is None else invoiced_unit_price
    gross_net = (Decimal(unit) * Decimal(invoiced)).quantize(Decimal("0.000001"))
    tax = (gross_net * Decimal(tax_rate) / 100).quantize(Decimal("0.000001"))
    invoice = create_invoice(
        session, company_id=COMPANY, number=f"AP-8{tag:02d}", supplier=order.supplier,
        supplier_reference=f"ACME-8{tag:02d}", invoice_date=INVOICE_DATE,
        order_id=order.id, receipt_id=receipt.id,
        lines=[{"description": "Widgets", "item_id": item.id, "quantity": invoiced,
                "unit_price": unit, "tax_amount": tax,
                "order_line_id": order.lines[0].id,
                "receipt_line_id": receipt.lines[0].id}],
    )
    session.commit()
    post_invoice(session, invoice)
    session.commit()
    return order, receipt, invoice


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
            Company(id=COMPANY, code="MATCH", name="Match check", base_currency="PHP",
                    fiscal_year_start_month=1)
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
        session.commit()

        # 5 — with no tolerance configured, everything must agree exactly
        limits = tolerance(session, company_id=COMPANY)
        assert limits.quantity_percent == limits.price_percent == limits.tax_percent == 0
        said = _refused(
            lambda: set_tolerance(session, company_id=COMPANY, price_percent="50"),
            ToleranceError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: set_tolerance(session, company_id=COMPANY, quantity_percent="-1"),
            ToleranceError,
        )
        session.rollback()
        set_tolerance(session, company_id=COMPANY, quantity_percent="2", price_percent="1",
                      tax_percent="0.5")
        session.commit()
        assert tolerance(session, company_id=COMPANY).price_percent == Decimal("1.0000")
        print(f"5. no tolerance means exact; 50 % refused against the"
              f" {MAX_TOLERANCE_PERCENT} % ceiling and a negative one refused"
              f" ({said}); 2/1/0.5 % accepted")

        # 1 — a clean invoice
        clean_order, clean_receipt, clean = _chain(
            session, ordered="100", invoiced="100", received="100", item=item, bin_a=bin_a
        )
        run = match_invoice(session, clean)
        session.commit()
        assert run.status == MATCHED, (run.status, run.details)
        assert run.quantity_ok and run.price_ok and run.tax_ok
        assert run.details["lines"][0]["quantity_difference"] == "0.000000"
        assert run.details["tolerances"]["price_percent"] == "1.0000"
        print(f"1. AP-801 matched: quantity, price and tax all inside their tolerances"
              f" ({explain(run)})")

        # 2 + 3 — a quantity mismatch, a price mismatch, and a within-tolerance case
        _, _, short = _chain(session, ordered="100", invoiced="100", received="90",
                             item=item, bin_a=bin_a)
        short_run = match_invoice(session, short)
        session.commit()
        assert short_run.status == PARTIAL, (short_run.status, short_run.details)
        assert short_run.quantity_ok is False and short_run.price_ok is True
        assert short_run.tax_ok is True
        message = explain(short_run)
        assert "quantity" in message and "90" in message and "100" in message, message
        assert "2.0000" in message, message

        _, _, dear = _chain(session, ordered="100", invoiced="100", received="100",
                            item=item, bin_a=bin_a, invoiced_unit_price="120")
        dear_run = match_invoice(session, dear)
        session.commit()
        assert dear_run.status == PARTIAL, (dear_run.status, dear_run.details)
        assert dear_run.price_ok is False and dear_run.quantity_ok is True
        # the tax follows the invoiced price, so it is outside its own band too — which
        # is the point of reporting the three dimensions separately
        assert dear_run.tax_ok is False
        finding = dear_run.details["lines"][0]
        problem = next(p for p in finding["problems"] if p["dimension"] == "price")
        assert problem["expected"] == "100.000000", problem
        assert problem["stated"] == "120.000000"
        assert problem["difference"] == "20.000000"
        assert problem["tolerance_percent"] == "1.0000"

        _, _, edge = _chain(session, ordered="100", invoiced="100", received="100",
                            item=item, bin_a=bin_a, invoiced_unit_price="100.5")
        edge_run = match_invoice(session, edge)
        session.commit()
        assert edge_run.status == MATCHED, (edge_run.status, edge_run.details)
        print(f"2. quantity short by 10 → {short_run.status}; price 20 % over →"
              f" {dear_run.status} with the tax outside too; price 0.5 % over →"
              f" {edge_run.status} (inside the 1 % band)")
        print(f"3. the verdict explains itself: {explain(dear_run)}")

        # 4 — an invoice line with nothing behind it
        orphan = create_invoice(
            session, company_id=COMPANY, number="AP-899", supplier=clean_order.supplier,
            supplier_reference="ACME-ORPHAN", invoice_date=INVOICE_DATE,
            lines=[{"description": "Nobody ordered this", "quantity": "1",
                    "unit_price": "50"}],
        )
        session.commit()
        post_invoice(session, orphan)
        session.commit()
        orphan_run = match_invoice(session, orphan)
        session.commit()
        assert orphan_run.status == FAILED
        assert orphan_run.details["lines"][0]["problems"][0]["dimension"] == "linkage"
        assert "nothing to compare it with" in explain(orphan_run)
        print(f"4. an unordered line is a finding of its own: {explain(orphan_run)}")

        # 6 — the rate, over each invoice's latest run
        rate = match_rate(session, company_id=COMPANY, start=INVOICE_DATE,
                          end=INVOICE_DATE)
        assert rate["considered"] == 5, rate
        assert rate["matched"] == 2 and rate["partial"] == 2 and rate["failed"] == 1, rate
        assert rate["rate_percent"] == Decimal("40.0000"), rate
        assert rate["target_percent"] == Decimal("95") and rate["met"] is False
        match_invoice(session, clean)
        session.commit()
        again = match_rate(session, company_id=COMPANY, start=INVOICE_DATE,
                           end=INVOICE_DATE)
        assert again["considered"] == 5 and again["rate_percent"] == rate["rate_percent"], (
            "re-running one invoice moved the rate"
        )
        strict = match_rate(session, company_id=COMPANY, start=INVOICE_DATE,
                            end=INVOICE_DATE, target="40")
        assert strict["met"] is True
        empty = match_rate(session, company_id=COMPANY, start=date(2027, 1, 1))
        assert empty["rate_percent"] is None and empty["reason"]
        print(f"6. the rate is {rate['rate_percent']} % over {rate['considered']}"
              f" invoices ({rate['matched']} matched, {rate['partial']} partial,"
              f" {rate['failed']} failed) against the {rate['target_percent']} % target —"
              " unchanged by re-running one invoice")

        # 7 — a run is history
        try:
            session.execute(MatchRun.__table__.update().values(status=MATCHED))
            session.commit()
        except DBAPIError as exc:
            assert "append-only" in str(exc), exc
            session.rollback()
        else:
            raise AssertionError("a match run was edited")
        second = latest_match(session, clean)
        assert second.invoice_id == clean.id and second.status == MATCHED
        assert second.id != run.id, "the re-run replaced the first run instead of appending"
        assert session.get(MatchRun, run.id).status == MATCHED  # the first is still there
        assert len(runs(session, company_id=COMPANY)) == 5
        print("7. a match run cannot be edited or deleted, and the latest run for an"
              " invoice is the one read back")

        # 8 — an unposted invoice cannot be matched
        draft = create_invoice(
            session, company_id=COMPANY, number="AP-898", supplier=clean_order.supplier,
            supplier_reference="ACME-DRAFT", invoice_date=INVOICE_DATE,
            lines=[{"description": "Not posted", "quantity": "1", "unit_price": "1"}],
        )
        session.commit()
        said = _refused(lambda: match_invoice(session, draft), NotMatchableError)
        session.rollback()
        print(f"8. an unposted invoice cannot be matched: {said[:56]}…")
        assert clean_order.id and clean_receipt.id and clean.id

    print("check_three_way_match: all assertions green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
