"""T-5.PAY.05 — a payslip is a **reading** of a run, not a document that can drift.

There is no payslip table, and that is the design: everything a payslip shows is read from the
run's own lines, components and days (T-5.PAY.02), so a payslip regenerated years later — after
further runs, further corrections and further everything — reproduces the figures it showed
the first time, because those figures were never copied anywhere else. A stored payslip is a
second copy of the truth, and this module's whole job is to avoid being one.

Two rules it is built around:

* **The totals on a payslip are the run's totals.** Earnings add up to the line's gross, the
  deductions add up to its deductions, net is gross minus the deductions, and the employer's
  own contributions sit beside the pay rather than inside it. A payslip that disagrees with
  payroll is not a payslip, so the check asserts the two against each other, not against a
  figure written here.
* **Who may read one is decided, not assumed.** An employee reading their own payslip is
  reading their own record — the caller states **which party the reader is**, resolved from the
  authenticated subject the way the rest of the platform resolves a caller — and anybody else
  needs the `payslip.read` capability, refused through T-0.SEC.01 so the attempt is on the
  record. `ponytail`: the ledger has no user→employee link yet (a subject is a login and roles
  hang off it, nothing more), so the party is stated by the caller; when the self-service
  surface lands (T-6), that resolution moves behind this function and its callers do not
  change.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from app.company import company_base_currency
from app.payroll import loans
from app.payroll.engine import PayrollLine, PayrollRun, line_components, line_inputs, lines_of
from app.security import require

# The capability a payroll role holds to read somebody else's payslip; the scope, as
# T-0.SEC.01 models scopes.
PAYSLIP_CAPABILITY = "payslip.read"
PAYSLIP_ENTITY = "payroll_line"


def _may_read(session: Session, line: PayrollLine, *, subject: str, viewer_party) -> None:
    """Refuse unless the reader is the employee or holds the payslip capability."""
    if viewer_party is not None and viewer_party.id == line.employee.party_id:
        return
    require(
        session,
        company_id=line.company_id,
        subject=subject,
        capability=PAYSLIP_CAPABILITY,
        entity=PAYSLIP_ENTITY,
        entity_id=line.id,
    )


def _reading(session: Session, line: PayrollLine) -> dict:
    """The payslip itself, with no question about who is asking — see :func:`payslip`.

    Reading a line is reading history: nothing here consults the current contract, the current
    roster or today's date.
    """
    run = line.run
    components = line_components(session, line)
    earnings = [
        {"code": row.code, "name": row.name, "amount": str(row.amount)}
        for row in components
        if row.kind == "earning"
    ]
    deductions = [
        {"code": row.code, "name": row.name, "amount": str(row.amount), "note": row.note}
        for row in components
        if row.kind == "deduction"
    ]
    contributions = [
        {"code": row.code, "name": row.name, "amount": str(row.amount)}
        for row in components
        if row.kind == "employer_contribution"
    ]
    days = line_inputs(session, line)
    loans_held = [
        {
            "reference": loan.reference,
            "principal": str(loan.principal),
            "outstanding": str(loans.outstanding_after(session, loan, period=run.period)),
        }
        for loan in loans.loans_of(session, line.employee)
    ]
    return {
        "employee": line.employee.number,
        "employee_name": line.employee.party.name,
        "currency": company_base_currency(session, company_id=line.company_id),
        "period": run.period,
        "from_date": run.from_date.isoformat(),
        "to_date": run.to_date.isoformat(),
        "cutoff_date": run.cutoff_date.isoformat(),
        "run_revision": run.revision,
        "run_state": run.state,
        "pack_versions": run.pack_versions,
        "basic_salary": str(line.basic_salary),
        "daily_rate": str(line.daily_rate),
        "basic_pay": str(line.basic_pay),
        "earnings": earnings,
        "gross": str(line.gross),
        "taxable_gross": str(line.taxable_gross),
        "deductions": deductions,
        "deductions_total": str(line.deductions_total),
        "employer_contributions": contributions,
        "employer_contributions_total": str(line.employer_contributions_total),
        "net": str(line.net),
        "days": {
            "period": line.period_days,
            "employed": line.employed_days,
            "worked": line.worked_days,
            "paid_leave": line.paid_leave_days,
            "unpaid_leave": line.unpaid_leave_days,
            "holiday": line.holiday_days,
            "absent": line.absent_days,
            "days_recorded": len(days),
        },
        "minutes": {
            "overtime": line.overtime_minutes,
            "late": line.late_minutes,
            "reference_schedule": line.reference_minutes,
        },
        "loans": loans_held,
        "attention": line.flag_reason,
    }


def payslip(session: Session, line: PayrollLine, *, subject: str, viewer_party=None) -> dict:
    """One employee's payslip for one run, for one reader.

    `viewer_party` is the party the reader **is**, where the reader is an employee; a payroll
    role states the subject it was authenticated as and is checked for the capability. An
    employee reading their own payslip needs no capability — it is their own record.
    """
    _may_read(session, line, subject=subject, viewer_party=viewer_party)
    return _reading(session, line)


def payslips_for_run(
    session: Session, run: PayrollRun, *, subject: str, viewer_party=None
) -> list[dict]:
    """Every payslip of a run, for a reader a payroll role authorised.

    The scope is the capability: an employee holding no payroll role reaches only their own
    line through :func:`payslip`, and this function refuses them the rest. The capability is
    asked for **once** for the whole run rather than once per payslip — the answer cannot
    change inside one request.
    """
    require(
        session,
        company_id=run.company_id,
        subject=subject,
        capability=PAYSLIP_CAPABILITY,
        entity=PAYSLIP_ENTITY,
        entity_id=run.id,
    )
    return [_reading(session, line) for line in lines_of(session, run)]


def reconciles(session: Session, line: PayrollLine, slip: dict) -> Decimal:
    """The difference between a payslip and its line: zero, or a defect.

    The three claims a payslip makes — the earnings add up to the gross, the deductions add up
    to what was deducted, and net is gross minus the deductions — returned as one number so a
    caller can assert it rather than trust it.
    """
    gross = sum((Decimal(row["amount"]) for row in slip["earnings"]), Decimal(0))
    deducted = sum((Decimal(row["amount"]) for row in slip["deductions"]), Decimal(0))
    difference = gross - Decimal(slip["gross"])
    difference += deducted - Decimal(slip["deductions_total"])
    difference += (
        Decimal(slip["gross"]) - Decimal(slip["deductions_total"]) - Decimal(slip["net"])
    )
    difference += Decimal(slip["net"]) - line.net
    difference += Decimal(slip["gross"]) - line.gross
    difference += Decimal(slip["deductions_total"]) - line.deductions_total
    return difference
