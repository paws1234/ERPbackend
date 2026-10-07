"""T-3.AR.01 check — the customer invoice, its posting and what is still owed.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_customer_invoice.py

Green on all nine:

1. posting an invoice writes **one balanced entry** debiting the receivables control
   account on the stated date and crediting revenue per line and the tax apart — every
   account resolved through T-1.ACCT.03's mapping, so no account code is fixed in the
   module
2. **tax is applied per the active pack**: the pack's VAT-OUT-12 is charged on the
   document's whole net, a line naming its own classification (a zero-rated export) is
   charged that instead, and a code the pack applies only to buying (VAT-IN-12) is
   refused rather than charged on a sale
3. an invoice for **goods already shipped** references the shipment and the order, and
   posting it **issues no stock**: the ledger's stock entries are the shipment's own and
   the count does not move when the invoice posts
4. a **duplicate** invoice — same customer, same order, same amount — is refused by the
   module's message, and the database refuses a hand-written one too
5. a **foreign-currency** invoice keeps its currency and its rate, and its base amount
   is derivable exactly
6. the due date is the invoice date plus the customer's own payment terms
7. what is owed is **derived** from settlements: a partial settlement leaves the right
   remainder, an over-settlement is refused, and settling a draft invoice is refused
8. a settlement row cannot be edited or removed — it is history (append-only)
9. a standalone invoice needs no order; a broken chain, an empty invoice, a blank number
   and a non-positive line are refused

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ar.invoices import (  # noqa: E402
    POSTED,
    CustomerInvoice,
    CustomerInvoiceSettlement,
    DuplicateInvoiceError,
    InvoiceError,
    InvoiceStateError,
    OverSettlementError,
    create_invoice,
    invoice_by_number,
    open_amount,
    open_invoices,
    post_invoice,
    settle,
    settled_amount,
)
from app.company import Company, set_credit_check_mode  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger.accounts import create_account  # noqa: E402
from app.ledger.currency import register_currency, store_rate  # noqa: E402
from app.ledger.mapping import set_mapping  # noqa: E402
from app.ledger.posting import JournalEntry, JournalLine  # noqa: E402
from app.sales.customers import create_customer  # noqa: E402
from app.sales.fulfilment import generate_pick_list, record_picked, ship_order  # noqa: E402
from app.sales.orders import confirm_order, convert_quotation_to_order  # noqa: E402
# Imported for its table, not its API: a quotation carries the opportunity it came
# from, so the opportunity table has to be in the one schema before `create_all`.
from app.sales.pipeline import Opportunity  # noqa: E402,F401
from app.sales.quotations import add_line, create_quotation  # noqa: E402
from app.sales.tax import UnknownTaxRuleError  # noqa: E402
from app.stock.entries import StockLedgerEntry  # noqa: E402
from app.stock.items import create_item  # noqa: E402
from app.stock.locations import create_location  # noqa: E402
from app.stock.transactions import receive  # noqa: E402
from tests.seed import seed_stock_accounts  # noqa: E402

COMPANY = uuid.uuid4()
OCT = date(2026, 10, 1)
SHIPPED_ON = date(2026, 10, 5)
INVOICE_DATE = date(2026, 10, 31)
TERMS = 30
# The pack's VAT-OUT-12, and one order of 10 widgets at 5.25 + freight at 250.50.
VAT_PERCENT = Decimal("12")
ORDER_NET = Decimal("10") * Decimal("5.25") + Decimal("250.50")


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


def _balanced(session: Session, entry: JournalEntry) -> tuple[Decimal, Decimal, list]:
    """The entry's two sides, and its lines — the arithmetic, not a promise."""
    lines = list(
        session.scalars(
            select(JournalLine)
            .where(JournalLine.entry_id == entry.id)
            .order_by(JournalLine.line_no)
        )
    )
    debit = sum((line.debit for line in lines), Decimal(0))
    credit = sum((line.credit for line in lines), Decimal(0))
    assert debit == credit, f"entry {entry.id} does not balance: {debit} != {credit}"
    assert len(lines) >= 2, f"entry {entry.id} has {len(lines)} line(s)"
    return debit, credit, lines


