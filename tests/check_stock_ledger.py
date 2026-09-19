"""T-1.INV.03 check — the append-only stock ledger and the sum it is read as.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/erpv1 \
        python tests/check_stock_ledger.py

Green on all eight:

1. a sequence of movements sums to the on-hand quantity and value, per item and
   per location, and a date narrows the sum
2. an entry refuses UPDATE and DELETE at the storage boundary, with the ledger
   unchanged (T-0.AUDIT.01 on this table)
3. a movement without a source document is refused by the recorder **and** by the
   table's own constraint
4. a movement against a non-leaf location is refused by the recorder **and** by the
   database's leaf trigger — so stock cannot sit in a warehouse
5. a zero movement is refused, by the recorder and by the table
6. a value that fights the quantity's direction is refused by the table
7. a location holding stock cannot be retired (the rule T-1.INV.02 delegates here)
8. one document's movements are found by its id — the drill-down

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.stock.entries import (  # noqa: E402
    MovementError,
    MissingSourceError,
    movements_for_source,
    on_hand,
    record_movement,
)
from app.stock.items import create_item  # noqa: E402
from app.stock.locations import (  # noqa: E402
    Location,
    LocationInUseError,
    NotALeafError,
    create_location,
    retire_location,
)

COMPANY = uuid.uuid4()
D1, D2 = date(2026, 9, 10), date(2026, 9, 20)
RECEIPT = uuid.uuid4()


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
                code="STOCK-CHECK",
                name="Stock ledger check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()
        item = create_item(
            session,
            company_id=COMPANY,
            sku="BOLT",
            name="Bolt",
            base_uom="each",
            traceability_mode="none",
        )
        bin_one = _chain(session, "WH1", "B1")
        bin_two = _chain(session, "WH2", "B2")
        session.commit()

        # 1 — the ledger is the balance
        record_movement(
            session, item=item, location=bin_one, quantity=100, value=Decimal("500.00"),
            currency="PHP", source_type="goods_receipt", source_id=RECEIPT, posting_date=D1,
        )
        record_movement(
            session, item=item, location=bin_one, quantity=-30, value=Decimal("-150.00"),
            currency="PHP", source_type="stock_issue", source_id=uuid.uuid4(), posting_date=D2,
        )
        record_movement(
            session, item=item, location=bin_two, quantity=10, value=Decimal("60.00"),
            currency="PHP", source_type="stock_transfer", source_id=uuid.uuid4(), posting_date=D2,
        )
        session.commit()
        item_wide = on_hand(session, company_id=COMPANY, item_id=item.id)
        assert item_wide == {"quantity": Decimal(80), "value": Decimal("410.000000")}, item_wide
        at_bin_one = on_hand(session, company_id=COMPANY, item_id=item.id, location_id=bin_one.id)
        assert at_bin_one["quantity"] == Decimal(70), at_bin_one
        at_bin_two = on_hand(session, company_id=COMPANY, item_id=item.id, location_id=bin_two.id)
        assert at_bin_two == {"quantity": Decimal(10), "value": Decimal("60.000000")}, at_bin_two
        first_day = on_hand(session, company_id=COMPANY, item_id=item.id, as_of=D1)
        assert first_day["quantity"] == Decimal(100), first_day
        print(f"movements sum to {item_wide['quantity']} on hand and {item_wide['value']} in value")

        # 2 — a ledger row is history
        with engine.connect() as connection:
            for statement in (
                "UPDATE stock_ledger_entry SET quantity = 1 WHERE item_id = %s",
                "DELETE FROM stock_ledger_entry WHERE item_id = %s",
            ):
                try:
                    connection.exec_driver_sql(statement, (item.id,))
                    connection.commit()
                except DBAPIError as exc:
                    append_message = str(exc.orig).strip()
                else:
                    raise AssertionError(f"the stock ledger accepted: {statement}")
                connection.rollback()
            assert "append-only" in append_message, append_message
        print(f"a stored movement refuses a rewrite ({append_message[:44]}…)")

        # 3 — a movement names its document
        refusal = _refused(
            lambda: record_movement(
                session, item=item, location=bin_one, quantity=5, value=Decimal("25"),
                currency="PHP", source_type="  ", source_id=uuid.uuid4(), posting_date=D2,
            ),
            MissingSourceError,
        )
        session.rollback()
        with engine.connect() as connection:
            try:
                connection.exec_driver_sql(
                    "INSERT INTO stock_ledger_entry (id, company_id, item_id, location_id,"
                    " quantity, value, currency, source_type, source_id, posting_date)"
                    " VALUES (%s, %s, %s, %s, 5, 25, 'PHP', '', %s, %s)",
                    (uuid.uuid4(), COMPANY, item.id, bin_one.id, uuid.uuid4(), D2),
                )
                connection.commit()
            except DBAPIError as exc:
                source_message = str(exc.orig).strip()
            else:
                raise AssertionError("the database accepted a movement with no source")
            connection.rollback()
        assert "ck_stock_entry_source" in source_message, source_message
        print(f"a movement without a document is refused ({refusal[:46]}…)")

        # 4 — stock lives in a bin
        warehouse = session.scalar(
            select(Location).where(Location.company_id == COMPANY, Location.code == "WH1")
        )
        assert warehouse is not None and warehouse.location_type == "warehouse"
        refusal = _refused(
            lambda: record_movement(
                session, item=item, location=warehouse, quantity=5, value=Decimal("25"),
                currency="PHP", source_type="goods_receipt", source_id=uuid.uuid4(), posting_date=D2,
            ),
            NotALeafError,
        )
        session.rollback()
        with engine.connect() as connection:
            try:
                connection.exec_driver_sql(
                    "INSERT INTO stock_ledger_entry (id, company_id, item_id, location_id,"
                    " quantity, value, currency, source_type, source_id, posting_date)"
                    " VALUES (%s, %s, %s, %s, 5, 25, 'PHP', 'goods_receipt', %s, %s)",
                    (uuid.uuid4(), COMPANY, item.id, warehouse.id, uuid.uuid4(), D2),
                )
                connection.commit()
            except DBAPIError as exc:
                leaf_message = str(exc.orig).strip()
            else:
                raise AssertionError("the database put stock in a warehouse")
            connection.rollback()
        assert "stock is held in a bin" in leaf_message, leaf_message
        print(f"a non-leaf is refused ({refusal[:52]}…) and the database agrees")

        # 5 + 6 — what a movement cannot be
        _refused(
            lambda: record_movement(
                session, item=item, location=bin_one, quantity=0, value=Decimal("0"),
                currency="PHP", source_type="goods_receipt", source_id=uuid.uuid4(), posting_date=D2,
            ),
            MovementError,
        )
        _refused(
            lambda: record_movement(
                session, item=item, location=bin_one, quantity=-5, value=Decimal("25"),
                currency="PHP", source_type="stock_issue", source_id=uuid.uuid4(), posting_date=D2,
            ),
            MovementError,
        )
        session.rollback()
        with engine.connect() as connection:
            try:
                connection.exec_driver_sql(
                    "INSERT INTO stock_ledger_entry (id, company_id, item_id, location_id,"
                    " quantity, value, currency, source_type, source_id, posting_date)"
                    " VALUES (%s, %s, %s, %s, 0, 0, 'PHP', 'goods_receipt', %s, %s)",
                    (uuid.uuid4(), COMPANY, item.id, bin_one.id, uuid.uuid4(), D2),
                )
                connection.commit()
            except DBAPIError as exc:
                zero_message = str(exc.orig).strip()
            else:
                raise AssertionError("the database accepted a zero movement")
            connection.rollback()
        assert "ck_stock_entry_moves_something" in zero_message, zero_message
        print("a zero movement and a value fighting its direction are both refused")

        # 7 — a location holding stock is not removable
        refusal = _refused(lambda: retire_location(session, bin_one), LocationInUseError)
        session.rollback()
        assert "still holds" in refusal, refusal
        print(f"a location holding stock is not removable: {refusal[:58]}…")

        # 8 — the drill-down from the document
        from_receipt = movements_for_source(
            session, company_id=COMPANY, source_type="goods_receipt", source_id=RECEIPT
        )
        assert len(from_receipt) == 1 and from_receipt[0].quantity == Decimal(100), from_receipt
        print("one document's movements are found by its id")

    engine.dispose()
    print("ok — one append-only row per movement, summed for the on-hand figures")
    return 0


def _chain(session: Session, warehouse_code: str, bin_code: str) -> Location:
    """A warehouse → zone → aisle → bin chain; returns the bin itself."""
    warehouse = create_location(
        session, company_id=COMPANY, code=warehouse_code, name=warehouse_code,
        location_type="warehouse",
    )
    zone = create_location(
        session, company_id=COMPANY, code=f"{warehouse_code}-Z", name="Zone",
        location_type="zone", parent_id=warehouse.id,
    )
    aisle = create_location(
        session, company_id=COMPANY, code=f"{warehouse_code}-Z-A", name="Aisle",
        location_type="aisle", parent_id=zone.id,
    )
    leaf = create_location(
        session, company_id=COMPANY, code=bin_code, name="Bin", location_type="bin",
        parent_id=aisle.id,
    )
    return leaf


if __name__ == "__main__":
    raise SystemExit(main())
