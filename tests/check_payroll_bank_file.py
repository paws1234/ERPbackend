"""T-5.PAY.06 check — the payment file is the run's own money, in the pack's own format.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_payroll_bank_file.py

It fails (non-zero exit) if any of these stops holding:

1. **the file's total is the run's net pay total, exactly** — with every employee payable the
   file pays the whole payroll to the cent; with one excluded the file **accounts for the whole
   payroll anyway**, because file total plus excluded total equals the run's net total, and the
   difference is on the list rather than missing
2. **the format is the pack's, and rows are validated against it** — the columns, their order,
   the delimiter, the encoding and what is required are read from the pack; a row missing a
   required column, or stating a column the format does not have, is refused rather than
   written
3. **employees without valid bank details are excluded and listed, never silently omitted** —
   the excluded list carries the employee, the amount and the reason, the account number is
   digits only (refused at entry, and by the database), and a net pay that is not positive is
   excluded for its own stated reason
4. **regenerating the file for the same run produces an identical file** — byte for byte, and
   a later run's file is its own, while the earlier revision's file is unchanged
5. a run that has not been computed cannot be paid, and a market the packs do not cover is
   refused by the localization package rather than paid as an empty file

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
from app.hr.employees import create_employee, record_contract  # noqa: E402
from app.ledger import posting  # noqa: E402,F401 — every check builds the one schema
from app.localization import PackError, bank_file_format  # noqa: E402
from app.payroll.bank_files import (  # noqa: E402
    InvalidBankDetailsError,
    RunNotPayableError,
    UnpayableRowError,
    bank_details_in_force,
    bank_file,
    define_bank_details,
    reconciles,
    render,
    validate_row,
)
from app.payroll.components import define_component, load_statutory_components  # noqa: E402
from app.payroll.engine import compute_run, correct_run, start_run  # noqa: E402


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
    format_ = bank_file_format("philippines")
    with Session(engine) as session:
        session.add(
            Company(
                id=company_id,
                code="PAY-BANK",
                name="Bank file check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()
        set_actor(session, "payroll")
        people = {}
        for party, salary in (("ANA", "30000"), ("BEN", "20000"), ("CARL", "10000")):
            person = create_employee(
                session,
                company_id=company_id,
                party_code=party,
                number=f"E-12{len(people) + 1:02d}",
                hire_date="2025-01-06",
                subject="hr",
                name=f"{party} Reyes",
            )
            record_contract(
                session, person, subject="hr", effective_from="2025-01-06",
                contract_type="regular", basic_salary=salary,
            )
            people[party] = person
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
        for party, account in (("ANA", "001234567890"), ("BEN", "002345678901")):
            define_bank_details(
                session,
                people[party],
                bank_code="BPI",
                account_number=account,
                holder_name=f"{party} REYES",
                effective_from="2026-01-01",
                actor="hr",
            )
        session.commit()

        # 3 — the account number is digits only, refused at entry and by the database
        _refuses(
            lambda: define_bank_details(
                session,
                people["CARL"],
                bank_code="BPI",
                account_number="0012-3456-7890",
                holder_name="CARL REYES",
                effective_from="2026-01-01",
                actor="hr",
            ),
            InvalidBankDetailsError,
            "an account number with dashes in it",
        )
        session.rollback()
        _refuses(
            lambda: define_bank_details(
                session,
                people["ANA"],
                bank_code="BPI",
                account_number="001234567890",
                holder_name="ANA REYES",
                effective_from="2026-01-01",
                actor="hr",
            ),
            InvalidBankDetailsError,
            "the same details stated twice for one date",
        )
        session.rollback()
        _refused(
            lambda: session.execute(
                text(
                    "INSERT INTO employee_bank_account"
                    " (id, company_id, employee_id, bank_code, account_number, holder_name,"
                    " effective_from, stated_by)"
                    " VALUES (gen_random_uuid(), :company, :employee, 'BPI', '12-34', 'X',"
                    " '2026-02-01', 'hr')"
                ),
                {"company": str(company_id), "employee": str(people["CARL"].id)},
            ),
            "ck_employee_bank_account_digits",
        )
        session.rollback()
        assert bank_details_in_force(session, people["CARL"], on=date(2026, 6, 30)) is None
        print(
            "an account number that is not digits only is refused by the service and by the"
            " database, the same details cannot be stated twice for one date, and one employee"
            " deliberately has none"
        )

        run = start_run(
            session, company_id=company_id, period="2026-06", actor="payroll", cutoff_day=15
        )
        compute_run(session, run, actor="payroll")
        session.commit()

        # 2 + 3 — the format is the pack's, and the employee without details is listed
        file_payload = bank_file(session, run, market="philippines")
        assert file_payload["columns"] == [
            "payee_name",
            "payee_account",
            "bank_code",
            "amount",
            "reference",
            "purpose",
        ], file_payload["columns"]
        assert file_payload["delimiter"] == format_["delimiter"]
        assert file_payload["encoding"] == format_["encoding"]
        assert [row["employee"] for row in file_payload["paid"]] == ["E-1201", "E-1202"]
        assert [row["employee"] for row in file_payload["excluded"]] == ["E-1203"]
        excluded = file_payload["excluded"][0]
        assert "payee_account" in excluded["reason"], excluded
        assert Decimal(excluded["net"]) > 0, excluded
        # 1 — the file and its excluded list account for the whole payroll
        assert reconciles(file_payload) == Decimal(0), reconciles(file_payload)
        assert Decimal(file_payload["total"]) < Decimal(file_payload["run_net_total"]), (
            "the excluded employee's pay is not in the file"
        )
        assert Decimal(file_payload["total"]) + Decimal(file_payload["excluded_total"]) == Decimal(
            file_payload["run_net_total"]
        ) + Decimal(file_payload["rounding_total"])
        assert [row["rounding"] for row in file_payload["paid"]] == ["0.000000"] * 2, (
            file_payload["paid"]
        )
        for row in file_payload["rows"]:
            assert row["payee_name"].endswith("REYES") and row["reference"] == "PAYROLL 2026-06"
            assert row["purpose"] == "Payroll 2026-06"
            assert len(row["amount"].split(".")[1]) == 2, row["amount"]
        _refuses(
            lambda: validate_row(
                {name: "x" for name in file_payload["columns"]} | {"amount": ""},
                format_["columns"],
            ),
            UnpayableRowError,
            "a row with a required column left empty",
        )
        _refuses(
            lambda: validate_row({"iban": "x"}, format_["columns"]),
            UnpayableRowError,
            "a row stating a column the format does not have",
        )
        print(
            "the file's six columns are the pack's own, its excluded list names the employee"
            " with no details and the amount that is not in the file, and file plus excluded"
            " equals the run's net total exactly"
        )

        # 1 again — with everybody payable, the file pays the whole payroll to the cent
        define_bank_details(
            session,
            people["CARL"],
            bank_code="BPI",
            account_number="003456789012",
            holder_name="CARL REYES",
            effective_from="2026-01-01",
            actor="hr",
        )
        session.commit()
        whole = bank_file(session, run, market="philippines")
        assert whole["excluded"] == []
        # Every net here is a whole number of cents, so the file pays the run's net total to the
        # cent (and the rounding figure is what says that, rather than the check assuming it).
        assert Decimal(whole["rounding_total"]) == Decimal("0.000000"), whole["rounding_total"]
        assert Decimal(whole["total"]) == Decimal(whole["run_net_total"])
        assert reconciles(whole) == Decimal(0)
        print(
            f"with every employee payable the file pays the run's net total"
            f" ({whole['total']}) exactly"
        )

        # 4 — the same run, the same file, byte for byte
        again = bank_file(session, run, market="philippines")
        assert render(again) == render(whole), "the same run produced a different file"
        assert render(whole).splitlines()[0] == ",".join(whole["columns"])
        assert render(whole, header=False).splitlines()[0].startswith("ANA REYES,")
        assert len(render(whole).splitlines()) == 4  # a header and three employees
        record_contract(
            session,
            people["ANA"],
            subject="hr",
            effective_from="2026-06-01",
            contract_type="regular",
            basic_salary="40000",
        )
        session.commit()
        revision_two = correct_run(
            session, run, actor="payroll", reason="the raise that took effect in June"
        )
        compute_run(session, revision_two, actor="payroll")
        session.commit()
        later = bank_file(session, revision_two, market="philippines")
        assert render(later) != render(whole), "the corrected run paid the same amounts"
        assert render(bank_file(session, run, market="philippines")) == render(whole)
        # 3 again — a net pay that is not positive is excluded for its own reason, stated
        dina = create_employee(
            session,
            company_id=company_id,
            party_code="DINA",
            number="E-1204",
            hire_date="2025-01-06",
            subject="hr",
            name="Dina Reyes",
        )
        record_contract(
            session, dina, subject="hr", effective_from="2025-01-06",
            contract_type="regular", basic_salary="100000",
        )
        define_bank_details(
            session,
            dina,
            bank_code="BPI",
            account_number="004567890123",
            holder_name="DINA REYES",
            effective_from="2026-01-01",
            actor="hr",
        )
        session.commit()
        # She joined after the period was computed once, so the run is corrected to include
        # her — the same path a late arrival takes in production.
        revision_three = correct_run(
            session, revision_two, actor="payroll", reason="a joiner was recorded after the run"
        )
        compute_run(session, revision_three, actor="payroll")
        session.commit()
        with_shortfall = bank_file(session, revision_three, market="philippines")
        reasons = {row["employee"]: row["reason"] for row in with_shortfall["excluded"]}
        assert reasons == {
            "E-1204": "nothing is payable: the net pay for the period is not positive"
        }, reasons
        # Her pay is accounted for on the list, so the file still adds up to the whole payroll.
        assert reconciles(with_shortfall) == Decimal(0)
        print(
            "an employee whose statutory deductions exceed their pay is excluded for that"
            " reason, with the amount that is not in the file"
        )

        # 5 — nothing to pay from a run nobody computed, and no pack is not an empty file
        draft = start_run(
            session, company_id=company_id, period="2026-07", actor="payroll", cutoff_day=15
        )
        session.commit()
        _refuses(
            lambda: bank_file(session, draft, market="philippines"),
            RunNotPayableError,
            "a payment file from a run that has not been computed",
        )
        session.rollback()
        _refuses(
            lambda: bank_file(session, run, market="atlantis"),
            PackError,
            "a payment file for a market the packs do not cover",
        )
        session.rollback()
        print(
            "the same run renders the same file byte for byte, a corrected run renders its own"
            " while the earlier revision's file is unchanged, a run nobody computed cannot be"
            " paid, and a market with no pack is refused rather than paid empty"
        )

    engine.dispose()
    print("ok — the pack's format, the run's money, and nobody left out quietly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
