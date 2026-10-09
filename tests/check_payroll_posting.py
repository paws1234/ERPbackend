"""T-5.PAY.07 check — an approved run posted once, reversed and reposted when it is corrected.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_payroll_posting.py

It fails (non-zero exit) if any of these stops holding:

1. **the posting balances, and its totals equal the run's gross, deductions and net exactly** —
   one entry, built from the run's own rows: the wage cost and the employer's contribution cost
   on one side; the employee deductions to their own structure accounts, the employer's share to
   the company's statutory-liability mapping, and the pay owed on the other
2. **a deduction that states no account is refused**, rather than posted somewhere plausible
3. **posting an already-posted run is refused** — and so is posting a run that is not approved,
   and posting into a locked period (the ledger's own refusal)
4. **a corrected run reverses and reposts rather than editing entries** — the superseded
   revision's entry is left exactly as it was, its mirror is posted, and the new revision's
   entry is the one that stands: the period's net liability is the correction, not double
5. **the statutory liability accounts reconcile to the statutory reports** — the credits the
   entry posted for the rules the forms claim, against the forms' own totals, is exactly zero
   (and a run that is not posted refuses rather than answering zero)

**Scratch database only**: it drops and recreates the schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.audit import set_actor  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.hr.employees import create_employee, record_contract  # noqa: E402
from app.hr.shifts import define_shift, roster_employee  # noqa: E402
from app.ledger import posting  # noqa: E402,F401 — every check builds the one schema
from app.ledger.accounts import import_coa_template  # noqa: E402
from app.ledger.mapping import set_mapping  # noqa: E402
from app.ledger.periods import PeriodLockedError, lock_period  # noqa: E402
from app.payroll.components import define_component, load_statutory_components  # noqa: E402
from app.payroll.engine import (  # noqa: E402
    approve_run,
    compute_run,
    correct_run,
    line_for,
    lines_of,
    start_run,
)
from app.payroll.posting import (  # noqa: E402
    EMPLOYER_CONTRIBUTION_EXPENSE_KEY,
    NET_PAY_PAYABLE_KEY,
    SALARY_EXPENSE_KEY,
    STATUTORY_PAYABLE_KEY,
    AlreadyPostedError,
    PostingError,
    RunNotApprovedError,
    UnmappedComponentError,
    post_run,
    posting_lines,
    posting_payload,
    reconcile_statutory,
    reconciles_to_run,
)


def _refuses(call, expected: tuple, what: str) -> Exception:
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
                code="PAY-GL",
                name="Payroll posting check",
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
        people = {}
        for index, (party, salary) in enumerate((("ANA", "30000"), ("BEN", "20000")), start=1):
            person = create_employee(
                session,
                company_id=company_id,
                party_code=party,
                number=f"E-13{index:02d}",
                hire_date="2025-01-06",
                subject="hr",
                name=f"{party} Reyes",
            )
            record_contract(
                session, person, subject="hr", effective_from="2025-01-06",
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
            roster_employee(session, person, shift=day_shift, effective_from="2025-01-06")
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
        # A company deduction, with the account it credits: the path a company's own
        # deduction takes.
        define_component(
            session,
            company_id=company_id,
            code="COOP",
            name="Co-operative dues",
            kind="deduction",
            basis="fixed",
            amount="100",
            order=95,
            account_code="2100",
            effective_from="2026-01-01",
        )
        session.commit()

        run = start_run(
            session, company_id=company_id, period="2026-06", actor="payroll", cutoff_day=15
        )
        compute_run(session, run, actor="payroll")
        session.commit()

        # 3 — only an approved run is posted
        _refuses(
            lambda: post_run(session, run, actor="controller"),
            RunNotApprovedError,
            "posting a run that has not been approved",
        )
        session.rollback()
        _refuses(
            lambda: reconcile_statutory(session, run, market="philippines"),
            PostingError,
            "reconciling the reports against a run that is not posted",
        )
        session.rollback()
        approve_run(session, run, actor="controller")
        session.commit()

        # 1 — the entry, its totals, and the run's own figures
        built = posting_lines(session, run)
        expected_gross = sum((line.gross for line in lines_of(session, run)), Decimal(0))
        expected_net = sum((line.net for line in lines_of(session, run)), Decimal(0))
        expected_contributions = sum(
            (line.employer_contributions_total for line in lines_of(session, run)), Decimal(0)
        )
        assert built["gross"] == expected_gross == Decimal("10000.000000"), built["gross"]
        assert built["net"] == expected_net
        assert built["employer_contributions"] == expected_contributions
        assert built["debits"] == built["credits"], built
        assert built["debits"] == expected_gross + expected_contributions
        accounts = {row["account"]: row for row in built["lines"]}
        # The wage cost, the employer's own cost, the employee contributions to their own
        # accounts, the employer's share and the company's own deduction to the payable the
        # mapping states, and the pay owed.
        assert accounts["5100"]["debit"] == expected_gross
        assert accounts["5120"]["debit"] == expected_contributions
        assert accounts["2400"]["credit"] == expected_contributions
        assert accounts["2110"]["credit"] == expected_net
        assert accounts["2100"]["credit"] == Decimal("200.000000"), accounts["2100"]
        assert accounts["2410"]["credit"] == Decimal("2500.000000"), accounts["2410"]
        # Nothing is owed on a loan this month, so there is no loan line at all rather than a
        # zero one.
        assert "2500" not in accounts, accounts["2500"]
        entry = post_run(session, run, actor="controller")
        session.commit()
        payload = posting_payload(session, run)
        assert payload["entry"] == str(entry.id) and payload["posted_by"] == "controller"
        assert payload["posted_on"] == "2026-06-30"
        assert reconciles_to_run(session, run, payload) == Decimal(0)
        # The ledger's own triggers enforce the balance in the database; this reads the entry
        # back as stored to confirm the figure the run was posted at.
        entry_debits = sum((line.debit for line in entry.lines), Decimal(0))
        entry_credits = sum((line.credit for line in entry.lines), Decimal(0))
        assert entry_debits == entry_credits == built["debits"], (entry_debits, entry_credits)
        # 5 — the reports and the ledger hold the same liabilities
        assert reconcile_statutory(session, run, market="philippines") == Decimal(0)
        print(
            f"one entry for the month: {built['debits']} of debits against the same in credits"
            f" — gross {expected_gross} and the employer's {expected_contributions} costed, the"
            f" employee contributions to their own accounts, and the net {expected_net} owed —"
            " with the statutory reports reconciling to the ledger to the cent"
        )

        # 3 — posting it again is refused
        _refuses(
            lambda: post_run(session, run, actor="controller"),
            AlreadyPostedError,
            "posting a run that is already in the ledger",
        )
        session.rollback()

        # 4 — a correction reverses the old entry and posts the new one
        record_contract(
            session, people["ANA"], subject="hr", effective_from="2026-06-15",
            contract_type="regular", basic_salary="36000",
        )
        session.commit()
        revision_two = correct_run(
            session, run, actor="payroll", reason="Ana's raise took effect mid-month"
        )
        compute_run(session, revision_two, actor="payroll")
        session.commit()
        approve_run(session, revision_two, actor="controller")
        session.commit()
        second_entry = post_run(session, revision_two, actor="controller")
        session.commit()
        old_payload = posting_payload(session, run)
        new_payload = posting_payload(session, revision_two)
        assert new_payload["entry"] == str(second_entry.id)
        assert new_payload["reversal_entry"] is not None
        assert old_payload["reversal_entry"] == new_payload["reversal_entry"]
        mirror = session.execute(
            text("SELECT debit, credit, account FROM journal_line WHERE entry_id = :id"),
            {"id": old_payload["reversal_entry"]},
        ).all()
        original = session.execute(
            text("SELECT debit, credit, account FROM journal_line WHERE entry_id = :id"),
            {"id": old_payload["entry"]},
        ).all()
        assert sorted(mirror) == sorted((credit, debit, account) for debit, credit, account in original)
        # The period's net liability is the correction rather than both months.
        ledger_net = session.execute(
            text(
                "SELECT coalesce(sum(credit) - sum(debit), 0) FROM journal_line WHERE account ="
                " '2110'"
            )
        ).scalar()
        assert Decimal(ledger_net) == sum(
            (line.net for line in lines_of(session, revision_two)), Decimal(0)
        ), ledger_net
        try:
            session.execute(
                text("UPDATE journal_entry SET memo = 'edited' WHERE id = :id"),
                {"id": str(entry.id)},
            )
        except (DBAPIError, ProgrammingError):
            pass
        else:
            raise AssertionError("the ledger let an entry be edited")
        session.rollback()
        assert reconcile_statutory(session, revision_two, market="philippines") == Decimal(0)
        print(
            "the correction posted the mirror of the old entry and its own, the original entry"
            " is byte for byte what it was (the ledger refuses to edit it), and the period's net"
            " liability is the revised payroll rather than both"
        )

        # 2 — a deduction that states no account is refused
        define_component(
            session,
            company_id=company_id,
            code="UNACCOUNTED",
            name="A deduction nobody gave an account",
            kind="deduction",
            basis="fixed",
            amount="50",
            order=96,
            effective_from="2026-07-01",
        )
        july = start_run(
            session, company_id=company_id, period="2026-07", actor="payroll", cutoff_day=15
        )
        compute_run(session, july, actor="payroll")
        session.commit()
        refusal = _refuses(
            lambda: posting_lines(session, july),
            UnmappedComponentError,
            "posting a deduction that states no account",
        )
        assert "UNACCOUNTED" in str(refusal), refusal
        session.rollback()

        # 3 again — the ledger's own guard: a locked period. The month is locked and a new
        # revision of it is approved for posting, which the ledger then refuses.
        lock_period(
            session,
            company_id=company_id,
            year=2026,
            month=6,
            actor="controller",
            reason="June is closed for posting",
        )
        session.commit()
        revision_three = correct_run(
            session, revision_two, actor="payroll", reason="one more look at June"
        )
        compute_run(session, revision_three, actor="payroll")
        session.commit()
        approve_run(session, revision_three, actor="controller")
        session.commit()
        refusal = _refuses(
            lambda: post_run(session, revision_three, actor="controller"),
            PeriodLockedError,
            "posting into a closed month",
        )
        assert "2026-06 is closed for posting" in str(refusal), refusal
        session.rollback()
        print(
            "a deduction with no account is refused by name, and a run for a month the ledger"
            " has closed cannot be posted at all"
        )

    engine.dispose()
    print("ok — one balanced entry per approved run, reversed and reposted when the run is corrected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
