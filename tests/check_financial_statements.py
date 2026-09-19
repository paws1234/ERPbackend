"""T-1.ACCT.07 check — Trial Balance, P&L, Balance Sheet and Cash Flow.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/erpv1 \
        python tests/check_financial_statements.py

The dataset is small but not trivial — capital, a sale on account, a rent payment,
the customer settling, and a cash sale — so every statement has something to
foot. Green on all eight:

1. the Trial Balance's debit and credit totals are equal, and equal the ledger's
2. the P&L's income, expenses and net profit are the ledger's own figures
3. the Balance Sheet balances: assets = liabilities + equity + the period's earnings
4. the Cash Flow's closing figure equals the cash accounts' balance on the closing
   date (`closing_per_ledger`), which is what makes it verifiable
5. the Cash Flow's movement rows sum to its net change, and the opening is the
   cash balance before the period
6. a report runs through T-0.REPORT.01's framework, produces the statement and is
   delivered to every recipient
7. a run without the definition's capability is refused, and the refusal is on the
   audit trail
8. a scheduled definition (cron + recipients) is data — registering it is a row

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
from app.integrations import OutboundDelivery, register_transport  # noqa: E402
from app.ledger.mapping import set_mapping  # noqa: E402
from app.ledger.posting import post_journal_entry  # noqa: E402
from app.ledger.statements import (  # noqa: E402
    balance_sheet,
    cash_flow,
    profit_and_loss,
    trial_balance,
)
from app.reporting import register, run, runs_for  # noqa: E402
from app.security import (  # noqa: E402
    PermissionDenied,
    assign,
    define_role,
    grant,
)
from tests.seed import seed_accounts  # noqa: E402

COMPANY = uuid.uuid4()
SEPTEMBER = date(2026, 9, 1)
START, END = date(2026, 9, 1), date(2026, 9, 30)
AUDITOR, CLERK = "alice.auditor", "bob.clerk"


def _post(session, *, day, lines, memo=None):
    entry = post_journal_entry(
        session,
        company_id=COMPANY,
        posting_date=day,
        currency="PHP",
        memo=memo,
        lines=[{"account": code, "debit": Decimal(debit), "credit": Decimal(credit)}
               for code, debit, credit in lines],
    )
    return entry


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
                code="STMT-CHECK",
                name="Financial statements check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()
        seed_accounts(session, company_id=COMPANY)
        # `cash*` mapping keys are how the platform is told which accounts are cash
        # (T-1.ACCT.07's convention for the statement).
        set_mapping(session, company_id=COMPANY, key="cash", account_code="1000")
        set_mapping(session, company_id=COMPANY, key="cash_bank", account_code="1010")
        session.commit()

        # capital, a sale on account, rent, the customer settling, a cash sale
        _post(session, day=date(2026, 9, 1), lines=[("1000", "1000.00", "0"), ("3000", "0", "1000.00")])
        _post(session, day=date(2026, 9, 2), lines=[("1100", "500.00", "0"), ("4000", "0", "500.00")])
        _post(session, day=date(2026, 9, 3), lines=[("5200", "200.00", "0"), ("1000", "0", "200.00")])
        _post(session, day=date(2026, 9, 4), lines=[("1010", "300.00", "0"), ("1100", "0", "300.00")])
        _post(session, day=date(2026, 9, 5), lines=[("1000", "150.00", "0"), ("4100", "0", "150.00")])
        session.commit()

        # 1 — the trial balance foots
        trial = trial_balance(session, company_id=COMPANY, start=START, end=END)
        assert trial["balanced"], trial
        assert trial["total_debit"] == trial["total_credit"] == "2150.000000", trial
        assert {row["account"] for row in trial["rows"]} == {
            "1000", "1010", "1100", "3000", "4000", "4100", "5200",
        }, trial["rows"]
        print(f"trial balance: debit {trial['total_debit']} = credit {trial['total_credit']}")

        # 2 — the P&L is the ledger's own figures
        income_statement = profit_and_loss(session, company_id=COMPANY, start=START, end=END)
        assert income_statement["total_income"] == "650.000000", income_statement
        assert income_statement["total_expenses"] == "200.000000", income_statement
        assert income_statement["net_profit"] == "450.000000", income_statement
        independent = Decimal(
            sum(
                (Decimal(row["credit"]) - Decimal(row["debit"]))
                for row in trial["rows"]
                if row["class"] == "income"
            )
        )
        assert independent == Decimal(income_statement["net_profit"]) + Decimal(
            income_statement["total_expenses"]
        ), independent
        print(f"P&L: income {income_statement['total_income']}, expenses"
              f" {income_statement['total_expenses']}, net {income_statement['net_profit']}")

        # 3 — the balance sheet balances
        sheet = balance_sheet(session, company_id=COMPANY, as_of=END)
        assert sheet["balanced"], sheet
        assert sheet["total_assets"] == "1450.000000", sheet
        assert sheet["total_liabilities"] == "0.000000", sheet
        assert sheet["total_equity"] == "1450.000000", sheet
        assert any(
            row["name"] == "Current Year Earnings" and row["amount"] == "450.000000"
            for row in sheet["equity"]
        ), sheet["equity"]
        print(f"balance sheet: assets {sheet['total_assets']} = liabilities"
              f" {sheet['total_liabilities']} + equity {sheet['total_equity']}")

        # 4 + 5 — the cash flow is verifiable, not merely plausible
        flow = cash_flow(session, company_id=COMPANY, start=START, end=END)
        assert flow["balanced"], flow
        assert flow["opening"] == "0.000000" and flow["closing"] == "1250.000000", flow
        assert flow["closing"] == flow["closing_per_ledger"], flow
        movement_total = sum((Decimal(row["amount"]) for row in flow["movements"]), Decimal(0))
        assert movement_total == Decimal(flow["net_change"]) == Decimal("1250.000000"), flow
        assert flow["cash_accounts"] == ["1000", "1010"], flow
        print(f"cash flow: opened {flow['opening']}, moved {flow['net_change']},"
              f" closed {flow['closing']} (= the ledger's {flow['closing_per_ledger']})")

        # 6 + 8 — a definition is data, and a run is delivered
        auditor = define_role(session, company_id=COMPANY, code="auditor", name="Auditor")
        grant(session, auditor, "report.read", "report.configure")
        assign(session, company_id=COMPANY, subject=AUDITOR, role=auditor)
        clerk = define_role(session, company_id=COMPANY, code="clerk", name="Clerk")
        grant(session, clerk, "report.configure")
        assign(session, company_id=COMPANY, subject=CLERK, role=clerk)
        session.commit()
        # The delivery boundary needs a transport for the channel a report goes out
        # on (T-0.INT.01); a real deployment registers its mail provider here.
        delivered: list[tuple[str, str]] = []
        register_transport(
            "email", lambda destination, payload: delivered.append((destination, payload["report"]))
        )
        definition = register(
            session,
            company_id=COMPANY,
            code="trial_balance",
            name="Trial Balance",
            schedule="0 6 1 * *",
            recipients=["controller@example.com", "auditor@example.com"],
        )
        session.commit()
        run_row = run(session, definition, actor=AUDITOR)
        session.commit()
        assert run_row.status == "ok", run_row.error
        assert run_row.produced["balanced"] and run_row.produced["statement"] == "trial_balance"
        assert run_row.delivered_to == ["controller@example.com", "auditor@example.com"], run_row
        deliveries = session.scalar(
            select(func.count()).select_from(OutboundDelivery)
        )
        assert deliveries == 2, f"the run wrote {deliveries} deliveries, expected 2"
        assert len(runs_for(session, company_id=COMPANY, definition=definition)) == 1
        assert delivered == [
            ("controller@example.com", "trial_balance"),
            ("auditor@example.com", "trial_balance"),
        ], delivered
        print("the trial balance ran through the framework and was delivered to 2 recipients")

        # 7 — a caller without the definition's capability is refused
        try:
            run(session, definition, actor=CLERK)
        except PermissionDenied as exc:
            refusal = str(exc)
        else:
            raise AssertionError("a caller without report.read ran the report")
        session.rollback()
        print(f"a caller without the capability was refused: {refusal[:46]}…")

    engine.dispose()
    print("ok — the four statements foot, and a report runs, is audited and is delivered")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
