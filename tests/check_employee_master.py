"""T-5.EMP.01 check — the employee master: one party, contracts as history, assets, restricted fields.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_employee_master.py

It fails (non-zero exit) if any of these stops holding:

1. an employee is a **role on the party**, not a second identity — giving an existing
   customer the employee role appends it to the one row, a repeated role is not stored
   twice, and a second employee profile for the same party is refused
2. **the contract history preserves the terms that applied**: a new contract closes the
   one it supersedes and leaves its terms untouched, `current_contract` and
   `contract_in_force` answer the two different questions (what applies now vs what
   applied on a stated date), overlapping terms and terms before the hire date are
   refused, and the database refuses to rewrite a contract that was already recorded
3. **an assigned asset is trackable to a return date**: it is held while `returned_on`
   is null, the return is recorded once, and a second return — or one dated before the
   assignment — is refused
4. **salary and personal fields are field-restricted**: a role that may not read one does
   not receive it at all (absent, not blank) while an unrestricted reader does, and a
   write of a restricted field is refused *and* recorded as a refusal on the trail
5. an employee is a master: it retires by marking, and the database refuses to delete it

**Scratch database only**: it drops and recreates the schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.audit import AuditLog, soft_delete  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.hr.employees import (  # noqa: E402
    AssetReturnError,
    ContractSequenceError,
    DuplicateEmployeeError,
    Employee,
    NotAnEmployeeError,
    UnknownEmployeeError,
    assets_held,
    assign_asset,
    contract_in_force,
    create_employee,
    employee_by_code,
    employee_by_number,
    employee_payload,
    latest_contract,
    record_contract,
    return_asset,
    set_personal_details,
)
from app.ledger import posting  # noqa: E402,F401 — every check builds the one schema
from app.party import Party, create_party  # noqa: E402
from app.security import (  # noqa: E402
    REFUSED,
    FieldAccessDenied,
    assign,
    define_role,
    grant,
    restrict,
)

# The party master's column set, as T-0.PARTY.01's own check states it. Nothing about
# employment may appear on the shared row.
SHARED_PARTY_COLUMNS = {"id", "company_id", "code", "name", "tax_id", "deleted_at"}


def _refused(call, expected: str) -> str:
    """The database's message if `call` is refused; fail the check otherwise."""
    try:
        call()
    except DBAPIError as exc:
        message = str(exc.orig).strip()
        assert expected in message, f"unclear database error: {message}"
        return message
    raise AssertionError(f"the database accepted what it must refuse ({expected!r})")


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
    other_company = uuid.uuid4()
    with Session(engine) as session:
        session.add_all(
            [
                Company(
                    id=company_id,
                    code="HR-CHECK",
                    name="Employee master check",
                    base_currency="PHP",
                    fiscal_year_start_month=1,
                ),
                Company(
                    id=other_company,
                    code="HR-OTHER",
                    name="Another company",
                    base_currency="PHP",
                    fiscal_year_start_month=1,
                ),
            ]
        )
        session.commit()

        # 1 — one party, several roles, one row ------------------------------------
        create_party(
            session, company_id=company_id, code="ANA", name="Ana Reyes", roles=["customer"]
        )
        session.commit()
        ana = create_employee(
            session,
            company_id=company_id,
            party_code="ANA",
            number="E-001",
            hire_date="2026-01-05",
            subject="hr",
            date_of_birth="1994-03-02",
            personal_email="Ana.Reyes@Example.com",
            mobile_number="+63 917 000 0000",
            home_address="12 Mabini Street, Cebu",
        )
        ana_id = ana.id
        session.commit()
        assert set(Party.__table__.columns.keys()) == SHARED_PARTY_COLUMNS, (
            "the party master grew a field that belongs to a role:"
            f" {sorted(set(Party.__table__.columns.keys()) - SHARED_PARTY_COLUMNS)}"
        )
        assert ana.party.has_role("customer") and ana.party.has_role("employee")
        assert session.scalar(select(func.count()).select_from(Party)) == 1, (
            "the employee role needed a second identity instead of being appended"
        )
        assert sorted(role.role for role in ana.party.roles) == ["customer", "employee"]
        assert ana.personal_email == "ana.reyes@example.com", "a personal e-mail was stored un-normalised"
        print("ANA is one party row holding customer + employee, one employee profile")

        # A party that does not exist yet is created with the employee role…
        ben = create_employee(
            session,
            company_id=company_id,
            party_code="BEN",
            number="E-002",
            hire_date="2026-02-01",
            subject="hr",
            name="Ben Cruz",
        )
        ben_id = ben.id
        session.commit()
        assert sorted(role.role for role in ben.party.roles) == ["employee"]
        # …and a second profile for one party is refused, however it is asked for.
        _refuses(
            lambda: create_employee(
                session,
                company_id=company_id,
                party_code="ANA",
                number="E-003",
                hire_date="2026-02-01",
                subject="hr",
            ),
            DuplicateEmployeeError,
            "a second employee profile for one party",
        )
        session.rollback()
        print("a new party is created with the employee role; a second profile is refused")

        # The two unique constraints are judged over every row, retired ones included, so
        # the refusals are named rather than left to the database's constraint error.
        karl = create_employee(
            session,
            company_id=company_id,
            party_code="KARL",
            number="E-004",
            hire_date="2026-03-01",
            subject="hr",
            name="Karl Lim",
        )
        session.commit()
        _refuses(
            lambda: create_employee(
                session,
                company_id=company_id,
                party_code="LINDA",
                number="E-004",
                hire_date="2026-04-01",
                subject="hr",
                name="Linda Tan",
            ),
            DuplicateEmployeeError,
            "an employee number another employee already has",
        )
        session.rollback()
        assert session.scalar(
            select(func.count()).select_from(Party).where(Party.code == "LINDA")
        ) == 0, "a refused creation left a party behind"
        soft_delete(session, karl)
        session.commit()
        _refuses(
            lambda: create_employee(
                session,
                company_id=company_id,
                party_code="KARL",
                number="E-005",
                hire_date="2026-04-01",
                subject="hr",
            ),
            DuplicateEmployeeError,
            "a second profile for a retired employee's party",
        )
        session.rollback()
        print("a taken number and a retired profile's party are both refused by name, writing nothing")

        # A lookup that names nothing, and a party that is not an employee.
        _refuses(
            lambda: employee_by_number(session, company_id=company_id, number="E-999"),
            UnknownEmployeeError,
            "an employee number nobody has",
        )
        create_party(session, company_id=company_id, code="CARL", name="Carl Poe", roles=["supplier"])
        session.commit()
        _refuses(
            lambda: employee_by_code(session, company_id=company_id, code="CARL"),
            NotAnEmployeeError,
            "a party without the employee role",
        )

        # 2 — the contract history, and the two questions it answers ----------------
        hired = record_contract(
            session,
            ana,
            subject="hr",
            effective_from="2026-01-05",
            basic_salary="30000.00",
            contract_type="probationary",
        )
        session.commit()
        regular = record_contract(
            session,
            ana,
            subject="hr",
            effective_from="2026-07-05",
            basic_salary="35000.00",
            contract_type="regular",
        )
        session.commit()
        assert latest_contract(ana).id == regular.id, "the latest contract is not the last one recorded"
        assert contract_in_force(ana, on=date(2026, 3, 15)).id == hired.id, (
            "a date inside the first contract's window did not read the first contract"
        )
        assert contract_in_force(ana, on=date(2026, 7, 5)).id == regular.id, (
            "the day the new terms start belongs to the old ones"
        )
        assert contract_in_force(ana, on=date(2025, 12, 31)) is None, (
            "terms were reported for a date before any contract existed"
        )
        # The superseded contract keeps its own terms and gains an end date — nothing
        # is overwritten, so a past period stays reproducible.
        assert hired.basic_salary == Decimal("30000.000000"), "the replaced terms were rewritten"
        assert hired.contract_type == "probationary", "the replaced contract type was rewritten"
        assert hired.effective_to == date(2026, 7, 5)
        assert regular.supersedes_id == hired.id, "the new contract does not name what it replaced"
        print(
            "two contracts on one history: 30000.000000 probationary 2026-01-05…2026-07-05"
            " superseded by 35000.000000 regular, both read back by date"
        )

        # Overlapping terms, and terms before the hire date, are refused.
        _refuses(
            lambda: record_contract(
                session,
                ana,
                subject="hr",
                effective_from="2026-06-01",
                basic_salary="33000.00",
                contract_type="regular",
            ),
            ContractSequenceError,
            "terms that would overlap the ones already recorded",
        )
        _refuses(
            lambda: record_contract(
                session,
                ben,
                subject="hr",
                effective_from="2025-11-01",
                basic_salary="20000.00",
                contract_type="regular",
            ),
            ContractSequenceError,
            "terms starting before the hire date",
        )
        # The history is append-only: the terms already recorded cannot be rewritten by
        # anything, this module included — a correction is a new contract, the way the
        # ledger is corrected by posting rather than by editing.
        message = _refused(
            lambda: (
                session.execute(
                    text(
                        "UPDATE employment_contract SET basic_salary = 99000"
                        " WHERE id = :id"
                    ),
                    {"id": hired.id},
                ),
                session.commit(),
            ),
            "is append-only",
        )
        session.rollback()
        print(f"the database refused to rewrite the recorded terms: {message}")

        # 3 — assets, and the return date that closes one out ----------------------
        laptop = assign_asset(
            session,
            ana,
            description="Laptop",
            identifier="SN-4711",
            assigned_on="2026-01-05",
        )
        phone = assign_asset(session, ana, description="Mobile phone", assigned_on="2026-03-01")
        session.commit()
        assert [asset.description for asset in assets_held(ana)] == ["Laptop", "Mobile phone"]
        return_asset(session, laptop, returned_on="2026-06-30")
        session.commit()
        assert [asset.description for asset in assets_held(ana)] == ["Mobile phone"], (
            "a returned asset is still held"
        )
        assert laptop.returned_on == date(2026, 6, 30)
        _refuses(
            lambda: return_asset(session, laptop, returned_on="2026-07-31"),
            AssetReturnError,
            "returning one asset twice",
        )
        session.rollback()
        _refuses(
            lambda: (
                session.execute(
                    text("UPDATE employee_asset SET returned_on = '2026-01-01' WHERE id = :id"),
                    {"id": phone.id},
                ),
                session.commit(),
            ),
            DBAPIError,
            "a return dated before the assignment",
        )
        session.rollback()
        print(
            "an asset is held until its return date is recorded, once, and never before it"
            " was assigned"
        )

        # 4 — salary and personal fields are restricted, not merely unshown --------
        payroll_role = define_role(
            session, company_id=company_id, code="payroll", name="Payroll officer"
        )
        grant(session, payroll_role, "employee.read")
        intern_role = define_role(session, company_id=company_id, code="intern", name="Intern")
        grant(session, intern_role, "employee.read")
        restrict(
            session,
            intern_role,
            entity="employee",
            field="mobile_number",
            can_read=False,
            can_write=False,
        )
        restrict(
            session,
            intern_role,
            entity="employment_contract",
            field="basic_salary",
            can_read=False,
            can_write=False,
        )
        assign(session, company_id=company_id, subject="payroll-user", role=payroll_role)
        assign(session, company_id=company_id, subject="intern-user", role=intern_role)
        session.commit()

        seen = employee_payload(
            session, company_id=company_id, subject="payroll-user", employee=ana
        )
        assert seen["mobile_number"] == "+63 917 000 0000", "an unrestricted reader lost the field"
        assert Decimal(seen["contracts"][-1]["basic_salary"]) == Decimal("35000.00"), (
            "an unrestricted reader lost the salary"
        )

        withheld = employee_payload(
            session, company_id=company_id, subject="intern-user", employee=ana
        )
        assert "mobile_number" not in withheld, "a restricted field was returned to the intern"
        assert withheld["date_of_birth"] == "1994-03-02", "the restriction hid more than one field"
        assert "basic_salary" not in withheld["contracts"][-1], (
            "a restricted salary was returned on the contract"
        )
        assert withheld["contracts"][-1]["contract_type"] == "regular", (
            "the salary restriction hid the rest of the contract"
        )
        print(
            "the intern's payload carries no mobile_number and no basic_salary, while the"
            " payroll payload carries both"
        )

        # A write of a restricted field is refused at entry, and the refusal is on the
        # trail naming who tried and what they held.
        denied = _refuses(
            lambda: set_personal_details(
                session, ana, subject="intern-user", mobile_number="+63 918 111 2222"
            ),
            FieldAccessDenied,
            "an intern writing a restricted personal field",
        )
        assert denied.field == "mobile_number", f"the refusal named {denied.field!r}"
        denied = _refuses(
            lambda: record_contract(
                session,
                ana,
                subject="intern-user",
                effective_from="2027-01-05",
                basic_salary="40000.00",
                contract_type="regular",
            ),
            FieldAccessDenied,
            "an intern writing a restricted salary",
        )
        assert denied.field == "basic_salary", f"the refusal named {denied.field!r}"
        refusals = list(
            session.scalars(
                select(AuditLog).where(
                    AuditLog.company_id == company_id, AuditLog.action == REFUSED
                )
            )
        )
        attempted = " ".join(
            str((entry.after_values or {}).get("attempted", "")) for entry in refusals
        )
        assert "employee.mobile_number" in attempted and "employment_contract.basic_salary" in attempted, (
            f"the refusals were not recorded on the trail: {attempted!r}"
        )
        assert all(
            (entry.after_values or {}).get("held_roles") == ["intern"] for entry in refusals
        ), "a refusal did not name the roles the actor held"
        print(
            f"writing a restricted field was refused at entry and recorded"
            f" ({len(refusals)} refusals on the trail), naming the actor's roles"
        )

        # 5 — a master retires by marking ------------------------------------------
        soft_delete(session, ana)
        session.commit()
        stored = session.execute(
            text("SELECT deleted_at FROM employee WHERE id = :id"), {"id": ana.id}
        ).one()
        assert stored[0] is not None, "the employee was removed, not marked"
        _refused(
            lambda: (
                session.execute(text("DELETE FROM employee WHERE id = :id"), {"id": ana.id}),
                session.commit(),
            ),
            "is a master",
        )
        session.rollback()
        print("an employee retires by marking; the database refuses to delete it")

    # A fresh session, so no answer comes from the identity map: this is what a later
    # request sees — the retired employee is gone from ordinary reads, the live one is not.
    with Session(engine) as session:
        assert session.scalar(select(Employee).where(Employee.id == ana_id)) is None, (
            "a retired employee is still readable"
        )
        assert session.scalar(select(Employee).where(Employee.id == ben_id)) is not None, (
            "the retirement hid the wrong employee"
        )

    engine.dispose()
    print("ok — the employee master holds one profile per party, terms as history, assets and restricted fields")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
