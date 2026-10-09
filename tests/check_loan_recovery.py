"""T-5.PAY.03 check — a loan recovered by the schedule, capped by policy, settled at zero.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_loan_recovery.py

It fails (non-zero exit) if any of these stops holding:

1. **recovery follows the schedule, and never over-recovers** — a `10,000` advance recovered at
   `3,000` a run has a schedule of `3,000 + 3,000 + 3,000 + 1,000` (the last one the
   remainder), the recoveries match that schedule period by period, and a loan smaller than one
   instalment is recovered once and then never again
2. **the policy's maximum is respected and the remainder is deferred visibly** — with a 20% cap
   and `10,000` of pay left, a `3,000` instalment recovers `2,000` and records `1,000` as
   deferred, so a short recovery is a stated fact rather than a smaller number
3. **the outstanding balance is derived and the last recovery closes the loan** — it is the
   principal minus the recoveries (there is no balance column), and the recovery that clears it
   settles the loan with the date and who did it; a settled loan recovers nothing further
4. **recovery is idempotent across recomputations** — a period already recovered for adds
   nothing on the next attempt, however many times a run is recomputed
5. **a company with no policy in force recovers nothing and says so** — on the loan module's own
   `NoPolicyError`, and in a payroll run as a **flagged line** rather than a silent zero
6. the engine's loan line is where it happens: a run's `LOAN-EE` figure is the recovery, and the
   deferral is named in the component's own note

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
from app.hr.employees import create_employee, record_contract  # noqa: E402
from app.ledger import posting  # noqa: E402,F401 — every check builds the one schema
from app.ledger.periods import lock_period  # noqa: E402
from app.payroll.components import define_component, load_statutory_components  # noqa: E402
from app.payroll.engine import (  # noqa: E402
    IncompleteAttendanceError,
    approve_run,
    compute_run,
    line_for,
    line_payload,
    start_run,
)
from app.payroll.loans import (  # noqa: E402
    ClosedPeriodError,
    InvalidLoanError,
    LoanSettledError,
    NoPolicyError,
    loan_payload,
    loans_of,
    outstanding_of,
    policy_in_force,
    record_loan,
    recover_for_period,
    recovered_of,
    schedule_of,
    set_policy,
    settle_loan,
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
    bare_company_id = uuid.uuid4()
    with Session(engine) as session:
        for cid, code in ((company_id, "LOANS"), (bare_company_id, "NO-POLICY")):
            session.add(
                Company(
                    id=cid,
                    code=code,
                    name=f"{code} check",
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
            number="E-801",
            hire_date="2025-01-06",
            subject="hr",
            name="Ana Reyes",
        )
        ben = create_employee(
            session,
            company_id=bare_company_id,
            party_code="BEN",
            number="E-901",
            hire_date="2025-01-06",
            subject="hr",
            name="Ben Reyes",
        )
        record_contract(
            session, ana, subject="hr", effective_from="2025-01-06",
            contract_type="regular", basic_salary="30000",
        )
        session.commit()

        # 5 — no policy, no recovery: a cap nobody stated is not "unlimited"
        _refuses(
            lambda: policy_in_force(session, company_id=company_id, on=date(2026, 6, 30)),
            NoPolicyError,
            "a recovery with no policy stated",
        )
        session.rollback()
        set_policy(
            session,
            company_id=company_id,
            max_recovery_percent="20",
            effective_from="2026-01-01",
            actor="hr-head",
            note="one fifth of take-home pay",
        )
        session.commit()
        _refuses(
            lambda: set_policy(
                session,
                company_id=company_id,
                max_recovery_percent="25",
                effective_from="2026-01-01",
                actor="hr-head",
            ),
            InvalidLoanError,
            "a second policy for the same date",
        )
        session.rollback()
        assert policy_in_force(
            session, company_id=company_id, on=date(2026, 6, 30)
        ).max_recovery_percent == Decimal("20.000")
        print(
            "a company with no policy in force cannot recover anything, and the policy is a"
            " dated row: 20% from 2026-01-01"
        )

        # 1 — the schedule is derived, and the last instalment is the remainder
        loan = record_loan(
            session,
            ana,
            reference="ADV-1",
            principal="10000",
            instalment_amount="3000",
            started_on="2026-06-01",
            actor="hr-head",
            reason="relocation advance, repaid over four months",
        )
        session.commit()
        assert [str(row["amount"]) for row in schedule_of(loan)] == [
            "3000.000000",
            "3000.000000",
            "3000.000000",
            "1000.000000",
        ], schedule_of(loan)
        for bad, what in (
            ({"principal": "0"}, "a principal of nothing"),
            ({"principal": "-100"}, "a negative principal"),
            ({"principal": 100.5}, "a float principal"),
            ({"instalment_amount": "0"}, "an instalment of nothing"),
            ({"actor": "  "}, "an advance nobody took"),
            ({"reason": ""}, "an advance with no reason"),
        ):
            settings = dict(
                reference="BAD",
                principal="1000",
                instalment_amount="500",
                started_on="2026-06-01",
                actor="hr-head",
                reason="advance",
            ) | bad
            _refuses(
                lambda settings=settings: record_loan(session, ana, **settings),
                InvalidLoanError,
                what,
            )
            session.rollback()
        assert outstanding_of(session, loan) == Decimal("10000.000000")

        # 2 + 3 + 4 — four periods: the cap defers, the last one closes, a repeat adds nothing
        first = recover_for_period(
            session, ana, period="2026-06", on=date(2026, 6, 30), available="20000"
        )
        session.commit()
        assert first["recovered"] == Decimal("3000.000000"), first
        assert first["deferred"] == Decimal("0.000000") and first["cap"] == Decimal("4000.000000")
        again = recover_for_period(
            session, ana, period="2026-06", on=date(2026, 6, 30), available="20000"
        )
        assert again["recovered"] == Decimal("3000.000000"), "a second attempt added nothing"
        assert outstanding_of(session, loan) == Decimal("7000.000000")
        capped = recover_for_period(
            session, ana, period="2026-07", on=date(2026, 7, 31), available="10000"
        )
        session.commit()
        assert capped["recovered"] == Decimal("2000.000000") and capped["cap"] == Decimal(
            "2000.000000"
        ), capped
        assert capped["deferred"] == Decimal("1000.000000"), "the policy's remainder is recorded"
        assert outstanding_of(session, loan) == Decimal("5000.000000")
        covered = recover_for_period(
            session, ana, period="2026-08", on=date(2026, 8, 31), available="50000"
        )
        session.commit()
        assert covered["recovered"] == Decimal("3000.000000") and covered["deferred"] == Decimal(
            "0.000000"
        )
        assert outstanding_of(session, loan) == Decimal("2000.000000")
        # The whole remainder in one run: the final settlement, through payroll like everything
        # else, closing the loan with a date and a name.
        fourth = recover_for_period(
            session, ana, period="2026-09", on=date(2026, 9, 30), available="50000"
        )
        session.commit()
        assert fourth["recovered"] == Decimal("2000.000000"), fourth
        assert fourth["recoveries"][0].settled_loan is True
        payload = loan_payload(session, loan)
        assert payload["outstanding"] == "0.000000" and payload["settled"] is True
        assert payload["settled_on"] == "2026-09-30"
        assert payload["settled_by"] is None, "the last instalment closed it, nobody settled it"
        assert payload["recovered"] == "10000.000000"
        assert [row["amount"] for row in payload["recoveries"]] == [
            "3000.000000",
            "2000.000000",
            "3000.000000",
            "2000.000000",
        ]
        assert [row["deferred"] for row in payload["recoveries"]] == [
            "0.000000",
            "1000.000000",
            "0.000000",
            "0.000000",
        ]
        # Nothing more is recoverable, and the loan is out of the open list.
        nothing = recover_for_period(
            session, ana, period="2026-10", on=date(2026, 10, 31), available="50000"
        )
        assert nothing["recovered"] == Decimal("0.000000")
        assert loans_of(session, ana, settled=False) == []
        assert [row.reference for row in loans_of(session, ana, settled=True)] == ["ADV-1"]
        _refuses(
            lambda: settle_loan(
                session, loan, on=date(2026, 10, 31), actor="hr", reason="too late"
            ),
            LoanSettledError,
            "settling a settled loan",
        )
        session.rollback()
        print(
            "3000 recovered, then 2000 with 1000 deferred by the policy, then 3000, then the"
            " last 2000 which closed the loan at zero on 2026-09-30 — and a second attempt at"
            " an already-recovered period added nothing"
        )

        # 1 again — a loan smaller than one instalment is recovered once and never again
        small = record_loan(
            session,
            ana,
            reference="ADV-2",
            principal="500",
            instalment_amount="3000",
            started_on="2026-11-01",
            actor="hr-head",
            reason="small advance",
        )
        session.commit()
        assert [str(row["amount"]) for row in schedule_of(small)] == ["500.000000"]
        once = recover_for_period(
            session, ana, period="2026-11", on=date(2026, 11, 30), available="50000"
        )
        session.commit()
        assert once["recovered"] == Decimal("500.000000"), once
        assert outstanding_of(session, small) == Decimal("0.000000")
        assert recovered_of(session, small) == Decimal("500.000000")
        twice = recover_for_period(
            session, ana, period="2026-12", on=date(2026, 12, 31), available="50000"
        )
        assert twice["recovered"] == Decimal("0.000000"), "a cleared loan over-recovered"
        print(
            "an advance smaller than one instalment recovers what it owes (500) and then"
            " nothing at all — a cleared loan cannot over-recover"
        )

        # the loan module's own guard: a recovery into a locked month
        lock_period(
            session,
            company_id=company_id,
            year=2027,
            month=1,
            actor="controller",
            reason="January is closed",
        )
        session.commit()
        _refuses(
            lambda: recover_for_period(
                session, ana, period="2027-01", on=date(2027, 1, 31), available="50000"
            ),
            ClosedPeriodError,
            "a recovery recorded inside a locked month",
        )
        session.rollback()
        print("a recovery dated inside a locked month is refused rather than slipped into a closed period")

        # 6 — the engine's own loan line: the recovery, and the deferral named beside it
        third = record_loan(
            session,
            ana,
            reference="ADV-3",
            principal="9000",
            instalment_amount="3000",
            started_on="2026-01-01",
            actor="hr-head",
            reason="car loan",
        )
        session.commit()
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
        session.commit()
        run = start_run(
            session, company_id=company_id, period="2027-02", actor="payroll", cutoff_day=15
        )
        compute_run(session, run, actor="payroll")
        session.commit()
        ana_line = line_for(session, run, ana)
        drawn = line_payload(session, ana_line)
        statutory = Decimal("1500.000000") + Decimal("750.000000") + Decimal("600.000000")
        # Gross is the allowance alone (nobody punched in), the statutory deductions are the
        # pack's, and the loan takes 20% of what is left — 430 of the 3000 instalment.
        remaining = Decimal("5000.000000") - statutory
        expected = (remaining * Decimal("20") / Decimal(100)).quantize(Decimal("0.000001"))
        loan_component = [row for row in drawn["components"] if row["code"] == "LOAN-EE"]
        assert len(loan_component) == 1, drawn["components"]
        assert loan_component[0]["amount"] == str(expected), loan_component[0]
        assert loan_component[0]["basis"] == "loan_schedule"
        assert "deferred by the recovery policy" in loan_component[0]["note"], loan_component[0]
        assert ana_line.deductions_total == statutory + expected, ana_line.deductions_total
        assert outstanding_of(session, third) == Decimal("9000.000000") - expected
        assert line_payload(session, ana_line)["net"] == str(
            Decimal("5000.000000") - statutory - expected
        )
        print(
            f"the run's own loan line recovered {expected} (20% of the 2150 left after the"
            " statutory deductions), named the 2570 the policy deferred, and left the loan's"
            " outstanding balance at 8570 — derived, not stored"
        )

        # 5 again — in a run: an employee who owes something and a company with no policy is
        # flagged, not quietly recovered from, and the run cannot be approved.
        set_actor(session, "payroll")
        record_contract(
            session, ben, subject="hr", effective_from="2025-01-06",
            contract_type="regular", basic_salary="20000",
        )
        record_loan(
            session,
            ben,
            reference="ADV-9",
            principal="4000",
            instalment_amount="1000",
            started_on="2027-01-01",
            actor="hr-head",
            reason="advance, no policy stated yet",
        )
        load_statutory_components(
            session, company_id=bare_company_id, market="philippines",
            effective_from="2026-01-01",
        )
        define_component(
            session,
            company_id=bare_company_id,
            code="ALLOWANCE",
            name="Transport allowance",
            kind="earning",
            basis="fixed",
            amount="5000",
            taxable=True,
            order=5,
            effective_from="2026-01-01",
        )
        session.commit()
        bare_run = start_run(
            session, company_id=bare_company_id, period="2027-02", actor="payroll", cutoff_day=15
        )
        compute_run(session, bare_run, actor="payroll")
        session.commit()
        ben_line = line_for(session, bare_run, ben)
        unpaid_loan = [row for row in line_payload(session, ben_line)["components"] if row["code"] == "LOAN-EE"]
        assert unpaid_loan[0]["amount"] == "0.000000", unpaid_loan
        assert "no loan recovery policy is in force" in unpaid_loan[0]["note"], unpaid_loan
        assert ben_line.incomplete and "no loan recovery policy" in ben_line.flag_reason
        _refuses(
            lambda: approve_run(session, bare_run, actor="controller"),
            IncompleteAttendanceError,
            "approving a run whose loan recovery has no policy",
        )
        session.rollback()
        print(
            "in a run, an employee who owes something and a company with no policy stated gets"
            " a flagged line with nothing recovered, and the run refuses to be approved"
        )

    engine.dispose()
    print(
        "ok — recovery follows a derived schedule, stops at the balance and at the policy, and"
        " is idempotent per period"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
