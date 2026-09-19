"""T-1.INV.01 check — item identity: variants, barcodes and exact UOM conversion.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/erpv1 \
        python tests/check_item_master.py

Green on all seven:

1. an item states how it is tracked, and a mode outside the three is refused
2. two variants are one item with two SKUs, keyed by their attribute combination —
   a repeated combination is refused and a variant with no attributes is refused
3. one item carries both an EAN and a QR code, and a scan resolves to it
4. a duplicate barcode value is refused by the helper **and by the database**, so a
   code can never resolve to two things
5. a two-step conversion (`base → box → pallet`) round-trips exactly at the
   quantity scale
6. `from = to`, a duplicate direction and a non-positive factor are refused where
   the conversion is declared — and the table's own check refuses the factor too
7. a pair with no declared path is refused rather than treated as 1:1, and an item
   is retired by marking (its row stays, `DELETE` is refused)

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.audit import soft_delete  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.stock.items import (  # noqa: E402
    ConversionError,
    DuplicateItemError,
    Item,
    TraceabilityError,
    add_barcode,
    add_uom_conversion,
    add_variant,
    convert_quantity,
    create_item,
    item_from_barcode,
    rename_item,
)

COMPANY = uuid.uuid4()
OTHER = uuid.uuid4()


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
        for code, name in (("INV-CHECK", "Item master check"), ("OTHER", "Another company")):
            session.add(
                Company(
                    id=COMPANY if code == "INV-CHECK" else OTHER,
                    code=code,
                    name=name,
                    base_currency="PHP",
                    fiscal_year_start_month=1,
                )
            )
        session.commit()

        # 1 — the mode is stated, never defaulted
        refusal = _refused(
            lambda: create_item(
                session,
                company_id=COMPANY,
                sku="WIDGET",
                name="Widget",
                base_uom="each",
                traceability_mode="sometimes",
            ),
            TraceabilityError,
        )
        session.rollback()
        shirt = create_item(
            session,
            company_id=COMPANY,
            sku="TEE",
            name="T-shirt",
            base_uom="each",
            traceability_mode="none",
        )
        batch_item = create_item(
            session,
            company_id=COMPANY,
            sku="MILK",
            name="Milk",
            base_uom="each",
            traceability_mode="batch_lot",
        )
        session.commit()
        assert (shirt.base_uom, shirt.traceability_mode) == ("each", "none")
        assert batch_item.is_tracked
        print(f"items state how they are tracked ({refusal[:40]}… refused)")

        # 2 — a variant matrix on one item
        small = add_variant(session, shirt, sku="TEE-S", attributes={"size": "S"})
        large = add_variant(session, shirt, sku="TEE-L", attributes={"size": "L"})
        session.commit()
        assert {small.sku, large.sku} == {"TEE-S", "TEE-L"}
        _refused(
            lambda: add_variant(session, shirt, sku="TEE-M", attributes={"size": "S"}),
            TraceabilityError,
        )
        session.rollback()
        _refused(
            lambda: add_variant(session, shirt, sku="TEE-X", attributes={}),
            TraceabilityError,
        )
        session.rollback()
        print("two variants are one item with two SKUs; a repeated combination is refused")

        # 3 + 4 — codes, unique across the installation
        add_barcode(session, shirt, value="0123456789012", symbology="EAN", variant=small)
        add_barcode(session, shirt, value="QR-TEE-L", symbology="qr", variant=large)
        session.commit()
        item, variant = item_from_barcode(session, company_id=COMPANY, value="QR-TEE-L")
        assert item.id == shirt.id and variant is not None and variant.sku == "TEE-L"
        duplicate = _refused(
            lambda: add_barcode(session, batch_item, value="0123456789012", symbology="upc"),
            DuplicateItemError,
        )
        session.rollback()
        with engine.connect() as connection:
            try:
                connection.exec_driver_sql(
                    "INSERT INTO item_barcode (id, company_id, item_id, value, symbology)"
                    " VALUES (%s, %s, %s, '0123456789012', 'ean')",
                    (uuid.uuid4(), OTHER, batch_item.id),
                )
                connection.commit()
            except DBAPIError as exc:
                database_message = str(exc.orig).strip()
            else:
                raise AssertionError("the database accepted a duplicate barcode")
            connection.rollback()
        assert "uq_item_barcode_value" in database_message, database_message
        print(f"a scan resolves to one item; a duplicate is refused ({duplicate[:40]}…)")

        # 5 — a two-step chain, exact both ways
        add_uom_conversion(session, shirt, from_uom="box", to_uom="each", factor=12)
        add_uom_conversion(session, shirt, from_uom="pallet", to_uom="box", factor=100)
        session.commit()
        loose = convert_quantity(session, shirt, quantity=Decimal("2"), from_uom="pallet", to_uom="each")
        assert loose == Decimal("2400.000000"), loose
        back = convert_quantity(session, shirt, quantity=loose, from_uom="each", to_uom="pallet")
        assert back == Decimal("2.000000"), back
        boxes = convert_quantity(session, shirt, quantity=Decimal("1"), from_uom="pallet", to_uom="box")
        assert boxes == Decimal("100.000000"), boxes
        print(f"2 pallets = {loose} each and back to {back}; 1 pallet = {boxes} box")

        # 6 — a factor that is not a conversion is refused where it is declared
        _refused(
            lambda: add_uom_conversion(
                session, shirt, from_uom="box", to_uom="box", factor=1
            ),
            ConversionError,
        )
        _refused(
            lambda: add_uom_conversion(session, shirt, from_uom="box", to_uom="each", factor=1),
            ConversionError,
        )
        _refused(
            lambda: add_uom_conversion(session, shirt, from_uom="each", to_uom="box", factor=0),
            ConversionError,
        )
        refusal = _refused(
            lambda: add_uom_conversion(
                session, shirt, from_uom="each", to_uom="box", factor=-2
            ),
            ConversionError,
        )
        session.rollback()
        with engine.connect() as connection:
            try:
                connection.exec_driver_sql(
                    "INSERT INTO uom_conversion (id, company_id, item_id, from_uom, to_uom,"
                    " factor) VALUES (%s, %s, %s, 'case', 'each', 0)",
                    (uuid.uuid4(), COMPANY, shirt.id),
                )
                connection.commit()
            except DBAPIError as exc:
                factor_message = str(exc.orig).strip()
            else:
                raise AssertionError("the database accepted a zero factor")
            connection.rollback()
        assert "ck_uom_conversion_positive" in factor_message, factor_message
        print(f"a non-positive factor is refused: {refusal[:52]}…")

        # 7 — no path, no conversion; and the item retires by marking
        no_path = _refused(
            lambda: convert_quantity(
                session, shirt, quantity=Decimal("1"), from_uom="case", to_uom="each"
            ),
            ConversionError,
        )
        session.rollback()
        rename_item(session, shirt, name="T-shirt (renamed)")
        session.commit()
        soft_delete(session, shirt)
        session.commit()
        # A fresh session for the read: the retired row is still in this one's
        # identity map, and `Session.get` would answer from there.
        with Session(engine) as fresh:
            assert fresh.get(Item, shirt.id) is None, "a retired item is still read"
        with engine.connect() as connection:
            still = connection.exec_driver_sql(
                "SELECT count(*) FROM item WHERE id = %s", (shirt.id,)
            ).scalar()
            assert still == 1, "the retired item's row is gone"
            try:
                connection.exec_driver_sql("DELETE FROM item WHERE id = %s", (shirt.id,))
                connection.commit()
            except DBAPIError as exc:
                delete_message = str(exc.orig).strip()
            else:
                raise AssertionError("the database deleted a master")
            connection.rollback()
        assert "is a master" in delete_message, delete_message
        print(f"an undeclared path is refused ({no_path[:40]}…); the item retired by marking")

    engine.dispose()
    print("ok — item identity is one row per thing, with exact conversions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
