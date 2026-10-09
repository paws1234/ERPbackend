"""T-5.PAY.01 check — the structure is configuration, and the statutory rates come from the pack.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_payroll_components.py

It fails (non-zero exit) if any of these stops holding:

1. **the statutory deductions are the pack's** — every rule the pack states becomes a
   component carrying that rule's code, name, rate, account and the pack version it came from,
   and the module knows no market: no pack code (`SSS-EE`, `PHIC-EE`, …) and no rate appears in
   its source, so a pack update is a load rather than a release
2. **a component's figure follows from its basis** — a `fixed` amount, a percentage of the
   basic salary, a percentage of gross earnings, an amount per worked day and a loan schedule
   are distinguishable bases, the amount/rate a basis needs is enforced by the service **and
   by the database**, and the pack's loan rule lands on the basis that has no figure of its own
3. **the structure is markable taxable or not** — an earning says whether it is taxable
   compensation, a deduction whether it is taken before tax, and the pack's own rows claim
   nothing the pack does not state
4. **order is part of the structure** — components are read in the order the run applies them,
   with the pack's rows spaced so a company line can sit between two of them
5. **a later version changes later periods only** — a restated allowance applies from its own
   date onward, the structure of an earlier date reads exactly as it did, the rows are
   append-only in the database, and the same component cannot be stated twice for one date
6. a component that would take effect inside a **locked** month is refused, because that
   period has been closed

**Scratch database only**: it drops and recreates the schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.audit import set_actor  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger import posting  # noqa: E402,F401 — every check builds the one schema
from app.ledger.periods import lock_period  # noqa: E402
from app.localization import load_pack  # noqa: E402
from app.payroll.components import (  # noqa: E402
    ClosedPeriodError,
    DuplicateComponentError,
    InvalidComponentError,
    PayrollComponent,
    component_payload,
    components_in_force,
    define_component,
    load_statutory_components,
    structure_on,
)

MODULE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "payroll", "components.py"
)


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
                code="PAY-STRUCT",
                name="Payroll structure check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()
        set_actor(session, "payroll")

        # 1 — the statutory deductions are the pack's, rate for rate
        pack = load_pack("philippines")
        loaded = load_statutory_components(
            session, company_id=company_id, market="philippines", effective_from="2026-01-01"
        )
        session.commit()
        by_code = {row.code: row for row in loaded}
        assert len(by_code) == len(pack["statutory_rules"]), len(by_code)
        for rule in pack["statutory_rules"]:
            row = by_code[rule["code"]]
            assert row.name == rule["name"], row.code
            assert row.pack_version == pack["version"], row.code
            assert row.account_code == rule.get("account"), row.code
            if row.basis == "percent_of_basic":
                assert row.rate_percent == Decimal(str(rule["rate_percent"])), row.code
        # No market is written into the module: the codes and the rates are the pack's own.
        source = open(MODULE, encoding="utf-8").read()
        for rule in pack["statutory_rules"]:
            assert rule["code"] not in source, rule["code"]
        for authority in ("SSS", "PhilHealth", "Pag-IBIG", "BIR"):
            assert authority not in source, authority
        print(
            f"{len(by_code)} statutory components loaded from pack version {pack['version']}"
            " — codes, rates, accounts and version from the pack, and none of them written in"
            " the module"
        )

        # 2 — kind: the employer's half is the employer's, read from the pack's own pairing
        employer = sorted(
            code for code, row in by_code.items() if row.kind == "employer_contribution"
        )
        assert employer == ["HDMF-ER", "PHIC-ER", "SSS-ER"], employer
        assert by_code["SSS-EE"].kind == "deduction" and by_code["WHT-COMP"].kind == "deduction"
        assert by_code["LOAN-EE"].basis == "loan_schedule"
        assert by_code["LOAN-EE"].rate_percent is None and by_code["LOAN-EE"].amount is None
        print(
            "the employer's shares are employer contributions (read from the pack's own"
            " pairing, not from a code ending in -ER), the employee's shares and the"
            " withholding are deductions, and the loan rule lands on the basis that has no"
            " figure of its own"
        )

        # 3 — a company states its own structure: bases, taxable flags, order
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
        define_component(
            session,
            company_id=company_id,
            code="MEAL",
            name="Meal benefit",
            kind="earning",
            basis="per_worked_day",
            amount="150",
            taxable=False,
            order=6,
            effective_from="2026-01-01",
        )
        define_component(
            session,
            company_id=company_id,
            code="COOP",
            name="Co-operative dues",
            kind="deduction",
            basis="percent_of_gross",
            rate_percent="1.5",
            taxable=True,
            order=95,
            effective_from="2026-01-01",
        )
        session.commit()
        june = structure_on(session, company_id=company_id, on=date(2026, 6, 30))
        assert [row["code"] for row in june["earnings"]] == ["ALLOWANCE", "MEAL"]
        assert [row["basis"] for row in june["earnings"]] == ["fixed", "per_worked_day"]
        assert [row["taxable"] for row in june["earnings"]] == [True, False]
        # A company deduction sits where its own order puts it, and the pack's rows are in
        # the pack's order — the structure's order is data, not the order rows were written.
        assert [row["code"] for row in june["deductions"]] == [
            "SSS-EE",
            "PHIC-EE",
            "HDMF-EE",
            "WHT-COMP",
            "WHT-COMP-13TH",
            "LOAN-EE",
            "COOP",
        ], june["deductions"]
        assert june["deductions"][-1]["order"] == 95
        assert june["deductions"][-1]["rate_percent"] == "1.500"
        assert june["pack_versions"] == [pack["version"]]
        # The pack's rows are spaced by ten, so a company line sits between two of them.
        assert june["order"][:4] == ["ALLOWANCE", "MEAL", "SSS-EE", "SSS-ER"], june["order"]
        pack_codes = {rule["code"] for rule in pack["statutory_rules"]}
        pack_rows = [
            row
            for group in ("deductions", "employer_contributions")
            for row in june[group]
            if row["code"] in pack_codes
        ]
        assert pack_rows and all(row["taxable"] is False for row in pack_rows), (
            "the pack states no tax treatment, so nothing is claimed for its rows"
        )
        # A deduction the company states says for itself whether it is taken before tax.
        assert [row["taxable"] for row in june["deductions"] if row["code"] == "COOP"] == [True]
        print(
            "a company states a fixed taxable allowance, a non-taxable per-day benefit and a"
            " percentage deduction, read in order with the pack's rows — and the pack's own"
            " rows claim no tax treatment the pack does not state"
        )

        # 2 again — the basis and the figure it needs, refused by name and by the database
        for bad, expected, what in (
            ({"basis": "per_worked_hour"}, InvalidComponentError, "a basis nobody defined"),
            (
                {"basis": "percent_of_basic", "amount": "100"},
                InvalidComponentError,
                "an amount on a percentage basis",
            ),
            (
                {"basis": "fixed", "rate_percent": "5"},
                InvalidComponentError,
                "a rate on an amount basis",
            ),
            ({"basis": "fixed", "amount": "-1"}, InvalidComponentError, "a negative amount"),
            (
                {"basis": "percent_of_basic", "rate_percent": "101"},
                InvalidComponentError,
                "a rate above 100",
            ),
            (
                {"basis": "percent_of_basic", "rate_percent": 5.0},
                InvalidComponentError,
                "a float rate",
            ),
            (
                {"basis": "fixed", "amount": "1", "source": "guesswork"},
                InvalidComponentError,
                "a source nobody defined",
            ),
        ):
            _refuses(
                lambda bad=bad: define_component(
                    session,
                    company_id=company_id,
                    code="BROKEN",
                    name="Broken",
                    kind="deduction",
                    effective_from="2026-02-01",
                    **bad,
                ),
                expected,
                what,
            )
        session.rollback()
        _refused(
            lambda: session.execute(
                text(
                    "INSERT INTO payroll_component"
                    " (id, company_id, code, name, kind, basis, rate_percent, taxable, order_no,"
                    " effective_from, source)"
                    " VALUES (gen_random_uuid(), :company, 'RAW', 'Raw', 'deduction', 'fixed',"
                    " 5, true, 1, '2026-02-01', 'company')"
                ),
                {"company": str(company_id)},
            ),
            "ck_payroll_component_amount_basis",
        )
        session.rollback()
        print(
            "a basis nobody defined, an amount on a percentage basis, a rate on an amount"
            " basis, a negative amount, a rate above 100 and a float are all refused — and the"
            " database refuses the same mismatch written around the service"
        )

        # 5 — a later version changes later periods only, and rows are never rewritten
        define_component(
            session,
            company_id=company_id,
            code="ALLOWANCE",
            name="Transport allowance",
            kind="earning",
            basis="fixed",
            amount="2500",
            taxable=True,
            order=5,
            effective_from="2026-09-01",
        )
        session.commit()
        def allowance_on(on: date) -> dict:
            """The `ALLOWANCE` component in force on a date."""
            rows = [
                row
                for row in components_in_force(session, company_id=company_id, on=on)
                if row.code == "ALLOWANCE"
            ]
            assert len(rows) == 1, rows
            return component_payload(rows[0])

        assert allowance_on(date(2026, 6, 30))["amount"] == "2000.000000"
        assert allowance_on(date(2026, 9, 30))["amount"] == "2500.000000"
        # The June reading is not merely equal — it is the row June always had.
        june_again = structure_on(session, company_id=company_id, on=date(2026, 6, 30))
        assert june_again["earnings"] == june["earnings"], (june_again["earnings"], june["earnings"])
        _refuses(
            lambda: define_component(
                session,
                company_id=company_id,
                code="ALLOWANCE",
                name="Transport allowance",
                kind="earning",
                basis="fixed",
                amount="3000",
                effective_from="2026-09-01",
            ),
            DuplicateComponentError,
            "a second statement of one component for one date",
        )
        session.rollback()
        _refused(
            lambda: session.execute(
                text("UPDATE payroll_component SET amount = 1 WHERE code = 'ALLOWANCE'")
            ),
            "is append-only",
        )
        session.rollback()
        rows = session.scalars(
            select(PayrollComponent).where(PayrollComponent.code == "ALLOWANCE")
        ).all()
        assert sorted(row.amount for row in rows) == [
            Decimal("2000.000000"),
            Decimal("2500.000000"),
        ]
        print(
            "an allowance restated from 2026-09-01 reads 2000 in June and 2500 in September,"
            " the June structure is the same dict it was, the same component cannot be stated"
            " twice for one date, and the database refuses to rewrite a row"
        )

        # 6 — a locked month is not restated
        lock_period(
            session,
            company_id=company_id,
            year=2026,
            month=8,
            actor="controller",
            reason="August payroll is approved",
        )
        session.commit()
        _refuses(
            lambda: define_component(
                session,
                company_id=company_id,
                code="AUGUST",
                name="Late addition",
                kind="earning",
                basis="fixed",
                amount="100",
                effective_from="2026-08-15",
            ),
            ClosedPeriodError,
            "a component effective inside a locked month",
        )
        session.rollback()
        print(
            "a component stated inside a locked month is refused, so a closed period is not"
            " restated by adding structure to it"
        )

    engine.dispose()
    print(
        "ok — the structure is rows, the statutory rules are the pack's, and later periods are"
        " the only ones that change"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
