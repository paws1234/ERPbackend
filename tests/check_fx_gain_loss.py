"""T-1.ACCT.06 check — realized and unrealized FX gain/loss on the ledger.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/erpv1 \
        python tests/check_fx_gain_loss.py

Green on all eight:

1. a foreign payable booked at one rate and settled at a higher one posts the
   realized difference against the control account, offset to the **loss**
   account, and the entry balances
2. that difference equals `foreign amount × (settlement rate − booked rate)`
3. settling at the rate it was booked at posts nothing
4. revaluing an open foreign receivable restates it and posts the unrealized
   difference to the **gain** account, balanced
5. the difference equals `balance × (new rate − booked rate)` — the change in rate
   applied to the open balance
6. running the same revaluation again posts **nothing** (repeatable without
   double-counting)
7. reversing the revaluation posts its negation, and a re-run then posts the
   difference again (the reversal genuinely undid it)
8. the base currency does not revalue, and every entry produced balances

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger.currency import register_currency, store_rate  # noqa: E402
from app.ledger.fx_gain_loss import (  # noqa: E402
    FxGainLossError,
    REVALUATION,
    already_revalued,
    open_foreign_balance,
    reverse_revaluation,
    revalue_open_balance,
    settle_document,
)
from app.ledger.mapping import set_mapping  # noqa: E402
from app.ledger.posting import JournalEntry, post_journal_entry  # noqa: E402
from tests.check_ledger_integrity import ledger_gate  # noqa: E402
from tests.seed import seed_accounts  # noqa: E402

COMPANY = uuid.uuid4()
D1, D2 = date(2026, 9, 10), date(2026, 9, 19)
BOOKED_RATE = Decimal("58.5000000000")
HIGHER_RATE = Decimal("59.7500000000")
USD = Decimal("100.00")
AP, AR = "2000", "1100"


def _balances(entry: JournalEntry) -> None:
    debits = sum((line.debit for line in entry.lines), Decimal(0))
    credits = sum((line.credit for line in entry.lines), Decimal(0))
    assert debits == credits and debits > 0, f"{entry.memo}: {debits} != {credits}"


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
                code="FXGL-CHECK",
                name="FX gain/loss check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()
        seed_accounts(session, company_id=COMPANY)
        register_currency(session, company_id=COMPANY, code="PHP", name="Philippine Peso")
        register_currency(session, company_id=COMPANY, code="USD", name="US Dollar")
        set_mapping(session, company_id=COMPANY, key="fx_gain", account_code="4910")
        set_mapping(session, company_id=COMPANY, key="fx_loss", account_code="5990")
        for on, rate in ((D1, BOOKED_RATE), (D2, HIGHER_RATE)):
            store_rate(
                session,
                company_id=COMPANY,
                base_currency="PHP",
                currency="USD",
                on=on,
                rate=rate,
                today=D2,
            )
        session.commit()

        # a foreign payable: expense 100 USD, payable 100 USD, booked at 58.5
        vendor_bill = uuid.uuid4()
        payable = post_journal_entry(
            session,
            company_id=COMPANY,
            posting_date=D1,
            currency="USD",
            source_type="supplier_invoice",
            source_id=vendor_bill,
            lines=[
                {"account": "5000", "debit": USD},
                {"account": AP, "credit": USD},
            ],
        )
        session.commit()
        assert payable.exchange_rate == BOOKED_RATE

        # 1 + 2 — realized on settlement, at a rate the balance was not booked at
        realized = settle_document(
            session,
            company_id=COMPANY,
            document_type="supplier_invoice",
            document_id=vendor_bill,
            account_code=AP,
            settlement_rate="59.75",
            settlement_date=D2,
        )
        session.commit()
        _balances(realized)
        control = next(line for line in realized.lines if line.account == AP)
        loss = next(line for line in realized.lines if line.account == "5990")
        difference = loss.debit - control.debit
        expected = USD * (HIGHER_RATE - BOOKED_RATE)
        assert control.credit == Decimal("125.000000"), control.credit
        assert difference == Decimal("125.000000"), difference
        assert difference == expected.quantize(Decimal("0.000001")), (difference, expected)
        print(f"a payable settled higher posted a realized loss of {difference} on {control.account}")

        # 3 — settling at the booked rate posts nothing
        same_rate_bill = uuid.uuid4()
        post_journal_entry(
            session,
            company_id=COMPANY,
            posting_date=D1,
            currency="USD",
            source_type="supplier_invoice",
            source_id=same_rate_bill,
            lines=[
                {"account": "5000", "debit": USD},
                {"account": AP, "credit": USD},
            ],
        )
        session.commit()
        assert (
            settle_document(
                session,
                company_id=COMPANY,
                document_type="supplier_invoice",
                document_id=same_rate_bill,
                account_code=AP,
                settlement_rate=BOOKED_RATE,
                settlement_date=D2,
            )
            is None
        ), "a settlement at the booked rate posted an entry"
        session.rollback()
        print("settling at the booked rate posted nothing")

        # a foreign receivable, open at 58.5
        receivable = uuid.uuid4()
        post_journal_entry(
            session,
            company_id=COMPANY,
            posting_date=D1,
            currency="USD",
            source_type="sales_invoice",
            source_id=receivable,
            lines=[
                {"account": AR, "debit": USD},
                {"account": "4000", "credit": USD},
            ],
        )
        session.commit()
        foreign, booked = open_foreign_balance(
            session, company_id=COMPANY, account_code=AR, currency="USD", as_of=D2
        )
        assert (foreign, booked) == (USD, Decimal("5850.0000000000")), (foreign, booked)

        # 4 + 5 — unrealized on the open balance
        revaluation = revalue_open_balance(
            session, company_id=COMPANY, account_code=AR, currency="USD", as_of=D2
        )
        session.commit()
        _balances(revaluation)
        restated = next(line for line in revaluation.lines if line.account == AR)
        gain = next(line for line in revaluation.lines if line.account == "4910")
        assert restated.debit == Decimal("125.000000"), restated.debit
        assert gain.credit == Decimal("125.000000"), gain.credit
        assert restated.debit == (foreign * (HIGHER_RATE - BOOKED_RATE)).quantize(
            Decimal("0.000001")
        ), restated.debit
        print(f"the open receivable revalued: +{restated.debit} on {AR}, credited to 4910")

        # 6 — running it again posts nothing
        posted_before = already_revalued(
            session, company_id=COMPANY, account_code=AR, currency="USD", as_of=D2
        )
        assert (
            revalue_open_balance(
                session, company_id=COMPANY, account_code=AR, currency="USD", as_of=D2
            )
            is None
        ), "the second revaluation of the same date posted again"
        session.rollback()
        revaluations = session.scalar(
            select(func.count())
            .select_from(JournalEntry)
            .where(JournalEntry.source_type == REVALUATION)
        )
        assert revaluations == 1, f"there are {revaluations} revaluations, expected one"
        print(f"a second run of the same date posted nothing (posted so far: {posted_before})")

        # 7 — reversing posts the negation, and a re-run works again
        reversed_entry = reverse_revaluation(
            session, company_id=COMPANY, account_code=AR, currency="USD", as_of=D2
        )
        session.commit()
        _balances(reversed_entry)
        assert (
            already_revalued(
                session, company_id=COMPANY, account_code=AR, currency="USD", as_of=D2
            )
            == 0
        ), "the reversal did not cancel the revaluation"
        again = revalue_open_balance(
            session, company_id=COMPANY, account_code=AR, currency="USD", as_of=D2
        )
        session.commit()
        _balances(again)
        assert again.lines[0].debit == Decimal("125.000000"), again.lines[0].debit
        print("the reversal cancelled it and the revaluation could be posted again")

        # 8 — the base currency does not revalue
        try:
            revalue_open_balance(
                session, company_id=COMPANY, account_code=AR, currency="PHP", as_of=D2
            )
        except FxGainLossError as exc:
            refusal = str(exc)
        else:
            raise AssertionError("the base currency was revalued")
        session.rollback()
        with engine.connect() as connection:
            assert ledger_gate(connection) == 0, "an entry in the ledger does not balance"
        print(f"the base currency is refused a revaluation: {refusal[:46]}…")

    engine.dispose()
    print("ok — realized and unrealized differences post balanced entries, once")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
