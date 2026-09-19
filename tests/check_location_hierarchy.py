"""T-1.INV.02 check — the four-level location hierarchy and its rules.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/erpv1 \
        python tests/check_location_hierarchy.py

Green on all six:

1. the four levels nest (warehouse → zone → aisle → bin) and read back as a tree
2. a skipped level is refused: a zone with no parent, a bin under a warehouse,
   a warehouse with a parent
3. the same skip is refused by the database at COMMIT when written straight in
4. a location cannot be moved into its own subtree
5. a parent with live children cannot be retired, by the helper or by raw SQL
6. a code is unique per company

The one rule this check does not cover — a location holding stock cannot be
retired, and a movement against a non-leaf is refused — needs the stock ledger, so
it is proved by `tests/check_stock_ledger.py` in the same batch (T-1.INV.03).

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import os
import sys
import uuid

from sqlalchemy import create_engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.stock import entries  # noqa: E402,F401 — the retirement guard reads the stock ledger
from app.stock.locations import (  # noqa: E402
    LocationError,
    LocationInUseError,
    LocationLevelError,
    create_location,
    location_by_code,
    location_tree,
    move_location,
    retire_location,
)

COMPANY = uuid.uuid4()


def _refused(call, expected: type[Exception]) -> str:
    try:
        call()
    except Exception as exc:  # noqa: BLE001 — the type and message are the point
        assert isinstance(exc, expected), f"refused with {type(exc).__name__}: {exc}"
        return str(exc)
    raise AssertionError("accepted what it must refuse")


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
                code="LOC-CHECK",
                name="Location check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()

        # 1 — the four levels
        warehouse = create_location(
            session, company_id=COMPANY, code="WH1", name="Main", location_type="warehouse"
        )
        zone = create_location(
            session,
            company_id=COMPANY,
            code="WH1-Z1",
            name="Dry",
            location_type="zone",
            parent_id=warehouse.id,
        )
        aisle = create_location(
            session,
            company_id=COMPANY,
            code="WH1-Z1-A1",
            name="Aisle 1",
            location_type="aisle",
            parent_id=zone.id,
        )
        bin_one = create_location(
            session,
            company_id=COMPANY,
            code="WH1-Z1-A1-B1",
            name="Bin 1",
            location_type="bin",
            parent_id=aisle.id,
        )
        session.commit()
        tree = location_tree(session, company_id=COMPANY)
        assert len(tree) == 1 and tree[0]["code"] == "WH1", tree
        depth = tree[0]
        for expected in ("WH1-Z1", "WH1-Z1-A1", "WH1-Z1-A1-B1"):
            depth = depth["children"][0]
            assert depth["code"] == expected, depth
        print("the four levels nest: Warehouse → Zone → Aisle → Bin")

        # 2 — a skipped level is refused, with the level named
        refusal = _refused(
            lambda: create_location(
                session,
                company_id=COMPANY,
                code="WH1-B9",
                name="A bin under the warehouse",
                location_type="bin",
                parent_id=warehouse.id,
            ),
            LocationLevelError,
        )
        session.rollback()
        _refused(
            lambda: create_location(
                session, company_id=COMPANY, code="Z9", name="A zone with no parent",
                location_type="zone",
            ),
            LocationLevelError,
        )
        _refused(
            lambda: create_location(
                session, company_id=COMPANY, code="WH9", name="A warehouse with a parent",
                location_type="warehouse", parent_id=warehouse.id,
            ),
            LocationLevelError,
        )
        session.rollback()
        print(f"a skipped level is refused: {refusal[:58]}…")

        # 3 — and by the database, for a writer that bypasses the helper
        with engine.connect() as connection:
            try:
                connection.exec_driver_sql(
                    "INSERT INTO location (id, company_id, code, name, type, parent_id)"
                    " VALUES (%s, %s, 'WH1-B8', 'Raw bin', 'bin', %s)",
                    (uuid.uuid4(), COMPANY, warehouse.id),
                )
                connection.commit()
            except DBAPIError as exc:
                database_message = str(exc.orig).strip()
            else:
                raise AssertionError("the database accepted a bin under a warehouse")
            connection.rollback()
        assert "cannot stand under" in database_message, database_message
        print(f"the database refuses it too ({database_message[:46]}…)")

        # 4 — no moving a location into its own subtree
        move_location(session, warehouse, parent_id=None)
        session.commit()
        with engine.connect() as connection:
            try:
                connection.exec_driver_sql(
                    "UPDATE location SET parent_id = %s WHERE id = %s",
                    (bin_one.id, warehouse.id),
                )
                connection.commit()
            except DBAPIError as exc:
                cycle_message = str(exc.orig).strip()
            else:
                raise AssertionError("the database accepted a cycle")
            connection.rollback()
        assert "inside its own subtree" in cycle_message, cycle_message
        print(f"a location cannot move into its own subtree ({cycle_message[:40]}…)")

        # 5 — a parent with live children cannot be retired
        _refused(lambda: retire_location(session, zone), LocationInUseError)
        session.rollback()
        with engine.connect() as connection:
            try:
                connection.exec_driver_sql(
                    "UPDATE location SET deleted_at = now() WHERE id = %s", (zone.id,)
                )
                connection.commit()
            except DBAPIError as exc:
                retired_message = str(exc.orig).strip()
            else:
                raise AssertionError("the database retired a parent with live children")
            connection.rollback()
        assert "still has live children" in retired_message, retired_message

        # 6 — a code is unique per company, and a leaf with nothing in it retires
        _refused(
            lambda: create_location(
                session, company_id=COMPANY, code="WH1", name="Duplicate", location_type="warehouse"
            ),
            LocationError,
        )
        session.rollback()
        retire_location(session, bin_one)
        session.commit()
        assert location_by_code(session, company_id=COMPANY, code="WH1-Z1-A1").is_leaf is False
        print("a parent with live children is refused retirement; a free leaf retires")

    engine.dispose()
    print("ok — the hierarchy is four levels deep, and nothing skips one")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