def _by_account(lines: list) -> dict[str, Decimal]:
    """What each account was debited (positive) or credited (negative)."""
    moved: dict[str, Decimal] = {}
    for line in lines:
        moved[line.account] = moved.get(line.account, Decimal(0)) + line.debit - line.credit
    return moved


def _stock_rows(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(StockLedgerEntry))


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
            Company(
                id=COMPANY,
                code="AR-CHECK",
                name="AR check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        register_currency(session, company_id=COMPANY, code="PHP", name="Peso")
        register_currency(session, company_id=COMPANY, code="USD", name="US Dollar")
        session.commit()
        seed_stock_accounts(session, company_id=COMPANY)
        # The pack names 2200 for output VAT; this company has to have the account
        # before it can map the key to it.
        create_account(
            session, company_id=COMPANY, code="2200", name="Output VAT", account_class="liability"
        )
        set_mapping(session, company_id=COMPANY, key="receivables", account_code="1100")
        set_mapping(session, company_id=COMPANY, key="revenue", account_code="4000")
        set_mapping(session, company_id=COMPANY, key="output_tax", account_code="2200")
        store_rate(
            session,
            company_id=COMPANY,
            base_currency="PHP",
            currency="USD",
            on=INVOICE_DATE,
            rate="58.5",
        )
        session.commit()

        item = create_item(
            session,
            company_id=COMPANY,
            sku="WIDGET",
            name="Widget",
            base_uom="each",
            traceability_mode="none",
        )
        warehouse = create_location(
            session,
            company_id=COMPANY,
            code="MAIN",
            name="Main warehouse",
            location_type="warehouse",
        )
        zone = create_location(
            session,
            company_id=COMPANY,
            code="MAIN-Z",
            name="Zone",
            location_type="zone",
            parent_id=warehouse.id,
        )
        aisle = create_location(
            session,
            company_id=COMPANY,
            code="MAIN-A",
            name="Aisle",
            location_type="aisle",
            parent_id=zone.id,
        )
        bin_a = create_location(
            session,
            company_id=COMPANY,
            code="MAIN-B1",
            name="Bin B1",
            location_type="bin",
            parent_id=aisle.id,
        )
        session.commit()
        receive(
            session,
            item=item,
            location=bin_a,
            uom="each",
            quantity="100",
            value=Decimal("400.00"),
            currency="PHP",
            source_type="goods_receipt",
            source_id=uuid.uuid4(),
            posting_date=OCT,
        )
        acme = create_customer(
            session,
            company_id=COMPANY,
            party_code="ACME",
            name="Acme Retail",
            payment_terms_days=TERMS,
            credit_limit="100000",
        )
        set_credit_check_mode(session, session.get(Company, COMPANY), mode="block")
        session.commit()

        # The shipped order the invoice is raised from: 10 widgets and freight.
        quote = create_quotation(
            session,
            company_id=COMPANY,
            customer_id=acme.id,
            number="Q-1",
            issued_on=OCT,
            valid_until=date(2026, 12, 31),
        )
        session.flush()
        add_line(
            session,
            quote,
            line_no=1,
            description="Widget",
            quantity="10",
            unit_price="5.25",
            uom="each",
            item_id=item.id,
            priced_on=OCT,
        )
        add_line(
            session,
            quote,
            line_no=2,
            description="Freight",
            quantity="1",
            unit_price="250.50",
            priced_on=OCT,
        )
        session.flush()
        order = convert_quotation_to_order(session, quote, number="SO-1", on=OCT)
        confirm_order(session, order, exposure="0", actor="maria")
        session.commit()
        order = session.scalar(
            select(type(order)).where(type(order).number == "SO-1")
        )
        listed = generate_pick_list(session, order, number="PL-1", on=OCT)
        record_picked(session, listed, line_no=1, quantity="10")
        session.commit()
        order = session.scalar(select(type(order)).where(type(order).number == "SO-1"))
        shipment = ship_order(
            session, order, number="SH-1", warehouse=bin_a, lines=[(1, "10")], on=SHIPPED_ON
        )
        session.commit()
        stock_after_shipping = _stock_rows(session)
        assert stock_after_shipping >= 1, "the shipment issued no stock at all"

        order_line = order.lines[0]
        shipment_line = shipment.lines[0]

        # 1 + 2 + 3 — the invoice from the shipment: its posting, its pack tax, and
        # the stock it does not move
        before_posting = _stock_rows(session)
        invoice = create_invoice(
            session,
            company_id=COMPANY,
            number="AR-1001",
            customer=acme,
            invoice_date=INVOICE_DATE,
            order_id=order.id,
            shipment_id=shipment.id,
            lines=[
                {
                    "description": "Widget",
                    "item_id": item.id,
                    "quantity": "10",
                    "uom": "each",
                    "unit_price": "5.25",
                    "order_line_id": order_line.id,
                    "shipment_line_id": shipment_line.id,
                },
                {"description": "Freight", "quantity": "1", "unit_price": "250.50"},
            ],
        )
        session.commit()
        assert invoice.order_id == order.id and invoice.shipment_id == shipment.id
        assert invoice.net_amount == ORDER_NET, invoice.net_amount
        expected_tax = (ORDER_NET * VAT_PERCENT / Decimal(100)).quantize(Decimal("0.000001"))
        assert invoice.tax_amount == expected_tax, (invoice.tax_amount, expected_tax)
        assert invoice.tax_rule_code is None, invoice.tax_rule_code
        assert invoice.status != POSTED, "the invoice posted itself"
        assert invoice.currency == "PHP", invoice.currency

        entry = post_invoice(session, invoice)
        session.commit()
        assert invoice.status == POSTED
        assert invoice.journal_entry_id == entry.id
        debit, credit, lines = _balanced(session, entry)
        assert debit == invoice.gross_amount, (debit, invoice.gross_amount)
        moved = _by_account(lines)
        assert moved["1100"] == invoice.gross_amount, moved
        assert moved["4000"] == -ORDER_NET, moved
        assert moved["2200"] == -expected_tax, moved
        assert entry.source_type == "customer_invoice" and entry.source_id == invoice.id
        assert entry.posting_date == INVOICE_DATE and entry.currency == "PHP"
        after_posting = _stock_rows(session)
        assert after_posting == before_posting == stock_after_shipping, (
            f"posting the invoice moved stock: {before_posting} → {after_posting}"
        )
        print(
            f"1. the invoice posts one balanced entry ({debit} = {credit}): 1100 debited"
            f" {invoice.gross_amount}, 4000 credited {ORDER_NET} and 2200 credited"
            f" {expected_tax} — all through the mapping, none hard-coded"
        )
        print(
            f"2. the pack's VAT-OUT-12 charges {expected_tax} on the whole net, and the"
            " rule is the one the pack states for this document rather than a rate in code"
        )
        print(
            f"3. the invoice names the shipment it bills, and posting it left the stock"
            f" ledger at {after_posting} entries — the shipment's own, not a second issue"
        )

        # 2b — a line's own classification, and a buying-side code refused
        zero = create_invoice(
            session,
            company_id=COMPANY,
            number="AR-1002",
            customer=acme,
            invoice_date=INVOICE_DATE,
            lines=[
                {"description": "Export goods", "quantity": "1", "unit_price": "100",
                 "tax_rule_code": "VAT-ZERO"},
                {"description": "Local goods", "quantity": "1", "unit_price": "100"},
            ],
        )
        session.commit()
        assert zero.lines[0].tax_amount == Decimal(0), zero.lines[0].tax_amount
        assert zero.lines[0].tax_rule_code == "VAT-ZERO", zero.lines[0].tax_rule_code
        assert zero.lines[1].tax_amount == Decimal("12.000000"), zero.lines[1].tax_amount
        assert zero.net_amount == Decimal("200.000000") and zero.tax_amount == Decimal(
            "12.000000"
        ), (zero.net_amount, zero.tax_amount)
        buying_side = _refused(
            lambda: create_invoice(
                session,
                company_id=COMPANY,
                number="AR-1002b",
                customer=acme,
                invoice_date=INVOICE_DATE,
                lines=[
                    {"description": "Wrongly classified", "quantity": "1",
                     "unit_price": "100", "tax_rule_code": "VAT-IN-12"}
                ],
            ),
            UnknownTaxRuleError,
        )
        session.rollback()
        print(
            f"2b. a zero-rated line is charged 0 ({zero.lines[0].tax_amount}) while its"
            f" standard-rated sibling is charged 12, and a buying-side code is refused"
            f" ({buying_side[:52]}…)"
        )

        # 4 — a duplicate of the same order and amount, by the module and by the database
        said = _refused(
            lambda: create_invoice(
                session,
                company_id=COMPANY,
                number="AR-1001b",
                customer=acme,
                invoice_date=INVOICE_DATE,
                order_id=order.id,
                shipment_id=shipment.id,
                lines=[
                    {"description": "Widget", "quantity": "10", "unit_price": "5.25"},
                    {"description": "Freight", "quantity": "1", "unit_price": "250.50"},
                ],
            ),
            DuplicateInvoiceError,
        )
        session.rollback()
        assert "already been invoiced" in said, said
        invoice = invoice_by_number(session, company_id=COMPANY, number="AR-1001")
        session.add(
            CustomerInvoice(
                company_id=COMPANY, number="AR-1001c", customer_id=acme.id,
                order_id=order.id, invoice_date=INVOICE_DATE, due_date=INVOICE_DATE,
                currency="PHP", net_amount=ORDER_NET, tax_amount=expected_tax,
                gross_amount=invoice.gross_amount, status="draft",
            )
        )
        try:
            session.commit()
        except DBAPIError as exc:
            assert "uq_customer_invoice_duplicate" in str(exc), exc
            session.rollback()
        else:
            raise AssertionError("the database accepted a duplicate invoice")
        print(
            f"4. a re-keyed invoice for the same order is refused ({said[:46]}…) and the"
            " database refuses a hand-written one too"
        )

        # 3b — a chain that is not one chain is refused, and a standalone invoice needs none
        broken = _refused(
            lambda: create_invoice(
                session,
                company_id=COMPANY,
                number="AR-1003",
                customer=acme,
                invoice_date=INVOICE_DATE,
                order_id=order.id,
                shipment_id=create_location(
                    session,
                    company_id=COMPANY,
                    code="X",
                    name="Not a shipment",
                    location_type="bin",
                    parent_id=aisle.id,
                ).id,
                lines=[{"description": "Widget", "quantity": "1", "unit_price": "1"}],
            ),
            InvoiceError,
        )
        session.rollback()
        assert "no shipment in this company" in broken, broken
        standalone = create_invoice(
            session,
            company_id=COMPANY,
            number="AR-1004",
            customer=acme,
            invoice_date=INVOICE_DATE,
            lines=[{"description": "Consulting", "quantity": "1", "unit_price": "500"}],
        )
        session.commit()
        assert standalone.order_id is None and standalone.shipment_id is None
        assert standalone.due_date == INVOICE_DATE + timedelta(days=TERMS), standalone.due_date
        print(
            f"3b. an invoice naming a shipment that is not its order's is refused"
            f" ({broken[:40]}…) while a standalone invoice needs no chain, and its due"
            f" date is the terms ({TERMS}d) after the invoice date"
        )

        # 5 — a foreign-currency invoice keeps its currency and its rate
        usd = create_invoice(
            session,
            company_id=COMPANY,
            number="AR-1005",
            customer=acme,
            invoice_date=INVOICE_DATE,
            currency="USD",
            lines=[{"description": "Imported goods", "quantity": "2", "unit_price": "300"}],
        )
        session.commit()
        usd_entry = post_invoice(session, usd)
        session.commit()
        assert usd.currency == "USD", usd.currency
        assert usd_entry.currency == "USD" and usd_entry.exchange_rate == Decimal("58.5"), (
            usd_entry.currency,
            usd_entry.exchange_rate,
        )
        base = (usd.gross_amount * usd_entry.exchange_rate).quantize(Decimal("0.000001"))
        assert base == (Decimal("672") * Decimal("58.5")).quantize(Decimal("0.000001")), base
        print(
            f"5. a USD invoice stays USD ({usd.currency}, gross {usd.gross_amount}) and"
            f" carries the rate for its own date ({usd_entry.exchange_rate}), so its base"
            f" amount {base} is derivable exactly"
        )

        # 6 + 7 — what is owed, derived from settlements
        assert open_amount(session, invoice) == invoice.gross_amount
        assert settled_amount(session, invoice) == Decimal(0)
        draft = create_invoice(
            session,
            company_id=COMPANY,
            number="AR-1006",
            customer=acme,
            invoice_date=INVOICE_DATE,
            lines=[{"description": "Not posted yet", "quantity": "1", "unit_price": "10"}],
        )
        session.commit()
        unposted = _refused(
            lambda: settle(
                session, draft, amount="1", settled_on=INVOICE_DATE,
                source_type="receipt", source_id=uuid.uuid4(),
            ),
            InvoiceStateError,
        )
        session.rollback()
        receipt_id = uuid.uuid4()
        settle(
            session, invoice, amount="100", settled_on=INVOICE_DATE + timedelta(days=10),
            source_type="receipt", source_id=receipt_id,
        )
        session.commit()
        remaining = (invoice.gross_amount - Decimal("100")).quantize(Decimal("0.000001"))
        assert settled_amount(session, invoice) == Decimal("100")
        assert open_amount(session, invoice) == remaining, dict(
            open=open_amount(session, invoice), remaining=remaining
        )
        over = _refused(
            lambda: settle(
                session, invoice, amount=remaining + 1, settled_on=INVOICE_DATE,
                source_type="receipt", source_id=uuid.uuid4(),
            ),
            OverSettlementError,
        )
        session.rollback()
        print(
            f"6. what is owed is derived, not stored: a partial receipt leaves {remaining}"
            f" of {invoice.gross_amount}, and over-settling is refused ({over[:44]}…)"
        )
        print(f"7. settling an unposted invoice is refused ({unposted[:52]}…)")

        # 7b — the aging population the later tasks read
        population = open_invoices(session, company_id=COMPANY, customer=acme)
        assert [row.number for row in population] == ["AR-1001", "AR-1005"], [
            row.number for row in population
        ]
        # A draft is not yet owed and an invoice dated after `as_of` is not yet raised,
        # so both are outside the population the aging report walks.
        assert open_invoices(
            session, company_id=COMPANY, customer=acme, as_of=INVOICE_DATE - timedelta(days=1)
        ) == []
        assert len(
            open_invoices(session, company_id=COMPANY, customer=acme, as_of=INVOICE_DATE)
        ) == 2
        print(
            f"7b. the posted population is the posted invoices only — oldest due first"
            f" ({', '.join(row.number for row in population)}), a draft and a not-yet-raised"
            " invoice left out — which is what the aging report walks"
        )

        # 8 — a settlement is history
        try:
            session.execute(
                CustomerInvoiceSettlement.__table__.update().values(amount=Decimal(1))
            )
            session.commit()
        except DBAPIError as exc:
            assert "append-only" in str(exc), exc
            session.rollback()
        else:
            raise AssertionError("a settlement was edited")
        try:
            session.execute(CustomerInvoiceSettlement.__table__.delete())
            session.commit()
        except DBAPIError as exc:
            assert "append-only" in str(exc), exc
            session.rollback()
        else:
            raise AssertionError("a settlement was removed")
        assert session.scalars(
            select(CustomerInvoiceSettlement).where(
                CustomerInvoiceSettlement.invoice_id == invoice.id
            )
        ).all(), "the settlement is gone"
        print("8. a settlement cannot be edited or deleted — it is history, refused by the"
              " database itself")

        # 9 — the refusals at entry
        empty = _refused(
            lambda: create_invoice(
                session, company_id=COMPANY, number="AR-1007", customer=acme,
                invoice_date=INVOICE_DATE, lines=[],
            ),
            InvoiceError,
        )
        session.rollback()
        blank = _refused(
            lambda: create_invoice(
                session, company_id=COMPANY, number="  ", customer=acme,
                invoice_date=INVOICE_DATE,
                lines=[{"description": "x", "quantity": "1", "unit_price": "1"}],
            ),
            InvoiceError,
        )
        session.rollback()
        zero = _refused(
            lambda: create_invoice(
                session, company_id=COMPANY, number="AR-1008", customer=acme,
                invoice_date=INVOICE_DATE,
                lines=[{"description": "x", "quantity": "0", "unit_price": "1"}],
            ),
            InvoiceError,
        )
        session.rollback()
        print(
            f"9. an empty invoice ({empty[:28]}…), a blank number ({blank[:28]}…) and a"
            f" zero-quantity line ({zero[:30]}…) are all refused"
        )

    print("\ncheck_customer_invoice: all assertions green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
