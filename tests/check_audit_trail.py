"""T-0.AUDIT.02 check — every change is recorded, and the record cannot be edited.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/postgres \
        python tests/check_audit_trail.py

It fails (non-zero exit) if any of these stops holding:

1. creating a master, editing it and retiring it each leave a trail row — with
   the actor, the timestamp and the changed values (before *and* after), and the
   retirement recorded as a retirement rather than as a nameless update
2. a posted transaction's trail row names the document it came from
3. the trail is written even when the session is an ordinary application role
   bound to one company — the trail is not a thing a caller can switch off
4. the trail cannot be edited: an UPDATE or DELETE against it is refused by the
   database, so no API built on this schema can rewrite history

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

from app.audit import (  # noqa: E402
    UNKNOWN_ACTOR,
    read_trail,
    set_actor,
    set_origin,
    soft_delete,
)
from app.company import Company  # noqa: E402
from app.db import Base, scope_to_company  # noqa: E402
from app.ledger.posting import post_journal_entry  # noqa: E402
from tests.seed import seed_accounts  # noqa: E402

APP_ROLE = "erp_audit_check"
DAY = date(2026, 9, 17)
ACTOR = "alice.auditor"
ORIGIN = ("supplier_invoice", "SI-2026-0001")
ORIGINAL_NAME = "Audit trail check"
RENAMED = "Audit trail check (renamed)"


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


def _one(rows, **match):
    found = [row for row in rows if all(getattr(row, key) == value for key, value in match.items())]
    assert len(found) == 1, f"expected exactly one {match}, got {len(found)}"
    return found[0]


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

    # The company is created with nobody stating who did it: the trail says so
    # rather than leaving the row unattributed.
    alpha_id = uuid.uuid4()
    with Session(engine) as session:
        session.add(
            Company(
                id=alpha_id,
                code="AUDIT-TRAIL",
                name=ORIGINAL_NAME,
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()
        # The posting below states account codes, which must exist (T-1.ACCT.01).
        seed_accounts(session, company_id=alpha_id)
        session.commit()

    # Everything after this happens as an ordinary application role bound to one
    # company — the way a request arrives.
    conn = engine.connect()
    try:
        _grant_app_role(conn)
        conn.exec_driver_sql(f"SET ROLE {APP_ROLE}")
        conn.commit()
        session = Session(bind=conn)

        # Everything after this happens as an ordinary application role bound to
        # one company — the way a request arrives. Both statements are
        # transaction-scoped (app.db, app.audit), so every unit of work states
        # whose company it is and who is acting.
        def begin() -> None:
            scope_to_company(session, alpha_id)
            set_actor(session, ACTOR)

        begin()
        set_origin(session, *ORIGIN)
        entry = post_journal_entry(
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

        begin()
        company = session.get(Company, alpha_id)
        company.name = RENAMED
        session.commit()

        begin()
        soft_delete(session, session.get(Company, alpha_id))
        session.commit()

        # 1 and 2 — read it back
        begin()
        trail = read_trail(session)
        created = _one(trail, entity="company", action="insert")
        assert created.actor == UNKNOWN_ACTOR, "an unstated actor was invented"
        assert created.occurred_at is not None, "the record has no timestamp"
        assert created.before_values is None and created.after_values["code"] == "AUDIT-TRAIL"
        print(f"master creation recorded by {created.actor!r} at {created.occurred_at:%H:%M:%S}")

        edited = _one(trail, entity="company", action="update")
        assert edited.actor == ACTOR, "the edit is not attributable"
        assert edited.before_values["name"] == ORIGINAL_NAME, "the change is missing its 'before'"
        assert edited.after_values["name"] == RENAMED, "the change is missing its 'after'"
        print(f"master edit recorded with before/after values, by {edited.actor}")

        retired = _one(trail, entity="company", action="soft_delete")
        assert retired.actor == ACTOR, "the retirement is not attributable"
        assert retired.before_values["deleted_at"] is None
        assert retired.after_values["deleted_at"] is not None, "the retirement is not named as one"
        print("master retirement recorded as a soft_delete, not as a nameless update")

        posted = _one(trail, entity="journal_entry", entity_id=str(entry.id))
        assert (posted.origin_type, posted.origin_id) == ORIGIN, "the posting is not traceable"
        assert posted.actor == ACTOR and posted.after_values["currency"] == "PHP"
        print(f"posting traceable to its document: {posted.origin_type} {posted.origin_id}")
        session.close()
    finally:
        # The role switch is session state on a *pooled* connection: reset it and
        # commit the reset, or the next connection hands the application role on
        # — where the trail's row-level security would hide the very rows this
        # check is about to try to rewrite.
        conn.exec_driver_sql("RESET ROLE")
        conn.commit()
        conn.close()

    # 4 — the trail refuses to be rewritten, whoever the writer is
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT count(*) FROM audit_log").scalar() > 0, (
            "the trail is empty, so refusing to rewrite it would prove nothing"
        )
        for statement in (
            "UPDATE audit_log SET actor = 'somebody.else'",
            "DELETE FROM audit_log",
        ):
            try:
                result = conn.exec_driver_sql(statement)
                conn.commit()
            except DBAPIError as exc:
                message = str(exc.orig).strip()
                assert "append-only" in message, f"unclear database error: {message}"
                conn.rollback()
            else:
                raise AssertionError(
                    f"the trail accepted {statement!r} ({result.rowcount} rows matched)"
                )
        print("the trail refuses UPDATE and DELETE: append-only, like the ledger")

    engine.dispose()
    print("ok — every master and transaction change is recorded and the record is immutable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
