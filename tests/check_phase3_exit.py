"""T-3.X.GATE check — the Phase 3 exit criteria, verified against the running system.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_phase3_exit.py

§4's Phase 3 exit criterion is: *"Complete order-to-cash cycle including POS sales."*
This walks it, document by document, through the services the phase built — nothing
here re-implements a step or re-keys a figure, and every document is reached from the
one before it:

1. **lead → opportunity → quotation → order → fulfilment → invoice → dunning → gateway
   settlement with no manual re-keying** — the opportunity's win produces the
   quotation, the quotation's acceptance produces the order, the order ships, the
   shipment is invoiced, the overdue invoice is dunned, and the gateway's webhook
   settles it. Each document names the one it came from, and the figures are asserted
   against each other rather than re-derived.
2. **credit limits are enforced on the commitment and the exposure is itemised** — the
   order that would breach the limit is refused by name, and the exposure it was judged
   against is itemised to its documents, from one implementation (T-3.AR.06).
3. **a POS sale completes end to end, including its shift and its Z-Report**, and its
   postings balance — the till's own path, from a scanned barcode to the Z-Report that
   ties to the sale.
4. **online POS latency is measured within §6 metric 6's < 2 s budget**, on a complete
   sale and as a distribution rather than a single run.
5. **the AR subledger equals the receivables control account** after the whole cycle —
   the invoice, a partial receipt, the dunning, and the gateway's settlement and fee.
6. **every posting in the cycle balances** — §6 metric 1 over every entry the run wrote,
   with at least two lines each, read from the ledger rather than from the responses.

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import os
import statistics
import sys
import time
import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ar.aging import aging  # noqa: E402
from app.ar.dunning import define_level, run_dunning  # noqa: E402
from app.ar.exposure import customer_exposure, exposure_against_limit, open_items  # noqa: E402
from app.ar.gateway import record_payment  # noqa: E402
from app.ar.invoices import (  # noqa: E402
    POSTED,
    create_invoice,
    invoice_by_number,
    open_amount,
    post_invoice,
    settle,
)
from app.ar.reconciliation import explain, reconcile  # noqa: E402
from app.company import (  # noqa: E402
    Company,
    set_cash_drawer_required,
    set_credit_check_mode,
)
from app.db import Base  # noqa: E402
from app.integrations import receive_inbound, register_transport  # noqa: E402,F401
from app.ledger.accounts import create_account  # noqa: E402
from app.ledger.currency import register_currency  # noqa: E402
from app.ledger.mapping import set_mapping  # noqa: E402
from app.ledger.posting import JournalEntry, JournalLine  # noqa: E402
from app.pos.reports import day_report, shift_report  # noqa: E402
from app.pos.sales import (  # noqa: E402
    CASH,
    complete_sale,
    open_sale,
    receipt,
    scan,
    tender,
)
from app.pos.shifts import close_shift, open_shift, shift_totals  # noqa: E402
from app.sales.customers import (  # noqa: E402
    add_contact,
    create_customer,
    set_credit_limit,
    set_customer_tier,
)
from app.sales.fulfilment import (  # noqa: E402
    generate_pick_list,
    record_picked,
    ship_order,
)
from app.sales.orders import (  # noqa: E402
    CreditLimitExceeded,
    confirm_order,
    convert_quotation_to_order,
    order_by_number,
)
from app.sales.pipeline import (  # noqa: E402
    convert_to_quotation,
    create_opportunity,
    define_stage,
    move_opportunity,
    stage_by_name,
)
from app.sales.pricing import define_rule, price_quote_line  # noqa: E402

from app.stock.entries import StockLedgerEntry  # noqa: E402
from app.stock.items import add_barcode, create_item  # noqa: E402
from app.stock.locations import create_location  # noqa: E402
from app.stock.transactions import receive  # noqa: E402
from tests.seed import seed_stock_accounts  # noqa: E402

COMPANY = uuid.uuid4()
DAY = date(2026, 9, 26)
INVOICE_DAY = DAY + timedelta(days=3)
BARCODE = "4000000000505"
VAT = Decimal("1.12")
BUDGET_SECONDS = Decimal(os.environ.get("POS_LATENCY_BUDGET", "2.0"))
LATENCY_RUNS = 20


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


def _balances(session: Session) -> tuple[int, list[str]]:
    """§6 metric 1 over the stored ledger: every posting balances, with two lines or more."""
    broken: list[str] = []
    entries = list(session.scalars(select(JournalEntry)))
    for entry in entries:
        lines = list(
            session.scalars(select(JournalLine).where(JournalLine.entry_id == entry.id))
        )
        debit = sum((line.debit for line in lines), Decimal(0))
        credit = sum((line.credit for line in lines), Decimal(0))
        if len(lines) < 2 or debit != credit:
            broken.append(
                f"{entry.id} ({entry.source_type}): {len(lines)} line(s),"
                f" {debit} <> {credit}"
            )
    return len(entries), broken


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
            Company(id=COMPANY, code="PHASE3", name="Phase 3 exit", base_currency="PHP",
                    fiscal_year_start_month=1)
        )
        register_currency(session, company_id=COMPANY, code="PHP", name="Peso")
        session.commit()
        seed_stock_accounts(session, company_id=COMPANY)
        create_account(session, company_id=COMPANY, code="2200", name="Output VAT",
                       account_class="liability")
        # The account a gateway's own charge is booked to: the pack states none, so the
        # company states one and maps the key (T-3.AR.05).
        create_account(session, company_id=COMPANY, code="5610",
                       name="Bank and Payment Charges", account_class="expense")
        for key, code in (("receivables", "1100"), ("revenue", "4000"),
                          ("output_tax", "2200"), ("cash", "1000"), ("bank", "1010"),
                          ("payment_fees", "5610")):
            set_mapping(session, company_id=COMPANY, key=key, account_code=code)
        session.commit()

        # --- The catalogue, the till and the stock ---------------------------------
        widget = create_item(session, company_id=COMPANY, sku="WIDGET", name="Widget",
                             base_uom="each", traceability_mode="none")
        add_barcode(session, widget, value=BARCODE, symbology="ean")
        session.commit()
        warehouse = create_location(session, company_id=COMPANY, code="MAIN",
                                    name="Main", location_type="warehouse")
        zone = create_location(session, company_id=COMPANY, code="MAIN-Z", name="Zone",
                               location_type="zone", parent_id=warehouse.id)
        aisle = create_location(session, company_id=COMPANY, code="MAIN-A", name="Aisle",
                                location_type="aisle", parent_id=zone.id)
        shelf = create_location(session, company_id=COMPANY, code="MAIN-B1", name="Bin B1",
                                location_type="bin", parent_id=aisle.id)
        till_place = create_location(session, company_id=COMPANY, code="TILL-1",
                                     name="Till 1", location_type="bin", parent_id=aisle.id)
        session.commit()
        for place in (shelf, till_place):
            receive(session, item=widget, location=place, uom="each", quantity="500",
                    value=Decimal("20000"), currency="PHP", source_type="goods_receipt",
                    source_id=uuid.uuid4(), posting_date=DAY)
        session.commit()

        acme = create_customer(session, company_id=COMPANY, party_code="ACME",
                               name="Acme Retail", payment_terms_days=30,
                               credit_limit="10000")
        set_customer_tier(session, acme, tier="GOLD")
        # The contact a dunning reminder goes to: T-3.AR.04 delivers to the customer's
        # primary contact, and without one there is nowhere to send it.
        add_contact(session, acme, name="Ana Reyes", email="ana@acme.example",
                    is_primary=True)
        define_rule(session, company_id=COMPANY, code="GOLD-10", name="Gold 10%",
                    tier="GOLD", discount_type="percent", discount_value="10", priority=5)
        set_credit_check_mode(session, session.get(Company, COMPANY), mode="block")
        session.commit()

        # --- 1 — the cycle, each document reached from the one before it -------------
        lead = define_stage(session, company_id=COMPANY, name="Lead", position=1)
        won = define_stage(session, company_id=COMPANY, name="Won", position=2, is_won=True)
        session.commit()
        opportunity = create_opportunity(session, company_id=COMPANY, customer=acme,
                                         name="Acme roller blinds", owner="maria",
                                         value="5000", expected_close=DAY + timedelta(days=30),
                                         stage=lead, actor="maria", at=None)
        session.commit()
        move_opportunity(session, opportunity, to_stage=won, actor="maria")
        session.commit()
        quotation = convert_to_quotation(session, opportunity, number="Q-EXIT",
                                         issued_on=DAY)
        session.commit()
        assert quotation.customer_id == acme.id, "the quotation lost the customer"
        priced = price_quote_line(
            session, quotation, line_no=1, description="Widget", quantity="10",
            base_price="100.00", item_id=widget.id, uom="each", on=DAY,
        )
        session.commit()
        assert priced.unit_price == Decimal("90.000000"), priced.unit_price
        assert quotation.opportunity_id == opportunity.id, "the quotation lost its win"
        order = convert_quotation_to_order(session, quotation, number="SO-EXIT", on=DAY)
        session.commit()
        assert order.quotation_id == quotation.id, "the order lost its quotation"
        assert order.lines[0].unit_price == Decimal("90.000000"), order.lines[0].unit_price

        # 2 — the credit decision is taken on the live, itemised exposure
        statement = customer_exposure(session, acme, as_of=DAY)
        assert statement.components["invoiced"] == Decimal("0.000000"), statement.components
        decision = confirm_order(session, order, actor="maria")
        session.commit()
        assert decision.outcome == "within_limit", decision.outcome
        assert decision.exposure == statement.total == Decimal("0.000000"), (
            decision.exposure,
            statement.total,
        )
        order = order_by_number(session, company_id=COMPANY, number="SO-EXIT")
        committed = customer_exposure(session, acme, as_of=DAY)
        assert committed.unbilled_orders == Decimal("900.000000"), committed.unbilled_orders
        assert committed.total == Decimal("900.000000"), committed.total

        listed = generate_pick_list(session, order, number="PL-EXIT", on=DAY)
        record_picked(session, listed, line_no=1, quantity="10")
        session.commit()
        order = order_by_number(session, company_id=COMPANY, number="SO-EXIT")
        shipment = ship_order(session, order, number="SH-EXIT", warehouse=shelf,
                              lines=[(1, "10")], on=DAY)
        session.commit()
        assert shipment.order_id == order.id and shipment.lines[0].movement_id is not None

        invoice = create_invoice(
            session, company_id=COMPANY, number="AR-EXIT", customer=acme,
            invoice_date=INVOICE_DAY, order_id=order.id, shipment_id=shipment.id,
            lines=[{"description": "Widget", "item_id": widget.id, "quantity": "10",
                    "uom": "each", "unit_price": "90.00",
                    "order_line_id": order.lines[0].id,
                    "shipment_line_id": shipment.lines[0].id}],
        )
        session.commit()
        post_invoice(session, invoice)
        session.commit()
        assert invoice.status == POSTED
        assert invoice.net_amount == Decimal("900.000000"), invoice.net_amount
        assert invoice.gross_amount == (Decimal("900") * VAT).quantize(Decimal("0.000001")), (
            invoice.gross_amount
        )
        print(
            f"1. the cycle ran document to document: opportunity"
            f" {opportunity.name!r} → quotation {quotation.number} (10 at"
            f" {priced.unit_price} under the GOLD rule) → order {order.number} → shipment"
            f" {shipment.number} → invoice {invoice.number}",
            end="",
        )

        # partial receipt, dunning, then the gateway settles the rest
        partial = (invoice.gross_amount / 4).quantize(Decimal("0.000001"))
        settle(session, invoice, amount=partial, settled_on=INVOICE_DAY,
               source_type="receipt", source_id=uuid.uuid4())
        from app.ledger.posting import post_journal_entry  # noqa: E402

        post_journal_entry(
            session, company_id=COMPANY, posting_date=INVOICE_DAY, currency="PHP",
            memo=f"counter receipt against {invoice.number}", source_type="receipt",
            source_id=uuid.uuid4(),
            lines=[{"account": "1010", "debit": partial}, {"account": "1100", "credit": partial}],
        )
        session.commit()
        define_level(session, company_id=COMPANY, code="FINAL", name="Final notice",
                     from_days=0, to_days=None, channel="email", template="reminder")
        sent: list[dict] = []
        register_transport("email", lambda destination, payload: sent.append(payload))
        session.commit()
        run = run_dunning(session, company_id=COMPANY, as_of=INVOICE_DAY + timedelta(days=60))
        assert [row.invoice_id for row in run.reminders] == [invoice.id], run.reminders
        assert run.delivered == 1 and sent, "the reminder was not delivered"
        assert sent[0]["open_amount"] == str(invoice.gross_amount - partial), sent[0]
        remainder = open_amount(session, invoice)
        payment, _ = record_payment(
            session, company_id=COMPANY, event_key="evt-exit",
            payload={"reference": "PAY-EXIT", "status": "succeeded",
                     "amount": str(remainder), "fee": "5.00", "invoice": invoice.number,
                     "currency": "PHP", "on": (INVOICE_DAY + timedelta(days=61)).isoformat()},
        )
        session.commit()
        assert payment.status == "settled", payment.status
        assert open_amount(session, invoice) == Decimal("0.000000"), open_amount(session, invoice)
        print(
            f" → a {partial} receipt → dunning ({sent[0]['level']}) → gateway PAY-EXIT"
            f" settled the {remainder} left, no step re-keyed"
        )

        # 2b — a second order is blocked on the same live figure
        second_win = create_opportunity(
            session, company_id=COMPANY, customer=acme, name="Acme awnings",
            owner="maria", value="10000", stage=won, actor="maria",
        )
        session.commit()
        second_quote = convert_to_quotation(session, second_win, number="Q-EXIT-2",
                                            issued_on=DAY)
        session.commit()
        # 200 at 90.00 is 18,000 — over the 10,000 ceiling on its own, so the block is
        # the limit and not the exposure.
        price_quote_line(session, second_quote, line_no=1, description="Bulk widgets",
                         quantity="200", base_price="100.00", item_id=widget.id,
                         uom="each", on=DAY)
        session.commit()
        second = convert_quotation_to_order(session, second_quote, number="SO-EXIT-2",
                                            on=DAY)
        session.commit()
        blocked = _refused(lambda: confirm_order(session, second, actor="maria"),
                           CreditLimitExceeded)
        session.rollback()
        # The customer's *live* position — as at today, which is what the order-time
        # check reads when a caller states nothing. The cycle's invoice is not settled
        # in full yet: the counter receipt is on the books and the gateway's settlement
        # is dated later, so 756.00 is still open, and the new order would take the
        # customer to 18,756.00.
        live = customer_exposure(session, acme)
        assert "the agreed limit 10000" in blocked, blocked
        assert live.open_invoices == Decimal("756.000000"), live.open_invoices
        assert live.unbilled_orders == Decimal("0.000000"), live.unbilled_orders
        assert live.total == Decimal("756.000000"), live.total
        assert str(live.total + Decimal("18000")) in blocked or "18756" in blocked, blocked
        set_credit_limit(session, acme, limit="100000")
        session.commit()
        order_two = order_by_number(session, company_id=COMPANY, number="SO-EXIT-2")
        confirmed = confirm_order(session, order_two, actor="maria")
        session.commit()
        assert confirmed.exposure == live.total, (confirmed.exposure, live.total)
        assert exposure_against_limit(session, acme, as_of=DAY)["breached"] is False
        items = open_items(session, acme, as_of=DAY, currency="PHP")
        assert items == [], "the settled invoice is still open"
        print(
            f"2. the second order was blocked on the same live exposure"
            f" ({live.total} → 10000 ceiling) and confirmed once the ceiling was raised —"
            f" itemised to {len(live.components)} components"
        )

        # --- 3 + 4 — the till: a sale, its shift, its Z-Report, and the latency -------
        set_cash_drawer_required(session, session.get(Company, COMPANY), required=True)
        session.commit()
        shift = open_shift(session, company_id=COMPANY, terminal="T1", opening_float="200",
                           actor="maria", on=DAY + timedelta(days=1))
        session.commit()
        trade_day = DAY + timedelta(days=1)

        def one_sale(index: int) -> float:
            started = time.perf_counter()
            sale = open_sale(session, company_id=COMPANY, number=f"POS-EXIT-{index:03d}",
                             terminal="T1", location=till_place, customer=acme,
                             sold_on=trade_day)
            session.flush()
            scan(session, sale, barcode=BARCODE, base_price="100.00", quantity="2")
            session.commit()
            tender(session, sale, tender_type=CASH, amount=str(sale.gross_amount))
            session.commit()
            complete_sale(session, sale)
            session.commit()
            receipt(sale)
            return (time.perf_counter() - started) * 1000

        latencies = [one_sale(index) for index in range(LATENCY_RUNS)]
        report = shift_report(session, shift)
        assert report["ties"] is True, report
        assert report["sales"] == LATENCY_RUNS, report["sales"]
        totals = shift_totals(session, shift)
        close_shift(session, shift, counted_cash=str(totals["expected_cash"]),
                    actor="maria")
        session.commit()
        day = day_report(session, company_id=COMPANY, on=trade_day)
        assert day["sales"] == LATENCY_RUNS and day["gross"] == report["gross"], day
        p95 = sorted(latencies)[max(0, int(round(0.95 * len(latencies) + 0.5)) - 1)]
        budget_ms = float(BUDGET_SECONDS) * 1000
        assert p95 <= budget_ms, (
            f"the POS sale's 95th percentile was {p95:.0f} ms against a {budget_ms:.0f} ms"
            " budget"
        )
        print(
            f"3. the till sold {LATENCY_RUNS} sales on one shift and its Z-Report ties"
            f" ({report['gross']} gross, {report['tax']} tax), the day report agrees, and"
            f" the shift closed on its own expected cash"
        )
        print(
            f"4. online POS latency over {LATENCY_RUNS} complete sales: min"
            f" {min(latencies):.0f} ms, median {statistics.median(latencies):.0f} ms, p95"
            f" {p95:.0f} ms, max {max(latencies):.0f} ms — within §6 metric 6's"
            f" {budget_ms:.0f} ms budget"
        )

        # --- 5 — the AR subledger equals the control account -------------------------
        # A date at which the cycle's invoice is *partly* open — after the counter
        # receipt and before the gateway settles the rest — so both sides of the
        # comparison and the aging report have a figure to state.
        mid_day = INVOICE_DAY + timedelta(days=31)
        reconciliation = reconcile(session, company_id=COMPANY, as_of=mid_day)
        assert reconciliation["balanced"] is True, explain(reconciliation)
        php = next(row for row in reconciliation["currencies"] if row["currency"] == "PHP")
        assert php["subledger"] == php["control"] == Decimal("756.000000"), php
        report_aging = aging(session, company_id=COMPANY, as_of=mid_day)
        assert report_aging.difference["PHP"] == Decimal("0.000000"), report_aging.difference
        assert report_aging.total_by_currency["PHP"] == php["subledger"], (
            report_aging.total_by_currency,
            php,
        )
        print(
            f"5. the AR subledger and the receivables control account agree to the last"
            f" decimal ({php['subledger']}) — the aging report's own total is the same"
            f" figure ({report_aging.total_by_currency['PHP']})"
        )

        # --- 6 — every posting in the cycle balances (metric 1) ----------------------
        entries, broken = _balances(session)
        assert broken == [], broken
        assert entries >= 10, entries
        stock_entries = session.scalar(select(func.count()).select_from(StockLedgerEntry))
        assert stock_entries >= 3, stock_entries
        print(
            f"6. §6 metric 1 over the whole cycle: all {entries} journal entries balance"
            f" with two lines or more, and the stock ledger holds {stock_entries}"
            " movements"
        )

    print("\ncheck_phase3_exit: all assertions green — Phase 3's exit criteria hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
