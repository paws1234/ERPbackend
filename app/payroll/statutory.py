"""T-5.PAY.04 — the statutory reports a payroll run feeds, from the pack's own definitions.

A report is not code: **which forms exist, who they go to, what they cover and which rules
they collect** are all stated by the localization pack (T-0.LOC.01), and this module's whole
job is to read one run and put that market's own figures into that market's own form. Nothing
here knows what a contribution is called, what it is charged at, or which authority wants it.

Two consequences this module is built around:

* **A report reconciles to the run it came from, or it is a defect.** Every figure on a report
  is read from the run's own line components, and `reconcile_report` re-reads them from the
  stored rows and reports the difference — so "the return agrees with the payroll" is a
  computed answer rather than a claim.
* **A form nobody feeds is refused, and a market with no pack is reported.** A form the pack
  states but no payroll run feeds (a VAT return, say) is refused by name rather than produced
  as an empty report, and a market the packs do not cover raises the localization package's own
  refusal — an empty or silent statutory return is the one kind of report that must never be
  produced.

What is deliberately *not* here: electronic filing with a revenue authority (the plan names
report production, not filing), payslips and bank files (T-5.PAY.05/06), and posting the
liabilities to the ledger (T-5.PAY.07, which reads the same components).
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from app.localization import load_pack, statutory_reports
from app.payroll.engine import PayrollRun, line_components, lines_of

# The states a run is in when a report can be produced: a draft has nothing computed yet, and
# an approved run's report is the one that was filed against.
REPORTABLE_STATES = ("computed", "approved")


class StatutoryError(ValueError):
    """Statutory reporting refused what was asked of it."""


class UnknownReportError(StatutoryError):
    """The pack states no such form."""


class NotAPayrollReportError(StatutoryError):
    """The pack states the form, but no payroll run feeds it."""


class RunNotReportableError(StatutoryError):
    """The run has not been computed, so there is nothing to report from."""


def payroll_reports(market: str) -> list[dict]:
    """The forms a payroll run feeds, as the pack states them — the forms it states no rules
    for are somebody else's returns."""
    return [report for report in statutory_reports(market) if report.get("covers_rules")]


def _definition(market: str, form: str) -> dict:
    """The pack's own statement of one form, or a refusal that says what it does state."""
    wanted = str(form).strip()
    states = statutory_reports(market)
    for report in states:
        if report.get("form") == wanted:
            if not report.get("covers_rules"):
                raise NotAPayrollReportError(
                    f"the {market!r} pack states the form {wanted!r} but no payroll rule"
                    f" feeds it (it covers {report.get('covers')!r}); a payroll report is"
                    " produced from a payroll run, and an empty return is not a report"
                )
            return report
    raise UnknownReportError(
        f"the {market!r} pack states no form {wanted!r}; it states"
        f" {', '.join(sorted(state.get('form', '?') for state in states))}"
    )


def reconcile_report(session: Session, run: PayrollRun, report: dict) -> Decimal:
    """The difference between a report's totals and the run's stored components: zero, or a bug.

    Read fresh from the rows rather than from the report's own arithmetic, which is what makes
    it a check: the report says what payroll paid, and payroll's rows say what payroll paid.
    """
    codes = set(report["rules"])
    stored = Decimal(0)
    for line in lines_of(session, run):
        for component in line_components(session, line):
            if component.code in codes:
                stored += component.amount
    return Decimal(report["total"]) - stored


def produce_report(session: Session, run: PayrollRun, *, market: str, form: str) -> dict:
    """One form, filled from one run.

    The rows are the run's lines for the rules the pack says the form collects, the totals are
    those rows added up, and the report states the market, the pack version, the period and the
    revision the figures came from — because a return that cannot say which payroll it was
    filed from cannot be checked later.
    """
    if run.state not in REPORTABLE_STATES:
        raise RunNotReportableError(
            f"{run.period} is {run.state}; a statutory report is produced from a run that has"
            " been computed (T-5.PAY.02)"
        )
    definition = _definition(market, form)
    rules = list(definition["covers_rules"])
    rows: list[dict] = []
    totals: dict[str, Decimal] = {code: Decimal(0) for code in rules}
    per_employee: dict[str, Decimal] = {}
    for line in lines_of(session, run):
        line_total = Decimal(0)
        for component in line_components(session, line):
            if component.code not in totals:
                continue
            rows.append(
                {
                    "employee": line.employee.number,
                    "code": component.code,
                    "name": component.name,
                    "kind": component.kind,
                    "amount": str(component.amount),
                }
            )
            totals[component.code] += component.amount
            line_total += component.amount
        per_employee[line.employee.number] = line_total
    total = sum(totals.values(), Decimal(0))
    return {
        "form": definition["form"],
        "authority": definition["authority"],
        "name": definition["name"],
        "frequency": definition["frequency"],
        "covers": definition["covers"],
        "market": market,
        "pack_version": load_pack(market)["version"],
        "period": run.period,
        "run_revision": run.revision,
        "run_state": run.state,
        "rules": rules,
        "rows": rows,
        "employees": len(per_employee),
        "totals_by_rule": {code: str(amount) for code, amount in totals.items()},
        "total": str(total),
        "employees_total": {number: str(amount) for number, amount in per_employee.items()},
    }


def reports_for_run(session: Session, run: PayrollRun, *, market: str) -> list[dict]:
    """Every form the market's pack states that this run feeds, in the pack's order."""
    return [
        produce_report(session, run, market=market, form=report["form"])
        for report in payroll_reports(market)
    ]
