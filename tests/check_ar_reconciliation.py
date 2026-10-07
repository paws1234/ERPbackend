"""T-3.AR.07 check — the AR subledger against the receivables control account.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_ar_reconciliation.py

Green on all seven:

1. a clean reconciliation: the subledger's open balances equal the control account **to
   currency precision**, per currency, with the difference stated
2. a **partial receipt** moves both sides by the same amount, because the subledger side
   is the settlements themselves
3. a **gateway settlement** with a fee does too — the fee is a debit of its own and
   never touches receivables, so it cannot drag the two apart
4. an **injected mismatch is reported**, never absorbed: a posting made straight to the
   control account shows up as a difference in that currency and only that one
5. a second **currency** is reconciled in its own currency, and one side being empty is
   still reported rather than skipped
6. a **period** reconciles as a period — both sides measuring the window's movement,
   with a window in which nothing moved balancing at zero while the position to date
   does not
7. the reconciliation says what it found in words a person can act on

5b. an invoice whose **document date and posting date differ** reconciles on both dates

6b. a window that ends **before it starts** is refused rather than reconciled

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ar.gateway import record_payment  # noqa: E402
from app.ar.invoices import create_invoice, post_invoice, settle  # noqa: E402
from app.ar.reconciliation import (  # noqa: E402
    control_balance,
    ReconciliationError,
    currencies_in_use,
    explain,
    reconcile,
    subledger_balance,
    subledger_movement,
)
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger.accounts import create_account  # noqa: E402
from app.ledger.currency import register_currency, store_rate  # noqa: E402
from app.ledger.mapping import set_mapping  # noqa: E402
from app.ledger.posting import post_journal_entry  # noqa: E402
from app.sales.customers import create_customer  # noqa: E402
from app.sales.fulfilment import Shipment  # noqa: E402,F401 — for its table
from app.sales.orders import SalesOrder  # noqa: E402,F401 — for its table
from app.sales.pipeline import Opportunity  # noqa: E402,F401 — for its table
from tests.seed import seed_accounts  # noqa: E402

COMPANY = uuid.uuid4()
DAY = date(2026, 9, 10)
VAT = Decimal("1.12")


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
                code="AR-RECON",
                name="AR reconciliation",
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
        create_account(session, company_id=COMPANY, code="5610",
                       name="Bank and Payment Charges", account_class="expense")
        set_mapping(session, company_id=COMPANY, key="receivables", account_code="1100")
        set_mapping(session, company_id=COMPANY, key="revenue", account_code="4000")
        set_mapping(session, company_id=COMPANY, key="output_tax", account_code="2200")
        set_mapping(session, company_id=COMPANY, key="bank", account_code="1010")
        set_mapping(session, company_id=COMPANY, key="payment_fees", account_code="5610")
        store_rate(session, company_id=COMPANY, base_currency="PHP", currency="USD",
                   on=DAY, rate="58.5")
        session.commit()

        acme = create_customer(session, company_id=COMPANY, party_code="ACME",
                               name="Acme Retail", payment_terms_days=30)
        session.commit()

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

        first = invoice("AR-1", "1000.00")
        second = invoice("AR-2", "200.00")

        # 1 — a clean reconciliation
        clean = reconcile(session, company_id=COMPANY, as_of=DAY)
        assert clean["balanced"] is True, clean
        php = next(row for row in clean["currencies"] if row["currency"] == "PHP")
        assert php["subledger"] == (_gross("1000") + _gross("200")), php
        assert php["control"] == php["subledger"], php
        assert php["difference"] == Decimal("0.000000"), php
        assert currencies_in_use(session, company_id=COMPANY) == ["PHP"], (
            currencies_in_use(session, company_id=COMPANY)
        )
        print(
            f"1. the subledger's {php['subledger']} equals the control account's"
            f" {php['control']} to the last decimal, difference {php['difference']}"
        )

        # 2 — a partial receipt moves both sides together
        settle(session, first, amount="400.00", settled_on=DAY, source_type="receipt",
               source_id=uuid.uuid4())
        post_journal_entry(
            session, company_id=COMPANY, posting_date=DAY, currency="PHP",
            memo="partial receipt against AR-1", source_type="receipt",
            source_id=uuid.uuid4(),
            lines=[{"account": "1010", "debit": Decimal("400")},
                   {"account": "1100", "credit": Decimal("400")}],
        )
        session.commit()
        partial = reconcile(session, company_id=COMPANY, as_of=DAY)
        php = next(row for row in partial["currencies"] if row["currency"] == "PHP")
        expected = _gross("1000") - Decimal("400") + _gross("200")
        assert php["subledger"] == expected, php
        assert php["control"] == expected, php
        assert php["balanced"] is True, php
        print(
            f"2. the 400.00 partial receipt moved both sides to {expected} — the"
            " subledger side being the settlement itself, not a separate figure"
        )

        # 3 — a gateway settlement with a fee
        record_payment(
            session, company_id=COMPANY, event_key="evt-recon",
            payload={"reference": "PAY-1", "status": "succeeded", "amount": "150.00",
                     "fee": "7.50", "invoice": "AR-2", "currency": "PHP",
                     "on": DAY.isoformat()},
        )
        session.commit()
        with_fee = reconcile(session, company_id=COMPANY, as_of=DAY)
        php = next(row for row in with_fee["currencies"] if row["currency"] == "PHP")
        expected = _gross("1000") - Decimal("400") + _gross("200") - Decimal("150")
        assert php["subledger"] == expected, php
        assert php["control"] == expected, php
        assert php["balanced"] is True, php
        assert control_balance(
            session, company_id=COMPANY, currency="PHP", as_of=DAY
        ) == expected
        print(
            f"3. the gateway settlement (150.00 with a 7.50 fee debited to the fee"
            f" account) left both sides at {expected} — the fee never touches"
            " receivables, so it cannot drag them apart"
        )

        # 4 — an injected mismatch is reported, not absorbed
        post_journal_entry(
            session, company_id=COMPANY, posting_date=DAY, currency="PHP",
            memo="injected straight to the control account", source_type="manual",
            source_id=uuid.uuid4(),
            lines=[{"account": "1100", "debit": Decimal("250")},
                   {"account": "4000", "credit": Decimal("250")}],
        )
        session.commit()
        injected = reconcile(session, company_id=COMPANY, as_of=DAY)
        php = next(row for row in injected["currencies"] if row["currency"] == "PHP")
        assert injected["balanced"] is False, injected
        assert php["difference"] == Decimal("-250.000000"), php
        assert php["subledger"] == expected, php
        assert php["control"] == expected + 250, php
        print(
            f"4. a 250.00 posting made straight to the control account is reported as a"
            f" difference of {php['difference']} in PHP — the two figures and the gap"
            " between them, not a corrected balance"
        )

        # 5 — a second currency reconciles in its own currency
        invoice("AR-3", "500.00", currency="USD")
        both = reconcile(session, company_id=COMPANY, as_of=DAY)
        assert currencies_in_use(session, company_id=COMPANY) == ["PHP", "USD"], (
            currencies_in_use(session, company_id=COMPANY)
        )
        usd = next(row for row in both["currencies"] if row["currency"] == "USD")
        assert usd["subledger"] == _gross("500"), usd
        assert usd["control"] == _gross("500"), usd
        assert usd["balanced"] is True, usd
        php = next(row for row in both["currencies"] if row["currency"] == "PHP")
        assert php["difference"] == Decimal("-250.000000"), php
        assert subledger_balance(
            session, company_id=COMPANY, currency="USD", as_of=DAY
        ) == _gross("500")
        print(
            f"5. the USD invoice reconciles in USD ({usd['subledger']} ="
            f" {usd['control']}) and the PHP difference is untouched by it"
            f" ({php['difference']})"
        )

        # 6 — a period is reconciled as a period: both sides measure the movement
        assert reconcile(session, company_id=COMPANY, as_of=DAY)["measure"] == "position"
        window = reconcile(session, company_id=COMPANY, as_of=DAY, start=DAY)
        php_window = next(row for row in window["currencies"] if row["currency"] == "PHP")
        assert window["start"] == DAY and window["measure"] == "period", window
        # What moved inside the window: the two PHP invoices raised in it (1120 + 224)
        # less the partial receipt's 400 and the gateway settlement's 150. The USD
        # invoice is a different currency and is not in this figure; the injected 250
        # is a control-account posting, so it is in the control figure and not in the
        # subledger's — which is the difference this report exists to state.
        moved = _gross("1000") + _gross("200") - Decimal("400") - Decimal("150")
        assert subledger_movement(
            session, company_id=COMPANY, currency="PHP", start=DAY, as_of=DAY
        ) == moved, subledger_movement(
            session, company_id=COMPANY, currency="PHP", start=DAY, as_of=DAY
        )
        assert control_balance(
            session, company_id=COMPANY, currency="PHP", as_of=DAY, start=DAY
        ) == moved + Decimal("250"), control_balance(
            session, company_id=COMPANY, currency="PHP", as_of=DAY, start=DAY
        )
        assert php_window["subledger"] == moved, php_window
        assert php_window["control"] == moved + Decimal("250"), php_window
        # The same 250 the position-to-date comparison reported, stated against the
        # window's own movement rather than against a position.
        assert php_window["difference"] == Decimal("-250.000000"), php_window

        # A window in which nothing moved balances at zero on both sides, while the
        # position to date does not: a position compared with a movement would call
        # this correct period a difference of everything outstanding.
        quiet = reconcile(
            session, company_id=COMPANY, as_of=DAY + timedelta(days=5),
            start=DAY + timedelta(days=2),
        )
        php_quiet = next(row for row in quiet["currencies"] if row["currency"] == "PHP")
        assert quiet["balanced"] is True, quiet
        assert (php_quiet["subledger"], php_quiet["control"]) == (
            Decimal(0), Decimal(0)
        ), php_quiet
        assert php_quiet["difference"] == Decimal(0), php_quiet
        outstanding = subledger_balance(
            session, company_id=COMPANY, currency="PHP", as_of=DAY + timedelta(days=5)
        )
        assert outstanding != 0, outstanding
        print(
            f"6. a window is reconciled as a window: {DAY}..{DAY} moved {moved} on both"
            f" sides with the injected 250 reported as {php_window['difference']}, and"
            f" the quiet {DAY + timedelta(days=2)}..{DAY + timedelta(days=5)} balances at"
            f" zero on both sides while {outstanding} is still outstanding — a position"
            " and a movement are not the same figure"
        )

        # 5b — a document dated one day and posted another reconciles on both dates
        skewed = create_invoice(
            session, company_id=COMPANY, number="AR-SKEWED", customer=acme,
            invoice_date=DAY + timedelta(days=4),
            lines=[{"description": "Goods", "quantity": "1", "unit_price": "100.00"}],
        )
        session.commit()
        post_invoice(session, skewed, posting_date=DAY)
        session.commit()
        on_document_date = reconcile(
            session, company_id=COMPANY, as_of=DAY, start=DAY
        )
        php_dated = next(
            row for row in on_document_date["currencies"] if row["currency"] == "PHP"
        )
        # The invoice is dated four days after the day it was posted, so a date-of-document
        # sweep would leave it out of the subledger while the control account holds it.
        assert subledger_balance(
            session, company_id=COMPANY, currency="PHP", as_of=DAY
        ) == expected + _gross("100"), subledger_balance(
            session, company_id=COMPANY, currency="PHP", as_of=DAY
        )
        assert php_dated["difference"] == Decimal("-250.000000"), php_dated
        later = reconcile(
            session, company_id=COMPANY, as_of=DAY + timedelta(days=4), start=DAY
        )
        php_later = next(
            row for row in later["currencies"] if row["currency"] == "PHP"
        )
        assert php_later["subledger"] == (expected + _gross("100")), php_later
        print(
            f"5b. an invoice dated four days after the entry it posted reconciles on both"
            f" dates: the subledger reads {php_later['subledger']} because it follows the"
            " posting, not the date printed on the document"
        )

        # 6b — a window that ends before it starts is refused
        try:
            reconcile(
                session, company_id=COMPANY, as_of=DAY, start=DAY + timedelta(days=1)
            )
        except ReconciliationError as refusal:
            backwards = str(refusal)
        else:
            raise AssertionError("a window that ends before it starts was reconciled")
        session.rollback()
        print(f"6b. a window that ends before it starts is refused ({backwards[:52]}…)")

        # 7 — the report says what it found
        said = explain(injected)
        assert "NOT balanced" in said and "differs by -250.000000" in said, said
        assert "USD" in explain(both) and "agrees" in explain(both), explain(both)
        print(f"7. the report reads: {said}")

    print("\ncheck_ar_reconciliation: all assertions green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
