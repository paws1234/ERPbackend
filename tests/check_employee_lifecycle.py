"""T-5.EMP.03 check — the employee lifecycle: dated movements, the period they affect, and the exit.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_employee_lifecycle.py

It fails (non-zero exit) if any of these stops holding:

1. **a movement is a dated fact, and rewrites nothing** — a joining, a transfer and a
   salary revision **append** (a movement, a placement, a contract), the terms and the
   placement of a past date still read as they did, and the database refuses to rewrite a
   recorded movement
2. **a mid-period movement affects the right portion of the period** — a mid-month joiner
   and a mid-month leaver are employed for the tail and the head of the month and not for
   the rest, and a salary revised mid-month pays the old rate before the revision and the
   new one after
3. **an exit ends employment** — nothing can be recorded from the exit date onwards, a
   second exit is refused, and `active_on` is false from that date: what leave accrual
   stops on and what a payroll run reads before it includes anybody
4. **every movement carries an actor and a reason** — both are required and stored, and the
   change is on the T-0.AUDIT.02 trail as well
5. **a movement's effects are its own** — a transfer appends the placement it creates
   (carrying forward what it does not state), a promotion appends the contract, and a kind
   that claims an effect it does not own — a joining or an exit with effects, a promotion
   stating a manager — is refused

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

from app.audit import AuditLog, set_actor  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.hr.employees import contract_in_force, create_employee, record_contract  # noqa: E402
from app.hr.movements import (  # noqa: E402
    InvalidMovementError,
    MovementError,
    MovementSequenceError,
    active_on,
    employment_in,
    exit_of,
    movements_of,
    record_movement,
)
from app.hr.org import manager_in_force, place_employee, placement_in_force  # noqa: E402
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
                code="LIFE-CHECK",
                name="Employee lifecycle check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()
        set_actor(session, "hr")

        boss = create_employee(
            session,
            company_id=company_id,
            party_code="BOSS",
            number="E-100",
            hire_date="2025-01-06",
            subject="hr",
            name="Boss Reyes",
        )
        ana = create_employee(
            session,
            company_id=company_id,
            party_code="ANA",
            number="E-101",
            hire_date="2026-06-15",
            subject="hr",
            name="Ana Reyes",
        )
        ben = create_employee(
            session,
            company_id=company_id,
            party_code="BEN",
            number="E-102",
            hire_date="2026-01-05",
            subject="hr",
            name="Ben Reyes",
        )
        session.commit()
        place_employee(session, boss, effective_from="2025-01-06")
        place_employee(
            session,
            ben,
            effective_from="2026-01-05",
            manager=boss,
            department="Finance",
            cost_centre="CC-1",
            location="Cebu",
        )
        session.commit()
        record_contract(
            session,
            ben,
            subject="hr",
            effective_from="2026-01-05",
            basic_salary="30000.00",
            contract_type="regular",
        )
        session.commit()

        # 1 + 4 — a movement is recorded, with who and why, and nothing else changes
        joining = record_movement(
            session,
            ana,
            kind="joining",
            effective_date="2026-06-15",
            reason="hired as a payroll assistant",
            actor="hr",
        )
        session.commit()
        assert joining.kind == "joining" and joining.effective_date == date(2026, 6, 15)
        assert joining.actor == "hr" and joining.reason == "hired as a payroll assistant"
        _refuses(
            lambda: record_movement(
                session,
                ana,
                kind="joining",
                effective_date="2026-06-15",
                reason="again",
                actor="hr",
            ),
            MovementSequenceError,
            "a second joining",
        )
        session.rollback()
        _refuses(
            lambda: record_movement(
                session,
                ana,
                kind="joining",
                effective_date="2026-07-01",
                reason="a month late",
                actor="hr",
            ),
            MovementSequenceError,
            "a joining on a date other than the hire date",
        )
        session.rollback()
        for blank in ({"reason": "", "actor": "hr"}, {"reason": "moved", "actor": " "}):
            _refuses(
                lambda blank=blank: record_movement(
                    session,
                    ana,
                    kind="transfer",
                    effective_date="2026-07-01",
                    department="Payroll",
                    **blank,
                ),
                InvalidMovementError,
                f"a movement with {blank}",
            )
            session.rollback()
        _refuses(
            lambda: record_movement(
                session,
                ana,
                kind="secondment",
                effective_date="2026-07-01",
                reason="a kind nobody has heard of",
                actor="hr",
            ),
            InvalidMovementError,
            "a movement kind outside the lifecycle",
        )
        session.rollback()
        assert session.scalar(
            select(AuditLog).where(
                AuditLog.entity == "employee_movement", AuditLog.action == "insert"
            )
        ) is not None, "a recorded movement is not on the audit trail"
        print(
            "a movement carries its actor and its reason, refuses a blank one and an unknown"
            " kind, and lands on the audit trail as well"
        )

        # 5 + 2 — a transfer appends the placement it creates, carrying forward what it keeps
        transfer = record_movement(
            session,
            ben,
            kind="transfer",
            effective_date="2026-06-15",
            reason="the payroll team moved under Ana's department",
            actor="hr",
            department="Payroll",
            location="Manila",
        )
        session.commit()
        after = placement_in_force(session, ben, on=date(2026, 6, 20))
        before = placement_in_force(session, ben, on=date(2026, 6, 10))
        assert after.department == "Payroll" and after.location == "Manila"
        assert after.cost_centre == "CC-1", "the transfer blanked the cost centre it did not change"
        assert after.manager_id == before.manager_id, "the transfer dropped the manager it kept"
        assert before.department == "Finance" and before.location == "Cebu", (
            "the transfer rewrote the placement that applied before it"
        )
        assert manager_in_force(session, ben, on=date(2026, 6, 10)).number == "E-100"
        message = _refused(
            lambda: (
                session.execute(
                    text("UPDATE employee_movement SET reason = 'edited' WHERE id = :id"),
                    {"id": transfer.id},
                ),
                session.commit(),
            ),
            "is append-only",
        )
        session.rollback()
        print(
            "the transfer appends a placement (Payroll/Manila, cost centre and manager carried"
            f" forward) and the database refuses to rewrite the movement ({message.splitlines()[0]})"
        )

        # A transfer that changes nothing, and a joining that claims an effect, are refused.
        _refuses(
            lambda: record_movement(
                session,
                ben,
                kind="transfer",
                effective_date="2026-07-01",
                reason="nothing in particular",
                actor="hr",
            ),
            InvalidMovementError,
            "a transfer that changes nothing",
        )
        session.rollback()
        _refuses(
            lambda: record_movement(
                session,
                ben,
                kind="exit",
                effective_date="2026-12-31",
                reason="left",
                actor="hr",
                department="Payroll",
            ),
            InvalidMovementError,
            "an exit that carries an effect",
        )
        session.rollback()
        # A kind may not claim an effect it does not apply: a promotion that stated a manager
        # would read as a reorganisation that never happened.
        _refuses(
            lambda: record_movement(
                session,
                ben,
                kind="promotion",
                effective_date="2026-07-01",
                reason="promoted and moved",
                actor="hr",
                basic_salary="40000.00",
                manager=boss,
            ),
            InvalidMovementError,
            "a promotion claiming a reporting-line change",
        )
        session.rollback()

        # 2 — a salary revised mid-month pays the old rate before and the new one after
        revision = record_movement(
            session,
            ben,
            kind="promotion",
            effective_date="2026-06-20",
            reason="confirmed as payroll lead after review",
            actor="hr",
            basic_salary="38000.00",
        )
        session.commit()
        assert revision.basic_salary == Decimal("38000.000000")
        assert revision.contract_type is None, (
            "the movement claims to have changed the engagement it carried forward"
        )
        assert contract_in_force(ben, on=date(2026, 6, 10)).basic_salary == Decimal("30000.000000")
        assert contract_in_force(ben, on=date(2026, 6, 25)).basic_salary == Decimal("38000.000000")
        assert contract_in_force(ben, on=date(2026, 6, 25)).contract_type == "regular"
        print(
            "the revision pays 30000.000000 up to 2026-06-19 and 38000.000000 from 2026-06-20,"
            " with the engagement carried forward"
        )
        _refuses(
            lambda: record_movement(
                session,
                ben,
                kind="promotion",
                effective_date="2026-07-01",
                reason="a title change",
                actor="hr",
            ),
            InvalidMovementError,
            "a promotion with no revised salary",
        )
        session.rollback()

        # 2 — the portion of a period: a mid-month joiner is paid the tail, not the month
        june = (date(2026, 6, 1), date(2026, 7, 1))
        assert employment_in(session, ana, since=june[0], until=june[1]) == (
            date(2026, 6, 15),
            date(2026, 7, 1),
        ), "a mid-month joiner is employed for the start of the month"
        assert not active_on(session, ana, on=date(2026, 6, 14)), "the day before the hire"
        assert active_on(session, ana, on=date(2026, 6, 15)), "the hire date itself"
        assert employment_in(session, ana, since=june[0], until=june[1]) == (
            max(june[0], ana.hire_date),
            june[1],
        )
        print(
            "the mid-month joiner is employed 2026-06-15…2026-07-01 — 16 of the month's 30"
            " days, not the whole period"
        )

        # 3 — the exit: from that date the employee is not active, and nothing follows it
        goodbye = record_movement(
            session,
            ana,
            kind="exit",
            effective_date="2026-09-30",
            reason="resigned; last day 2026-09-29",
            actor="hr",
        )
        session.commit()
        assert exit_of(session, ana) == date(2026, 9, 30)
        assert active_on(session, ana, on=date(2026, 9, 29)), "the last day worked"
        assert not active_on(session, ana, on=date(2026, 9, 30)), "the exit date is a day not employed"
        assert not active_on(session, ana, on=date(2026, 10, 1))
        assert employment_in(
            session, ana, since=date(2026, 9, 1), until=date(2026, 10, 1)
        ) == (date(2026, 9, 1), date(2026, 9, 30)), "a leaver is paid for the days they were here"
        assert employment_in(
            session, ana, since=date(2026, 10, 1), until=date(2026, 11, 1)
        ) is None, "a leaver is still employed after leaving"
        _refuses(
            lambda: record_movement(
                session,
                ana,
                kind="exit",
                effective_date="2026-10-31",
                reason="left again",
                actor="hr",
            ),
            MovementSequenceError,
            "a second exit",
        )
        session.rollback()
        _refuses(
            lambda: record_movement(
                session,
                ana,
                kind="transfer",
                effective_date="2026-10-01",
                reason="moved after leaving",
                actor="hr",
                department="Sales",
            ),
            MovementSequenceError,
            "a movement dated after the exit",
        )
        session.rollback()
        _refused(
            lambda: (
                session.execute(
                    text(
                        "INSERT INTO employee_movement"
                        " (id, company_id, employee_id, kind, effective_date, actor, reason)"
                        " VALUES (:id, :company, :employee, 'exit', '2026-11-30', 'hr', 'a second one')"
                    ),
                    {"id": uuid.uuid4(), "company": company_id, "employee": ana.id},
                ),
                session.commit(),
            ),
            "uq_employee_movement_exit",
        )
        session.rollback()
        print(
            "the exit ends employment on 2026-09-30: active to 2026-09-29, paid for the head of"
            f" September, unreachable afterwards ({goodbye.effective_date}), and a second exit is"
            " refused by the service and by the database"
        )

        # A transfer for somebody with no reporting line cannot invent a manager: it is
        # refused with what to do about it rather than quietly making them a second root.
        refused = _refuses(
            lambda: record_movement(
                session,
                ana,
                kind="transfer",
                effective_date="2026-07-01",
                reason="moved",
                actor="hr",
                department="Payroll",
            ),
            MovementError,
            "a transfer for an employee with no reporting line",
        )
        session.rollback()
        assert "state the manager" in str(refused), f"the refusal says: {refused}"
        record_movement(
            session,
            ana,
            kind="transfer",
            effective_date="2026-07-01",
            reason="moved into payroll under Ben",
            actor="hr",
            department="Payroll",
            manager=ben,
        )
        session.commit()
        assert manager_in_force(session, ana, on=date(2026, 7, 5)).number == "E-102"
        assert [movement.kind for movement in movements_of(session, ana)] == [
            "joining",
            "transfer",
            "exit",
        ], "the life is not readable in order"
        assert movements_of(session, ana)[1].location is None, (
            "the movement claims a location it did not state"
        )
        print(
            "a transfer with no reporting line to keep is refused with what to state, and the"
            " life reads joining → transfer → exit in order"
        )

    engine.dispose()
    print("ok — the lifecycle is dated history, the period it affects is the portion employed, and the exit ends it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
