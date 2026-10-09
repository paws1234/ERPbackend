"""T-5.LEAVE.02 check — leave types, idempotent accrual, the capped carry-forward and the derived balance.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_leave_accrual.py

It fails (non-zero exit) if any of these stops holding:

1. **accrual posts on the configured cadence, and a re-run does not double-accrue** — a monthly
   type accrues `1.25` days for `2026-06` dated `2026-06-30`, an annual type accrues only on a
   year, re-running either adds nothing, and the database refuses a second entry for the same
   employee, type and period
2. **a carry-forward cap is enforced at the year boundary, with the expiry recorded** — the
   year's remainder lapses and the capped part is carried into the new year as its own entry
   naming the date it expires, where it counts until that date and no longer; `0` carries
   nothing and null is uncapped
3. **the balance is always derivable from the entries** — it is the sum of the movements that
   had happened and had not expired by the date asked about, there is **no balance column** to
   drift, and a past date answers as it did then
4. **unpaid leave types are distinguishable from paid ones** — a type says it is paid or it is
   not, rather than being inferred from its name
5. an employee who has **left** stops accruing (T-5.EMP.03), and a movement dated outside the
   employment is refused

**Scratch database only**: it drops and recreates the schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.audit import set_actor  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.hr.employees import create_employee  # noqa: E402
from app.hr.leave import (  # noqa: E402
    LeaveEntry,
    LeaveType,
    InvalidLeaveError,
    accrue_period,
    balance,
    balances,
    carry_forward,
    define_leave_type,
    entries_of,
    record_entry,
)
from app.hr.movements import record_movement  # noqa: E402
from app.ledger import posting  # noqa: E402,F401 — every check builds the one schema


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
    with Session(engine) as session:
        session.add(
            Company(
                id=company_id,
                code="LEAVE-CHECK",
                name="Leave accrual check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()
        set_actor(session, "hr")
        ana = create_employee(
            session,
            company_id=company_id,
            party_code="ANA",
            number="E-501",
            hire_date="2025-01-06",
            subject="hr",
            name="Ana Reyes",
        )
        ben = create_employee(
            session,
            company_id=company_id,
            party_code="BEN",
            number="E-502",
            hire_date="2025-01-06",
            subject="hr",
            name="Ben Reyes",
        )
        session.commit()

        # 3 + 4 — the types, and no balance column to drift
        vacation = define_leave_type(
            session,
            company_id=company_id,
            code="VACATION",
            name="Vacation leave",
            cadence="monthly",
            accrual_days="1.25",
            carry_forward_cap="3",
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
        birthday = define_leave_type(
            session,
            company_id=company_id,
            code="BIRTHDAY",
            name="Birthday leave",
            cadence="annual",
            accrual_days="1",
            paid=True,
        )
        session.commit()
        # There is no balance to drift: the columns that could hold one are simply not there,
        # and the balance the module reports is a sum of entries.
        for table in ("leave_type", "leave_entry"):
            names = {column["name"] for column in inspect(engine).get_columns(table)}
            assert not any("balance" in name for name in names), (table, names)
        assert vacation.paid and not unpaid.paid and birthday.paid
        print(
            "VACATION (1.25/month, cap 3, paid), BIRTHDAY (1/year, paid) and UNPAID (unpaid,"
            " stated not inferred) — and no balance column anywhere"
        )
        for bad in ("-1.25", "nonsense", 1.5):
            _refuses(
                lambda bad=bad: define_leave_type(
                    session,
                    company_id=company_id,
                    code="BAD",
                    name="Bad",
                    cadence="monthly",
                    accrual_days=bad,
                ),
                InvalidLeaveError,
                f"an accrual of {bad!r}",
            )
            session.rollback()
        _refuses(
            lambda: define_leave_type(
                session,
                company_id=company_id,
                code="BAD",
                name="Bad",
                cadence="fortnightly",
                accrual_days="1",
            ),
            InvalidLeaveError,
            "an accrual cadence nobody defined",
        )
        session.rollback()

        # 1 — the cadence, and a re-run that adds nothing
        june = accrue_period(session, company_id=company_id, period="2026-06")
        session.commit()
        assert [entry.kind for entry in june] == ["accrual", "accrual"], (
            "the monthly cadence did not post for both employees and one type"
        )
        assert june[0].days == Decimal("1.2500") and june[0].on_date == date(2026, 6, 30), june[0]
        assert balance(session, ana, leave_type=vacation, on=date(2026, 6, 30)) == Decimal("1.2500")
        again = accrue_period(session, company_id=company_id, period="2026-06")
        session.commit()
        assert again == [], "re-running the period accrued a second time"
        assert balance(session, ana, leave_type=vacation, on=date(2026, 6, 30)) == Decimal("1.2500")
        # The annual type is not this period's; the year is.
        accrue_period(session, company_id=company_id, period="2026")
        session.commit()
        assert balance(session, ana, leave_type=birthday, on=date(2026, 12, 31)) == Decimal("1.0000")
        assert balance(session, ana, leave_type=vacation, on=date(2026, 12, 31)) == Decimal("1.2500")
        message = _refused(
            lambda: (
                session.execute(
                    text(
                        "INSERT INTO leave_entry"
                        " (id, company_id, employee_id, leave_type_id, kind, days, on_date, period)"
                        " VALUES (:id, :company, :employee, :type, 'accrual', 1.25, '2026-06-30',"
                        " '2026-06')"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "company": company_id,
                        "employee": ana.id,
                        "type": vacation.id,
                    },
                ),
                session.commit(),
            ),
            "uq_leave_entry_period",
        )
        session.rollback()
        print(
            "June accrues 1.25 days dated 2026-06-30, re-running adds nothing, and the database"
            f" refuses a second entry for the period ({message.splitlines()[0]})"
        )

        # A balance is a sum of movements, so a taken day moves it and an adjustment can fix it
        record_entry(
            session,
            ana,
            leave_type=vacation,
            kind="leave_taken",
            days="-1.25",
            on=date(2026, 7, 6),
            source="request LR-1",
        )
        session.commit()
        assert balance(session, ana, leave_type=vacation, on=date(2026, 7, 31)) == Decimal("0")
        assert balance(session, ana, leave_type=vacation, on=date(2026, 6, 30)) == Decimal("1.2500"), (
            "a movement after the date asked about changed that date's balance"
        )
        _refuses(
            lambda: record_entry(
                session,
                ana,
                leave_type=vacation,
                kind="leave_taken",
                days="1",
                on=date(2026, 7, 6),
            ),
            InvalidLeaveError,
            "leave taken recorded as a positive movement",
        )
        session.rollback()
        assert [entry.kind for entry in entries_of(session, ana, leave_type=vacation)] == [
            "accrual",
            "leave_taken",
        ]

        # 2 — the year boundary: the remainder lapses, the capped part is carried with an expiry
        record_entry(
            session, ana, leave_type=vacation, kind="adjustment", days="5", on=date(2026, 8, 3),
            source="long service grant",
        )
        session.commit()
        assert balance(session, ana, leave_type=vacation, on=date(2026, 12, 31)) == Decimal("5.0000")
        moved = carry_forward(
            session, company_id=company_id, year=2026, expires_on="2027-06-30"
        )
        session.commit()
        ana_vacation = [
            (entry.kind, entry.days)
            for entry in moved
            if entry.employee_id == ana.id and entry.leave_type_id == vacation.id
        ]
        assert ana_vacation == [
            ("adjustment", Decimal("-5.0000")),
            ("carry_forward", Decimal("3.0000")),
        ], ana_vacation
        carried = next(
            entry
            for entry in moved
            if entry.employee_id == ana.id
            and entry.leave_type_id == vacation.id
            and entry.kind == "carry_forward"
        )
        assert carried.expires_on == date(2027, 6, 30) and carried.on_date == date(2027, 1, 1)
        assert balance(session, ana, leave_type=vacation, on=date(2027, 1, 1)) == Decimal("3.0000")
        assert balance(session, ana, leave_type=vacation, on=date(2027, 6, 30)) == Decimal("3.0000")
        assert balance(session, ana, leave_type=vacation, on=date(2027, 7, 1)) == Decimal("0"), (
            "days past their expiry still counted"
        )
        assert carry_forward(session, company_id=company_id, year=2026) == [], (
            "re-running the year end moved the balance twice"
        )
        # 0 carries nothing; null is uncapped.
        ben_leave_capped = define_leave_type(
            session,
            company_id=company_id,
            code="SICK",
            name="Sick leave",
            cadence="monthly",
            accrual_days="1",
            carry_forward_cap="0",
        )
        ben_leave_uncapped = define_leave_type(
            session,
            company_id=company_id,
            code="FLEX",
            name="Flexible leave",
            cadence="monthly",
            accrual_days="1",
        )
        session.commit()
        record_entry(
            session, ben, leave_type=ben_leave_capped, kind="accrual", days="4", on=date(2026, 5, 1),
            period="2026-05",
        )
        record_entry(
            session, ben, leave_type=ben_leave_uncapped, kind="accrual", days="4", on=date(2026, 5, 1),
            period="2026-05",
        )
        session.commit()
        carry_forward(session, company_id=company_id, year=2026)
        session.commit()
        assert balance(session, ben, leave_type=ben_leave_capped, on=date(2027, 1, 2)) == Decimal("0"), (
            "a cap of 0 carried days forward"
        )
        assert balance(session, ben, leave_type=ben_leave_uncapped, on=date(2027, 1, 2)) == Decimal("4.0000"), (
            "an uncapped type lost what it had not used"
        )
        print(
            "5 days at the boundary: 5 lapsed and 3 carried to 2027-01-01 expiring 2027-06-30"
            f" (counted to 2027-06-30, gone on 2027-07-01); cap 0 carried nothing, uncapped kept 4"
        )

        # 5 — a leaver stops accruing, and a movement outside the employment is refused
        record_movement(
            session, ana, kind="exit", effective_date="2027-02-28", reason="resigned", actor="hr"
        )
        session.commit()
        march = accrue_period(session, company_id=company_id, period="2027-03")
        session.commit()
        assert all(entry.employee_id != ana.id for entry in march), (
            "an employee who had left accrued leave in March"
        )
        assert any(entry.employee_id == ben.id for entry in march), "the accrual stopped for everybody"
        _refuses(
            lambda: record_entry(
                session,
                ana,
                leave_type=vacation,
                kind="leave_taken",
                days="-1",
                on=date(2027, 3, 15),
            ),
            InvalidLeaveError,
            "leave taken after the exit",
        )
        session.rollback()
        # `balances` reads every type for a date, and the lapsing carry-forward is part of it.
        stated = balances(session, ben, on=date(2027, 1, 2))
        assert stated["FLEX"] == Decimal("4.0000") and stated["SICK"] == Decimal("0"), stated
        print(
            "the leaver stopped accruing in March while the remaining employee did not, a"
            " movement after the exit was refused, and the balances read per type as a sum"
        )

    engine.dispose()
    print("ok — accruals are idempotent, the cap and its expiry are on the record, and every balance is a sum")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
