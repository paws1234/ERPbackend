"""T-3.AR.05 check — gateway webhooks and the settlement they post.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_gateway_payments.py

Green on all nine:

1. a successful payment settles the invoice and posts **one balanced entry** — bank and
   the gateway's fee debited, receivables credited the whole payment, every account
   resolved through T-1.ACCT.03's mapping
2. a **duplicated webhook** settles the invoice once: the replayed delivery is answered
   from T-0.INT.01's record without running the handler again, and a *second event key*
   describing the same payment is answered from the payment row — one settlement, one
   entry, one delivery processed
3. a **partial** payment leaves the correct open amount and is recorded as partial
4. a **failed** payment leaves the invoice open, writes no settlement, and is recorded
   with the gateway's reason
5. an **unmatched** payment is parked and reported rather than dropped, and a parked
   payment is placeable later — the path a late invoice is handled by
6. a payment in another currency, and one larger than what is open, are each parked
   with both figures in the reason
7. a malformed event is **refused at entry** — an unclassified outcome, no reference, a
   fee larger than the payment, an amount that is not an exact decimal — and the
   unclassified one settles nothing and records no payment
8. the settlement row cannot be appended twice for one source, and cannot be edited
   (append-only, T-0.AUDIT.01)
9. the report accounts for every payment by state, with the unplaced ones spelled out

6b. the money is **attributed to a customer** where the event or the invoice says so —
   and parked with its reason where nobody known is named

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ar.gateway import (  # noqa: E402
    FAILED,
    PARKED,
    PARTIAL,
    SETTLED,
    GatewayError,
    UnknownGatewayOutcome,
    match,
    parked_payments,
    payment_report,
    payments_for,
    record_payment,
)
from app.ar.invoices import (  # noqa: E402
    CustomerInvoice,
    CustomerInvoiceSettlement,
    create_invoice,
    open_amount,
    post_invoice,
)
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.integrations import InboundEvent  # noqa: E402
from app.ledger.accounts import create_account  # noqa: E402
from app.ledger.currency import register_currency  # noqa: E402
from app.ledger.mapping import set_mapping  # noqa: E402
from app.ledger.posting import JournalEntry, JournalLine  # noqa: E402
from app.sales.customers import create_customer  # noqa: E402
from app.sales.fulfilment import Shipment  # noqa: E402,F401 — for its table
from app.sales.orders import SalesOrder  # noqa: E402,F401 — for its table
from app.sales.pipeline import Opportunity  # noqa: E402,F401 — for its table
from tests.seed import seed_accounts  # noqa: E402

COMPANY = uuid.uuid4()
PAID_ON = date(2026, 11, 20)
VAT = Decimal("1.12")


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


def _event(reference, *, invoice=None, amount, fee=0, status="succeeded", currency="PHP",
           customer=None):
    return {
        "reference": reference,
        "status": status,
        "amount": str(amount),
        "fee": str(fee),
        "invoice": invoice,
        "customer": customer,
        "currency": currency,
        "on": PAID_ON.isoformat(),
    }


def _entry_of(session: Session, payment) -> JournalEntry:
    entry = session.get(JournalEntry, payment.journal_entry_id)
    assert entry is not None, f"payment {payment.reference!r} posted nothing"
    return entry


def _lines(session: Session, entry: JournalEntry) -> list[JournalLine]:
    return list(
        session.scalars(
            select(JournalLine)
            .where(JournalLine.entry_id == entry.id)
            .order_by(JournalLine.line_no)
        )
    )


def _balanced(session: Session, entry: JournalEntry) -> tuple[Decimal, Decimal, list]:
    lines = _lines(session, entry)
    debit = sum((line.debit for line in lines), Decimal(0))
    credit = sum((line.credit for line in lines), Decimal(0))
    assert debit == credit, f"entry {entry.id} does not balance: {debit} != {credit}"
    assert len(lines) >= 2, f"entry {entry.id} has {len(lines)} line(s)"
    return debit, credit, lines


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
                code="GATEWAY",
                name="Gateway",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        register_currency(session, company_id=COMPANY, code="PHP", name="Peso")
        register_currency(session, company_id=COMPANY, code="USD", name="US Dollar")
        session.commit()
        seed_accounts(session, company_id=COMPANY)
        create_account(session, company_id=COMPANY, code="2200", name="Output VAT",
                       account_class="liability")
        # The pack states no account for what a gateway charges to collect; the company
        # states one and maps the key to it, which is the whole point of the mapping.
        create_account(session, company_id=COMPANY, code="5610",
                       name="Bank and Payment Charges", account_class="expense")
        set_mapping(session, company_id=COMPANY, key="receivables", account_code="1100")
        set_mapping(session, company_id=COMPANY, key="revenue", account_code="4000")
        set_mapping(session, company_id=COMPANY, key="output_tax", account_code="2200")
        set_mapping(session, company_id=COMPANY, key="bank", account_code="1010")
        set_mapping(session, company_id=COMPANY, key="payment_fees", account_code="5610")
        session.commit()

        acme = create_customer(session, company_id=COMPANY, party_code="ACME",
                               name="Acme Retail", payment_terms_days=30)
        # A second customer, for the case where a payment is attributed to one and
        # names the other's invoice.
        other = create_customer(session, company_id=COMPANY, party_code="ACME-2",
                                name="Acme Two", payment_terms_days=30)
        session.commit()

        def invoice(number: str, net: str, *, customer=None) -> CustomerInvoice:
            made = create_invoice(
                session, company_id=COMPANY, number=number, customer=customer or acme,
                invoice_date=PAID_ON,
                lines=[{"description": "Goods", "quantity": "1", "unit_price": net}],
            )
            session.commit()
            post_invoice(session, made)
            session.commit()
            return made

        first = invoice("AR-1", "1000.00")
        second = invoice("AR-2", "200.00")
        third = invoice("AR-3", "300.00")

        # 1 — a successful payment settles and posts the fee apart
        payment, duplicate = record_payment(
            session, company_id=COMPANY, event_key="evt-1",
            payload=_event("PAY-1", invoice="AR-1", amount=_gross("1000"), fee="20.00"),
        )
        session.commit()
        assert duplicate is False, duplicate
        assert payment.status == SETTLED, payment.status
        assert payment.invoice_id == first.id, payment.invoice_id
        assert open_amount(session, first) == Decimal("0.000000"), open_amount(session, first)
        entry = _entry_of(session, payment)
        debit, credit, lines = _balanced(session, entry)
        assert debit == _gross("1000") and credit == _gross("1000"), (debit, credit)
        moved = {}
        for line in lines:
            moved[line.account] = moved.get(line.account, Decimal(0)) + line.debit - line.credit
        assert moved["1010"] == _gross("1000") - Decimal("20.00"), moved
        assert moved["5610"] == Decimal("20.00"), moved
        assert moved["1100"] == -_gross("1000"), moved
        assert entry.currency == "PHP" and entry.source_type == "gateway_payment"
        print(
            f"1. the payment settled AR-1 ({_gross('1000')}) and posted one balanced"
            f" entry: bank debited {moved['1010']}, the gateway's fee debited"
            f" {moved['5610']} and receivables credited {_gross('1000')}"
        )

        # 2 — the same webhook twice, and a second event key for the same payment
        again, repeated = record_payment(
            session, company_id=COMPANY, event_key="evt-1",
            payload=_event("PAY-1", invoice="AR-1", amount=_gross("1000"), fee="20.00"),
        )
        session.commit()
        assert repeated is True and again.id == payment.id, repeated
        second_event, acknowledged = record_payment(
            session, company_id=COMPANY, event_key="evt-1b",
            payload=_event("PAY-1", invoice="AR-1", amount=_gross("1000"), fee="20.00"),
        )
        session.commit()
        assert acknowledged is True and second_event.id == payment.id, acknowledged
        settlements = session.scalars(
            select(CustomerInvoiceSettlement).where(
                CustomerInvoiceSettlement.invoice_id == first.id
            )
        ).all()
        assert len(settlements) == 1, f"{len(settlements)} settlements for one payment"
        entries = session.scalars(
            select(JournalEntry).where(JournalEntry.source_type == "gateway_payment")
        ).all()
        assert len(entries) == 1, f"{len(entries)} entries for one payment"
        deliveries = session.scalar(select(func.count()).select_from(InboundEvent))
        print(
            f"2. the replayed webhook settled nothing again (repeated={repeated}), a"
            f" second event key describing the same payment was answered from the"
            f" payment row (settled={second_event.status}), and the invoice still has"
            f" {len(settlements)} settlement and {len(entries)} entry, from"
            f" {deliveries} recorded deliveries"
        )

        # 3 — a partial payment leaves the right open amount
        partial, _ = record_payment(
            session, company_id=COMPANY, event_key="evt-2",
            payload=_event("PAY-2", invoice="AR-2", amount="100.00"),
        )
        session.commit()
        assert partial.status == PARTIAL, partial.status
        assert open_amount(session, second) == (_gross("200") - Decimal("100")), (
            open_amount(session, second)
        )
        debit, credit, _ = _balanced(session, _entry_of(session, partial))
        assert debit == Decimal("100.00"), debit
        print(
            f"3. a 100.00 payment against a {_gross('200')} invoice left"
            f" {open_amount(session, second)} open and is recorded as {partial.status}"
        )

        # 4 — a failed payment leaves the invoice open and is recorded
        failed, _ = record_payment(
            session, company_id=COMPANY, event_key="evt-3",
            payload=_event("PAY-3", invoice="AR-3", amount=_gross("300"),
                           status="failed", currency="PHP")
            | {"reason": "insufficient funds"},
        )
        session.commit()
        assert failed.status == FAILED, failed.status
        assert "insufficient funds" in failed.reason, failed.reason
        assert open_amount(session, third) == _gross("300"), open_amount(session, third)
        assert session.scalars(
            select(CustomerInvoiceSettlement).where(
                CustomerInvoiceSettlement.invoice_id == third.id
            )
        ).all() == [], "a failed payment wrote a settlement"
        assert failed.journal_entry_id is None, "a failed payment posted an entry"
        print(
            f"4. the failed payment left AR-3 open at {open_amount(session, third)},"
            f" wrote no settlement and no entry, and is recorded"
            f" ({failed.status}: {failed.reason})"
        )

        # 5 — an unmatched payment is parked, reported, and placeable later
        parked, _ = record_payment(
            session, company_id=COMPANY, event_key="evt-4",
            payload=_event("PAY-4", invoice="AR-LATER", amount="56.00"),
        )
        session.commit()
        assert parked.status == PARKED, parked.status
        assert "no customer invoice 'AR-LATER'" in parked.reason, parked.reason
        assert [row.reference for row in parked_payments(session, company_id=COMPANY)] == [
            "PAY-4"
        ], parked_payments(session, company_id=COMPANY)
        later = invoice("AR-LATER", "50.00")
        assert match(session, parked) is parked
        session.commit()
        assert parked.status == SETTLED, parked.status
        assert parked.invoice_id == later.id, parked.invoice_id
        # Placing it late, the invoice that made it placeable also says whose it is.
        assert parked.customer_id == acme.id, parked.customer_id
        assert open_amount(session, later) == Decimal("0.000000")
        assert parked_payments(session, company_id=COMPANY) == [], "the parked payment stayed parked"
        print(
            f"5. the unmatched payment was parked with its reason and reported; when"
            f" AR-LATER existed, matching it settled the invoice ({parked.status}) and"
            " emptied the parked list"
        )

        # 6 — the wrong currency, and more than is open, are parked with both figures
        wrong_currency, _ = record_payment(
            session, company_id=COMPANY, event_key="evt-5",
            payload=_event("PAY-5", invoice="AR-3", amount="10.00", currency="USD"),
        )
        session.commit()
        assert wrong_currency.status == PARKED, wrong_currency.status
        assert "USD" in wrong_currency.reason and "PHP" in wrong_currency.reason, (
            wrong_currency.reason
        )
        too_much, _ = record_payment(
            session, company_id=COMPANY, event_key="evt-6",
            payload=_event("PAY-6", invoice="AR-3", amount=str(_gross("300") + 1)),
        )
        session.commit()
        assert too_much.status == PARKED, too_much.status
        assert str(_gross("300")) in too_much.reason, too_much.reason
        assert open_amount(session, third) == _gross("300"), "an unplaceable payment settled"
        print(
            f"6. the USD payment ({wrong_currency.reason[:52]}…) and the over-payment"
            f" ({too_much.reason[:44]}…) are each parked with both figures in the reason,"
            " and AR-3 is untouched"
        )

        # 6b — whose money it is, is recorded with the payment
        assert wrong_currency.customer_id == third.customer_id, (
            "the invoice's own customer did not attribute the parked payment"
        )
        assert too_much.customer_id == third.customer_id, too_much.customer_id
        on_account, _ = record_payment(
            session, company_id=COMPANY, event_key="evt-6b",
            payload=_event("PAY-ON-ACCOUNT", amount="75.00", customer="ACME"),
        )
        session.commit()
        assert on_account.status == PARKED, on_account.status
        assert on_account.customer_id == acme.id, on_account.customer_id
        nobodies, _ = record_payment(
            session, company_id=COMPANY, event_key="evt-6c",
            payload=_event("PAY-NOBODY", amount="90.00"),
        )
        session.commit()
        assert nobodies.status == PARKED and nobodies.customer_id is None, nobodies
        assert "no invoice was named" in nobodies.reason, nobodies.reason
        unknown, _ = record_payment(
            session, company_id=COMPANY, event_key="evt-6d",
            payload=_event("PAY-UNKNOWN", amount="5.00", customer="NOBODY"),
        )
        session.commit()
        assert unknown.status == PARKED and unknown.customer_id is None, unknown
        assert "NOBODY" in unknown.reason, unknown.reason
        rivalled = invoice("AR-OTHER", "20.00", customer=other)
        mismatched, _ = record_payment(
            session, company_id=COMPANY, event_key="evt-6e",
            payload=_event("PAY-MM", invoice="AR-OTHER", amount="10.00", customer="ACME"),
        )
        session.commit()
        # Parked, and still attributed to whoever the event named: nothing here
        # quietly moves the money to the invoice's customer instead.
        assert mismatched.status == PARKED and mismatched.customer_id == acme.id, (
            mismatched, mismatched.reason
        )
        assert "another customer" in mismatched.reason, mismatched.reason
        assert open_amount(session, rivalled) == _gross("20"), (
            "a mismatched payment settled somebody else's invoice"
        )
        print(
            f"6b. the money is attributed where it can be: the payments parked on AR-3"
            f" carry the invoice's customer, the on-account receipt names ACME, and the"
            f" one naming somebody unknown is parked instead of guessed at"
            f" ({unknown.reason[:40]}…), and one naming a customer that is not the"
            f" invoice's is parked rather than settling the wrong account"
        )

        # 7 — malformed events are refused at entry
        before = session.scalar(select(func.count()).select_from(InboundEvent))
        unclassified = _refused(
            lambda: record_payment(
                session, company_id=COMPANY, event_key="evt-7",
                payload=_event("PAY-7", invoice="AR-3", amount="10.00", status="pending"),
            ),
            UnknownGatewayOutcome,
        )
        session.rollback()
        unreferenced = _refused(
            lambda: record_payment(session, company_id=COMPANY, event_key="evt-8",
                                   payload=_event("", invoice="AR-3", amount="10.00")),
            GatewayError,
        )
        session.rollback()
        fee_too_big = _refused(
            lambda: record_payment(
                session, company_id=COMPANY, event_key="evt-9",
                payload=_event("PAY-9", invoice="AR-3", amount="10.00", fee="11.00"),
            ),
            GatewayError,
        )
        session.rollback()
        inexact = _refused(
            lambda: record_payment(
                session, company_id=COMPANY, event_key="evt-10",
                payload={"reference": "PAY-10", "status": "succeeded", "amount": 10.5},
            ),
            GatewayError,
        )
        session.rollback()
        assert session.scalar(select(func.count()).select_from(InboundEvent)) == before, (
            "a refused settlement recorded a delivery for an event it never processed"
        )
        assert open_amount(session, third) == _gross("300"), "a malformed event settled"
        print(
            f"7. an unclassified outcome ({unclassified[:38]}…), no reference"
            f" ({unreferenced[:32]}…), a fee above the payment ({fee_too_big[:32]}…) and an"
            f" inexact amount ({inexact[:36]}…) are each refused before anything is"
            " recorded, and AR-3 stays open"
        )

        # 8 — one settlement per source, and a settlement is history
        session.add(
            CustomerInvoiceSettlement(
                company_id=COMPANY, invoice_id=first.id, settled_on=PAID_ON,
                amount=Decimal("1"), source_type="gateway_payment", source_id=payment.id,
            )
        )
        try:
            session.commit()
        except DBAPIError as exc:
            assert "uq_customer_settlement_once_per_source" in str(exc), exc
            session.rollback()
        else:
            raise AssertionError("the database accepted a second settlement for one source")
        try:
            session.execute(
                CustomerInvoiceSettlement.__table__.update().values(amount=Decimal("1"))
            )
            session.commit()
        except DBAPIError as exc:
            assert "append-only" in str(exc), exc
            session.rollback()
        else:
            raise AssertionError("a settlement was edited")
        print(
            "8. the database refuses a second settlement for one source"
            " (uq_customer_settlement_once_per_source) and refuses to edit the one it has"
        )

        # 9 — the report accounts for every payment
        report = payment_report(session, company_id=COMPANY)
        # Two parked on AR-3, and the four 6b added: the on-account receipt, the one
        # naming nobody, the one naming a party this company does not have, and the
        # one naming a customer that is not its invoice's.
        assert report["counts"] == {SETTLED: 2, PARTIAL: 1, PARKED: 6, FAILED: 1}, (
            report["counts"]
        )
        assert report["collected"] == (_gross("1000") + Decimal("100") + Decimal("56")), (
            report["collected"]
        )
        assert {row["status"] for row in report["unplaced"]} == {PARKED, FAILED}, report
        assert len(payments_for(session, first)) == 1, payments_for(session, first)
        print(
            f"9. the report counts every payment by state {report['counts']}, states"
            f" {report['collected']} collected, and spells out the"
            f" {len(report['unplaced'])} unplaced ones with their reasons"
        )

    print("\ncheck_gateway_payments: all assertions green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
