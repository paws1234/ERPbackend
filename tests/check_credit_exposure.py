"""T-3.AR.06 check — the live exposure, and the one figure the order check uses.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_credit_exposure.py

Green on all nine:

1. the exposure is **itemised**: what was invoiced, what was received against it, what
   confirmed orders commit and what has been received on account, adding to the total
2. the **same figure** the exposure statement shows is the one the order-time check
   decides on — confirming an order with nothing stated is judged against it, and the
   refusal names it
3. an increase in exposure from a **new invoice is immediately visible** to the next
   order check, which blocks on the higher figure
4. an **on-account receipt** (money received with no invoice to apply it to) reduces the
   exposure, and a credit larger than what is owed leaves the total at zero with the
   credit named — never a negative exposure
5. a receipt that names **no customer** is nobody's credit — it reduces no customer's
   exposure and is still on the parked list — while the receipt that names one is that
   customer's alone
6. a **limit change is audited**: the trail records the `customer` row's change with the
   before and the after
7. exposure and the limit it is judged against are stated together, null limit included
8. the open invoices behind the figure are itemised document by document
9. documents in another **currency** are reported beside the figure, never added into it
   — and a caller that still states an exposure is honoured exactly as before

10. goods **shipped but not yet invoiced** stay in the exposure: a four-of-ten shipment
   left the figure where it was, and invoicing it moved the net from `unbilled` to the
   invoice's gross

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

from app.ar.exposure import (  # noqa: E402
    INVOICED,
    ON_ACCOUNT,
    RECEIVED,
    UNBILLED,
    customer_exposure,
    exposure_against_limit,
    open_items,
)
from app.ar.gateway import parked_payments, record_payment  # noqa: E402
from app.ar.invoices import create_invoice, post_invoice, settle  # noqa: E402
from app.audit import AuditLog  # noqa: E402
from app.company import Company, set_credit_check_mode  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger.accounts import create_account  # noqa: E402
from app.ledger.currency import register_currency, store_rate  # noqa: E402
from app.ledger.mapping import set_mapping  # noqa: E402
from app.ledger.posting import post_journal_entry  # noqa: E402
from app.sales.customers import create_customer, set_credit_limit  # noqa: E402
from app.sales.fulfilment import (  # noqa: E402
    Shipment,
    generate_pick_list,
    record_picked,
    ship_order,
)
from app.sales.orders import (  # noqa: E402
    CreditLimitExceeded,
    confirm_order,
    convert_quotation_to_order,
)
from app.sales.pipeline import Opportunity  # noqa: E402,F401 — for its table
from app.sales.quotations import add_line, create_quotation  # noqa: E402
from app.stock.items import create_item  # noqa: E402
from app.stock.locations import create_location  # noqa: E402
from app.stock.transactions import receive  # noqa: E402
from tests.seed import seed_stock_accounts  # noqa: E402

COMPANY = uuid.uuid4()
# Before the day the check runs: confirmation computes the live exposure as at
# *today*, so every document below has to have happened by now.
DAY = date(2026, 9, 1)
TERMS = 30
VAT = Decimal("1.12")
LIMIT = Decimal("1000")


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


def _gross(net: str) -> Decimal:
    return (Decimal(net) * VAT).quantize(Decimal("0.000001"))


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
                code="EXPOSURE",
                name="Exposure",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        register_currency(session, company_id=COMPANY, code="PHP", name="Peso")
        register_currency(session, company_id=COMPANY, code="USD", name="US Dollar")
        session.commit()
        # The usual chart plus the stock mappings: the section below ships goods, and
        # an issue posts through `inventory`/`stock_issue` (T-1.INV.07).
        seed_stock_accounts(session, company_id=COMPANY)
        create_account(session, company_id=COMPANY, code="2200", name="Output VAT",
                       account_class="liability")
        set_mapping(session, company_id=COMPANY, key="receivables", account_code="1100")
        set_mapping(session, company_id=COMPANY, key="revenue", account_code="4000")
        set_mapping(session, company_id=COMPANY, key="output_tax", account_code="2200")
        store_rate(session, company_id=COMPANY, base_currency="PHP", currency="USD",
                   on=DAY, rate="58.5")
        session.commit()

        acme = create_customer(session, company_id=COMPANY, party_code="ACME",
                               name="Acme Retail", payment_terms_days=TERMS,
                               credit_limit=LIMIT)
        session.commit()
        set_credit_check_mode(session, session.get(Company, COMPANY), mode="off")
        session.commit()

        # A confirmed order commits the customer before anything is billed.
        quote = create_quotation(session, company_id=COMPANY, customer_id=acme.id,
                                number="Q-1", issued_on=DAY,
                                valid_until=date(2026, 12, 31))
        session.flush()
        add_line(session, quote, line_no=1, description="Widget", quantity="10",
                 unit_price="100.00", uom="each", priced_on=DAY)
        session.flush()
        order = convert_quotation_to_order(session, quote, number="SO-1", on=DAY)
        first_decision = confirm_order(session, order, actor="maria")
        session.commit()
        assert first_decision.exposure == Decimal("0.000000"), first_decision.exposure
        assert first_decision.outcome == "within_limit", first_decision.outcome

        def invoice(number: str, net: str, currency=None):
            made = create_invoice(
                session, company_id=COMPANY, number=number, customer=acme,
                invoice_date=DAY, currency=currency,
                lines=[{"description": "Goods", "quantity": "1", "unit_price": net}],
            )
            session.commit()
            post_invoice(session, made)
            session.commit()
            return made

        billed = invoice("AR-1", "500.00")
        abroad = invoice("AR-2", "100.00", currency="USD")
        settle(session, billed, amount="60.00", settled_on=DAY, source_type="receipt",
               source_id=uuid.uuid4())
        # The receipt's money arriving, as T-3.AR.05 would post it: without it the
        # ledger would still show the whole invoice as owed.
        post_journal_entry(
            session, company_id=COMPANY, posting_date=DAY, currency="PHP",
            memo="receipt against AR-1", source_type="receipt", source_id=uuid.uuid4(),
            lines=[{"account": "1010", "debit": Decimal("60")},
                   {"account": "1100", "credit": Decimal("60")}],
        )
        session.commit()
        # Money received with no invoice to apply it to: T-3.AR.05 parks it, and it is
        # the customer's credit until something is billed — the gateway says which
        # customer paid, which is what lets it be that customer's credit and nobody
        # else's.
        record_payment(
            session, company_id=COMPANY, event_key="evt-on-account",
            payload={"reference": "PAY-ON-ACCOUNT", "status": "succeeded",
                     "amount": "50.00", "fee": "0", "currency": "PHP",
                     "customer": "ACME", "on": DAY.isoformat()},
        )
        session.commit()

        # 1 — the exposure is itemised
        exposure = customer_exposure(session, acme, as_of=DAY)
        assert exposure.currency == "PHP", exposure.currency
        assert exposure.components == {
            INVOICED: _gross("500"),
            RECEIVED: Decimal("60.000000"),
            UNBILLED: Decimal("1000.000000"),
            ON_ACCOUNT: Decimal("50.000000"),
        }, exposure.components
        assert exposure.open_invoices == (_gross("500") - Decimal("60")), exposure.open_invoices
        assert exposure.total == (_gross("500") - Decimal("60") + Decimal("1000") - Decimal("50")), (
            exposure.total
        )
        assert exposure.total == Decimal("1450.000000"), exposure.total
        assert exposure.credit == Decimal(0), exposure.credit
        print(
            f"1. the exposure is itemised: invoiced {exposure.components[INVOICED]}, received"
            f" {exposure.components[RECEIVED]}, unbilled orders"
            f" {exposure.components[UNBILLED]}, on account"
            f" {exposure.components[ON_ACCOUNT]} → {exposure.total}"
        )

        # 2 — the order-time check decides on the same figure
        set_credit_check_mode(session, session.get(Company, COMPANY), mode="block")
        session.commit()
        quote_two = create_quotation(session, company_id=COMPANY, customer_id=acme.id,
                                     number="Q-2", issued_on=DAY,
                                     valid_until=date(2026, 12, 31))
        session.flush()
        add_line(session, quote_two, line_no=1, description="Widget", quantity="1",
                 unit_price="100.00", uom="each", priced_on=DAY)
        session.flush()
        second = convert_quotation_to_order(session, quote_two, number="SO-2", on=DAY)
        session.commit()
        said = _refused(
            lambda: confirm_order(session, second, actor="maria"), CreditLimitExceeded
        )
        session.rollback()
        # An order is worth its lines (T-3.SALES.04's `order_total`): 1 × 100.00, the
        # tax is applied when it is invoiced, not when it is ordered.
        after = exposure.total + Decimal("100.000000")
        assert str(after) in said, said
        assert str(LIMIT) in said, said
        session.rollback()
        second = session.scalar(
            select(type(second)).where(type(second).number == "SO-2")
        )
        print(
            f"2. confirming SO-2 with nothing stated was judged against the statement's"
            f" own {exposure.total} (plus its 100.00 → {after} over the {LIMIT} limit):"
            f" refused — one implementation, not two"
        )

        # 3 — a new invoice is immediately visible to the next check
        invoice("AR-3", "100.00")
        grown = customer_exposure(session, acme, as_of=DAY)
        assert grown.total == (exposure.total + _gross("100")), (grown.total, exposure.total)
        said_again = _refused(
            lambda: confirm_order(session, second, actor="maria"), CreditLimitExceeded
        )
        session.rollback()
        assert str(grown.total + Decimal("100.000000")) in said_again, said_again
        print(
            f"3. the new invoice took the exposure to {grown.total}, and the very next"
            f" order check refused on {grown.total + Decimal('100.000000')} — the"
            " increase was visible without anything being recomputed by hand"
        )

        # 4 — an on-account receipt reduces it, and a credit never goes negative
        record_payment(
            session, company_id=COMPANY, event_key="evt-big-credit",
            payload={"reference": "PAY-CREDIT", "status": "succeeded",
                     "amount": "2000.00", "fee": "0", "currency": "PHP",
                     "customer": "ACME", "on": DAY.isoformat()},
        )
        session.commit()
        credited = customer_exposure(session, acme, as_of=DAY)
        assert credited.total == Decimal(0), credited.total
        # Everything the customer has paid, against everything billed and committed:
        # the invoices' 672 open less the 60 received, plus 1000 unbilled, against
        # 2050 received on account (the 50 first, then the 2000).
        expected_credit = (
            Decimal("60") + Decimal("2050")
            - (_gross("500") + _gross("100") + Decimal("1000"))
        )
        assert credited.credit == expected_credit, (credited.credit, expected_credit)
        assert credited.on_account_receipts == Decimal("2050.000000"), (
            credited.on_account_receipts
        )
        print(
            f"4. the 2000.00 on-account receipt took the exposure to {credited.total}"
            f" with {credited.credit} held as a credit — subtracted, never a negative"
            " exposure"
        )

        # 4b — a receipt that names nobody is nobody's credit
        rival = create_customer(session, company_id=COMPANY, party_code="RIVAL",
                                name="Rival Retail", payment_terms_days=TERMS)
        session.commit()
        rival_invoice = create_invoice(
            session, company_id=COMPANY, number="AR-RIVAL", customer=rival,
            invoice_date=DAY,
            lines=[{"description": "Goods", "quantity": "1", "unit_price": "1000.00"}],
        )
        session.commit()
        post_invoice(session, rival_invoice)
        session.commit()
        before_rival = customer_exposure(session, rival, as_of=DAY)
        before_acme = customer_exposure(session, acme, as_of=DAY)
        unattributed, _ = record_payment(
            session, company_id=COMPANY, event_key="evt-unattributed",
            payload={"reference": "PAY-NOBODY", "status": "succeeded",
                     "amount": "900.00", "fee": "0", "currency": "PHP",
                     "on": DAY.isoformat()},
        )
        session.commit()
        assert unattributed.status == "parked", unattributed.status
        assert unattributed.customer_id is None, unattributed.customer_id
        assert customer_exposure(session, rival, as_of=DAY).total == before_rival.total, (
            "an unattributed receipt credited another customer"
        )
        assert customer_exposure(session, acme, as_of=DAY).total == before_acme.total, (
            "an unattributed receipt credited the customer that asked"
        )
        assert customer_exposure(session, rival, as_of=DAY).on_account_receipts == Decimal(0), (
            "an unattributed receipt was reported as the rival's own credit"
        )
        # It is not lost: the parked list still shows the money, with its reason.
        assert any(p.reference == "PAY-NOBODY" for p in parked_payments(
            session, company_id=COMPANY
        )), "the unattributed receipt vanished from the parked list"
        print(
            "4b. a 900.00 receipt that names no customer is nobody's credit — the"
            f" rival still owes {customer_exposure(session, rival, as_of=DAY).total}"
            " and the parked list still holds the money"
        )

        # 6 — a limit change is audited
        before_rows = session.scalars(
            select(AuditLog).where(
                AuditLog.entity == "customer",
                AuditLog.entity_id == str(acme.id),
                AuditLog.action == "update",
            )
        ).all()
        set_credit_limit(session, acme, limit="2500")
        session.commit()
        after_rows = session.scalars(
            select(AuditLog).where(
                AuditLog.entity == "customer",
                AuditLog.entity_id == str(acme.id),
                AuditLog.action == "update",
            )
        ).all()
        assert len(after_rows) == len(before_rows) + 1, (len(before_rows), len(after_rows))
        entry = sorted(after_rows, key=lambda row: row.occurred_at)[-1]
        # The trail stores the row as JSON, so the amount arrives as a JSON number:
        # compared as a decimal, which is what it is, rather than by its formatting.
        assert Decimal(str(entry.before_values["credit_limit"])) == LIMIT, entry.before_values
        assert Decimal(str(entry.after_values["credit_limit"])) == Decimal("2500"), (
            entry.after_values
        )
        print(
            f"6. changing the ceiling from {LIMIT} to 2500 wrote a trail row naming the"
            f" before ({entry.before_values.get('credit_limit')}) and the after"
            f" ({entry.after_values.get('credit_limit')})"
        )

        # 7 — the exposure beside the limit it is judged against
        against = exposure_against_limit(session, acme, as_of=DAY)
        assert Decimal(against["limit"]) == Decimal("2500"), against
        assert against["breached"] is False, against
        assert against["statement"]["total"] == "0.000000", against["statement"]
        assert Decimal(against["headroom"]) == (Decimal("2500") - credited.total), against
        none_agreed = create_customer(
            session, company_id=COMPANY, party_code="NO-LIMIT", name="No limit",
            payment_terms_days=TERMS,
        )
        session.commit()
        assert exposure_against_limit(session, none_agreed)["limit"] is None, (
            "a customer with no limit agreed reads as a zero limit"
        )
        print(
            f"7. the exposure is stated beside its limit ({against['limit']}, headroom"
            f" {against['headroom']}, breached={against['breached']}), and a customer with"
            " no limit agreed reads null rather than zero"
        )

        # 8 — the open invoices behind the figure
        items = open_items(session, acme, as_of=DAY)
        assert [row["invoice"] for row in items] == ["AR-1", "AR-2", "AR-3"], items
        php_items = open_items(session, acme, as_of=DAY, currency="PHP")
        assert [row["invoice"] for row in php_items] == ["AR-1", "AR-3"], php_items
        assert php_items[0]["open_amount"] == (_gross("500") - Decimal("60")), php_items[0]
        assert sum(
            (row["open_amount"] for row in php_items), Decimal(0)
        ) == credited.open_invoices, php_items
        print(
            f"8. the figure opens up into the documents behind it"
            f" ({[(row['invoice'], str(row['open_amount'])) for row in php_items]}, with"
            f" AR-2 listed in its own currency)"
        )

        # 9 — another currency is reported beside it, and a stated exposure still stands
        assert credited.other_currencies == {"USD": abroad.gross_amount}, (
            credited.other_currencies
        )
        assert credited.total < Decimal("100000"), "the USD invoice was added into PHP"
        stated = customer_exposure(session, acme, as_of=DAY, currency="USD")
        assert stated.total == abroad.gross_amount, stated.total
        assert stated.other_currencies and "PHP" in stated.other_currencies, (
            stated.other_currencies
        )
        quote_three = create_quotation(session, company_id=COMPANY, customer_id=acme.id,
                                       number="Q-3", issued_on=DAY,
                                       valid_until=date(2026, 12, 31))
        session.flush()
        add_line(session, quote_three, line_no=1, description="Widget", quantity="1",
                 unit_price="100.00", uom="each", priced_on=DAY)
        session.flush()
        third = convert_quotation_to_order(session, quote_three, number="SO-3", on=DAY)
        session.commit()
        decision = confirm_order(session, third, exposure="700.00", actor="maria")
        session.commit()
        assert decision.exposure == Decimal("700.000000"), decision.exposure
        assert decision.limit_amount == Decimal("2500"), decision.limit_amount
        print(
            f"9. the USD invoice is reported beside the PHP figure"
            f" ({credited.other_currencies}) and asked for in its own currency"
            f" ({stated.total}); and an exposure the caller states is still recorded"
            f" verbatim ({decision.exposure})"
        )

        # 10 — goods shipped but not yet invoiced stay in the exposure
        widget = create_item(session, company_id=COMPANY, sku="WIDGET", name="Widget",
                             base_uom="each", traceability_mode="none")
        warehouse = create_location(session, company_id=COMPANY, code="MAIN",
                                    name="Main", location_type="warehouse")
        zone = create_location(session, company_id=COMPANY, code="MAIN-Z", name="Zone",
                               location_type="zone", parent_id=warehouse.id)
        aisle = create_location(session, company_id=COMPANY, code="MAIN-1", name="Aisle",
                                location_type="aisle", parent_id=zone.id)
        bin_a = create_location(session, company_id=COMPANY, code="MAIN-1-A", name="Bin A",
                                location_type="bin", parent_id=aisle.id)
        session.commit()
        receive(session, item=widget, location=bin_a, uom="each", quantity="10",
                value=Decimal("400"), currency="PHP", source_type="goods_receipt",
                source_id=uuid.uuid4(), posting_date=DAY)
        session.commit()

        quote_four = create_quotation(session, company_id=COMPANY, customer_id=acme.id,
                                      number="Q-4", issued_on=DAY,
                                       valid_until=date(2026, 12, 31))
        session.flush()
        add_line(session, quote_four, line_no=1, description="Widget", quantity="10",
                 unit_price="100.00", uom="each", item_id=widget.id, priced_on=DAY)
        session.flush()
        fourth = convert_quotation_to_order(session, quote_four, number="SO-4", on=DAY)
        confirm_order(session, fourth, actor="maria")
        session.commit()
        before_shipping = customer_exposure(session, acme, as_of=DAY)
        assert before_shipping.components[UNBILLED] >= Decimal("1000.000000"), (
            before_shipping.components
        )
        committed = before_shipping.components[UNBILLED]

        listed = generate_pick_list(session, fourth, number="PL-4", on=DAY)
        record_picked(session, listed, line_no=1, quantity="4")
        session.commit()
        fourth = session.scalar(select(type(fourth)).where(type(fourth).number == "SO-4"))
        shipment = ship_order(session, fourth, number="SH-4", warehouse=bin_a,
                              lines=[(1, "4")], on=DAY)
        session.commit()
        assert fourth.lines[0].shipped_quantity == Decimal("4.000000"), (
            fourth.lines[0].shipped_quantity
        )
        shipped_only = customer_exposure(session, acme, as_of=DAY)
        assert shipped_only.components[UNBILLED] == committed, (
            "shipping four units without invoicing them freed up credit"
        )
        assert shipped_only.total == before_shipping.total, (
            shipped_only.total, before_shipping.total
        )

        billed = create_invoice(
            session, company_id=COMPANY, number="AR-SO4", customer=acme,
            invoice_date=DAY, order_id=fourth.id, shipment_id=shipment.id,
            lines=[{"description": "Widget", "item_id": widget.id, "quantity": "4",
                    "uom": "each", "unit_price": "100.00",
                    "order_line_id": fourth.lines[0].id,
                    "shipment_line_id": shipment.lines[0].id}],
        )
        session.commit()
        post_invoice(session, billed)
        session.commit()
        invoiced = customer_exposure(session, acme, as_of=DAY)
        assert invoiced.components[UNBILLED] == committed - Decimal("400.000000"), (
            invoiced.components, committed
        )
        assert invoiced.components[INVOICED] == (
            shipped_only.components[INVOICED] + Decimal("448.000000")
        ), (invoiced.components, shipped_only.components)
        print(
            f"10. four of SO-4's ten units shipped without an invoice left the exposure"
            f" exactly where it was ({invoiced.components[UNBILLED]} +"
            f" {invoiced.components[INVOICED]} of it billed): measuring the order from"
            " the shipment would have freed credit for goods the customer already has"
        )

    print("\ncheck_credit_exposure: all assertions green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
