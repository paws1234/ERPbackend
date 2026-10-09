"""T-5.PAY.02 check — a month computed from the contract, the roster, the punches and leave.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_payroll_run.py

It fails (non-zero exit) if any of these stops holding:

1. **attendance and leave feed the calculation, and every figure is traceable** — the basic pay
   is the sum of the days: a full day pays a day, a short day pays the fraction of the schedule
   worked, a late day is short by exactly its unworked minutes, an approved paid leave day pays
   a day, an unpaid leave day pays nothing, and overtime is priced at its band's multiplier.
   Each day carries its minutes and its **source record** (the day's attendance, or the leave
   request that covered it), and each figure carries the component and basis that produced it
2. **the same inputs produce the same figures** — a correction that appends a revision
   recomputes an unchanged employee to exactly the numbers the superseded revision holds
3. **an approved run is closed** — recomputing it is refused, a second run for the period is
   refused, and the way forward is a correction that names who asked and why, after which the
   approved revision still holds its own lines and figures
4. **incomplete attendance is flagged, never paid a default** — an employee with no roster gets
   no invented schedule: their line is marked incomplete, pays nothing, and **blocks approval**
   until the roster is stated, which is then done as a correction
5. **the run states the period, cutoff and pack version used** — and the totals on the line add
   up: net is gross minus the deductions, with the employer's own contributions reported beside
   it rather than taken out of anybody's pay
6. the figures are history: the database refuses to change or remove a line

**Scratch database only**: it drops and recreates the schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.audit import set_actor  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.hr.attendance import record_punch  # noqa: E402
from app.hr.employees import create_employee, record_contract  # noqa: E402
from app.hr.leave import balance, define_leave_type, record_entry  # noqa: E402
from app.hr.leave_requests import decide_request, request_leave  # noqa: E402
from app.hr.overtime import state_rule  # noqa: E402
from app.hr.shifts import define_shift, roster_employee  # noqa: E402
from app.ledger import posting  # noqa: E402,F401 — every check builds the one schema
from app.payroll.components import (  # noqa: E402
    define_component,
    load_statutory_components,
)
from app.payroll.engine import (  # noqa: E402
    IncompleteAttendanceError,
    InvalidRunError,
    RunClosedError,
    approve_run,
    compute_run,
    correct_run,
    line_for,
    line_payload,
    lines_of,
    run_payload,
    start_run,
)
from app.workflow import APPROVE, configure  # noqa: E402

PERIOD = "2026-06"
# The shift: 09:00–18:00 with an hour's break is 480 scheduled minutes.
SCHEDULED = 480


def _refuses(call, expected: type, what: str) -> Exception:
    """The refusal `call` raises; fail the check if it does not refuse."""
    try:
        call()
    except expected as exc:
        return exc
    raise AssertionError(f"{what} was accepted")


def _refused(call, expected: str) -> str:
    """The database's message if `call` is refused; fail the check otherwise."""
    try:
        call()
    except DBAPIError as exc:
        message = str(exc.orig).strip()
        assert expected in message, f"unclear database error: {message}"
        return message
    raise AssertionError(f"the database accepted what it must refuse ({expected!r})")


def _figures(payload: dict) -> dict:
    """A line's figures without the identifiers, so two revisions can be compared."""
    return {
        key: payload[key]
        for key in (
            "employee",
            "basic_salary",
            "daily_rate",
            "worked_days",
            "paid_leave_days",
            "unpaid_leave_days",
            "holiday_days",
            "absent_days",
            "overtime_minutes",
            "late_minutes",
            "reference_minutes",
            "basic_pay",
            "gross",
            "taxable_gross",
            "deductions_total",
            "employer_contributions_total",
            "net",
        )
    } | {
        "components": [
            (row["code"], row["amount"], row["basis"]) for row in payload["components"]
        ],
        "days": [
            (
                row["on"],
                row["kind"],
                row["worked_minutes"],
                row["late_minutes"],
                row["overtime_minutes"],
                row["amount"],
            )
            for row in payload["days"]
        ],
    }


