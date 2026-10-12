"""T-8.MIG.02 check — a schema the install's migration tree built scopes companies, and a
second install over it is refused and writes nothing.

    DATABASE_URL=postgresql+psycopg://… python tests/check_migrated_scoping.py

T-8.MIG.01's check compares the migrated schema with a `create_all` one table by table, policies
included. This one drives the *behaviour* those policies are for, on a database nothing but the
install command built. A `create_all` schema gets its scoping from `app/db.py`'s `after_create`
hook; the migrated one has to arrive by the same route (`migrations/env.py` replays the same
listeners) or a deployment and a check disagree about who can see what — and the disagreement
would show up as one company reading another's ledger.

The install is run the way an operator runs it (`python -m app.bootstrap`, in its own process,
against an empty schema), so the schema under test is the schema a deployment gets and the rows
are the rows an operator creates.

It fails (non-zero exit) if any of these stops holding:

1. the install leaves a **migrated** schema — stamped by Alembic and carrying the queue's own
   tables, neither of which a `create_all` schema has
2. that schema scopes companies: one company cannot see the other's posting, cannot write a row
   filed under the other, and an unscoped session sees nothing. Driven as an ordinary
   application role, because a superuser bypasses row-level security
3. the install runs **on top of** the migration tree — a second run is refused by name and
   writes nothing at all

**Scratch database only**: it drops and recreates the schema, as the other checks do.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.company import Company  # noqa: E402
from app.db import COMPANY_SETTING, scope_to_company  # noqa: E402
from app.ledger.posting import post_journal_entry  # noqa: E402
from tests.seed import seed_accounts  # noqa: E402

MARKET = "philippines"
DAY = date(2026, 9, 17)
INSTALLED = "MIGRATED-INSTALL"
OTHER = "MIGRATED-OTHER"
APP_ROLE = "erp_migrated_check"


def install(url: str, code: str, company_name: str) -> subprocess.CompletedProcess:
    """The operator's own command, in its own process, against `url`."""
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env["DATABASE_URL"] = url
    return subprocess.run(
        [
            sys.executable, "-m", "app.bootstrap",
            "--company-code", code,
            "--company-name", company_name,
            "--market", MARKET,
            "--fiscal-year-start-month", "1",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def counts(engine) -> tuple[int, int, int]:
    """Companies, accounts and roles as the database stands — what a refusal must not change."""
    with engine.connect() as connection:
        companies = connection.exec_driver_sql("SELECT count(*) FROM company").scalar()
        accounts = connection.exec_driver_sql("SELECT count(*) FROM account").scalar()
        roles = connection.exec_driver_sql("SELECT count(*) FROM role").scalar()
    return companies, accounts, roles


def _count(session: Session, table: str) -> int:
    return session.scalar(text(f"SELECT count(*) FROM {table}"))


def _grant_app_role(connection) -> None:
    """Create the application role if it is missing, and give it the tables.

    A superuser or a table's owner bypasses row-level security, so the isolation half of this
    check has to run as a plain role — which is what an application connection is.
    """
    connection.exec_driver_sql(
        "DO $$ BEGIN"
        f" IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN"
        f" CREATE ROLE {APP_ROLE} NOLOGIN; END IF; END $$"
    )
    connection.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    connection.exec_driver_sql(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}"
    )
    connection.commit()


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

    # 1 and 3 — the operator's command, on an empty schema, in its own process
    first = install(url, INSTALLED, "Migrated install")
    assert first.returncode == 0, (
        f"the install failed on an empty schema:\n{first.stdout}{first.stderr}"
    )
    with engine.connect() as connection:
        stamp = connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
        queued = connection.exec_driver_sql(
            "SELECT to_regclass('pgqueuer') IS NOT NULL"
        ).scalar()
    assert stamp, "the install left no migration stamp, so the schema was not built by the tree"
    assert queued, "the install left no queue schema, which the migration environment installs"
    print(f"the install built a migrated schema (stamped {stamp}, queue schema installed)")

    with Session(engine) as session:
        alpha = session.scalar(select(Company).where(Company.code == INSTALLED))
        assert alpha is not None, "the install left no company with the code it was given"
        beta = Company(
            code=OTHER, name="Migrated other", base_currency="PHP", fiscal_year_start_month=7
        )
        session.add(beta)
        session.commit()
        seed_accounts(session, company_id=beta.id)
        session.commit()
        alpha_id, beta_id = alpha.id, beta.id
    print("a second company on the migrated schema, with its own chart")

    # 2 — isolation, driven as an ordinary application role
    connection = engine.connect()
    try:
        _grant_app_role(connection)
        connection.exec_driver_sql(f"SET ROLE {APP_ROLE}")
        connection.commit()
        session = Session(bind=connection)

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
        print("the installed company's posting is invisible to the other — entry and lines")

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
        print(f"the other company cannot post for it: {message}")

        session.execute(text(f"SELECT set_config('{COMPANY_SETTING}', '', true)"))
        assert _count(session, "journal_entry") == 0, "an unscoped session sees entries"
        assert _count(session, "journal_line") == 0, "an unscoped session sees lines"
        session.rollback()
        session.close()
        print("an unscoped session sees nothing")
    finally:
        connection.exec_driver_sql("RESET ROLE")
        connection.close()

    # 3 — the same command again: the migration half is a no-op, the install half is refused
    before = counts(engine)
    second = install(url, INSTALLED, "Second attempt")
    assert second.returncode != 0, (
        f"a second install of the same company was allowed:\n{second.stdout}{second.stderr}"
    )
    assert INSTALLED in second.stderr, (
        f"the refusal does not name the company code, so it is not the install refusing:"
        f"\n{second.stderr}"
    )
    assert counts(engine) == before, (
        f"the refused install wrote something anyway: {before} → {counts(engine)}"
    )
    with engine.connect() as connection:
        after = connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
    assert after == stamp, f"the second run moved the migration stamp: {stamp} → {after}"
    print(
        f"a second install over the migrated schema is refused by name ({second.stderr.strip()[:60]}…)"
        " and wrote nothing"
    )

    engine.dispose()
    print("ok — the install's migration tree leaves a schema that scopes companies, on its own")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
