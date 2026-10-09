"""T-5.PAY.04 check — every statutory report reconciles to the run, and comes from the pack.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_payroll_statutory.py

It fails (non-zero exit) if any of these stops holding:

1. **each report reconciles exactly to the run it derives from** — the rows are the run's own
   line components for the rules the form collects, the totals are those rows added up, and
   `reconcile_report` re-reads the stored rows and finds a difference of exactly zero (checked
   for every form the pack feeds from payroll)
2. **the report states the pack version and the period** — plus the market, the authority, the
   frequency and the revision of the run it was filed from
3. **the definitions are the pack's and no market is in the code** — the forms, their names,
   authorities, frequencies and the rules each collects are read from the pack, and the
   module's own source names none of the pack's forms or authorities
4. **a market or a form that produces nothing is refused, never emptied** — a form the pack
   states but payroll does not feed, a form the pack does not state at all, a market with no
   pack, and a run that has not been computed are each refused, by name

**Scratch database only**: it drops and recreates the schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.audit import set_actor  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.hr.employees import create_employee, record_contract  # noqa: E402
from app.ledger import posting  # noqa: E402,F401 — every check builds the one schema
from app.localization import PackError, load_pack  # noqa: E402
from app.payroll.components import load_statutory_components  # noqa: E402
from app.payroll.engine import compute_run, start_run  # noqa: E402
from app.payroll.statutory import (  # noqa: E402
    NotAPayrollReportError,
    RunNotReportableError,
    UnknownReportError,
    payroll_reports,
    produce_report,
    reconcile_report,
    reports_for_run,
)

MODULE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "app",
    "payroll",
    "statutory.py",
)


def _refuses(call, expected: type, what: str) -> Exception:
    """The refusal `call` raises; fail the check if it does not refuse."""
    try:
        call()
    except expected as exc:
        return exc
    raise AssertionError(f"{what} was accepted")


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

    company_id = uuid.uuid4()
    pack = load_pack("philippines")
    with Session(engine) as session:
        session.add(
            Company(
                id=company_id,
                code="PAY-STAT",
                name="Statutory report check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()
        set_actor(session, "payroll")
        for number, party, salary in (("E-1001", "ANA", "30000"), ("E-1002", "BEN", "20000")):
            person = create_employee(
                session,
                company_id=company_id,
                party_code=party,
                number=number,
                hire_date="2025-01-06",
                subject="hr",
                name=f"{party} Reyes",
            )
            record_contract(
                session, person, subject="hr", effective_from="2025-01-06",
                contract_type="regular", basic_salary=salary,
            )
        load_statutory_components(
            session, company_id=company_id, market="philippines", effective_from="2026-01-01"
        )
        session.commit()
        run = start_run(
            session, company_id=company_id, period="2026-06", actor="payroll", cutoff_day=15
        )
        compute_run(session, run, actor="payroll")
        session.commit()

        # 1 + 2 — every form the pack feeds from payroll reconciles, and says what it was made from
        definitions = payroll_reports("philippines")
        assert [row["form"] for row in definitions] == [
            "1601-C",
            "1604-C",
            "2316",
            "R-3",
            "RF-1",
            "MCRF",
        ], [row["form"] for row in definitions]
        reports = reports_for_run(session, run, market="philippines")
        assert len(reports) == len(definitions)
        for report in reports:
            assert report["period"] == "2026-06" and report["market"] == "philippines"
            assert report["pack_version"] == pack["version"]
            assert report["authority"] and report["frequency"] and report["name"]
            assert report["run_revision"] == 1 and report["run_state"] == "computed"
            assert reconcile_report(session, run, report) == Decimal(0), report["form"]
            assert Decimal(report["total"]) == sum(
                Decimal(amount) for amount in report["totals_by_rule"].values()
            )
        # The figures, computed here from the contracts and the pack's own rates: the employee
        # on 30,000 contributes 1500 (5%) and 750 (2.5%), the one on 20,000 contributes 1000
        # and 500, and their employer's shares are 3000/2000 and 750/500.
        social = [report for report in reports if report["form"] == "R-3"][0]
        assert social["totals_by_rule"] == {"SSS-EE": "2500.000000", "SSS-ER": "5000.000000"}
        assert social["total"] == "7500.000000"
        assert social["employees_total"] == {"E-1001": "4500.000000", "E-1002": "3000.000000"}
        health = [report for report in reports if report["form"] == "RF-1"][0]
        assert health["totals_by_rule"] == {"PHIC-EE": "1250.000000", "PHIC-ER": "1250.000000"}
        # The pack states a 0% rate for compensation withholding ("the pack carries the
        # schedules, not a single rate"), so the return is the pack's own figure rather than a
        # rate invented here: 0.000000, on the record as such.
        withholding = [report for report in reports if report["form"] == "1601-C"][0]
        assert withholding["total"] == "0.000000"
        assert any(
            rule["code"] == "WHT-COMP" and rule["rate_percent"] == 0
            for rule in pack["statutory_rules"]
        )
        print(
            "six forms a payroll run feeds, each reconciling to the run (difference 0), each"
            " stating its market, pack version, period and revision — and the withholding"
            " return is the pack's own 0% rather than a rate invented here"
        )

        # 3 — the definitions are the pack's, and no market is written in the module
        source = open(MODULE, encoding="utf-8").read()
        for report in pack["statutory_reports"]:
            assert report["form"] not in source, report["form"]
            assert report["authority"] not in source, report["authority"]
        for rule in pack["statutory_rules"]:
            assert rule["code"] not in source, rule["code"]
        rule_codes = {rule["code"] for rule in pack["statutory_rules"]}
        for report in pack["statutory_reports"]:
            for covered in report.get("covers_rules", []):
                assert covered in rule_codes, (report["form"], covered)
        print(
            "the forms, their authorities and the rules each collects are the pack's own data"
            " (and the pack refuses a report that covers a rule it does not state), while the"
            " module names no form, authority or rule code"
        )

        # 4 — nothing is produced where nothing can be: refused by name, never emptied
        _refuses(
            lambda: produce_report(session, run, market="philippines", form="2550M"),
            NotAPayrollReportError,
            "a form the pack states but payroll does not feed",
        )
        named = _refuses(
            lambda: produce_report(session, run, market="philippines", form="NOPE"),
            UnknownReportError,
            "a form the pack does not state",
        )
        assert "1601-C" in str(named) and "MCRF" in str(named), named
        _refuses(
            lambda: produce_report(session, run, market="atlantis", form="R-3"),
            PackError,
            "a market the packs do not cover",
        )
        draft = start_run(
            session, company_id=company_id, period="2026-07", actor="payroll", cutoff_day=15
        )
        session.commit()
        _refuses(
            lambda: produce_report(session, draft, market="philippines", form="R-3"),
            RunNotReportableError,
            "a report from a run that has not been computed",
        )
        session.rollback()
        print(
            "a VAT return no payroll run feeds, a form the pack does not state, a market with"
            " no pack and a run that has not been computed are each refused by name — an empty"
            " statutory return is never produced"
        )

    engine.dispose()
    print("ok — the pack's own returns, filled from the run and reconciled to it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
