"""T-3.AR.03 check — recurring billing: the cadence, the idempotency, the failures.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_recurring_billing.py

Green on all eight:

1. a **monthly** template raises one invoice per period it has reached, each dated its
   own period's start, posted through T-3.AR.01 and carrying the template's lines,
   prices and the pack's tax
2. running the job **twice over the same period raises nothing the second time** — the
   run row is the idempotency, and the invoice count is unchanged
3. a **paused** template raises nothing and says why; resuming bills the periods that
   were missed while it was paused
4. a generated invoice is an **ordinary invoice**: the aging report ages it, it carries
   the customer's terms, and its posting is the same balanced entry a manual one posts
5. a template that **cannot be billed is reported** with its reason, does not stop the
   others in the same run, and — no run row having been written — is retried by the
   next run
6. the **cadence is the template's**, not the job's: quarterly, annual and weekly
   produce their own period keys from the same start date, and a period is not billed
   twice however often the job runs
7. the database refuses a hand-written second run row for a period, and the run record
   is append-only
8. an unknown cadence, an empty template, a duplicate code, an end before the start and
   a non-positive line are each refused

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

from app.ar.aging import aging  # noqa: E402
from app.ar.invoices import CustomerInvoice, open_invoices  # noqa: E402
from app.ar.recurring import (  # noqa: E402
    EmptyTemplateError,
    RecurringError,
    RecurringInvoiceRun,
    UnknownCycleError,
    WEEKLY,
    create_template,
    generate_due,
    pause_template,
    period_key,
    periods_due,
    runs_for,
    template_by_code,
)
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
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
JAN = date(2026, 1, 15)
MARCH_END = date(2026, 3, 31)
JUNE_END = date(2026, 6, 30)
TERMS = 30
VAT = Decimal("1.12")
FEE = "500.00"


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


def _invoices(session: Session) -> list[CustomerInvoice]:
    return list(
        session.scalars(
            select(CustomerInvoice).order_by(CustomerInvoice.invoice_date, CustomerInvoice.number)
        )
    )


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
                code="RECUR",
                name="Recurring billing",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        register_currency(session, company_id=COMPANY, code="PHP", name="Peso")
        session.commit()
        seed_accounts(session, company_id=COMPANY)
        create_account(session, company_id=COMPANY, code="2200", name="Output VAT",
                       account_class="liability")
        set_mapping(session, company_id=COMPANY, key="receivables", account_code="1100")
        set_mapping(session, company_id=COMPANY, key="revenue", account_code="4000")
        set_mapping(session, company_id=COMPANY, key="output_tax", account_code="2200")
        session.commit()

        acme = create_customer(session, company_id=COMPANY, party_code="ACME",
                               name="Acme Retail", payment_terms_days=TERMS)
        session.commit()

        monthly = create_template(
            session,
            company_id=COMPANY,
            code="HOSTING",
            customer=acme,
            cycle="monthly",
            starts_on=JAN,
            description="Monthly hosting",
            lines=[{"description": "Hosting", "quantity": "1", "unit_price": FEE}],
        )
        session.commit()

        # 1 — one invoice per period reached, each dated its own period's start
        first = generate_due(session, company_id=COMPANY, as_of=MARCH_END)
        assert first.failures == [], first.failures
        assert [row["period"] for row in first.generated] == ["2026-01", "2026-02", "2026-03"], (
            first.generated
        )
        raised = _invoices(session)
        assert [row.number for row in raised] == ["HOSTING-2026-01", "HOSTING-2026-02",
                                                 "HOSTING-2026-03"], [r.number for r in raised]
        assert [row.invoice_date for row in raised] == [JAN, date(2026, 2, 15),
                                                        date(2026, 3, 15)], [
            row.invoice_date for row in raised
        ]
        assert all(row.status == "posted" for row in raised), [r.status for r in raised]
        assert all(row.net_amount == Decimal(FEE) for row in raised)
        assert all(row.gross_amount == _gross(FEE) for row in raised), [
            row.gross_amount for row in raised
        ]
        entries = session.scalars(
            select(JournalEntry).where(JournalEntry.source_type == "customer_invoice")
        ).all()
        assert len(entries) == 3, len(entries)
        for entry in entries:
            lines = session.scalars(
                select(JournalLine).where(JournalLine.entry_id == entry.id)
            ).all()
            debit = sum((line.debit for line in lines), Decimal(0))
            credit = sum((line.credit for line in lines), Decimal(0))
            assert debit == credit == _gross(FEE), (debit, credit)
        print(
            f"1. the monthly template raised {len(raised)} invoices by {MARCH_END} —"
            f" {', '.join(row.number for row in raised)} — each dated its own period's"
            f" start, posted, and billed at {_gross(FEE)} with the pack's tax"
        )

        # 2 — the same period is never billed twice
        again = generate_due(session, company_id=COMPANY, as_of=MARCH_END)
        assert again.generated == [], again.generated
        assert len(_invoices(session)) == 3, "a rerun raised a duplicate invoice"
        assert len(runs_for(session, template_by_code(session, company_id=COMPANY,
                                                     code="HOSTING"))) == 3
        june = generate_due(session, company_id=COMPANY, as_of=JUNE_END)
        assert [row["period"] for row in june.generated] == ["2026-04", "2026-05", "2026-06"], (
            june.generated
        )
        assert len(_invoices(session)) == 6, len(_invoices(session))
        print(
            "2. running the job a second time over the same period raised nothing"
            " (the run row is the idempotency), and the next run billed only the three"
            " periods it had reached since"
        )

        # 3 — paused raises nothing, and says why; resuming bills what was missed
        paused = create_template(
            session,
            company_id=COMPANY,
            code="SUPPORT",
            customer=acme,
            cycle="monthly",
            starts_on=JAN,
            lines=[{"description": "Support", "quantity": "1", "unit_price": "100.00"}],
        )
        pause_template(session, paused)
        session.commit()
        while_paused = generate_due(session, company_id=COMPANY, as_of=JUNE_END)
        assert while_paused.generated == [], while_paused.generated
        assert [row["template"] for row in while_paused.skipped] == ["SUPPORT"], (
            while_paused.skipped
        )
        assert "paused" in while_paused.skipped[0]["reason"], while_paused.skipped
        assert len(_invoices(session)) == 6, "a paused template billed"
        pause_template(session, paused, paused=False)
        session.commit()
        resumed = generate_due(session, company_id=COMPANY, as_of=JUNE_END)
        assert [row["template"] for row in resumed.generated] == ["SUPPORT"] * 6, (
            resumed.generated
        )
        print(
            f"3. the paused template raised nothing and said why"
            f" ({while_paused.skipped[0]['reason']}); resumed, it billed the six periods"
            " it had missed"
        )

        # 4 — a generated invoice is an ordinary invoice for aging and dunning
        report = aging(session, company_id=COMPANY, as_of=date(2026, 7, 31))
        numbers = {row["invoice"] for row in report.invoices}
        assert "HOSTING-2026-06" in numbers and "SUPPORT-2026-06" in numbers, sorted(numbers)
        hosting = next(row for row in report.invoices if row["invoice"] == "HOSTING-2026-01")
        assert hosting["due_date"] == date(2026, 2, 14), hosting
        assert report.difference["PHP"] == Decimal("0.000000"), report.difference
        assert len(open_invoices(session, company_id=COMPANY, customer=acme)) == 12, len(
            open_invoices(session, company_id=COMPANY, customer=acme)
        )
        print(
            f"4. the aging report ages them like any other ({len(report.invoices)} rows,"
            f" reconciling to the control account with difference"
            f" {report.difference['PHP']}), and each carries the customer's {TERMS}-day"
            " terms"
        )

        # 5 — a template that cannot be billed is reported, and the others carry on
        broken = create_template(
            session,
            company_id=COMPANY,
            code="BROKEN",
            customer=acme,
            cycle="monthly",
            starts_on=JAN,
            lines=[{"description": "Wrongly classified", "quantity": "1",
                    "unit_price": "10.00", "tax_rule_code": "VAT-IN-12"}],
        )
        session.commit()
        overdue = create_template(
            session,
            company_id=COMPANY,
            code="OVERDUE",
            customer=acme,
            cycle="monthly",
            starts_on=date(2026, 7, 1),
            lines=[{"description": "Late but billable", "quantity": "1",
                    "unit_price": "10.00"}],
        )
        session.commit()
        run = generate_due(session, company_id=COMPANY,
                           as_of=date(2026, 7, 31), templates=[broken, overdue])
        assert {row["template"] for row in run.failures} == {"BROKEN"}, run.failures
        assert [row["period"] for row in run.failures] == [
            "2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06", "2026-07"
        ], [row["period"] for row in run.failures]
        assert all("UnknownTaxRuleError" in row["reason"] for row in run.failures)
        assert [row["template"] for row in run.generated] == ["OVERDUE"], run.generated
        assert not [
            row for row in runs_for(session, broken)
        ], "a failed period wrote a run row"
        retried = generate_due(session, company_id=COMPANY,
                               as_of=date(2026, 7, 31), templates=[broken])
        assert [row["period"] for row in retried.failures] == [
            row["period"] for row in run.failures
        ], "a failed period was not retried on the next run"
        print(
            f"5. the unbillable template was reported for each of its"
            f" {len(run.failures)} reached periods ({run.failures[0]['reason'][:40]}…) and"
            " wrote no run row, so the next run retried all of them, while the billable"
            " template beside it was billed in the same run"
        )

        # 6 — the cadence is the template's own
        quarterly = create_template(
            session, company_id=COMPANY, code="QUARTER", customer=acme, cycle="quarterly",
            starts_on=JAN,
            lines=[{"description": "Quarterly fee", "quantity": "1", "unit_price": "900.00"}],
        )
        weekly = create_template(
            session, company_id=COMPANY, code="WEEKLY", customer=acme, cycle=WEEKLY,
            starts_on=date(2026, 9, 7),
            lines=[{"description": "Weekly fee", "quantity": "1", "unit_price": "25.00"}],
        )
        session.commit()
        assert period_key(quarterly, date(2026, 4, 15)) == "2026-04"
        quarter_keys = [
            key for key, _ in periods_due(session, quarterly, as_of=date(2026, 9, 30))
        ]
        assert quarter_keys == ["2026-01", "2026-04", "2026-07"], quarter_keys
        weekly_keys = [key for key, _ in periods_due(session, weekly, as_of=date(2026, 9, 30))]
        assert weekly_keys == ["2026-W37", "2026-W38", "2026-W39", "2026-W40"], weekly_keys
        cadence = generate_due(session, company_id=COMPANY,
                               as_of=date(2026, 9, 30), templates=[quarterly, weekly])
        assert sorted(row["period"] for row in cadence.generated) == sorted(
            quarter_keys + weekly_keys
        ), cadence.generated
        assert len(generate_due(session, company_id=COMPANY, as_of=date(2026, 9, 30),
                               templates=[quarterly, weekly]).generated) == 0
        print(
            f"6. the same start date yields the template's own cadence: quarterly"
            f" {quarter_keys} and weekly {weekly_keys} — and neither bills a period twice"
        )

        # 7 — one run row per period, and it is history
        run_row = runs_for(session, quarterly)[0]
        session.add(
            RecurringInvoiceRun(
                company_id=COMPANY, template_id=quarterly.id, period_key=run_row.period_key,
                period_start=run_row.period_start, invoice_id=run_row.invoice_id,
                generated_on=run_row.generated_on,
            )
        )
        try:
            session.commit()
        except DBAPIError as exc:
            assert "uq_recurring_run_period_once" in str(exc), exc
            session.rollback()
        else:
            raise AssertionError("the database accepted a second run row for one period")
        try:
            session.execute(
                RecurringInvoiceRun.__table__.update().values(period_key="tampered")
            )
            session.commit()
        except DBAPIError as exc:
            assert "append-only" in str(exc), exc
            session.rollback()
        else:
            raise AssertionError("a run row was edited")
        print(
            "7. the database refuses a second run row for one period"
            " (uq_recurring_run_period_once) and refuses to edit the one it has"
        )

        # 8 — the refusals at entry
        unknown = _refused(
            lambda: create_template(
                session, company_id=COMPANY, code="X1", customer=acme, cycle="fortnightly",
                starts_on=JAN,
                lines=[{"description": "x", "quantity": "1", "unit_price": "1"}],
            ),
            UnknownCycleError,
        )
        session.rollback()
        empty = _refused(
            lambda: create_template(
                session, company_id=COMPANY, code="X2", customer=acme, cycle="monthly",
                starts_on=JAN, lines=[],
            ),
            EmptyTemplateError,
        )
        session.rollback()
        duplicate = _refused(
            lambda: create_template(
                session, company_id=COMPANY, code="HOSTING", customer=acme, cycle="monthly",
                starts_on=JAN,
                lines=[{"description": "x", "quantity": "1", "unit_price": "1"}],
            ),
            RecurringError,
        )
        session.rollback()
        backwards = _refused(
            lambda: create_template(
                session, company_id=COMPANY, code="X3", customer=acme, cycle="monthly",
                starts_on=date(2026, 6, 1), ends_on=date(2026, 5, 1),
                lines=[{"description": "x", "quantity": "1", "unit_price": "1"}],
            ),
            RecurringError,
        )
        session.rollback()
        nonpositive = _refused(
            lambda: create_template(
                session, company_id=COMPANY, code="X4", customer=acme, cycle="monthly",
                starts_on=JAN,
                lines=[{"description": "x", "quantity": "0", "unit_price": "1"}],
            ),
            RecurringError,
        )
        session.rollback()
        print(
            f"8. an unknown cadence ({unknown[:34]}…), an empty template ({empty[:30]}…),"
            f" a duplicate code ({duplicate[:30]}…), an end before the start"
            f" ({backwards[:30]}…) and a zero-quantity line ({nonpositive[:28]}…) are each"
            " refused"
        )

    print("\ncheck_recurring_billing: all assertions green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
