"""T-0.AUDIT.01 check — history cannot be rewritten and masters are never removed.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/postgres \
        python tests/check_audit_conventions.py

It fails (non-zero exit) if any of these stops holding:

1. an UPDATE and a DELETE against a posted ledger entry are refused **by the
   database** — including a raw SQL write, which is how a hand-edit or a
   restored dump arrives — and the posting is unchanged afterwards
2. a master (the company) cannot be removed: the DELETE is refused by the
   database
3. retiring a master marks it instead: the row is still there in raw SQL, with
   ``deleted_at`` set
4. ordinary reads no longer return it, and only the explicit opt-in does

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

from app.audit import INCLUDE_SOFT_DELETED, soft_delete  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger.posting import post_journal_entry  # noqa: E402

DAY = date(2026, 9, 17)


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
    # The checks share one scratch database, so reset the schema rather than only
    # the tables this file imports: a table another module added keeps a foreign
    # key on `company` and would block the rebuild.
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    Base.metadata.create_all(engine)

    company_id = uuid.uuid4()
    with Session(engine) as session:
        session.add(
            Company(
                id=company_id,
                code="AUDIT-CHECK",
                name="Audit convention check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()
        post_journal_entry(
            session,
            company_id=company_id,
            posting_date=DAY,
            currency="PHP",
            lines=[
                {"account": "1000", "debit": Decimal("100.00")},
                {"account": "4000", "credit": Decimal("100.00")},
            ],
        )
        session.commit()

    # 1 — the ledger refuses to be rewritten, whoever the writer is
    with engine.connect() as conn:
        row = conn.exec_driver_sql("SELECT id FROM journal_entry LIMIT 1").scalar()
        assert row is not None, "the post did not land"
        message = _refused(
            lambda: conn.exec_driver_sql(
                "UPDATE journal_line SET debit = 1 WHERE entry_id = %s", (row,)
            ),
            "append-only",
        )
        conn.rollback()
        print(f"the database refused an edit of a posted line: {message}")
        message = _refused(
            lambda: conn.exec_driver_sql("DELETE FROM journal_entry WHERE id = %s", (row,)),
            "append-only",
        )
        conn.rollback()
        print(f"the database refused a delete of a posted entry: {message}")
        assert conn.exec_driver_sql(
            "SELECT count(*) FROM journal_entry WHERE id = %s", (row,)
        ).scalar() == 1, "the refused write changed the ledger"
        assert conn.exec_driver_sql(
            "SELECT sum(debit) FROM journal_line WHERE entry_id = %s", (row,)
        ).scalar() == Decimal("100.000000"), "the refused write changed the posting"

    # 2 — a master cannot be removed, even through the ORM
    with Session(engine) as session:
        company = session.get(Company, company_id)
        _refused(lambda: (session.delete(company), session.flush()), "is a master")
        session.rollback()

    # 3 — retiring it marks it, and the row survives
    with Session(engine) as session:
        master = session.get(Company, company_id)
        soft_delete(session, master)
        session.commit()
    with engine.connect() as conn:
        stored = conn.exec_driver_sql(
            "SELECT deleted_at FROM company WHERE id = %s", (company_id,)
        ).one_or_none()
        assert stored is not None, "the retired master was removed instead of marked"
        assert stored[0] is not None, "the retired master carries no deleted_at"
        print(f"the master is still there, marked deleted_at {stored[0]:%Y-%m-%d}")

    # 4 — reads hide it unless asked, and only then
    with Session(engine) as session:
        assert session.get(Company, company_id) is None, "a read returned a retired master"
        assert (
            session.scalar(select(func.count()).select_from(Company)) == 0
        ), "a list returned a retired master"
        seen = session.scalar(
            select(func.count())
            .select_from(Company)
            .execution_options(**{INCLUDE_SOFT_DELETED: True})
        )
        assert seen == 1, "the opt-in did not return the retired master"
    print("ordinary reads hide the retired master; the opt-in returns it")

    engine.dispose()
    print("ok — ledgers are append-only and masters retire by marking, in the database")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
