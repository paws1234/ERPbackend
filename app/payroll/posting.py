"""T-5.PAY.07 — an approved payroll run, posted to the ledger as it stands.

The ledger is append-only and every entry balances (T-0.CORE.01's triggers), so this module's
job is narrow and exact: turn one approved run into **one balanced entry**, refuse to post it
twice, and — when a correction supersedes a posted run — **reverse** the old entry and post the
new one rather than touching what was already written.

The shape of the entry, and where it comes from:

* **Expense** — the run's gross, to the account the company maps as `salary_expense`, and the
  employer's own contributions to `employer_contribution_expense`.
* **Statutory liabilities** — each **employee** deduction credits the account its own row of the
  structure states (the pack carries them: a contributions account, a withholding account, a
  loans-payable account), so the liability the return is about and the liability the ledger
  holds are the same account.
* **The employer's share is a liability, not an expense account.** The pack states the employer
  rules' accounts as expense accounts (`5120` and friends: they say where the *cost* belongs),
  so the credit goes to the company's `statutory_payable` mapping instead of crediting an
  expense account — crediting `5120` while debiting `5120` would leave the contribution with no
  expense and no liability at all.
* **Net pay** — to the `net_pay_payable` mapping.

A deduction row that states **no** account is refused rather than posted somewhere plausible:
"the deduction went to the expense account" is how a liability disappears from the books.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.company import company_base_currency
from app.ledger.mapping import mapped_account
from app.ledger.posting import JournalEntry, post_journal_entry
from app.payroll.engine import PayrollRun, line_components, lines_of
from app.payroll.statutory import payroll_reports, produce_report

# What the entry is: the run, and the reversal of a run it supersedes.
DOC_TYPE = "payroll_run"
REVERSAL_DOC_TYPE = "payroll_run_reversal"

# The mapping keys the company states (T-1.ACCT.03's `set_mapping`): where the wage cost, the
# employer's contribution cost, the employer's contribution liability and the pay owed to
# employees go. Everything else in the entry comes from the structure's own rows.
SALARY_EXPENSE_KEY = "salary_expense"
EMPLOYER_CONTRIBUTION_EXPENSE_KEY = "employer_contribution_expense"
STATUTORY_PAYABLE_KEY = "statutory_payable"
NET_PAY_PAYABLE_KEY = "net_pay_payable"
MAPPING_KEYS = (
    SALARY_EXPENSE_KEY,
    EMPLOYER_CONTRIBUTION_EXPENSE_KEY,
    STATUTORY_PAYABLE_KEY,
    NET_PAY_PAYABLE_KEY,
)


class PostingError(ValueError):
    """The payroll posting refused what was asked of it."""


class RunNotApprovedError(PostingError):
    """Only an approved run is posted."""


class AlreadyPostedError(PostingError):
    """The run is already in the ledger; a correction reverses and reposts."""


class UnmappedComponentError(PostingError):
    """A deduction or contribution states no account, so its credit has nowhere to go."""


def posting_lines(session: Session, run: PayrollRun) -> dict:
    """The entry a run makes: its lines (one per account), and the three totals beside them.

    Built from the run's own rows, so the entry and the payslips cannot disagree: the debit is
    the gross each line was paid, the credits are the deductions the structure took and the pay
    that is owed, and the employer's own share is added to both sides.
    """
    debits: dict[str, Decimal] = {}
    credits: dict[str, Decimal] = {}
    gross = Decimal(0)
    deducted = Decimal(0)
    contributions = Decimal(0)
    net = Decimal(0)
    for line in lines_of(session, run):
        gross += line.gross
        deducted += line.deductions_total
        contributions += line.employer_contributions_total
        net += line.net
        for component in line_components(session, line):
            if component.kind == "deduction":
                if not component.account_code:
                    raise UnmappedComponentError(
                        f"the deduction {component.code!r} states no account, so its credit has"
                        " nowhere to go; state the account on the component (T-5.PAY.01) rather"
                        " than posting it somewhere plausible"
                    )
                credits[component.account_code] = (
                    credits.get(component.account_code, Decimal(0)) + component.amount
                )
    salary = mapped_account(session, company_id=run.company_id, key=SALARY_EXPENSE_KEY)
    employer_expense = mapped_account(
        session, company_id=run.company_id, key=EMPLOYER_CONTRIBUTION_EXPENSE_KEY
    )
    statutory_payable = mapped_account(
        session, company_id=run.company_id, key=STATUTORY_PAYABLE_KEY
    )
    net_payable = mapped_account(session, company_id=run.company_id, key=NET_PAY_PAYABLE_KEY)
    debits[salary.code] = debits.get(salary.code, Decimal(0)) + gross
    if contributions:
        debits[employer_expense.code] = (
            debits.get(employer_expense.code, Decimal(0)) + contributions
        )
        credits[statutory_payable.code] = (
            credits.get(statutory_payable.code, Decimal(0)) + contributions
        )
    if net:
        credits[net_payable.code] = credits.get(net_payable.code, Decimal(0)) + net
    lines = [
        {"account": code, "debit": debits[code], "credit": credits.get(code, Decimal(0))}
        for code in debits
    ] + [
        {"account": code, "debit": Decimal(0), "credit": credits[code]}
        for code in credits
        if code not in debits
    ]
    # A component that came to nothing is not a line: the ledger refuses a row with no debit
    # and no credit (ck_journal_line_not_zero), and a zero line on an entry is noise a reader
    # has to rule out rather than information. The totals below are unchanged by it.
    lines = [row for row in lines if row["debit"] or row["credit"]]
    return {
        "lines": lines,
        "gross": gross,
        "deductions": deducted,
        "employer_contributions": contributions,
        "net": net,
        "debits": sum(debits.values(), Decimal(0)),
        "credits": sum(credits.values(), Decimal(0)),
    }


def _reverse(session: Session, entry: JournalEntry, *, memo: str) -> JournalEntry:
    """The mirror of an entry: the same accounts, the two sides swapped.

    A ledger is appended to, never rewritten (T-0.AUDIT.01): the way to undo a posting is to
    post its mirror, which leaves both on the record and the account balances where they should
    be. The mirror is dated as the entry it mirrors, because it is the same period being
    restated rather than a new event.
    """
    return post_journal_entry(
        session,
        company_id=entry.company_id,
        posting_date=entry.posting_date,
        currency=entry.currency,
        memo=memo,
        source_type=REVERSAL_DOC_TYPE,
        source_id=entry.source_id,
        lines=[
            {"account": line.account, "debit": line.credit, "credit": line.debit}
            for line in entry.lines
        ],
    )


def post_run(session: Session, run: PayrollRun, *, actor: str, on: date | None = None) -> JournalEntry:
    """Post an approved run; refuse to post it twice; reverse what a correction replaced.

    The reversal is part of **this** act on purpose: a revision that supersedes a posted run is
    the same payroll month stated again, so letting both entries stand would report the month
    twice, and reversing it somewhere else would leave a window in which both do.
    """
    who = "" if actor is None else str(actor).strip()
    if not who:
        raise PostingError("who is posting the run is required")
    if run.state != "approved":
        raise RunNotApprovedError(
            f"{run.period} is {run.state}; only an approved run is posted (T-5.PAY.02:"
            " approve_run), and the ledger is not the place to find out whether a figure was"
            " agreed"
        )
    if run.journal_entry_id is not None:
        raise AlreadyPostedError(
            f"{run.period} revision {run.revision} is already posted as entry"
            f" {run.journal_entry_id}; a corrected run reverses it and posts the new revision"
            " rather than being posted again"
        )
    superseded = None
    if run.supersedes_id is not None:
        superseded = session.get(PayrollRun, run.supersedes_id)
        if superseded is not None and superseded.journal_entry_id is not None:
            entry = session.get(JournalEntry, superseded.journal_entry_id)
            reversal = _reverse(
                session,
                entry,
                memo=(
                    f"reversal of payroll {run.period} revision {superseded.revision}"
                    f" ({run.correction_reason or 'corrected'})"
                ),
            )
            superseded.reversal_entry_id = reversal.id
            run.reversal_entry_id = reversal.id
            session.flush()
    built = posting_lines(session, run)
    posting_date = run.to_date if on is None else on
    entry = post_journal_entry(
        session,
        company_id=run.company_id,
        posting_date=posting_date,
        currency=company_base_currency(session, company_id=run.company_id),
        memo=f"payroll {run.period} revision {run.revision}",
        source_type=DOC_TYPE,
        source_id=run.id,
        lines=built["lines"],
    )
    run.journal_entry_id = entry.id
    run.posted_on = posting_date
    run.posted_by = who
    session.flush()
    return entry


def posting_payload(session: Session, run: PayrollRun) -> dict:
    """What a run posted: the entry, its lines, and the totals it was built from."""
    entry = None if run.journal_entry_id is None else session.get(JournalEntry, run.journal_entry_id)
    reversal = (
        None if run.reversal_entry_id is None else session.get(JournalEntry, run.reversal_entry_id)
    )
    built = posting_lines(session, run)
    return {
        "period": run.period,
        "revision": run.revision,
        "state": run.state,
        "posted_on": None if run.posted_on is None else run.posted_on.isoformat(),
        "posted_by": run.posted_by,
        "entry": None if entry is None else str(entry.id),
        "reversal_entry": None if reversal is None else str(reversal.id),
        "lines": [
            {"account": row["account"], "debit": str(row["debit"]), "credit": str(row["credit"])}
            for row in built["lines"]
        ],
        "totals": {
            "gross": str(built["gross"]),
            "deductions": str(built["deductions"]),
            "employer_contributions": str(built["employer_contributions"]),
            "net": str(built["net"]),
            "debits": str(built["debits"]),
            "credits": str(built["credits"]),
        },
    }


def reconciles_to_run(session: Session, run: PayrollRun, payload: dict | None = None) -> Decimal:
    """The difference between the entry's totals and the run's: zero, or a defect.

    The ledger holds a wage cost equal to the run's gross on one side and, on the other, the
    deductions the run took, the employer's share and the pay that is owed — and this adds them
    up from the stored lines rather than from the entry, so it is the run's own figures that
    the entry is checked against.
    """
    stated = posting_payload(session, run) if payload is None else payload
    gross = sum((line.gross for line in lines_of(session, run)), Decimal(0))
    net = sum((line.net for line in lines_of(session, run)), Decimal(0))
    contributions = sum(
        (line.employer_contributions_total for line in lines_of(session, run)), Decimal(0)
    )
    difference = Decimal(stated["totals"]["gross"]) - gross
    difference += Decimal(stated["totals"]["net"]) - net
    difference += Decimal(stated["totals"]["employer_contributions"]) - contributions
    difference += Decimal(stated["totals"]["debits"]) - Decimal(stated["totals"]["credits"])
    return difference


def reconcile_statutory(session: Session, run: PayrollRun, *, market: str) -> Decimal:
    """What the ledger holds for a period's statutory liabilities against what the reports claim.

    One side is the **posted entry**: the credits it made to the accounts this period's
    statutory rules post to — the employee deductions' own accounts, and the company's
    `statutory_payable` account for the employer's shares. The other side is the **reports**,
    which read the run's components. They are built by different paths from the same payroll, so
    a report and the liability it is about agree only when both are right; zero is the answer
    this returns when they do. A loan recovery is not a statutory liability and no form claims
    it, so it is not in either total.

    A run that has not been posted has nothing to reconcile against, which is a refusal rather
    than a zero.
    """
    if run.journal_entry_id is None:
        raise PostingError(
            f"{run.period} revision {run.revision} is not posted, so there is no liability in"
            " the ledger to reconcile the reports against"
        )
    covered = {
        code for report in payroll_reports(market) for code in report["covers_rules"]
    }
    payable = mapped_account(
        session, company_id=run.company_id, key=STATUTORY_PAYABLE_KEY
    ).code
    accounts = {
        component.account_code if component.kind == "deduction" else payable
        for line in lines_of(session, run)
        for component in line_components(session, line)
        if component.code in covered
    }
    entry = session.get(JournalEntry, run.journal_entry_id)
    held = sum(
        (line.credit for line in entry.lines if line.account in accounts), Decimal(0)
    )
    claimed = sum(
        (
            Decimal(produce_report(session, run, market=market, form=report["form"])["total"])
            for report in payroll_reports(market)
        ),
        Decimal(0),
    )
    return held - claimed
