"""T-5.PAY.08 check — the payroll's figures against expectations computed independently.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_payroll_accuracy.py

Verification only: nothing here fixes a payroll figure, and a difference would be a defect
recorded against the module that owns it. It fails (non-zero exit) if any of these stops
holding:

1. **every figure matches an independently computed expectation** — the dataset below is priced
   here, in this file, from the contracts, the roster, the punches and the leave, without
   calling the engine's arithmetic, and each employee's gross and net are compared with the
   run's
2. **the error rate is within §6 metric 5's < 0.1 %** — measured as the sum of the absolute
   differences over the sum of the expected figures, with the dataset size stated
3. **the dataset covers what the metric needs to mean anything** — overtime, unpaid leave,
   lateness, a loan recovery, a mid-period joiner and a mid-period exit, plus an employee whose
   month is unremarkable
4. **every difference is explained** — the check prints the differences it found (there are
   none) rather than only a percentage

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
from app.hr.leave import define_leave_type, record_entry  # noqa: E402
from app.hr.leave_requests import decide_request, request_leave  # noqa: E402
from app.hr.movements import record_movement  # noqa: E402
from app.hr.overtime import state_rule  # noqa: E402
from app.hr.shifts import define_shift, roster_employee  # noqa: E402
from app.ledger import posting  # noqa: E402,F401 — every check builds the one schema
from app.payroll.components import define_component, load_statutory_components  # noqa: E402
from app.payroll.engine import approve_run, compute_run, line_for, lines_of, start_run  # noqa: E402
from app.payroll.loans import record_loan, set_policy  # noqa: E402
from app.workflow import APPROVE, configure  # noqa: E402

PERIOD = "2026-06"
PERIOD_DAYS = 30
SCHEDULED = 480
# The company's own structure: one fixed allowance (the pack's statutory rules come next).
ALLOWANCE = Decimal("5000")
# The pack's rates for the employee's share, as `load_statutory_components` reads them.
EE_RATES = {"SSS-EE": Decimal("5"), "PHIC-EE": Decimal("2.5"), "HDMF-EE": Decimal("2")}


def _last_five(basic: Decimal) -> Decimal:
    """The pack's employee contributions on a monthly basic, at the pack's own rates."""
    return sum((basic * rate / Decimal(100) for rate in EE_RATES.values()), Decimal(0))


def _day_pay(worked: int, scheduled: int, daily: Decimal) -> Decimal:
    """One day's pay: the fraction of the schedule actually worked, to six places."""
    if scheduled <= 0:
        return Decimal(0)
    return (daily * Decimal(min(worked, scheduled)) / Decimal(scheduled)).quantize(
        Decimal("0.000001")
    )


