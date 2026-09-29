"""T-2.AP.05 check — the payables subledger against the control account.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/erpv1 \
        python tests/check_ap_reconciliation.py

Green on all seven:

1. a posted invoice's open amount and the control account's balance agree **exactly**,
   to currency precision
2. a **partial settlement** moves both sides together — the subledger falls by what was
   paid and so does the control account, so they still agree
3. a **debit note** moves both sides too, for the same reason
4. an **injected mismatch** is *reported* — a difference, `balanced: False` and a line
   naming the currency — never corrected here
5. the comparison is **per currency**: a USD invoice is compared against the USD entry
   it produced, and its difference is reported separately from the PHP one
6. the period narrows the control side, so a reconciliation for a period that includes
   nothing reports the whole open balance as a difference rather than silently agreeing
7. nothing to compare says so, and a date cutoff on the subledger is honoured

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ap.debit_notes import create_debit_note, post_debit_note  # noqa: E402
from app.ap.invoices import (  # noqa: E402
    PAYABLES_KEY,
    create_invoice,
    open_amount,
    post_invoice,
    settle,
)
from app.ap.reconciliation import (  # noqa: E402
    control_balance,
    currencies_in_use,
    explain,
    reconcile,
    subledger_balance,
)
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger.accounts import create_account  # noqa: E402
from app.ledger.currency import register_currency, store_rate  # noqa: E402
from app.ledger.mapping import mapped_account, set_mapping  # noqa: E402
from app.ledger.posting import post_journal_entry  # noqa: E402
from app.procurement import receipts as _receipts  # noqa: E402,F401 — the FK target
from app.procurement.suppliers import create_supplier  # noqa: E402

COMPANY = uuid.uuid4()
OTHER = uuid.uuid4()
INVOICE_DATE = date(2026, 11, 5)
BATCH = uuid.uuid4()


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
        for company_id, code in ((COMPANY, "RECON"), (OTHER, "OTHER-CO")):
            session.add(
                Company(id=company_id, code=code, name=f"{code} company",
                        base_currency="PHP", fiscal_year_start_month=1)
            )
            register_currency(session, company_id=company_id, code="PHP", name="Peso")
            register_currency(session, company_id=company_id, code="USD", name="US Dollar")
        session.commit()
        store_rate(session, company_id=COMPANY, base_currency="PHP", currency="USD",
                   on=INVOICE_DATE, rate="58.5")
        session.commit()
        for company_id in (COMPANY, OTHER):
            create_account(session, company_id=company_id, code="2000",
                           name="Accounts Payable", account_class="liability")
            create_account(session, company_id=company_id, code="5200",
                           name="Rent Expense", account_class="expense")
            create_account(session, company_id=company_id, code="1010",
                           name="Cash in Bank", account_class="asset")
            set_mapping(session, company_id=company_id, key="payables", account_code="2000")
            set_mapping(session, company_id=company_id, key="expense", account_code="5200")
        create_account(session, company_id=COMPANY, code="1310", name="Input VAT",
                       account_class="asset")
        set_mapping(session, company_id=COMPANY, key="input_tax", account_code="1310")
        session.commit()
        supplier = create_supplier(session, company_id=COMPANY, party_code="ACME",
                                   name="Acme Supplies", payment_terms_days=30)
        # a second company's own books, to prove the comparison is per company
        other_supplier = create_supplier(session, company_id=OTHER, party_code="BETA",
                                         name="Beta", payment_terms_days=30)
        session.commit()

        def _invoice(number, *, net, currency=None, tax="0", company_id=COMPANY,
                     who=None):
            invoice = create_invoice(
                session, company_id=company_id, number=number,
                supplier=who or supplier, supplier_reference=number,
                invoice_date=INVOICE_DATE, currency=currency,
                lines=[{"description": "Goods", "quantity": "1", "unit_price": net,
                        "tax_amount": tax}],
            )
            session.commit()
            post_invoice(session, invoice)
            session.commit()
            return invoice

        first = _invoice("AP-501", net="1000.00", tax="120.00")
        # the other company's invoice must not appear here
        _invoice("AP-999", net="5000.00", company_id=OTHER, who=other_supplier)

        # 1 — the two sides agree
        report = reconcile(session, company_id=COMPANY, as_of=INVOICE_DATE)
        assert report["balanced"] is True, report
        assert [row["currency"] for row in report["currencies"]] == ["PHP"], report
        row = report["currencies"][0]
        assert row["subledger"] == row["control"] == Decimal("1120.000000"), row
        assert row["difference"] == Decimal("0.000000")
        assert open_amount(session, first) == Decimal("1120.000000")
        assert "agree" in explain(report)
        print(f"1. the subledger and the control account both read {row['subledger']}"
              f" — {explain(report)}")

        # 2 — a partial settlement moves both sides
        settle(session, first, amount="500.00", settled_on=date(2026, 11, 15),
               source_type="payment_batch", source_id=BATCH)
        post_journal_entry(
            session,
            company_id=COMPANY,
            posting_date=date(2026, 11, 15),
            currency="PHP",
            memo="payment",
            source_type="payment_batch",
            source_id=BATCH,
            lines=[
                {"account": "2000", "debit": Decimal("500.00")},
                {"account": "1010", "credit": Decimal("500.00")},
            ],
        )
        session.commit()
        after = reconcile(session, company_id=COMPANY, as_of=date(2026, 11, 30))
        assert after["balanced"] is True, after
        assert after["currencies"][0]["subledger"] == Decimal("620.000000"), after
        assert after["currencies"][0]["control"] == Decimal("620.000000"), after
        print(f"2. settling 500.00 left both sides at {after['currencies'][0]['subledger']}"
              " — they move together")

        # 3 — a debit note moves both sides
        note = create_debit_note(
            session, number="DN-501", invoice=first, note_date=date(2026, 11, 20),
            kind="adjustment",
            lines=[{"description": "Adjustment", "quantity": "1", "unit_price": "20"}],
        )
        session.commit()
        post_debit_note(session, note)
        session.commit()
        noted = reconcile(session, company_id=COMPANY, as_of=date(2026, 11, 30))
        assert noted["balanced"] is True, noted
        assert noted["currencies"][0]["subledger"] == Decimal("600.000000"), noted
        print(f"3. the 20.00 debit note took both sides to"
              f" {noted['currencies'][0]['subledger']} as well")

        # 5 — per currency: a USD invoice is compared against its own entry
        usd = _invoice("AP-502", net="200.00", currency="USD", tax="24.00")
        both = reconcile(session, company_id=COMPANY, as_of=date(2026, 11, 30))
        assert both["balanced"] is True, both
        assert [row["currency"] for row in both["currencies"]] == ["PHP", "USD"]
        php_row, usd_row = both["currencies"]
        assert php_row["subledger"] == Decimal("600.000000")
        assert usd_row["subledger"] == usd_row["control"] == Decimal("224.000000"), usd_row
        assert open_amount(session, usd) == Decimal("224.000000")
        assert currencies_in_use(session, company_id=COMPANY) == ["PHP", "USD"]
        assert subledger_balance(session, company_id=COMPANY, currency="USD") == Decimal(
            "224.000000"
        )
        assert control_balance(session, company_id=COMPANY, currency="PHP") == Decimal(
            "600.000000"
        )
        print(f"5. PHP {php_row['subledger']} and USD {usd_row['subledger']} are compared"
              " separately, each against the entry it produced")

        # 4 — an injected mismatch is reported, never corrected
        injected = post_journal_entry(
            session,
            company_id=COMPANY,
            posting_date=date(2026, 11, 25),
            currency="PHP",
            memo="an entry somebody posted straight to the control account",
            source_type="manual",
            source_id=uuid.uuid4(),
            lines=[
                {"account": "2000", "credit": Decimal("300.00")},
                {"account": "5200", "debit": Decimal("300.00")},
            ],
        )
        session.commit()
        broken = reconcile(session, company_id=COMPANY, as_of=date(2026, 11, 30))
        assert broken["balanced"] is False, broken
        php_row = broken["currencies"][0]
        assert php_row["difference"] == Decimal("-300.000000"), php_row
        assert php_row["subledger"] == Decimal("600.000000")
        assert php_row["control"] == Decimal("900.000000")
        assert "PHP: the subledger says 600.000000 is owed" in explain(broken)
        assert broken["difference_total"] == Decimal("300.000000")
        print(f"4. the injected 300.00 is reported, not absorbed: {explain(broken)}")

        # 6 — the period narrows the control side only
        windowed = reconcile(session, company_id=COMPANY, as_of=date(2026, 11, 30),
                             start=date(2026, 11, 26))
        assert windowed["currencies"][0]["control"] == Decimal("0.000000"), windowed
        assert windowed["balanced"] is False
        assert windowed["currencies"][0]["difference"] == Decimal("600.000000")
        print("6. a period starting after the last posting reads a control balance of"
              " 0.000000 against an open 600.000000 — reported, not quietly agreed")

        # 7 — a currency with nothing in it reads nil on both sides, and a company
        # with no invoices at all says there is nothing to compare
        empty = reconcile(session, company_id=COMPANY, currencies=["EUR"])
        assert empty["balanced"] is True, empty
        assert [row["currency"] for row in empty["currencies"]] == ["EUR"]
        assert empty["currencies"][0]["subledger"] == Decimal("0.000000")
        assert empty["currencies"][0]["control"] == Decimal("0.000000")
        nothing = reconcile(session, company_id=uuid.uuid4())
        assert nothing["balanced"] is True and nothing["currencies"] == []
        assert nothing["reason"] and "nothing to compare" in explain(nothing)
        before = reconcile(session, company_id=COMPANY, as_of=date(2026, 11, 1))
        assert before["balanced"] is True, before
        assert before["currencies"][0]["subledger"] == Decimal("0.000000"), before
        assert before["currencies"][0]["control"] == Decimal("0.000000"), before
        print(f"    an empty currency reads 0.000000 on both sides and a company with no"
              f" invoices says so ({explain(nothing)}); a date before every document"
              " reads nil too")

    print("check_ap_reconciliation: all assertions green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