def _business_day(session: Session, employee, *, on: date, arrives: str, leaves: str) -> None:
    """One day's punches, from the clock times as written."""
    record_punch(
        session, employee, at=f"{on.isoformat()}T{arrives}:00", direction="in", source="manual",
        actor="supervisor", reason="timesheet",
    )
    record_punch(
        session, employee, at=f"{on.isoformat()}T{leaves}:00", direction="out", source="manual",
        actor="supervisor", reason="timesheet",
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
                code="PAY-RUN",
                name="Payroll run check",
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
            number="E-701",
            hire_date="2025-01-06",
            subject="hr",
            name="Ana Reyes",
        )
        ben = create_employee(
            session,
            company_id=company_id,
            party_code="BEN",
            number="E-702",
            hire_date="2025-01-06",
            subject="hr",
            name="Ben Reyes",
        )
        record_contract(
            session, ana, subject="hr", effective_from="2025-01-06",
            contract_type="regular", basic_salary="30000",
        )
        record_contract(
            session, ben, subject="hr", effective_from="2025-01-06",
            contract_type="regular", basic_salary="20000",
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
        # Ana is rostered; Ben deliberately is not.
        roster_employee(session, ana, shift=day_shift, effective_from="2025-01-06")
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
            session, ana, leave_type=vacation, kind="accrual", days="20",
            on=date(2026, 6, 1), period="2026-06",
        )
        session.commit()

        # 1 — Ana's month: four kinds of day, from the punches
        _business_day(session, ana, on=date(2026, 6, 1), arrives="09:00", leaves="18:00")
        _business_day(session, ana, on=date(2026, 6, 2), arrives="09:00", leaves="17:00")
        _business_day(session, ana, on=date(2026, 6, 3), arrives="09:25", leaves="18:00")
        _business_day(session, ana, on=date(2026, 6, 4), arrives="09:00", leaves="20:00")
        session.commit()
        paid_leave = request_leave(
            session, ana, leave_type=vacation, from_date="2026-06-08", to_date="2026-06-08",
            actor="ana",
        )
        decide_request(
            session, paid_leave, actor="maria", action=APPROVE, role="line manager"
        )
        unpaid_leave = request_leave(
            session, ana, leave_type=unpaid, from_date="2026-06-09", to_date="2026-06-09",
            actor="ana", override_actor="hr-head", override_reason="no balance: docked pay agreed",
        )
        decide_request(
            session, unpaid_leave, actor="maria", action=APPROVE, role="line manager"
        )
        # The structure: the pack's statutory deductions, plus the company's own allowance.
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
            amount="2000",
            taxable=True,
            order=5,
            effective_from="2026-01-01",
        )
        session.commit()

        run = start_run(
            session, company_id=company_id, period=PERIOD, actor="payroll", cutoff_day=15
        )
        compute_run(session, run, actor="payroll")
        session.commit()

        # 5 — the run states what it was computed from
        stated = run_payload(session, run)
        assert stated["period"] == PERIOD and stated["state"] == "computed"
        assert stated["from_date"] == "2026-06-01" and stated["to_date"] == "2026-06-30"
        assert stated["cutoff_date"] == "2026-06-15", stated["cutoff_date"]
        assert stated["pack_versions"] == "1.0.0" and stated["structure_as_of"] == "2026-06-30"
        assert stated["computed_by"] == "payroll"
        assert stated["employees"] == 2 and stated["incomplete_lines"] == 1

        ana_line = line_for(session, run, ana)
        ana_payload = line_payload(session, ana_line)
        assert ana_line.period_days == 30 and ana_line.daily_rate == Decimal("1000.000000")
        assert ana_line.worked_days == 4 and ana_line.paid_leave_days == 1
        assert ana_line.unpaid_leave_days == 1 and ana_line.absent_days == 24, (
            ana_line.absent_days
        )
        assert ana_line.late_minutes == 15 and ana_line.overtime_minutes == 120
        assert ana_line.reference_minutes == SCHEDULED
        # The days, in the order they happened: a full day, a short day, a late day, a long
        # day, a paid leave day and an unpaid one — each with the record it came from.
        days = {row["on"]: row for row in ana_payload["days"]}
        assert days["2026-06-01"]["amount"] == "1000.000000", days["2026-06-01"]
        assert days["2026-06-02"]["amount"] == "875.000000", days["2026-06-02"]
        assert days["2026-06-03"]["amount"] == "947.916667", days["2026-06-03"]
        assert days["2026-06-03"]["late_minutes"] == 15
        assert days["2026-06-04"]["amount"] == "1000.000000"  # overtime is not in the basic
        assert days["2026-06-04"]["overtime_minutes"] == 120
        assert days["2026-06-04"]["multiplier"] == "1.250"
        assert days["2026-06-04"]["source_type"] == "attendance"
        assert days["2026-06-08"]["kind"] == "paid_leave"
        assert days["2026-06-08"]["source_type"] == "leave_request"
        assert days["2026-06-08"]["source_id"] == str(paid_leave.id)
        assert days["2026-06-09"]["kind"] == "unpaid_leave"
        assert days["2026-06-09"]["amount"] == "0.000000"
        assert days["2026-06-05"]["kind"] == "absent" and days["2026-06-05"]["amount"] == "0.000000"
        assert ana_line.basic_pay == Decimal("4822.916667"), ana_line.basic_pay
        # The figure, and the component and basis it came from.
        overtime = [row for row in ana_payload["components"] if row["code"] == "OVERTIME"]
        assert len(overtime) == 1 and overtime[0]["amount"] == "312.500000", overtime
        assert overtime[0]["basis"] == "attendance" and overtime[0]["source"] == "attendance"
        allowance = [row for row in ana_payload["components"] if row["code"] == "ALLOWANCE"]
        assert allowance[0]["amount"] == "2000.000000"
        assert allowance[0]["basis"] == "fixed" and allowance[0]["source"] == "company"
        assert ana_line.gross == Decimal("7135.416667"), ana_line.gross
        print(
            "the month is the days: a full day 1000, an early out 875, a late arrival"
            " 947.916667, a long day 1000 with 120 minutes of overtime at 1.250, a paid leave"
            " day a day, an unpaid leave day nothing — each day naming its source record"
        )

        # 5 — the deductions are the pack's, the employer's share is not the employee's
        sss = [row for row in ana_payload["components"] if row["code"] == "SSS-EE"][0]
        er = [row for row in ana_payload["components"] if row["code"] == "SSS-ER"][0]
        assert sss["amount"] == "1500.000000" and sss["source"] == "pack", sss
        assert er["amount"] == "3000.000000" and er["kind"] == "employer_contribution", er
        assert ana_line.deductions_total == Decimal("2850.000000"), ana_line.deductions_total
        assert ana_line.employer_contributions_total == Decimal(
            "4350.000000"
        ), ana_line.employer_contributions_total
        assert ana_line.net == Decimal("4285.416667"), ana_line.net
        assert ana_line.net == ana_line.gross - ana_line.deductions_total
        assert ana_line.taxable_gross == ana_line.gross
        print(
            "the deductions are the pack's own rates (1500.000000 at 5% of the basic salary)"
            " while the employer's 3000.000000 is reported beside the pay rather than taken"
            " out of it, and net is gross minus the deductions"
        )

        # 4 — Ben has no roster: flagged, paid nothing, and the run cannot be approved
        ben_line = line_for(session, run, ben)
        ben_payload = line_payload(session, ben_line)
        assert ben_line.incomplete and ben_line.basic_pay == Decimal("0.000000"), ben_line.basic_pay
        assert "no shift is on the roster" in ben_line.flag_reason, ben_line.flag_reason
        assert "+27 more day(s)" in ben_line.flag_reason, ben_line.flag_reason
        assert all(row["kind"] == "not_worked" for row in ben_payload["days"])
        refused = _refuses(
            lambda: approve_run(session, run, actor="controller"),
            IncompleteAttendanceError,
            "approving a run whose lines could not be computed",
        )
        assert "E-702" in str(refused) and "no shift is on the roster" in str(refused), refused
        session.rollback()
        print(
            "an employee nobody rostered is not paid a guessed schedule: 30 days flagged,"
            " nothing paid, and the run refuses to be approved while the flag stands"
        )

        # 2 + 3 — the correction: a revision, with a name and a reason
        _refuses(
            lambda: compute_run(session, run, actor="payroll"),
            InvalidRunError,
            "computing a run twice",
        )
        session.rollback()
        roster_employee(session, ben, shift=day_shift, effective_from="2026-06-01")
        session.commit()
        _refuses(
            lambda: correct_run(session, run, actor="payroll", reason="   "),
            InvalidRunError,
            "a correction with no reason",
        )
        session.rollback()
        revision_two = correct_run(
            session,
            run,
            actor="payroll",
            reason="Ben's roster was missing for June; he is on the day shift",
        )
        compute_run(session, revision_two, actor="payroll")
        session.commit()
        assert revision_two.revision == 2 and revision_two.supersedes_id == run.id
        assert run.superseded_by_id == revision_two.id and run.state == "computed"
        # Ana's inputs did not change, so her figures did not either — the whole line, not one
        # number of it, and every day of it.
        assert _figures(line_payload(session, line_for(session, revision_two, ana))) == _figures(
            ana_payload
        )
        assert line_payload(session, line_for(session, run, ana)) == ana_payload
        # Ben's, now that his roster exists, are computed from his own absences.
        ben_again = line_payload(session, line_for(session, revision_two, ben))
        assert ben_again["incomplete"] is False and ben_again["absent_days"] == 30
        assert ben_again["basic_pay"] == "0.000000"
        approve_run(session, revision_two, actor="controller")
        session.commit()
        assert revision_two.state == "approved" and revision_two.approved_by == "controller"
        _refuses(
            lambda: compute_run(session, revision_two, actor="payroll"),
            RunClosedError,
            "recomputing an approved run",
        )
        session.rollback()
        _refuses(
            lambda: start_run(
                session, company_id=company_id, period=PERIOD, actor="payroll"
            ),
            InvalidRunError,
            "a second run for a period that already has one",
        )
        session.rollback()
        assert run_payload(session, revision_two)["revision"] == 2
        assert run_payload(session, revision_two)["correction_reason"].startswith("Ben's roster")
        assert len(lines_of(session, revision_two)) == 2
        print(
            "a correction names who asked and why, appends revision 2, recomputes an unchanged"
            " employee to the very same figures and days, and lets the run be approved; the"
            " approved run is then closed to recomputation and to a second run for the period"
        )

        # 4 again — a missing contract is a flag too, not a reason to fail the whole run
        carl = create_employee(
            session,
            company_id=company_id,
            party_code="CARL",
            number="E-703",
            hire_date="2026-06-01",
            subject="hr",
            name="Carl Santos",
        )
        session.commit()
        revision_three = correct_run(
            session, revision_two, actor="payroll", reason="a joiner is on the payroll now"
        )
        compute_run(session, revision_three, actor="payroll")
        session.commit()
        carl_line = line_for(session, revision_three, carl)
        assert carl_line.incomplete and carl_line.contract_id is None
        assert "has no contract in force" in carl_line.flag_reason, carl_line.flag_reason
        assert carl_line.basic_salary == Decimal("0.000000")
        # The other two are unchanged by a third line: the run still holds what it held.
        assert line_for(session, revision_three, ana).net == Decimal("4285.416667")
        _refuses(
            lambda: approve_run(session, revision_three, actor="controller"),
            IncompleteAttendanceError,
            "approving a run with an employee who has no contract",
        )
        session.rollback()
        print(
            "an employee with no contract is flagged on their own line with the reason, the"
            " rest of the run is still computed, and the run still refuses to be approved"
        )

        # 6 — the figures are history
        _refused(
            lambda: session.execute(
                text("UPDATE payroll_line SET net = 0 WHERE id = :id"), {"id": str(ana_line.id)}
            ),
            "is append-only",
        )
        session.rollback()
        assert line_for(session, run, ana).net == Decimal("4285.416667")

    engine.dispose()
    print(
        "ok — a month computed from the contract, the roster, the punches and leave, flagged"
        " where the records were incomplete, and reproducible from the same inputs"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
