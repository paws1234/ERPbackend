"""T-5.LEAVE.03 check — one approved, one rejected and one over-balance application.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_leave_approval.py

It fails (non-zero exit) if any of these stops holding:

1. **an application is priced by the calendar** — the day count is how many days in the range
   T-5.LEAVE.01 calls worked, so a market holiday and the region's own holiday inside a week
   each cost nothing while another region's holiday costs nothing either, and the figure the
   approver sees is the figure the balance moves by
2. **an application over the balance is refused unless somebody signs the difference** — the
   refusal names the balance and the ask, an override missing either the actor or the reason
   is refused, and an override that is given is on the request's own record
3. **approval deducts the balance exactly once** — the chain follows its configuration (one
   level for a small ask, two for a large one, the wrong role refused at each), the balance
   moves when the chain completes and not before, a further decision on the finished request
   is refused, and the entry that took the days is named on the request
4. **a rejected or cancelled application restores nothing incorrectly** — a rejection moves
   no balance, cancelling a *pending* application credits nothing, and cancelling an
   *approved* one gives back exactly the days it took, as an adjustment rather than an edit
5. **an approved leave day is visible in attendance** — the day reports the request, its type
   and whether the type is paid, the period lists the leave day by day, and a day either side
   of it reports none
6. a company with **no configured leave chain is refused**, and nothing half-written is left
   behind by the refusal

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

from app.audit import set_actor  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.hr.attendance import attendance_day  # noqa: E402
from app.hr.employees import create_employee  # noqa: E402
from app.hr.holidays import state_holiday  # noqa: E402
from app.hr.leave import balance, define_leave_type, entries_of, record_entry  # noqa: E402
from app.hr.leave_requests import (  # noqa: E402
    InsufficientBalanceError,
    LeaveRequest,
    NotDecidableError,
    applications_of,
    cancel_request,
    decide_request,
    leave_in_period,
    leave_on,
    request_leave,
    working_days,
)
from app.hr.org import place_employee  # noqa: E402
from app.ledger import posting  # noqa: E402,F401 — every check builds the one schema
from app.workflow import (  # noqa: E402
    APPROVE,
    REJECT,
    NoWorkflowConfigured,
    RequestNotPending,
    WrongApprover,
    configure,
)

# The day the deductions are made is the day they are decided — T-5.LEAVE.03 dates an
# approved application's movement on the day of the approval — so the balances below are read
# on **today**, whatever today is, rather than on a date written down here. The dataset's own
# dates (November and December 2026) are the leave being applied for, not the reading.
TODAY = date.today()
BALANCE_ON = TODAY


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
    other_company_id = uuid.uuid4()
    with Session(engine) as session:
        for cid, code in ((company_id, "LEAVE-APP"), (other_company_id, "NO-CHAIN")):
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
        set_actor(session, "hr")
        ana = create_employee(
            session,
            company_id=company_id,
            party_code="ANA",
            number="E-601",
            hire_date="2025-01-06",
            subject="hr",
            name="Ana Reyes",
        )
        stranger = create_employee(
            session,
            company_id=other_company_id,
            party_code="ZOE",
            number="E-901",
            hire_date="2025-01-06",
            subject="hr",
            name="Zoe Cruz",
        )
        session.commit()

        # The region whose calendar applies is the one the employee is placed in (T-5.EMP.02).
        place_employee(session, ana, effective_from="2025-01-06", department="Ops", location="DXB")
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
        # The chain is configuration. One level decides a small ask, two decide a large one.
        configure(
            session,
            company_id=company_id,
            doc_type="leave",
            name="Leave approval",
            levels=[(0, "line manager"), (10, "hr head")],
        )
        record_entry(
            session,
            ana,
            leave_type=vacation,
            kind="accrual",
            days="20",
            # Dated before the leave these applications ask for, so the balances below are the
            # entitlement plus the movements and not an artefact of the day the check ran.
            on=min(TODAY, date(2026, 11, 1)),
            period="2026-10",
            source="accrual 2026-10",
        )
        session.commit()

        # 1 — the day count is read from the calendar, never typed
        assert working_days(
            session, ana, from_date="2026-11-02", to_date="2026-11-06"
        ) == Decimal(5), "a week of working days is five days"
        state_holiday(
            session,
            company_id=company_id,
            on="2026-11-04",
            name="Market-wide holiday",
            kind="special",
        )
        assert working_days(session, ana, from_date="2026-11-02", to_date="2026-11-06") == 4
        state_holiday(
            session,
            company_id=company_id,
            on="2026-11-05",
            name="A holiday in another region",
            region="PHL",
        )
        assert working_days(session, ana, from_date="2026-11-02", to_date="2026-11-06") == 4
        state_holiday(
            session,
            company_id=company_id,
            on="2026-11-06",
            name="Holiday in the employee's own region",
            region="DXB",
        )
        assert working_days(session, ana, from_date="2026-11-02", to_date="2026-11-06") == 3
        session.commit()

        week = request_leave(
            session,
            ana,
            leave_type=vacation,
            from_date="2026-11-02",
            to_date="2026-11-06",
            actor="ana",
            reason="family",
        )
        assert week.days == Decimal("3.0000"), week.days
        assert week.state == "pending" and week.approval_request_id is not None
        # A pending application has taken nothing: it is waiting, not spent.
        assert balance(session, ana, leave_type=vacation, on=BALANCE_ON) == Decimal("20.0000")
        print(
            "a Monday-to-Friday application costs 3 days: the market holiday and the region's"
            " own holiday cost nothing, another region's holiday is not this employee's, and"
            " the day count is read rather than typed"
        )

        # 2 — over the balance, with an override taken on the record
        january = dict(from_date="2027-01-04", to_date="2027-02-05")
        assert working_days(session, ana, **january) == Decimal(33)
        session.commit()
        refused = _refuses(
            lambda: request_leave(
                session, ana, leave_type=vacation, actor="ana", reason="long leave", **january
            ),
            InsufficientBalanceError,
            "an application for more days than the balance holds",
        )
        assert "holds 20.0000 day(s)" in str(refused) and "asks for 33" in str(refused), refused
        _refuses(
            lambda: request_leave(
                session,
                ana,
                leave_type=vacation,
                actor="ana",
                override_actor="hr",
                **january,
            ),
            InsufficientBalanceError,
            "an override with an actor and no reason",
        )
        _refuses(
            lambda: request_leave(
                session,
                ana,
                leave_type=vacation,
                actor="ana",
                override_reason="unpaid leave approved in advance",
                **january,
            ),
            InsufficientBalanceError,
            "an override with a reason and no actor",
        )
        over = request_leave(
            session,
            ana,
            leave_type=vacation,
            actor="ana",
            override_actor="hr-head",
            override_reason="long service, approved in writing",
            **january,
        )
        assert over.days == Decimal("33.0000") and over.override_actor == "hr-head"
        session.commit()
        cancel_request(session, over, actor="hr-head", reason="withdrawn while pending")
        assert balance(session, ana, leave_type=vacation, on=BALANCE_ON) == Decimal("20.0000")
        print(
            "20 days held against 33 asked for is refused by name, an override needs both an"
            " actor and a reason, and the one that was given names both on the request"
        )

        # 3 — the chain: one level for a small ask, two for a large one, once only
        _refuses(
            lambda: decide_request(
                session, week, actor="maria", action=APPROVE, role="hr head"
            ),
            WrongApprover,
            "the second level deciding the first",
        )
        decide_request(session, week, actor="maria", action=APPROVE, role="line manager")
        assert week.state == "approved" and week.entry_id is not None
        assert balance(session, ana, leave_type=vacation, on=BALANCE_ON) == Decimal("17.0000")
        taken = [
            entry
            for entry in entries_of(session, ana, leave_type=vacation)
            if entry.kind == "leave_taken"
        ]
        assert [entry.days for entry in taken] == [Decimal("-3.0000")], taken
        _refuses(
            lambda: decide_request(
                session, week, actor="maria", action=APPROVE, role="line manager"
            ),
            RequestNotPending,
            "a second decision on a finished request",
        )
        assert balance(session, ana, leave_type=vacation, on=BALANCE_ON) == Decimal("17.0000")

        month = request_leave(
            session,
            ana,
            leave_type=vacation,
            from_date="2027-02-08",
            to_date="2027-02-19",
            actor="ana",
            reason="wedding",
        )
        assert month.days == Decimal("12.0000")
        session.commit()
        _refuses(
            lambda: decide_request(session, month, actor="maria", action=APPROVE, role="hr head"),
            WrongApprover,
            "the second level deciding the first",
        )
        decide_request(session, month, actor="maria", action=APPROVE, role="line manager")
        # Waiting on the second level is not approved: the balance has not moved.
        assert month.state == "pending" and month.entry_id is None
        assert balance(session, ana, leave_type=vacation, on=BALANCE_ON) == Decimal("17.0000")
        decide_request(session, month, actor="joel", action=APPROVE, role="hr head")
        assert month.state == "approved"
        assert balance(session, ana, leave_type=vacation, on=BALANCE_ON) == Decimal("5.0000")
        history = [
            (decision.level_no, decision.actor, decision.action)
            for decision in month.approval_request.decisions
        ]
        assert history == [(1, "maria", "approve"), (2, "joel", "approve")], history
        print(
            "the chain followed its configuration: one level decided three days, two decided"
            " twelve, the wrong role was refused at each, the balance moved only when the"
            " chain completed, and the second decision on the finished request changed nothing"
        )

        # 4 — rejected and cancelled applications
        rejected = request_leave(
            session,
            ana,
            leave_type=vacation,
            from_date="2026-12-07",
            to_date="2026-12-08",
            actor="ana",
        )
        session.commit()
        decide_request(
            session, rejected,
            actor="maria",
            action=REJECT,
            role="line manager",
            reason="the whole team is on peak-season cover",
        )
        assert rejected.state == "rejected"
        assert balance(session, ana, leave_type=vacation, on=BALANCE_ON) == Decimal("5.0000")
        assert rejected.approval_request.decisions[-1].reason.startswith("the whole team")

        pending = request_leave(
            session,
            ana,
            leave_type=vacation,
            from_date="2026-12-07",
            to_date="2026-12-08",
            actor="ana",
        )
        session.commit()
        before = len(entries_of(session, ana, leave_type=vacation))
        cancel_request(session, pending, actor="ana", reason="plans changed")
        assert pending.state == "cancelled" and pending.cancelled_by == "ana"
        assert len(entries_of(session, ana, leave_type=vacation)) == before
        assert balance(session, ana, leave_type=vacation, on=BALANCE_ON) == Decimal("5.0000")

        revisited = request_leave(
            session,
            ana,
            leave_type=vacation,
            from_date="2026-12-07",
            to_date="2026-12-08",
            actor="ana",
        )
        session.commit()
        decide_request(session, revisited, actor="maria", action=APPROVE, role="line manager")
        assert balance(session, ana, leave_type=vacation, on=BALANCE_ON) == Decimal("3.0000")
        cancel_request(session, revisited, actor="ana", reason="plans changed again")
        assert revisited.state == "cancelled"
        # The days came back as a movement, not as an edit of the movement that took them.
        assert balance(session, ana, leave_type=vacation, on=BALANCE_ON) == Decimal("5.0000")
        adjustments = [
            entry
            for entry in entries_of(session, ana, leave_type=vacation)
            if entry.kind == "adjustment"
        ]
        assert [entry.days for entry in adjustments] == [Decimal("2.0000")], adjustments
        _refuses(
            lambda: cancel_request(session, rejected, actor="ana", reason="too late"),
            NotDecidableError,
            "cancelling a rejected application",
        )
        print(
            "a rejection moved no balance, cancelling a pending application credited nothing,"
            " and cancelling an approved one gave back exactly two days as an adjustment"
        )

        # 5 — approved leave, and what attendance makes of it
        fortnight = request_leave(
            session,
            ana,
            leave_type=vacation,
            from_date="2026-12-14",
            to_date="2026-12-15",
            actor="ana",
        )
        unpaid_day = request_leave(
            session,
            ana,
            leave_type=unpaid,
            from_date="2026-12-16",
            to_date="2026-12-16",
            actor="ana",
            override_actor="hr-head",
            override_reason="no balance: docked pay agreed",
        )
        session.commit()
        decide_request(session, fortnight, actor="maria", action=APPROVE, role="line manager")
        decide_request(session, unpaid_day, actor="maria", action=APPROVE, role="line manager")
        assert balance(session, ana, leave_type=unpaid, on=BALANCE_ON) == Decimal("-1.0000")

        assert leave_on(session, ana, on=date(2026, 12, 14)) is fortnight
        assert leave_on(session, ana, on=date(2026, 12, 13)) is None
        day = attendance_day(session, ana, on=date(2026, 12, 14))
        assert day["leave"] == {
            "request": str(fortnight.id),
            "leave_type": "VACATION",
            "paid": True,
        }, day["leave"]
        assert attendance_day(session, ana, on=date(2026, 12, 13))["leave"] is None
        unpaid_reading = attendance_day(session, ana, on=date(2026, 12, 16))["leave"]
        assert unpaid_reading["paid"] is False and unpaid_reading["leave_type"] == "UNPAID"
        november = leave_in_period(
            session, ana, from_date=date(2026, 11, 1), to_date=date(2026, 11, 30)
        )
        # The same calendar reading the day count was made of: the 2nd, 3rd and 5th, because
        # the 4th is the market holiday and the 6th is this employee's own.
        assert [row["on"] for row in november] == [
            date(2026, 11, day_no) for day_no in (2, 3, 5)
        ], november
        december = leave_in_period(
            session, ana, from_date=date(2026, 12, 1), to_date=date(2026, 12, 31)
        )
        assert [row["on"] for row in december] == [
            date(2026, 12, day_no) for day_no in (14, 15, 16)
        ], december
        assert [row["paid"] for row in december] == [True, True, False], december
        print(
            "the approved days are on the attendance record with the type and whether it is"
            " paid, the days of the cancelled approval are not, and a day beside one is"
            " nothing at all"
        )

        # 6 — a company with no chain, and the refusal leaves nothing behind
        stranger_type = define_leave_type(
            session,
            company_id=other_company_id,
            code="VACATION",
            name="Vacation leave",
            cadence="monthly",
            accrual_days="1.25",
        )
        session.commit()
        _refuses(
            lambda: request_leave(
                session,
                stranger,
                leave_type=stranger_type,
                from_date="2026-12-14",
                to_date="2026-12-15",
                actor="zoe",
            ),
            NoWorkflowConfigured,
            "an application in a company with no leave chain",
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(LeaveRequest)
                .where(LeaveRequest.employee_id == stranger.id)
            )
            == 0
        ), "the refusal left a half-written application behind"

        # The database's own guards on the document: a state nobody defined, an override with
        # one half of itself, and removal — which never happens to a document.
        for statement, expected in (
            ("UPDATE leave_request SET state = 'mumbled' WHERE id = :id", "ck_leave_request_state"),
            (
                "UPDATE leave_request SET override_actor = 'hr' WHERE id = :id",
                "ck_leave_request_override_named",
            ),
        ):
            _refused(
                lambda statement=statement: session.execute(
                    text(statement), {"id": str(week.id)}
                ),
                expected,
            )
            session.rollback()
        _refused(
            lambda: session.execute(
                text("DELETE FROM leave_request WHERE id = :id"), {"id": str(week.id)}
            ),
            "DELETE is refused",
        )
        session.rollback()
        print(
            "no chain is a refusal that writes nothing, and the table itself refuses a state"
            " nobody defined, half an override and removal"
        )

        # Every balance is a sum of the movements that were actually made.
        kinds = [entry.kind for entry in entries_of(session, ana, leave_type=vacation)]
        assert kinds.count("accrual") == 1 and kinds.count("leave_taken") == 4
        assert kinds.count("adjustment") == 1 and len(kinds) == 6, kinds
        assert balance(session, ana, leave_type=vacation, on=BALANCE_ON) == Decimal("3.0000")
        assert len(applications_of(session, ana, state="cancelled")) == 3
        print(
            "20 accrued, 3 + 12 + 2 + 2 taken, 2 given back and one day docked with no"
            " balance: every figure is a sum of named movements, and 6 movements produced it"
        )

    engine.dispose()
    print(
        "ok — one approved, one rejected and one over-balance application, priced, decided"
        " and posted"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
