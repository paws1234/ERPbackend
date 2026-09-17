"""T-0.CORE.03 check — two companies cannot see each other, and no table may skip
the company dimension (the platform is multi-company by data isolation, plan §8).

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/erpv1 \
        python tests/check_company_isolation.py

It fails (non-zero exit) if any of these stops holding:

1. a second company is created on the schema already in place — no DDL between
   the two — and each company keeps its own fiscal calendar
2. a table with no company dimension, and no declaration in `app/db.py`, is
   **refused when the schema is built**: that guard is what makes "fails if the
   dimension is missing on a new table" true for every table added after this one
3. the database isolates the rows, not the caller's query: as an ordinary
   application role, a session bound to company A sees A's posting and none of
   B's — entry *and* lines — and neither company can write a row for the other
4. a session bound to no company sees nothing at all

The role matters: superusers and a table's owner bypass row-level security, so
the isolation half runs as a plain role via `SET ROLE`, which is what an
application connection is.

**Scratch database only**: it drops and recreates the schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import Column, Integer, Table, create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.company import Company  # noqa: E402
from app.db import (  # noqa: E402
    COMPANY_SETTING,
    Base,
    UnscopedTableError,
    company_scoping_ddl,
    scope_to_company,
)
from app.ledger.posting import post_journal_entry  # noqa: E402

APP_ROLE = "erp_company_check"
DAY = date(2026, 9, 17)


def _add_company(session: Session, code: str, month: int) -> uuid.UUID:
    """Create one company; returns its id."""
    company = Company(
        code=code,
        name=f"{code} check",
        base_currency="PHP",
        fiscal_year_start_month=month,
    )
    session.add(company)
    session.commit()
    return company.id


def _count(session: Session, table: str) -> int:
    return session.scalar(text(f"SELECT count(*) FROM {table}"))


def _grant_app_role(conn) -> None:
    """Create the application role if it is missing, and give it the tables."""
    conn.exec_driver_sql(
        "DO $$ BEGIN"
        f" IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN"
        f" CREATE ROLE {APP_ROLE} NOLOGIN; END IF; END $$"
    )
    conn.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    conn.exec_driver_sql(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}"
    )
    conn.commit()


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
    Base.metadata.create_all(engine)  # creates the schema *and* its scoping

    # 2 — a table without the company dimension never reaches the database
    with engine.connect() as conn:
        probe = Table(
            "unscoped_probe", Base.metadata, Column("id", Integer, primary_key=True)
        )
        try:
            company_scoping_ddl(Base.metadata, conn)
        except UnscopedTableError as exc:
            print(f"guard refused an unscoped table: {exc}")
        else:
            raise AssertionError("a table with no company dimension was accepted")
        finally:
            Base.metadata.remove(probe)

    # 1 — a second company, on the schema already built, with its own calendar
    with Session(engine) as session:
        alpha_id = _add_company(session, "ALPHA", 1)
        beta_id = _add_company(session, "BETA", 7)
        assert session.get(Company, alpha_id).fiscal_year_start_month == 1
        assert session.get(Company, beta_id).fiscal_year_start_month == 7
    print("two companies on one schema, each with its own fiscal calendar")

    # 3 and 4 — isolation, as an ordinary application role
    conn = engine.connect()
    try:
        _grant_app_role(conn)
        conn.exec_driver_sql(f"SET ROLE {APP_ROLE}")
        conn.commit()

        session = Session(bind=conn)

        # ALPHA posts through the primitive: its own company passes the policy
        scope_to_company(session, alpha_id)
        post_journal_entry(
            session,
            company_id=alpha_id,
            posting_date=DAY,
            currency="PHP",
            lines=[
                {"account": "1000", "debit": Decimal("100.00")},
                {"account": "4000", "credit": Decimal("100.00")},
            ],
        )
        session.commit()

        scope_to_company(session, alpha_id)
        assert _count(session, "journal_entry") == 1, "a company cannot see its own posting"
        assert _count(session, "journal_line") == 2, "a company cannot see its own lines"

        scope_to_company(session, beta_id)
        assert _count(session, "journal_entry") == 0, "another company's posting is visible"
        assert _count(session, "journal_line") == 0, "another company's lines are visible"
        print("alpha's posting is invisible to beta — entry and lines")

        # ... and the write side refuses to file a row under the other company:
        # bound to beta, this posting is for alpha
        message = _refused(
            lambda: session.execute(
                text(
                    "INSERT INTO journal_entry (id, company_id, posting_date, currency)"
                    " VALUES (:id, :company, :day, 'PHP')"
                ),
                {"id": uuid.uuid4(), "company": alpha_id, "day": DAY},
            ),
            "row-level security",
        )
        session.rollback()
        print(f"beta cannot post for alpha: {message}")

        # 4 — no company bound: nothing visible, and no error either
        session.execute(text(f"SELECT set_config('{COMPANY_SETTING}', '', true)"))
        assert _count(session, "journal_entry") == 0, "an unscoped session sees entries"
        assert _count(session, "journal_line") == 0, "an unscoped session sees lines"
        session.rollback()
        session.close()
        print("an unscoped session sees nothing")
    finally:
        conn.exec_driver_sql("RESET ROLE")
        conn.close()
        engine.dispose()

    print("ok — the company dimension is enforced; one company cannot see another's rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
