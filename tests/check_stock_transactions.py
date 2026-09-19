"""T-1.INV.05 check — receipt, issue and transfer.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/erpv1 \
        python tests/check_stock_transactions.py

Green on all seven:

1. a receipt increases the quantity and the value at the target location, and it
   states its quantity in whatever UOM arrived — 2 boxes become 24 each
2. an issue decreases both, valued by the valuation engine rather than by the
   caller
3. an issue larger than the location holds is refused, and leaves nothing behind
   (negative stock is never allowed, and the transaction is atomic)
4. a transfer moves the quantity **and** the value between two bins, with the
   item's totals unchanged
5. a transfer out of a location that does not hold the stock is refused
6. a transfer to the location it came from is refused
7. every movement the three write names the document that caused it, and the
   ledger-integrity gate is green over what they wrote

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

from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.stock.entries import StockLedgerEntry, movements_for_source, on_hand  # noqa: E402
from app.stock.items import TraceabilityError, add_uom_conversion, add_variant, create_item  # noqa: E402
from app.stock.locations import create_location  # noqa: E402
from app.stock.transactions import (  # noqa: E402
    InsufficientStockError,
    TransactionError,
    issue,
    receive,
    transfer,
)
from tests.check_ledger_integrity import ledger_gate  # noqa: E402
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


def _bin(session: Session, *, code: str, parent: str) -> object:
    warehouse = create_location(
        session, company_id=COMPANY, code=f"{parent}-WH", name="Main", location_type="warehouse"
    )
    zone = create_location(
        session, company_id=COMPANY, code=f"{parent}-Z", name="Zone", location_type="zone",
        parent_id=warehouse.id,
    )
    aisle = create_location(
        session, company_id=COMPANY, code=f"{parent}-A", name="Aisle", location_type="aisle",
        parent_id=zone.id,
    )
    return create_location(
        session, company_id=COMPANY, code=code, name="Bin", location_type="bin",
        parent_id=aisle.id,
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
                code="TXN-CHECK",
                name="Stock transactions check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()
        # The movements below post to the GL (T-1.INV.07), so the company needs the
        # accounts and the mappings that posting reads.
        seed_stock_accounts(session, company_id=COMPANY)
        session.commit()
        item = create_item(
            session,
            company_id=COMPANY,
            sku="BOLT",
            name="Bolt",
            base_uom="each",
            traceability_mode="none",
        )
        other = create_item(
            session,
            company_id=COMPANY,
            sku="NUT",
            name="Nut",
            base_uom="each",
            traceability_mode="none",
        )
        stray_variant = add_variant(session, other, sku="NUT-M6", attributes={"size": "M6"})
        add_uom_conversion(session, item, from_uom="box", to_uom="each", factor=12)
        from_bin = _bin(session, code="B1", parent="SRC")
        to_bin = _bin(session, code="B2", parent="DST")
        session.commit()

        _refused(
            lambda: receive(
                session, item=item, location=from_bin, uom="each", quantity=1,
                value=Decimal("10.00"), currency="PHP", source_type="goods_receipt",
                source_id=uuid.uuid4(), posting_date=DAY, variant=stray_variant,
            ),
            TraceabilityError,
        )
        session.rollback()

        # 1 — a receipt in boxes, stored in each
        receipt = uuid.uuid4()
        received = receive(
            session, item=item, location=from_bin, uom="box", quantity=2, value=Decimal("240.00"),
            currency="PHP", source_type="goods_receipt", source_id=receipt, posting_date=DAY,
        )
        session.commit()
        assert received.quantity == Decimal("24.000000"), received.quantity
        assert received.value == Decimal("240.000000"), received.value
        assert on_hand(session, company_id=COMPANY, item_id=item.id, location_id=from_bin.id) == {
            "quantity": Decimal(24),
            "value": Decimal("240.000000"),
        }
        print("2 boxes arrived as 24 each, worth 240.00")

        # 2 — an issue priced by the engine (240/24 = 10 each)
        issue_doc = uuid.uuid4()
        issued = issue(
            session, item=item, location=from_bin, uom="each", quantity=4, currency="PHP",
            source_type="stock_issue", source_id=issue_doc, posting_date=DAY,
        )
        session.commit()
        assert issued.quantity == Decimal("-4.000000"), issued.quantity
        assert issued.value == Decimal("-40.000000"), issued.value
        print(f"an issue of 4 was valued at {issued.value} by the engine")

        # 3 — negative stock is refused, and nothing is written
        before = session.scalar(select(func.count()).select_from(StockLedgerEntry))
        refusal = _refused(
            lambda: issue(
                session, item=item, location=from_bin, uom="each", quantity=100, currency="PHP",
                source_type="stock_issue", source_id=uuid.uuid4(), posting_date=DAY,
            ),
            InsufficientStockError,
        )
        session.rollback()
        assert session.scalar(select(func.count()).select_from(StockLedgerEntry)) == before, (
            "the refused issue left a movement behind"
        )
        print(f"an issue beyond what is held is refused: {refusal[:56]}…")

        # 4 — a transfer keeps the totals
        totals_before = on_hand(session, company_id=COMPANY, item_id=item.id)
        transfer_doc = uuid.uuid4()
        out, into = transfer(
            session, item=item, from_location=from_bin, to_location=to_bin, uom="each",
            quantity=10, currency="PHP", source_type="stock_transfer", source_id=transfer_doc,
            posting_date=DAY,
        )
        session.commit()
        assert out.value == -into.value == Decimal("-100.000000"), (out.value, into.value)
        assert on_hand(session, company_id=COMPANY, item_id=item.id) == totals_before
        assert on_hand(session, company_id=COMPANY, item_id=item.id, location_id=to_bin.id)[
            "quantity"
        ] == Decimal(10)
        assert len(
            movements_for_source(
                session, company_id=COMPANY, source_type="stock_transfer", source_id=transfer_doc
            )
        ) == 2
        print(f"a transfer of 10 moved {into.value} with the goods; the totals are unchanged")

        # 5 + 6 — what a transfer will not do
        _refused(
            lambda: transfer(
                session, item=item, from_location=to_bin, to_location=from_bin, uom="each",
                quantity=50, currency="PHP", source_type="stock_transfer",
                source_id=uuid.uuid4(), posting_date=DAY,
            ),
            InsufficientStockError,
        )
        _refused(
            lambda: transfer(
                session, item=item, from_location=from_bin, to_location=from_bin, uom="each",
                quantity=1, currency="PHP", source_type="stock_transfer",
                source_id=uuid.uuid4(), posting_date=DAY,
            ),
            TransactionError,
        )
        session.rollback()
        print("a transfer out of a short location, and to where it came from, are both refused")

        # 7 — every movement is traceable, and the ledger still balances
        rows = session.scalars(select(StockLedgerEntry)).all()
        assert all(row.source_type and row.source_id for row in rows), rows
        with engine.connect() as connection:
            assert ledger_gate(connection) == 0, "the ledger-integrity gate is not green"
        assert len(rows) == 4, f"expected four movements, found {len(rows)}"
        print(f"{len(rows)} movements, each naming its document; the integrity gate is green")

    engine.dispose()
    print("ok — receipt, issue and transfer move quantity and value, and never below zero")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
