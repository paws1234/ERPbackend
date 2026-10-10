"""T-5.PAY.05 check — a payslip is the run's own figures, and only its owner may read it.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_payroll_payslip.py

It fails (non-zero exit) if any of these stops holding:

1. **the payslip's totals are the payroll run's figures for that employee** — the earnings row
   by row add up to the line's gross, the deductions to its deductions, the employer's own
   contributions are reported beside the pay rather than taken from it, and `reconciles` returns
   exactly zero for every claim a payslip makes
2. **net pay equals gross minus the deductions** — asserted on the payslip, on the line, and
   between the two
3. **a payslip regenerated after a later run reproduces the original figures exactly** — a
   correction that changes the period's inputs produces different figures in the new revision,
   while the superseded revision's payslip reads **word for word** what it read before
4. **access is restricted** — an employee reading somebody else's payslip is refused, an
   employee reading their own is not, a payroll role holding the payslip capability reads the
   whole run, and an employee holding no role cannot list the run's payslips
5. the payslip states the period, the cutoff, the pack version and the revision it was read
   from, and shows the outstanding loan balance **after** that period

**Scratch database only**: it drops and recreates the schema.
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

from app.audit import set_actor  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.hr.attendance import record_punch  # noqa: E402
from app.hr.employees import create_employee, record_contract  # noqa: E402
from app.hr.shifts import define_shift, roster_employee  # noqa: E402
from app.ledger import posting  # noqa: E402,F401 — every check builds the one schema
from app.localization import holidays, load_pack  # noqa: E402
from app.payroll.components import define_component, load_statutory_components  # noqa: E402
from app.payroll.engine import (  # noqa: E402
    compute_run,
    correct_run,
    line_for,
    start_run,
)
from app.payroll.loans import record_loan, set_policy  # noqa: E402
from app.payroll.payslips import (  # noqa: E402
    PAYSLIP_CAPABILITY,
    payslip,
    payslips_for_run,
    reconciles,
)
from app.security import PermissionDenied, assign, define_role, grant  # noqa: E402


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
    with Session(engine) as session:
        session.add(
            Company(
                id=company_id,
                code="PAY-SLIP",
                name="Payslip check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()
        set_actor(session, "payroll")
        ana = create_employee(
            session,
            company_id=company_id,
            party_code="ANA",
            number="E-1101",
            hire_date="2025-01-06",
            subject="hr",
            name="Ana Reyes",
        )
        ben = create_employee(
            session,
            company_id=company_id,
            party_code="BEN",
            number="E-1102",
            hire_date="2025-01-06",
            subject="hr",
            name="Ben Reyes",
        )
        for person, salary in ((ana, "30000"), (ben, "20000")):
            record_contract(
                session, person, subject="hr", effective_from="2025-01-06",
                contract_type="regular", basic_salary=salary,
            )
        load_statutory_components(
            session, company_id=company_id, market="philippines", effective_from="2026-01-01"
        )
        define_component(
            session,
            company_id=company_id,
            code="ALLOWANCE",
            name="Transport allowance",
            kind="earning",
            basis="fixed",
            amount="5000",
            taxable=True,
            order=5,
            effective_from="2026-01-01",
        )
        set_policy(
            session,
            company_id=company_id,
            max_recovery_percent="20",
            effective_from="2026-01-01",
            actor="hr-head",
        )
        record_loan(
            session,
            ana,
            reference="ADV-1",
            principal="6000",
            instalment_amount="1000",
            started_on="2026-01-01",
            actor="hr-head",
            reason="advance",
        )
        day_shift = define_shift(
            session,
            company_id=company_id,
            code="DAY",
            name="Day shift",
            starts_at="09:00",
            ends_at="18:00",
            break_minutes=60,
            late_grace_minutes=10,
        )
        roster_employee(session, ana, shift=day_shift, effective_from="2025-01-06")
        record_punch(
            session, ana, at="2026-06-01T09:00:00", direction="in", source="manual",
            actor="supervisor", reason="timesheet",
        )
        record_punch(
            session, ana, at="2026-06-01T18:00:00", direction="out", source="manual",
            actor="supervisor", reason="timesheet",
        )
        session.commit()

        run = start_run(
            session, company_id=company_id, period="2026-06", actor="payroll", cutoff_day=15
        )
        compute_run(session, run, actor="payroll")
        session.commit()
        ana_line = line_for(session, run, ana)
        ben_line = line_for(session, run, ben)

        # 4 — who may read what, decided by T-0.SEC.01 rather than assumed
        own = payslip(session, ana_line, subject="ana", viewer_party=ana.party)
        _refuses(
            lambda: payslip(session, ben_line, subject="ana", viewer_party=ana.party),
            PermissionDenied,
            "an employee reading somebody else's payslip",
        )
        session.rollback()
        _refuses(
            lambda: payslips_for_run(session, run, subject="ana", viewer_party=ana.party),
            PermissionDenied,
            "an employee listing a run's payslips",
        )
        session.rollback()
        payroll_role = define_role(
            session, company_id=company_id, code="PAYROLL", name="Payroll officer"
        )
        grant(session, payroll_role, PAYSLIP_CAPABILITY)
        assign(session, company_id=company_id, subject="payroll", role=payroll_role)
        session.commit()
        whole_run = payslips_for_run(session, run, subject="payroll")
        assert [row["employee"] for row in whole_run] == ["E-1101", "E-1102"], whole_run
        # A payroll role reads anybody's payslip, and the same one the employee read.
        assert whole_run[0] == own, "the payroll role's reading differs from the employee's own"
        print(
            "an employee reads their own payslip and is refused somebody else's (and the run's"
            " list), while a role holding payslip.read reads the whole run — the same figures"
            " either way"
        )

        # 1 + 2 — the payslip's totals are the run's, and net is gross minus the deductions
        earnings = sum(Decimal(row["amount"]) for row in own["earnings"])
        deducted = sum(Decimal(row["amount"]) for row in own["deductions"])
        assert earnings == ana_line.gross, (earnings, ana_line.gross)
        assert deducted == ana_line.deductions_total, (deducted, ana_line.deductions_total)
        assert Decimal(own["gross"]) == ana_line.gross
        assert Decimal(own["net"]) == ana_line.net
        assert Decimal(own["net"]) == Decimal(own["gross"]) - Decimal(own["deductions_total"])
        assert reconciles(session, ana_line, own) == Decimal(0)
        # One day worked to a full shift: basic pay and the allowance, and no overtime line
        # at all — a payslip shows what happened, not every component the structure states.
        assert [row["code"] for row in own["earnings"]] == ["BASIC", "ALLOWANCE"]
        # The employer's own share is on the payslip and is **not** taken out of the net.
        contributions = sum(Decimal(row["amount"]) for row in own["employer_contributions"])
        assert contributions == ana_line.employer_contributions_total > 0
        assert Decimal(own["net"]) > Decimal(own["gross"]) - deducted - contributions
        # The days and the minutes behind it, and the loan balance after this period.
        assert own["days"] == {
            "period": 30,
            "employed": 30,
            "worked": 1,
            "paid_leave": 0,
            "unpaid_leave": 0,
            "holiday": 0,
            "absent": 29,
            "days_recorded": 30,
        }, own["days"]
        assert own["minutes"]["late"] == 0 and own["minutes"]["reference_schedule"] == 480
        loan_rows = own["loans"]
        assert [row["reference"] for row in loan_rows] == ["ADV-1"], loan_rows
        assert loan_rows[0]["principal"] == "6000.000000"
        # The recovery this period is the policy's share of what was left of the pay, and the
        # balance the payslip shows moved by exactly that.
        loan_component = [row for row in own["deductions"] if row["code"] == "LOAN-EE"][0]
        other_deductions = sum(
            (Decimal(row["amount"]) for row in own["deductions"] if row["code"] != "LOAN-EE"),
            Decimal(0),
        )
        assert Decimal(loan_component["amount"]) == (
            (Decimal(own["gross"]) - other_deductions) * Decimal("20") / Decimal(100)
        ).quantize(Decimal("0.000001")), loan_component
        assert "deferred by the recovery policy" in loan_component["note"], loan_component
        assert Decimal(loan_rows[0]["outstanding"]) == Decimal("6000.000000") - Decimal(
            loan_component["amount"]
        ), loan_rows
        # 5 — what it was read from
        assert own["period"] == "2026-06" and own["cutoff_date"] == "2026-06-15"
        assert own["run_revision"] == 1 and own["run_state"] == "computed"
        assert own["pack_versions"] == load_pack("philippines")["version"]
        assert own["currency"] == "PHP"
        print(
            f"the payslip's earnings add up to the run's gross {ana_line.gross}, its"
            f" deductions to {ana_line.deductions_total} and its net is the difference; the"
            " employer's own share sits beside the pay, and the loan shows what was left"
            " after this period's recovery"
        )

        # 3 — a later revision changes its own figures and leaves the earlier reading alone
        record_punch(
            session, ana, at="2026-06-02T09:00:00", direction="in", source="manual",
            actor="supervisor", reason="a forgotten day, entered late",
        )
        record_punch(
            session, ana, at="2026-06-02T18:00:00", direction="out", source="manual",
            actor="supervisor", reason="a forgotten day, entered late",
        )
        session.commit()
        revision_two = correct_run(
            session, run, actor="payroll", reason="one day's punches arrived late"
        )
        compute_run(session, revision_two, actor="payroll")
        session.commit()
        again = payslip(session, ana_line, subject="payroll")
        assert again == own, "the superseded payslip changed after a later run"
        changed = payslip(session, line_for(session, revision_two, ana), subject="payroll")
        assert changed["gross"] != own["gross"], "the new revision did not recompute"
        assert changed["run_revision"] == 2 and changed["run_state"] == "computed"
        assert changed["days"]["worked"] == 2 and changed["days"]["absent"] == 28, changed["days"]
        print(
            "a correction with the missing day recomputes the period to a different gross,"
            " while the superseded revision's payslip still reads exactly what it read before"
            " — it is a reading of the run, not a copy of it"
        )

    engine.dispose()
    print("ok — the payslip is the run's figures, reachable by its owner and by payroll")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
