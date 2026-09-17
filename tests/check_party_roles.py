"""T-0.PARTY.01 check — one identity, several roles, no role-specific fields.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/postgres \
        python tests/check_party_roles.py

It fails (non-zero exit) if any of these stops holding:

1. one party holds several roles with **one** master row — the same counterparty
   is a supplier *and* a customer without being recorded twice
2. the base record carries nothing about a role: its column set is exactly the
   shared identity, so a role's own attributes live with the role (Phase 2/3/5)
   and cannot creep onto the shared row
3. a party without a role is refused — by the caller's guard *and* by the
   database, so a writer that bypasses `create_party` cannot leave one behind
4. a role name nobody has heard of is refused, and a repeated role is stored once
5. a party is retired by marking it: the row survives, and the database refuses
   to delete it

**Scratch database only**: it drops and recreates the schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from decimal import Decimal

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.audit import soft_delete  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger import posting  # noqa: E402,F401 — every check builds the one schema
from app.party import ROLES, Party, PartyRole, UnknownRoleError, create_party  # noqa: E402

# The whole shared identity, and nothing that belongs to a role.
SHARED_COLUMNS = {"id", "company_id", "code", "name", "tax_id", "deleted_at"}


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
                code="PARTY-CHECK",
                name="Party check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()

        # 2 — the shared record holds the shared identity only
        assert set(Party.__table__.columns.keys()) == SHARED_COLUMNS, (
            "the party master grew a field that belongs to a role:"
            f" {sorted(set(Party.__table__.columns.keys()) - SHARED_COLUMNS)}"
        )
        print("the party master carries the shared identity only:", sorted(SHARED_COLUMNS))

        # 1 — one identity, two roles, one master row
        acme = create_party(
            session,
            company_id=company_id,
            code="ACME",
            name="Acme Trading",
            roles=["customer", "supplier"],
        )
        session.commit()
        assert acme.has_role("customer") and acme.has_role("supplier")
        assert session.scalar(select(func.count()).select_from(Party)) == 1, (
            "a second master row was needed to hold the second role"
        )
        assert session.scalar(select(func.count()).select_from(PartyRole)) == 2
        print("ACME is one row holding customer + supplier")

        # 4 — a repeat is one role; a role nobody knows is refused
        repeated = create_party(
            session,
            company_id=company_id,
            code="REPEAT",
            name="Repeat Holdings",
            roles=["supplier", "supplier"],
        )
        session.commit()
        assert [held.role for held in repeated.roles] == ["supplier"]
        for bad in ([], ["vendor"], ["customer", "vendor"]):
            try:
                create_party(
                    session,
                    company_id=company_id,
                    code="BAD",
                    name="Bad role",
                    roles=bad,
                )
            except UnknownRoleError as exc:
                assert "role" in str(exc), f"unclear error: {exc}"
                session.rollback()
            else:
                raise AssertionError(f"a party was created with roles {bad!r}")
        print("a repeated role is stored once; an unknown role and no role are refused")

        # 3 — and the database refuses a roleless party whoever wrote it
        message = _refused(
            lambda: (
                session.execute(
                    text(
                        "INSERT INTO party (id, company_id, code, name)"
                        " VALUES (:id, :company, 'ROLELESS', 'Roleless Ltd')"
                    ),
                    {"id": uuid.uuid4(), "company": company_id},
                ),
                session.commit(),
            ),
            "holds no role",
        )
        session.rollback()
        print(f"the database refused a roleless party: {message}")

        # 5 — retirement marks, it does not remove
        soft_delete(session, session.get(Party, acme.id))
        session.commit()
        stored = session.execute(
            text("SELECT deleted_at FROM party WHERE id = :id"), {"id": acme.id}
        ).one_or_none()
        assert stored is not None and stored[0] is not None, "the party was removed, not marked"
        print("a party retires by marking; the database refuses to delete it")

    # A fresh session, so no answer comes from the identity map: this is what a
    # later request sees.
    with Session(engine) as session:
        assert session.get(Party, acme.id) is None, "a retired party is still readable"
        _refused(
            lambda: (
                session.execute(text("DELETE FROM party WHERE id = :id"), {"id": acme.id}),
                session.commit(),
            ),
            "is a master",
        )
        session.rollback()

    # The roles are the three §5 names, spelled once for the whole platform.
    assert ROLES == ("customer", "supplier", "employee")

    engine.dispose()
    print("ok — one party identity holds every role it has, and no role's data on the base row")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