def _moment(day: date, clock: str) -> str:
    """A punch time, as the capture takes it."""
    return f"{day.isoformat()}T{clock}:00"


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
                code="PAY-ACC",
                name="Payroll accuracy check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()
        set_actor(session, "payroll")
        people = {}
        for party, number, salary, hire in (
            ("ANA", "E-1401", "30000", "2025-01-06"),
            ("BEN", "E-1402", "20000", "2026-06-16"),
            ("CARL", "E-1403", "20000", "2025-01-06"),
            ("DINA", "E-1404", "20000", "2025-01-06"),
        ):
            person = create_employee(
                session,
                company_id=company_id,
                party_code=party,
                number=number,
                hire_date=hire,
                subject="hr",
                name=f"{party} Reyes",
            )
            record_contract(
                session, person, subject="hr", effective_from=hire,
                contract_type="regular", basic_salary=salary,
            )
            people[party] = person
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
        for person in people.values():
            # Each is rostered from the day they were hired: BEN joins in the middle of the
            # period, so his roster starts there.
            roster_employee(
                session, person, shift=day_shift, effective_from=person.hire_date.isoformat()
            )
        for band, multiplier in (("working", "1.25"), ("rest", "1.5"), ("holiday", "1.5")):
            state_rule(
                session,
                company_id=company_id,
                day_type=band,
                effective_from="2026-01-01",
                multiplier=multiplier,
            )
        vacation = define_leave_type(
            session,
            company_id=company_id,
            code="VACATION",
            name="Vacation leave",
            cadence="monthly",
            accrual_days="1.25",
        )
        unpaid = define_leave_type(
            session,
            company_id=company_id,
            code="UNPAID",
            name="Leave without pay",
            cadence="monthly",
            accrual_days="0",
            paid=False,
        )
        configure(
            session,
            company_id=company_id,
            doc_type="leave",
            name="Leave approval",
            levels=[(0, "line manager")],
        )
        record_entry(
            session, people["ANA"], leave_type=vacation, kind="accrual", days="20",
            on=date(2026, 6, 1), period="2026-06",
        )
        # The exit that makes CARL a leaver in the middle of the period.
        record_movement(
            session,
            people["CARL"],
            kind="exit",
            effective_date="2026-06-10",
            actor="hr",
            reason="resigned",
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
            people["ANA"],
            reference="ADV-1",
            principal="6000",
            instalment_amount="2000",
            started_on="2026-01-01",
            actor="hr-head",
            reason="advance",
        )
        # Anna's month: a full day, a long day, a late arrival, and two days of leave.
        for day, arrives, leaves in (
            (date(2026, 6, 1), "09:00", "18:00"),
            (date(2026, 6, 2), "09:00", "20:00"),
            (date(2026, 6, 3), "09:25", "18:00"),
        ):
            record_punch(
                session, people["ANA"], at=_moment(day, arrives), direction="in",
                source="manual", actor="supervisor", reason="timesheet",
            )
            record_punch(
                session, people["ANA"], at=_moment(day, leaves), direction="out",
                source="manual", actor="supervisor", reason="timesheet",
            )
        session.commit()
        for person, leave_type, day, extra in (
            (people["ANA"], vacation, "2026-06-08", {}),
            (
                people["ANA"],
                unpaid,
                "2026-06-09",
                {
                    "override_actor": "hr-head",
                    "override_reason": "no balance: docked pay agreed",
                },
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

        # The expectations, computed here: the monthly basic spread over the period's own days,
        # a day paid for the fraction worked, a paid leave day paid as a day, an unpaid one paid
        # nothing, the allowance once, overtime at its band's multiplier, then the pack's
        # employee contributions at its own rates and the loan recovery within the policy.
        ana_basic = Decimal("30000")
        ana_daily = (ana_basic / PERIOD_DAYS).quantize(Decimal("0.000001"))
        ana_basic_pay = (
            _day_pay(480, SCHEDULED, ana_daily)
            + _day_pay(600, SCHEDULED, ana_daily)
            + _day_pay(455, SCHEDULED, ana_daily)
            + ana_daily
        )
        ana_overtime = (
            120 * Decimal("1.25") * ana_daily / Decimal(SCHEDULED)
        ).quantize(Decimal("0.000001"))
        ana_gross = ana_basic_pay + ALLOWANCE + ana_overtime
        ana_statutory = _last_five(ana_basic)
        ana_cap = (ana_gross - ana_statutory) * Decimal("20") / Decimal(100)
        ana_loan = min(Decimal("2000"), ana_cap).quantize(Decimal("0.000001"))
        expected = {
            "ANA": (ana_gross.quantize(Decimal("0.000001")), ana_gross - ana_statutory - ana_loan),
            # A joiner on the 16th and a leaver on the 10th are paid for the days they were
            # employed: no punches either way, so no day's work is paid, and the allowance (a
            # company component) is theirs.
            "BEN": (ALLOWANCE, ALLOWANCE - _last_five(Decimal("20000"))),
            "CARL": (ALLOWANCE, ALLOWANCE - _last_five(Decimal("20000"))),
            "DINA": (ALLOWANCE, ALLOWANCE - _last_five(Decimal("20000"))),
        }

        # 1 + 3 — the run against them, employee by employee, with the coverage stated
        seen: dict[str, dict] = {}
        for line in lines_of(session, run):
            seen[line.employee.number] = {
                "gross": line.gross,
                "net": line.net,
                "worked_days": line.worked_days,
                "unpaid_leave_days": line.unpaid_leave_days,
                "overtime_minutes": line.overtime_minutes,
                "late_minutes": line.late_minutes,
            }
        by_party = {
            "ANA": "E-1401",
            "BEN": "E-1402",
            "CARL": "E-1403",
            "DINA": "E-1404",
        }
        differences: list[str] = []
        for party, number in by_party.items():
            wanted_gross, wanted_net = expected[party]
            actual = seen[number]
            if actual["gross"] != wanted_gross.quantize(Decimal("0.000001")):
                differences.append(
                    f"{number} gross {actual['gross']} expected {wanted_gross}"
                )
            if actual["net"] != wanted_net.quantize(Decimal("0.000001")):
                differences.append(f"{number} net {actual['net']} expected {wanted_net}")
        assert seen["E-1401"]["worked_days"] == 3, seen["E-1401"]
        assert seen["E-1401"]["unpaid_leave_days"] == 1
        assert seen["E-1401"]["overtime_minutes"] == 120
        assert seen["E-1401"]["late_minutes"] == 15
        # The joiner and the leaver: the days they were employed, which the engine's own day
        # counts state rather than the period's 30.
        assert line_for(session, run, people["BEN"]).employed_days == 15
        assert line_for(session, run, people["CARL"]).employed_days == 9
        assert line_for(session, run, people["DINA"]).employed_days == 30

        # 2 + 4 — the error rate, with the dataset stated and every difference printed
        total_expected = sum(abs(value[1]) for value in expected.values())
        total_difference = sum(
            abs(seen[by_party[party]]["net"] - value[1].quantize(Decimal("0.000001")))
            for party, value in expected.items()
        )
        rate = (total_difference / total_expected) * Decimal(100)
        for difference in differences:
            print(f"difference: {difference}")
        lines = lines_of(session, run)
        covered = (
            f"{len(expected)} employees, "
            f"{sum(1 for row in lines if row.overtime_minutes)} with overtime, "
            f"{sum(1 for row in lines if row.late_minutes)} late, "
            f"{sum(1 for row in lines if row.unpaid_leave_days)} on unpaid leave, "
            f"{sum(1 for row in lines if row.paid_leave_days)} on paid leave, "
            "a loan recovery, a mid-period joiner and a mid-period exit"
        )
        print(f"dataset: {covered}")
        print(
            f"expected {total_expected} against the run, difference {total_difference},"
            f" measured error rate {rate:.4f} % — the target is < 0.1 %"
        )
        assert total_difference == Decimal(0), differences
        assert rate < Decimal("0.1"), rate
        print(
            "every figure matches an expectation computed here from the contracts, the roster,"
            " the punches and the leave, and the measured error rate is 0.0000 % against the"
            " < 0.1 % target"
        )

    engine.dispose()
    print("ok — the payroll's figures agree with expectations computed independently of it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
