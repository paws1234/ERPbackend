"""T-1.INV.09 check — serial identity, status and where a unit is.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/erpv1 \
        python tests/check_serial_tracking.py

Green on all six:

1. a serial-tracked item refuses a movement with no serial, and refuses a movement
   of more than one unit (a unit is one row)
2. receipt puts the unit in stock at the bin, a transfer moves it to the other bin,
   and an issue takes it out of stock
3. the item's quantity equals its count of distinct serials in stock — the
   property the Phase 1 exit criteria name
4. a duplicate serial within the item is refused, while the same code on another
   item is allowed
5. a serial-tracked item cannot be issued twice (the unit that is gone stays gone)
6. an untracked item is unaffected (no regression to T-1.INV.05)

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.stock.entries import on_hand  # noqa: E402
from app.stock.items import TraceabilityError, create_item  # noqa: E402
from app.stock.locations import create_location  # noqa: E402
from app.stock.serials import (  # noqa: E402
    IN_STOCK,
    ISSUED,
    UNRECEIVED,
    DuplicateSerialError,
    SerialError,
    add_serial,
    serials_in_stock,
)
from app.stock.transactions import (  # noqa: E402
    InsufficientStockError,
    issue,
    receive,
    transfer,
)
from tests.seed import seed_stock_accounts  # noqa: E402

COMPANY = uuid.uuid4()
DAY = date(2026, 9, 17)


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
                code="SERIAL-CHECK",
                name="Serial tracking check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()
        # Every movement here posts to the GL (T-1.INV.07).
        seed_stock_accounts(session, company_id=COMPANY)
        session.commit()
        pump = create_item(
            session, company_id=COMPANY, sku="PUMP", name="Pump", base_uom="each",
            traceability_mode="serial",
        )
        spare = create_item(
            session, company_id=COMPANY, sku="PUMP-SPARE", name="Spare pump", base_uom="each",
            traceability_mode="serial",
        )
        bolt = create_item(
            session, company_id=COMPANY, sku="BOLT", name="Bolt", base_uom="each",
            traceability_mode="none",
        )
        unit_a = add_serial(session, item=pump, code="SN-0001")
        unit_b = add_serial(session, item=pump, code="SN-0002")
        spare_unit = add_serial(session, item=spare, code="SN-SPARE")
        warehouse = create_location(
            session, company_id=COMPANY, code="WH1", name="Main", location_type="warehouse"
        )
        zone = create_location(
            session, company_id=COMPANY, code="WH1-Z", name="Zone", location_type="zone",
            parent_id=warehouse.id,
        )
        aisle = create_location(
            session, company_id=COMPANY, code="WH1-Z-A", name="Aisle", location_type="aisle",
            parent_id=zone.id,
        )
        bin_one = create_location(
            session, company_id=COMPANY, code="B1", name="Bin 1", location_type="bin",
            parent_id=aisle.id,
        )
        bin_two = create_location(
            session, company_id=COMPANY, code="B2", name="Bin 2", location_type="bin",
            parent_id=aisle.id,
        )
        session.commit()

        # 1 — the identity is not optional, and a unit is one unit
        refusal = _refused(
            lambda: receive(
                session, item=pump, location=bin_one, uom="each", quantity=1,
                value=Decimal("500.00"), currency="PHP", source_type="goods_receipt",
                source_id=uuid.uuid4(), posting_date=DAY,
            ),
            TraceabilityError,
        )
        session.rollback()
        _refused(
            lambda: receive(
                session, item=pump, location=bin_one, uom="each", quantity=3,
                value=Decimal("1500.00"), currency="PHP", source_type="goods_receipt",
                source_id=uuid.uuid4(), posting_date=DAY, serial=unit_a,
            ),
            TraceabilityError,
        )
        session.rollback()
        _refused(
            lambda: receive(
                session, item=pump, location=bin_one, uom="each", quantity=1,
                value=Decimal("500.00"), currency="PHP", source_type="goods_receipt",
                source_id=uuid.uuid4(), posting_date=DAY, serial=spare_unit,
            ),
            TraceabilityError,
        )
        session.rollback()
        assert unit_a.status == UNRECEIVED and serials_in_stock(session, item=pump) == [], unit_a
        print(f"a serial-tracked item moves one named unit at a time: {refusal[:48]}…")

        # 2 — in, across, out
        for serial, value in ((unit_a, Decimal("500.00")), (unit_b, Decimal("700.00"))):
            receive(
                session, item=pump, location=bin_one, uom="each", quantity=1,
                value=value, currency="PHP", source_type="goods_receipt",
                source_id=uuid.uuid4(), posting_date=DAY, serial=serial,
            )
        session.commit()
        assert (unit_a.status, unit_a.location_id) == (IN_STOCK, bin_one.id), unit_a
        moved_out, moved_in = transfer(
            session, item=pump, from_location=bin_one, to_location=bin_two, uom="each",
            quantity=1, currency="PHP", source_type="stock_transfer", source_id=uuid.uuid4(),
            posting_date=DAY, serial=unit_a,
        )
        session.commit()
        assert (moved_out.value, moved_in.value) == (
            Decimal("-500.000000"),
            Decimal("500.000000"),
        ), (moved_out.value, moved_in.value)
        assert unit_a.location_id == bin_two.id, unit_a
        _refused(
            lambda: issue(
                session, item=pump, location=bin_one, uom="each", quantity=1, currency="PHP",
                source_type="stock_issue", source_id=uuid.uuid4(), posting_date=DAY, serial=unit_a,
            ),
            InsufficientStockError,
        )
        session.rollback()
        issue(
            session, item=pump, location=bin_two, uom="each", quantity=1, currency="PHP",
            source_type="stock_issue", source_id=uuid.uuid4(), posting_date=DAY, serial=unit_a,
        )
        session.commit()
        assert (unit_a.status, unit_a.location_id) == (ISSUED, None), unit_a
        print("SN-0001 was received at B1, transferred to B2 and issued out of stock")

        # 3 — the quantity is the count of serials in stock
        in_stock = serials_in_stock(session, item=pump)
        held = on_hand(session, company_id=COMPANY, item_id=pump.id)
        assert len(in_stock) == 1 and held["quantity"] == Decimal(1), (in_stock, held)
        assert [serial.code for serial in in_stock] == ["SN-0002"], in_stock
        print(f"{held['quantity']} in stock = {len(in_stock)} distinct serial in stock")

        # 4 — a code repeats only across items
        _refused(lambda: add_serial(session, item=pump, code="SN-0002"), DuplicateSerialError)
        session.rollback()
        other = add_serial(session, item=spare, code="SN-0002")
        session.commit()
        assert other.id != unit_b.id
        print("SN-0002 is refused twice on one item and allowed on another")

        # 5 — the unit that is gone stays gone
        try:
            issue(
                session, item=pump, location=bin_two, uom="each", quantity=1, currency="PHP",
                source_type="stock_issue", source_id=uuid.uuid4(), posting_date=DAY,
                serial=unit_a,
            )
        except Exception as exc:  # noqa: BLE001 — either refusal is the right answer here:
            # the location no longer holds it (InsufficientStockError) and the unit is
            # already out (the state it left behind).
            assert isinstance(exc, (InsufficientStockError, SerialError)), exc
        else:
            raise AssertionError("a unit that is already out was issued again")
        session.rollback()
        _refused(lambda: add_serial(session, item=bolt, code="SN-0003"), SerialError)
        session.rollback()
        print("issuing a unit that is already out is refused")

        # 6 — an untracked item is untouched by any of it
        receive(
            session, item=bolt, location=bin_one, uom="each", quantity=4,
            value=Decimal("40.00"), currency="PHP", source_type="goods_receipt",
            source_id=uuid.uuid4(), posting_date=DAY,
        )
        session.commit()
        assert on_hand(session, company_id=COMPANY, item_id=bolt.id)["quantity"] == Decimal(4)
        print("an untracked item moves exactly as before")

    engine.dispose()
    print("ok — one row per unit, one place at a time, and the quantity is the count")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
