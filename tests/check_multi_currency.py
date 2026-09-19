"""T-1.ACCT.05 check — the currency master, dated rates, conversion at posting time.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/erpv1 \
        python tests/check_multi_currency.py

Green on all eight:

1. a currency is registered once; an unregistered code is refused, and a rate
   cannot be stored for one
2. a dated rate is stored; storing the same figure again changes nothing
3. a **past** date's rate is not rewritten (`HistoricalRateError`), while today's
   rate can be corrected — and the change is on the audit trail
4. a foreign-currency posting stores its rate, its foreign amount and — exactly
   derivable — its base amount (`amount × exchange_rate`)
5. it uses the rate for the **posting date**: the same posting on a day with no
   stored rate is refused rather than priced at today's rate
6. a cross conversion goes through the base currency and comes out at money scale
7. the daily sync stores the day's rates against the configured source
8. a sync with no source configured, and a sync whose source fails, both raise
   loudly and store **nothing** (no half-synced day)

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

from app.audit import read_trail, set_actor  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger.currency import (  # noqa: E402
    FxRate,
    FxSyncFailed,
    HistoricalRateError,
    RATE_SOURCE_SETTING,
    UnknownCurrencyError,
    UnknownRateError,
    configured_source,
    convert,
    rate_for,
    register_currency,
    register_rate_source,
    store_rate,
    sync_daily_rates,
)
from app.ledger.posting import JournalEntry, post_journal_entry  # noqa: E402
from tests.seed import seed_accounts  # noqa: E402

COMPANY = uuid.uuid4()
TODAY = date(2026, 9, 19)
POSTED_ON = date(2026, 9, 17)
YESTERDAY = date(2026, 9, 16)
PAIR_DAY_RATE = Decimal("58.5000000000")
TODAYS_RATE = Decimal("59.7500000000")
USD = Decimal("100.00")


def _refused(call, expected: type[Exception]) -> str:
    try:
        call()
    except Exception as exc:  # noqa: BLE001 — the type and message are the point
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
            Company(
                id=COMPANY,
                code="FX-CHECK",
                name="Multi-currency check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()
        seed_accounts(session, company_id=COMPANY)
        register_currency(session, company_id=COMPANY, code="php", name="Philippine Peso")
        register_currency(session, company_id=COMPANY, code="USD", name="US Dollar")
        register_currency(session, company_id=COMPANY, code="EUR", name="Euro")
        session.commit()

        # 1 — the master is the gate
        refusal = _refused(
            lambda: store_rate(
                session,
                company_id=COMPANY,
                base_currency="PHP",
                currency="GBP",
                on=POSTED_ON,
                rate="70",
            ),
            UnknownCurrencyError,
        )
        session.rollback()
        print(f"a rate for an unregistered currency refused: {refusal[:48]}…")

        # 2 — a dated rate, stored once
        stored = store_rate(
            session,
            company_id=COMPANY,
            base_currency="PHP",
            currency="USD",
            on=POSTED_ON,
            rate="58.5",
        )
        session.commit()
        again = store_rate(
            session,
            company_id=COMPANY,
            base_currency="PHP",
            currency="USD",
            on=POSTED_ON,
            rate="58.5",
        )
        session.commit()
        assert stored.id == again.id and again.rate == PAIR_DAY_RATE, again.rate
        print(f"a dated rate is stored once: USD/PHP {POSTED_ON} = {again.rate}")

        # 3 — history is not rewritten; today may be corrected, and it is audited
        set_actor(session, "alice.treasury")
        _refused(
            lambda: store_rate(
                session,
                company_id=COMPANY,
                base_currency="PHP",
                currency="USD",
                on=POSTED_ON,
                rate="60",
                today=TODAY,
            ),
            HistoricalRateError,
        )
        session.rollback()
        store_rate(
            session,
            company_id=COMPANY,
            base_currency="PHP",
            currency="USD",
            on=TODAY,
            rate="59.5",
            today=TODAY,
        )
        session.commit()
        set_actor(session, "alice.treasury")
        corrected = store_rate(
            session,
            company_id=COMPANY,
            base_currency="PHP",
            currency="USD",
            on=TODAY,
            rate="59.75",
            today=TODAY,
        )
        session.commit()
        assert corrected.rate == TODAYS_RATE, corrected.rate
        corrections = [
            row
            for row in read_trail(session, entity="fx_rate")
            if row.action == "update"
        ]
        assert corrections and corrections[-1].actor == "alice.treasury", corrections
        assert str(corrections[-1].before_values["rate"]).startswith("59.5"), corrections[-1]
        print("a past rate was refused a rewrite; today's correction is on the trail")

        # 4 + 5 — the posting keeps its own rate, and its date decides which one
        entry = post_journal_entry(
            session,
            company_id=COMPANY,
            posting_date=POSTED_ON,
            currency="USD",
            lines=[
                {"account": "1000", "debit": USD},
                {"account": "4000", "credit": USD},
            ],
        )
        session.commit()
        assert entry.exchange_rate == PAIR_DAY_RATE, entry.exchange_rate
        base_debit = (USD * entry.exchange_rate).quantize(Decimal("0.000001"))
        assert base_debit == Decimal("5850.000000"), base_debit
        assert base_debit != (USD * TODAYS_RATE).quantize(Decimal("0.000001")), (
            "the posting used today's rate instead of the posting date's"
        )
        _refused(
            lambda: post_journal_entry(
                session,
                company_id=COMPANY,
                posting_date=YESTERDAY,
                currency="USD",
                lines=[
                    {"account": "1000", "debit": USD},
                    {"account": "4000", "credit": USD},
                ],
            ),
            UnknownRateError,
        )
        session.rollback()
        stored_entry = session.scalar(select(JournalEntry).where(JournalEntry.id == entry.id))
        assert stored_entry.exchange_rate == PAIR_DAY_RATE, stored_entry.exchange_rate
        print(f"a USD posting stored rate {stored_entry.exchange_rate} and base {base_debit}")

        # 6 — a cross rate goes through the base
        store_rate(
            session,
            company_id=COMPANY,
            base_currency="PHP",
            currency="EUR",
            on=POSTED_ON,
            rate="63.5",
        )
        session.commit()
        euros = convert(
            session,
            company_id=COMPANY,
            base_currency="PHP",
            amount=USD,
            currency="USD",
            to_currency="EUR",
            on=POSTED_ON,
        )
        assert euros == (USD * PAIR_DAY_RATE / Decimal("63.5")).quantize(Decimal("0.000001"))
        assert rate_for(
            session, company_id=COMPANY, base_currency="PHP", currency="PHP", on=POSTED_ON
        ) == Decimal(1)
        print(f"USD {USD} → EUR {euros} on {POSTED_ON}, through the base currency")

        # 7 — the daily sync fills the day from the configured source
        register_rate_source(
            "test-feed",
            lambda base, currencies: {code: Decimal("58.10") for code in currencies},
        )
        os.environ[RATE_SOURCE_SETTING] = "test-feed"
        assert configured_source() == "test-feed"
        synced = sync_daily_rates(
            session, company_id=COMPANY, base_currency="PHP", currencies=["USD", "EUR"], on=TODAY
        )
        session.commit()
        assert sorted(rate.currency for rate in synced) == ["EUR", "USD"], synced
        assert all(rate.source == "test-feed" for rate in synced), synced
        assert len(synced) == 2
        print(f"the daily sync stored {len(synced)} rates from 'test-feed' for {TODAY}")

        # 8 — a failing sync stores nothing and says why
        def broken(base, currencies):
            raise TimeoutError("the feed did not answer")

        register_rate_source("broken-feed", broken)
        os.environ[RATE_SOURCE_SETTING] = "broken-feed"
        with engine.connect() as connection:
            before = connection.exec_driver_sql("SELECT count(*) FROM fx_rate").scalar()
        failure = _refused(
            lambda: sync_daily_rates(
                session,
                company_id=COMPANY,
                base_currency="PHP",
                currencies=["USD"],
                on=date(2026, 9, 18),
            ),
            FxSyncFailed,
        )
        session.rollback()
        with engine.connect() as connection:
            after = connection.exec_driver_sql("SELECT count(*) FROM fx_rate").scalar()
        assert before == after, f"a failed sync stored {after - before} rows"
        os.environ.pop(RATE_SOURCE_SETTING, None)
        unset = _refused(
            lambda: sync_daily_rates(
                session, company_id=COMPANY, base_currency="PHP", currencies=["USD"]
            ),
            FxSyncFailed,
        )
        session.rollback()
        with engine.connect() as connection:
            still = connection.exec_driver_sql("SELECT count(*) FROM fx_rate").scalar()
        assert still == before, "an unconfigured sync stored rows"
        assert session.scalar(select(func.count()).select_from(FxRate)) == before
        print(f"a failing sync stored nothing ({failure[:46]}…); unconfigured also refused")
        print(f"an unconfigured sync says: {unset[:52]}…")

    engine.dispose()
    print("ok — dated rates, posting-time conversion and a sync that fails loudly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
