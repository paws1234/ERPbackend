"""T-5.X.GATE — the Phase 5 exit criteria, against the running system.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_phase5_exit.py

§4 Phase 5's exit criterion is *"Accurate monthly payroll generation linked to attendance."*
This check drives one month end to end — people, contracts, a roster, punches, leave, the
pack's statutory structure, a loan, bank details, the ledger's mappings — and holds the phase to
its own acceptance criteria:

1. **a monthly payroll run is produced from real attendance and leave** — the month is the days:
   a full day, a long day with its overtime band, a day late, an absence, a paid leave day and an
   unpaid one, and every day on the line names the **source record** it came from (the day's
   attendance, or the leave request that covered it)
2. **the measured payroll error rate is within the < 0.1 % target** (§6 metric 5) — the figures
   are priced here, independently of the engine, from the contracts, the roster, the punches and
   the leave, and the difference over the sum of the expectations is reported with the dataset
   stated
3. **statutory deductions follow the localization pack, and the reports reconcile to the run** —
   the contributions are the pack's own rates on the pack's own bases, the six forms a payroll
   run feeds are produced, and the liabilities the ledger holds reconcile to them
4. **payslips and banking files reconcile exactly to the run's figures** — every payslip
   reconciles to its line to the cent, and the payment file pays the run's net total exactly
5. **the run's GL posting balances, and its totals are the run's** (§6 metric 1) — one entry,
   debits equal credits, and the wage cost, the employer's share and the pay owed are the run's
   own figures
6. **a lifecycle movement is reflected for the period** — an employee who leaves mid-month is
   paid for the days up to the day before the exit and no further, which the run's own day count
   states

**Scratch database only**: it drops and recreates the schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.audit import set_actor  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.hr.attendance import record_punch  # noqa: E402
from app.hr.employees import create_employee, record_contract  # noqa: E402
from app.hr.leave import define_leave_type, record_entry  # noqa: E402
from app.hr.leave_requests import decide_request, request_leave  # noqa: E402
from app.hr.movements import record_movement  # noqa: E402
from app.hr.overtime import state_rule  # noqa: E402
from app.hr.shifts import define_shift, roster_employee  # noqa: E402
from app.ledger import posting  # noqa: E402,F401 — every check builds the one schema
from app.ledger.accounts import import_coa_template  # noqa: E402
from app.ledger.mapping import set_mapping  # noqa: E402
from app.localization import load_pack  # noqa: E402
from app.payroll.bank_files import bank_file, define_bank_details, reconciles  # noqa: E402
from app.payroll.components import define_component, load_statutory_components  # noqa: E402
from app.payroll.engine import (  # noqa: E402
    approve_run,
    compute_run,
    line_for,
    line_inputs,
    lines_of,
    start_run,
)
from app.payroll.loans import record_loan, set_policy  # noqa: E402
from app.payroll.payslips import payslip  # noqa: E402
from app.payroll.payslips import reconciles as slip_reconciles  # noqa: E402
from app.payroll.posting import (  # noqa: E402
    EMPLOYER_CONTRIBUTION_EXPENSE_KEY,
    NET_PAY_PAYABLE_KEY,
    SALARY_EXPENSE_KEY,
    STATUTORY_PAYABLE_KEY,
    post_run,
    posting_payload,
    reconcile_statutory,
    reconciles_to_run,
)
from app.payroll.statutory import payroll_reports, produce_report  # noqa: E402

PERIOD = "2026-06"
PERIOD_DAYS = 30
SCHEDULED = 480
ALLOWANCE = Decimal("5000")


def _last_five(basic: Decimal) -> Decimal:
    """The pack's employee contributions on a monthly basic, at the pack's own rates."""
    return (
        basic * Decimal("5") / Decimal(100)
        + basic * Decimal("2.5") / Decimal(100)
        + basic * Decimal("2") / Decimal(100)
    )


def _day_pay(worked: int, daily: Decimal) -> Decimal:
    """One day's pay: the fraction of the schedule worked, to six places."""
    return (daily * Decimal(min(worked, SCHEDULED)) / Decimal(SCHEDULED)).quantize(
        Decimal("0.000001")
    )


