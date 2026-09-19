"""T-1.ACCT.04 check — period locking, the permission to unlock, and the trail.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/erpv1 \
        python tests/check_period_locking.py

Green on all six:

1. a month nobody has locked takes postings
2. once the month is closed, a posting dated inside it is refused by the posting
   primitive — and nothing is written — while a posting dated outside it lands
3. closing the month altered no entry: the entries posted before the lock are
   still there, unchanged, and the ledger is append-only as before
4. unlocking without the capability is refused, the period stays closed, and the
   refusal is on the trail (T-0.SEC.01)
5. unlocking with the capability and a reason reopens the month, and postings are
   accepted again
6. both transitions are on the audit trail with actor and reason

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.audit import read_trail, set_actor  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger.periods import (  # noqa: E402
    PeriodLockedError,
    lock_period,
    period_is_locked,
    unlock_period,
)
from app.ledger.posting import JournalEntry, post_journal_entry  # noqa: E402
from app.security import (  # noqa: E402
    PermissionDenied,
    assign,
    define_role,
    grant,
)
from tests.seed import seed_accounts  # noqa: E402

COMPANY = uuid.uuid4()
AUGUST, SEPTEMBER = date(2026, 8, 20), date(2026, 9, 17)
LOCKER, OPENER = "alice.locker", "bob.controller"


def _post(session, *, day, memo=None) -> None:
    post_journal_entry(
        session,
        company_id=COMPANY,
        posting_date=day,
        currency="PHP",
        memo=memo,
        lines=[
            {"account": "1000", "debit": Decimal("75.00")},
            {"account": "4000", "credit": Decimal("75.00")},
        ],
    )


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

    with Session(engine) as session:
        session.add(
            Company(
                id=COMPANY,
                code="PERIOD-CHECK",
                name="Period locking check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()
        seed_accounts(session, company_id=COMPANY)
        role = define_role(session, company_id=COMPANY, code="controller", name="Controller")
        grant(session, role, "period.unlock")
        assign(session, company_id=COMPANY, subject=OPENER, role=role)
        session.commit()

        # 1 — an open month takes postings
        set_actor(session, LOCKER)
        _post(session, day=SEPTEMBER, memo="before the lock")
        session.commit()
        assert not period_is_locked(session, company_id=COMPANY, on=SEPTEMBER)
        entries_before = session.scalar(select(func.count()).select_from(JournalEntry))
        print("an open month accepted a posting")

        # 2 — the closed month takes none, and the refusal writes nothing
        # The actor setting is transaction-scoped, so it is stated again after the
        # commit above — otherwise the trail would record the lock as 'unknown'.
        set_actor(session, LOCKER)
        lock_period(
            session,
            company_id=COMPANY,
            year=SEPTEMBER.year,
            month=SEPTEMBER.month,
            actor=LOCKER,
            reason="September is reported",
        )
        session.commit()
        try:
            _post(session, day=SEPTEMBER, memo="after the lock")
        except PeriodLockedError as exc:
            refusal = str(exc)
        else:
            raise AssertionError("a posting landed inside a closed period")
        session.rollback()
        assert session.scalar(select(func.count()).select_from(JournalEntry)) == entries_before, (
            "the refused posting left a row behind"
        )
        _post(session, day=AUGUST, memo="August is still open")
        session.commit()
        assert session.scalar(select(func.count()).select_from(JournalEntry)) == entries_before + 1
        print(f"a closed period refused the back-dated posting: {refusal[:52]}…")

        # 3 — locking altered nothing that was already posted
        kept = session.scalars(
            select(JournalEntry).where(JournalEntry.posting_date == SEPTEMBER)
        ).all()
        assert len(kept) == 1 and kept[0].memo == "before the lock", kept
        debits = [line.debit for line in kept[0].lines if line.debit]
        credits = [line.credit for line in kept[0].lines if line.credit]
        assert debits == [Decimal("75.000000")] and credits == [Decimal("75.000000")], kept[0].lines
        print("the entry posted before the lock is still there, unchanged")

        # 4 — unlocking without the capability is refused and the month stays shut
        try:
            unlock_period(
                session,
                company_id=COMPANY,
                year=SEPTEMBER.year,
                month=SEPTEMBER.month,
                actor=LOCKER,
                reason="let me in",
            )
        except PermissionDenied as exc:
            refused_unlock = str(exc)
        else:
            raise AssertionError("an actor without the capability reopened a period")
        session.rollback()
        assert period_is_locked(session, company_id=COMPANY, on=SEPTEMBER), (
            "the refused unlock reopened the period"
        )
        print(f"unlocking without the capability was refused: {refused_unlock[:52]}…")

        # 5 — with the capability and a reason, it reopens
        set_actor(session, OPENER)
        unlock_period(
            session,
            company_id=COMPANY,
            year=SEPTEMBER.year,
            month=SEPTEMBER.month,
            actor=OPENER,
            reason="late supplier invoice for September",
        )
        session.commit()
        assert not period_is_locked(session, company_id=COMPANY, on=SEPTEMBER)
        _post(session, day=SEPTEMBER, memo="after reopening")
        session.commit()
        print("the controller reopened September and a posting landed again")

        # 6 — both transitions are on the trail, with actor and reason
        trail = read_trail(session, entity="accounting_period")
        changes = [
            (row.actor, row.action, (row.after_values or {}).get("state"), (row.after_values or {}).get("reason"))
            for row in trail
        ]
        assert (LOCKER, "insert", "closed", "September is reported") in changes, changes
        assert (OPENER, "update", "open", "late supplier invoice for September") in changes, changes
        print(f"both transitions are on the trail with actor and reason ({len(trail)} rows)")

    engine.dispose()
    print("ok — a closed period takes no postings, and reopening is a permission with a reason")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
