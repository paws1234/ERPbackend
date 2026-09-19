"""T-1.INV.08 check — batch/lot identity, FEFO, and the expiry refusal.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/erpv1 \
        python tests/check_batch_tracking.py

Green on all seven:

1. a batch-tracked item refuses a movement with no batch, and an untracked item
   refuses one that carries a batch
2. receiving and issuing by batch keep the batch's own quantity, which sums to the
   item's total
3. FEFO suggests the earliest expiry that still holds stock
4. an expired batch is refused on issue
5. with an explicit override **and** the actor who took the decision, the issue
   posts — and the override is on the audit trail with its origin
6. per-batch valuation: one batch's value is its own movement, not the item's
7. an untracked item is unaffected by any of it (no regression to T-1.INV.05)

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

from app.audit import read_trail  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.stock.batches import (  # noqa: E402
    BatchError,
    ExpiredBatchError,
    create_batch,
    fefo_batch,
    require_usable,
)
from app.stock.entries import on_hand  # noqa: E402
from app.stock.items import TraceabilityError, create_item  # noqa: E402
from app.stock.locations import create_location  # noqa: E402
from app.stock.transactions import InsufficientStockError, issue, receive, transfer  # noqa: E402
from app.stock.valuation import set_costing_method, valuation  # noqa: E402
from tests.seed import seed_stock_accounts  # noqa: E402

COMPANY = uuid.uuid4()
LONG_AGO, SOON, LATER = date(2026, 1, 1), date(2027, 3, 1), date(2027, 6, 30)
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
                code="BATCH-CHECK",
                name="Batch tracking check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()
        # Every movement here posts to the GL (T-1.INV.07).
        seed_stock_accounts(session, company_id=COMPANY)
        session.commit()
        milk = create_item(
            session, company_id=COMPANY, sku="MILK", name="Milk", base_uom="each",
            traceability_mode="batch_lot",
        )
        bolt = create_item(
            session, company_id=COMPANY, sku="BOLT", name="Bolt", base_uom="each",
            traceability_mode="none",
        )
        juice = create_item(
            session, company_id=COMPANY, sku="JUICE", name="Juice", base_uom="each",
            traceability_mode="batch_lot",
        )
        old = create_batch(session, item=milk, code="MILK-OLD", expiry_date=LONG_AGO)
        fresh = create_batch(session, item=milk, code="MILK-SOON", expiry_date=SOON)
        latest = create_batch(session, item=milk, code="MILK-LATER", expiry_date=LATER)
        other_item_batch = create_batch(session, item=juice, code="JUICE-LOT", expiry_date=LATER)
        missing = create_batch(session, item=milk, code="MILK-MISSING", expiry_date=LATER)
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

        # 1 — the identity is not optional
        refusal = _refused(
            lambda: receive(
                session, item=milk, location=bin_one, uom="each", quantity=10,
                value=Decimal("100.00"), currency="PHP", source_type="goods_receipt",
                source_id=uuid.uuid4(), posting_date=DAY,
            ),
            TraceabilityError,
        )
        session.rollback()
        _refused(
            lambda: receive(
                session, item=bolt, location=bin_one, uom="each", quantity=1,
                value=Decimal("10.00"), currency="PHP", source_type="goods_receipt",
                source_id=uuid.uuid4(), posting_date=DAY, batch=fresh,
            ),
            TraceabilityError,
        )
        session.rollback()
        _refused(
            lambda: receive(
                session, item=milk, location=bin_one, uom="each", quantity=1,
                value=Decimal("10.00"), currency="PHP", source_type="goods_receipt",
                source_id=uuid.uuid4(), posting_date=DAY, batch=other_item_batch,
            ),
            TraceabilityError,
        )
        session.rollback()
        print(f"a batch-tracked item moves by batch: {refusal[:52]}…")

        # 2 — receipts and an issue by batch
        for batch, quantity, value in ((latest, 20, "400.00"), (fresh, 30, "900.00")):
            receive(
                session, item=milk, location=bin_one, uom="each", quantity=quantity,
                value=Decimal(value), currency="PHP", source_type="goods_receipt",
                source_id=uuid.uuid4(), posting_date=DAY, batch=batch,
            )
        receive(
            session, item=bolt, location=bin_one, uom="each", quantity=5,
            value=Decimal("50.00"), currency="PHP", source_type="goods_receipt",
            source_id=uuid.uuid4(), posting_date=DAY,
        )
        session.commit()
        issue(
            session, item=milk, location=bin_one, uom="each", quantity=5, currency="PHP",
            source_type="stock_issue", source_id=uuid.uuid4(), posting_date=DAY, batch=fresh,
        )
        session.commit()
        at_fresh = on_hand(session, company_id=COMPANY, item_id=milk.id, batch_id=fresh.id)
        assert at_fresh == {"quantity": Decimal(25), "value": Decimal("750.000000")}, at_fresh
        total = on_hand(session, company_id=COMPANY, item_id=milk.id)
        assert total["quantity"] == Decimal(45), total
        assert total["value"] == Decimal("1150.000000"), total
        print(f"batch MILK-SOON holds {at_fresh['quantity']}; the item totals {total['quantity']}")

        refusal = _refused(
            lambda: issue(
                session, item=milk, location=bin_one, uom="each", quantity=1, currency="PHP",
                source_type="stock_issue", source_id=uuid.uuid4(), posting_date=DAY, batch=missing,
            ),
            InsufficientStockError,
        )
        session.rollback()
        print(f"a missing batch at the bin is refused even when the item total is positive: {refusal[:40]}…")

        # 3 — FEFO suggests the soonest expiry that holds stock
        suggestion = fefo_batch(session, item=milk, location_id=bin_one.id)
        assert suggestion is not None and suggestion.code == "MILK-SOON", suggestion
        print(f"FEFO suggests {suggestion.code} (expires {suggestion.expiry_date})")

        # 4 — an expired batch cannot be issued
        receive(
            session, item=milk, location=bin_one, uom="each", quantity=5,
            value=Decimal("100.00"), currency="PHP", source_type="goods_receipt",
            source_id=uuid.uuid4(), posting_date=DAY, batch=old,
        )
        session.commit()
        refusal = _refused(
            lambda: issue(
                session, item=milk, location=bin_one, uom="each", quantity=2, currency="PHP",
                source_type="stock_issue", source_id=uuid.uuid4(), posting_date=DAY, batch=old,
            ),
            ExpiredBatchError,
        )
        session.rollback()
        print(f"an expired batch is refused on issue: {refusal[:54]}…")

        # 5 — the override is explicit, named, and on the trail
        _refused(
            lambda: require_usable(session, old, on=DAY, allow_expired=True),
            ExpiredBatchError,
        )
        session.rollback()
        issue(
            session, item=milk, location=bin_one, uom="each", quantity=2, currency="PHP",
            source_type="stock_issue", source_id=uuid.uuid4(), posting_date=DAY, batch=old,
            allow_expired=True, actor="alice.warehouse",
        )
        session.commit()
        overrides = [
            row for row in read_trail(session) if row.origin_type == "expired_batch_override"
        ]
        assert overrides and overrides[-1].origin_id.startswith("MILK-OLD"), overrides
        assert on_hand(session, company_id=COMPANY, item_id=milk.id, batch_id=old.id)[
            "quantity"
        ] == Decimal(3)
        print("the override names its actor and is on the trail with its origin")

        # 6 — per-batch valuation
        per_batch = valuation(
            session, company_id=COMPANY, item=milk, location_id=bin_one.id, batch_id=fresh.id
        )
        assert per_batch["quantity"] == "25.000000" and per_batch["value"] == "750.000000", (
            per_batch
        )
        print(f"per-batch valuation reads {per_batch['value']} for {per_batch['quantity']} units")

        moved_out, moved_in = transfer(
            session, item=milk, from_location=bin_one, to_location=bin_two, uom="each",
            quantity=2, currency="PHP", source_type="stock_transfer", source_id=uuid.uuid4(),
            posting_date=DAY, batch=latest,
        )
        session.commit()
        assert (moved_out.value, moved_in.value) == (
            Decimal("-40.000000"),
            Decimal("40.000000"),
        ), (moved_out.value, moved_in.value)
        _refused(
            lambda: transfer(
                session, item=milk, from_location=bin_one, to_location=bin_two, uom="each",
                quantity=1, currency="PHP", source_type="stock_transfer", source_id=uuid.uuid4(),
                posting_date=DAY, batch=missing,
            ),
            InsufficientStockError,
        )
        session.rollback()

        # 7 — an untracked item is unaffected
        transfer(
            session, item=bolt, from_location=bin_one, to_location=bin_two, uom="each",
            quantity=2, currency="PHP", source_type="stock_transfer", source_id=uuid.uuid4(),
            posting_date=DAY,
        )
        session.commit()
        assert on_hand(session, company_id=COMPANY, item_id=bolt.id, location_id=bin_two.id)[
            "quantity"
        ] == Decimal(2)
        _refused(
            lambda: create_batch(session, item=bolt, code="NOT-A-LOT"),
            BatchError,
        )
        session.rollback()
        set_costing_method(session, session.get(Company, COMPANY), method="fifo")
        session.commit()
        assert valuation(session, company_id=COMPANY, item=bolt)["quantity"] == "5.000000"
        print("an untracked item moves and values exactly as before")

    engine.dispose()
    print("ok — batch identity is enforced on every movement, and expiry needs a name")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