def _punch(session: Session, employee, day: date, arrives: str, leaves: str) -> None:
    """One day's attendance, as the capture takes it."""
    for clock, direction in ((arrives, "in"), (leaves, "out")):
        record_punch(
            session,
            employee,
            at=f"{day.isoformat()}T{clock}:00",
            direction=direction,
            source="manual",
            actor="supervisor",
            reason="timesheet",
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

    company_id = uuid.uuid4()
    with Session(engine) as session:
        session.add(
            Company(
                id=company_id,
                code="PHASE5",
                name="Phase 5 exit check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()
        set_actor(session, "payroll")
        import_coa_template(session, company_id=company_id, market="philippines")
        for key, account in (
            (SALARY_EXPENSE_KEY, "5100"),
            (EMPLOYER_CONTRIBUTION_EXPENSE_KEY, "5120"),
            (STATUTORY_PAYABLE_KEY, "2400"),
            (NET_PAY_PAYABLE_KEY, "2110"),
        ):
            set_mapping(session, company_id=company_id, key=key, account_code=account)
        ana = create_employee(
            session, company_id=company_id, party_code="ANA", number="E-1501",
            hire_date="2025-01-06", subject="hr", name="Ana Reyes",
        )
        ben = create_employee(
            session, company_id=company_id, party_code="BEN", number="E-1502",
            hire_date="2025-01-06", subject="hr", name="Ben Reyes",
        )
        record_contract(
            session, ana, subject="hr", effective_from="2025-01-06",
            contract_type="regular", basic_salary="30000",
        )
        record_contract(
            session, ben, subject="hr", effective_from="2025-01-06",
            contract_type="regular", basic_salary="20000",
        )
        shift = define_shift(
            session, company_id=company_id, code="DAY", name="Day shift",
            starts_at="09:00", ends_at="18:00", break_minutes=60, late_grace_minutes=10,
        )
        roster_employee(session, ana, shift=shift, effective_from="2025-01-06")
        roster_employee(session, ben, shift=shift, effective_from="2025-01-06")
        for band, multiplier in (("working", "1.25"), ("rest", "1.5"), ("holiday", "1.5")):
            state_rule(
                session, company_id=company_id, day_type=band, effective_from="2026-01-01",
                multiplier=multiplier,
            )
        # 6 — the lifecycle movement: Ben leaves in the middle of the period.
        record_movement(
            session, ben, kind="exit", effective_date="2026-06-10", actor="hr",
            reason="resigned",
        )
        vacation = define_leave_type(
            session, company_id=company_id, code="VACATION", name="Vacation leave",
            cadence="monthly", accrual_days="1.25",
        )
        unpaid = define_leave_type(
            session, company_id=company_id, code="UNPAID", name="Leave without pay",
            cadence="monthly", accrual_days="0", paid=False,
        )
        from app.workflow import APPROVE, configure  # noqa: E402 — the leave chain

        configure(
            session, company_id=company_id, doc_type="leave", name="Leave approval",
            levels=[(0, "line manager")],
        )
        record_entry(
            session, ana, leave_type=vacation, kind="accrual", days="20",
            on=date(2026, 6, 1), period="2026-06",
        )
        load_statutory_components(
            session, company_id=company_id, market="philippines", effective_from="2026-01-01"
        )
        define_component(
            session, company_id=company_id, code="ALLOWANCE", name="Transport allowance",
            kind="earning", basis="fixed", amount="5000", taxable=True, order=5,
            effective_from="2026-01-01",
        )
        set_policy(
            session, company_id=company_id, max_recovery_percent="20",
            effective_from="2026-01-01", actor="hr-head",
        )
        record_loan(
            session, ana, reference="ADV-1", principal="6000", instalment_amount="2000",
            started_on="2026-01-01", actor="hr-head", reason="advance",
        )
        for person, account in ((ana, "001234567890"), (ben, "002345678901")):
            define_bank_details(
                session, person, bank_code="BPI", account_number=account,
                holder_name=person.party.name.upper(), effective_from="2026-01-01", actor="hr",
            )
        # 1 — the month's attendance and leave: a full day, a long day, a late day, an
        # absence, a paid leave day and an unpaid one (Ana), and one worked day (Ben).
        _punch(session, ana, date(2026, 6, 1), "09:00", "18:00")
        _punch(session, ana, date(2026, 6, 2), "09:00", "20:00")
        _punch(session, ana, date(2026, 6, 3), "09:25", "18:00")
        _punch(session, ben, date(2026, 6, 2), "09:00", "18:00")
        session.commit()
        for person, leave_type, day, extra in (
            (ana, vacation, "2026-06-08", {}),
            (
                ana,
                unpaid,
                "2026-06-09",
                {"override_actor": "hr-head", "override_reason": "no balance: docked pay"},
            ),
        ):
            application = request_leave(
                session, person, leave_type=leave_type, from_date=day, to_date=day,
                actor="ana", **extra,
            )
            decide_request(
                session, application, actor="maria", action=APPROVE, role="line manager"
            )
        session.commit()

        run = start_run(
            session, company_id=company_id, period=PERIOD, actor="payroll", cutoff_day=15
        )
        compute_run(session, run, actor="payroll")
        approve_run(session, run, actor="controller")
        session.commit()

        # 1 — every figure traceable: the days name the record they came from, and the line's
        # totals are the sum of its days plus its own structure.
        ana_line = line_for(session, run, ana)
        days = {row.on_date: row for row in line_inputs(session, ana_line)}
        assert days[date(2026, 6, 1)].source_type == "attendance"
        assert days[date(2026, 6, 8)].source_type == "leave_request"
        assert days[date(2026, 6, 8)].kind == "paid_leave"
        assert days[date(2026, 6, 9)].kind == "unpaid_leave"
        assert days[date(2026, 6, 4)].kind == "absent"
        assert days[date(2026, 6, 2)].overtime_minutes == 120
        assert days[date(2026, 6, 3)].late_minutes == 15
        assert all(row.source_type in ("attendance", "leave_request") for row in days.values())
        assert len(days) == 30
        print(
            "the month is the days: a full day, 120 minutes of overtime banded at 1.250, a"
            " 15-minute late arrival, an absence, a paid leave day and an unpaid one — each"
            " naming the attendance or the leave request it came from"
        )

        # 6 — the leaver is paid for the days up to the day before the exit
        ben_line = line_for(session, run, ben)
        assert ben_line.employed_days == 9, ben_line.employed_days
        assert len(line_inputs(session, ben_line)) == 9
        assert [row.on_date for row in line_inputs(session, ben_line)][-1] == date(2026, 6, 9)
        assert ben_line.worked_days == 1 and ben_line.absent_days == 8

        # 2 — the error rate, against figures priced here rather than by the engine
        ana_daily = (Decimal("30000") / PERIOD_DAYS).quantize(Decimal("0.000001"))
        ana_pay = (
            _day_pay(480, ana_daily) + _day_pay(600, ana_daily) + _day_pay(455, ana_daily)
            + ana_daily  # the paid leave day
        )
        ana_overtime = (120 * Decimal("1.25") * ana_daily / SCHEDULED).quantize(
            Decimal("0.000001")
        )
        ana_gross = ana_pay + ALLOWANCE + ana_overtime
        ana_statutory = _last_five(Decimal("30000"))
        ana_loan = min(
            Decimal("2000"), (ana_gross - ana_statutory) * Decimal("20") / Decimal(100)
        ).quantize(Decimal("0.000001"))
        ben_daily = (Decimal("20000") / PERIOD_DAYS).quantize(Decimal("0.000001"))
        ben_gross = _day_pay(480, ben_daily) + ALLOWANCE
        expected = {
            "E-1501": (
                ana_gross.quantize(Decimal("0.000001")),
                ana_gross - ana_statutory - ana_loan,
            ),
            "E-1502": (ben_gross, ben_gross - _last_five(Decimal("20000"))),
        }
        lines = {line.employee.number: line for line in lines_of(session, run)}
        differences = [
            f"{number}: gross {lines[number].gross} expected {figures[0]}, net"
            f" {lines[number].net} expected {figures[1]}"
            for number, figures in expected.items()
            if lines[number].gross != figures[0] or lines[number].net != figures[1]
        ]
        total_expected = sum(abs(figures[1]) for figures in expected.values())
        total_difference = sum(
            abs(lines[number].net - figures[1]) for number, figures in expected.items()
        )
        rate = (total_difference / total_expected) * Decimal(100)
        for difference in differences:
            print(f"difference: {difference}")
        print(
            f"dataset: {len(expected)} employees (one of them leaving mid-period), overtime,"
            f" lateness, paid and unpaid leave and a loan recovery; expected {total_expected},"
            f" difference {total_difference}, error rate {rate:.4f} % against the < 0.1 % target"
        )
        assert total_difference == Decimal(0), differences
        assert rate < Decimal("0.1")

        # 3 — the pack's statutory structure, and the reports reconciling to the ledger
        definitions = payroll_reports("philippines")
        assert len(definitions) == 6, definitions
        for report in definitions:
            produced = produce_report(session, run, market="philippines", form=report["form"])
            assert produced["period"] == PERIOD
            assert produced["pack_version"] == load_pack("philippines")["version"]

        # 4 — payslips and the payment file, reconciled to the run
        for number, line in lines.items():
            # Each employee reads their own payslip: no capability is needed for your own
            # record, and `payslips_for_run` covers the payroll role's wider scope (T-5.PAY.05).
            own = payslip(
                session, line, subject=line.employee.number, viewer_party=line.employee.party
            )
            assert slip_reconciles(session, line, own) == Decimal(0), number
            assert Decimal(own["net"]) == line.net
        file_payload = bank_file(session, run, market="philippines")
        assert file_payload["excluded"] == [], file_payload["excluded"]
        # A net pay that is not a whole number of cents is paid to the cent, with the fraction
        # stated: the file, the excluded list and that rounding are the whole payroll.
        assert Decimal(file_payload["total"]) - Decimal(file_payload["rounding_total"]) == Decimal(
            file_payload["run_net_total"]
        ), (file_payload["total"], file_payload["rounding_total"])
        assert reconciles(file_payload) == Decimal(0)

        # 5 — the posting: balanced, and the run's own figures
        entry = post_run(session, run, actor="controller")
        session.commit()
        payload = posting_payload(session, run)
        assert reconciles_to_run(session, run, payload) == Decimal(0)
        stored = session.execute(
            text("SELECT coalesce(sum(debit), 0), coalesce(sum(credit), 0) FROM journal_line")
        ).one()
        assert stored[0] == stored[1] == sum(
            (line.gross + line.employer_contributions_total for line in lines.values()),
            Decimal(0),
        ), stored
        assert reconcile_statutory(session, run, market="philippines") == Decimal(0)
        print(
            f"the statutory reports and the ledger agree (difference 0), every payslip"
            f" reconciles to its line, the payment file pays the run's net"
            f" to the cent ({file_payload['total']}, rounding"
            f" {file_payload['rounding_total']}), and the posting balances at {stored[0]} with"
            " the statutory liabilities reconciled to the returns"
        )

    engine.dispose()
    print(
        "ok — Phase 5's exit criterion: accurate monthly payroll generation linked to attendance"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
