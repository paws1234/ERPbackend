"""T-1.INV.06 check — physical count, variance, approval-gated adjustment.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/erpv1 \
        python tests/check_physical_count.py

Green on all six:

1. a count snapshots the system quantity per item at the location
2. recording what was found produces a variance per item — one positive, one
   negative in this check
3. a below-threshold adjustment posts ledger entries for **exactly** the two
   variances, and the location's on-hand quantity then equals what was counted
4. the posting is attributable: the entries name the count, and the count's own
   rows are on the audit trail
5. an above-threshold adjustment is refused while its approval is pending, and
   writes nothing
6. once the configured chain approves it, the same call posts — and posting the
   same count twice is refused

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

from app.audit import read_trail  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.stock.counts import (  # noqa: E402
    ApprovalRequiredError,
    CountAlreadyPostedError,
    PhysicalCount,
    adjustment_value,
    counted,
    post_adjustment,
    record_count,
    start_count,
)
from app.stock.entries import StockLedgerEntry, movements_for_source, on_hand  # noqa: E402
from app.stock.items import create_item  # noqa: E402
from app.stock.locations import create_location  # noqa: E402
from app.stock.transactions import receive  # noqa: E402
from tests.seed import seed_stock_accounts  # noqa: E402
from app.workflow import APPROVE, configure, decide, start_approval  # noqa: E402

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
                code="COUNT-CHECK",
                name="Physical count check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()
        # The receipts and the adjustment post to the GL (T-1.INV.07), so the company
        # needs the accounts and mappings that posting reads.
        seed_stock_accounts(session, company_id=COMPANY)
        session.commit()
        bolt = create_item(
            session, company_id=COMPANY, sku="BOLT", name="Bolt", base_uom="each",
            traceability_mode="none",
        )
        nut = create_item(
            session, company_id=COMPANY, sku="NUT", name="Nut", base_uom="each",
            traceability_mode="none",
        )
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
        session.commit()
        for item, quantity, value in ((bolt, 100, "1000.00"), (nut, 50, "1000.00")):
            receive(
                session, item=item, location=bin_one, uom="each", quantity=quantity,
                value=Decimal(value), currency="PHP", source_type="goods_receipt",
                source_id=uuid.uuid4(), posting_date=DAY,
            )
        session.commit()
        # The chain has to exist before an adjustment can be posted at all: T-0.WF.01
        # refuses a document type nobody configured rather than waving it through, so
        # the threshold is what makes a small adjustment need no approval.
        configure(
            session,
            company_id=COMPANY,
            doc_type="inventory_adjustment",
            name="Inventory adjustments",
            levels=[(Decimal("50"), "controller")],
        )
        session.commit()

        # 1 — the snapshot
        count = start_count(session, company_id=COMPANY, location=bin_one, actor="alice")
        session.commit()
        snapshot = {line.item_id: line.system_quantity for line in count.lines}
        assert snapshot[bolt.id] == Decimal("100.000000"), snapshot
        assert snapshot[nut.id] == Decimal("50.000000"), snapshot
        print("a count snapshots 100 bolt and 50 nut at the bin")

        # 2 — the variances
        record_count(session, count, item=bolt, counted_quantity=99)
        record_count(session, count, item=nut, counted_quantity=51)
        session.commit()
        variances = {line.item_id: line.variance for line in counted(session, count)}
        assert variances[bolt.id] == Decimal("-1.000000"), variances
        assert variances[nut.id] == Decimal("1.000000"), variances
        # bolt: 1 unit at 10.00; nut: 1 unit at 20.00 → 30.00 in total
        assert adjustment_value(session, count) == Decimal("30.000000"), adjustment_value(
            session, count
        )
        print(f"counted 99 bolt and 51 nut: variances {variances[bolt.id]} and {variances[nut.id]}")

        # 3 — below the configured threshold, so no approval is involved
        written = post_adjustment(session, count)
        session.commit()
        assert len(written) == 2, written
        assert sorted(entry.quantity for entry in written) == [
            Decimal("-1.000000"),
            Decimal("1.000000"),
        ], written
        held = on_hand(session, company_id=COMPANY, item_id=bolt.id, location_id=bin_one.id)
        assert held["quantity"] == Decimal(99), held
        assert (
            on_hand(session, company_id=COMPANY, item_id=nut.id, location_id=bin_one.id)["quantity"]
            == Decimal(51)
        )
        print(f"the adjustment posted exactly the variance; the bin now holds {held['quantity']} bolt")

        # 4 — attributable, both ends
        assert len(
            movements_for_source(
                session, company_id=COMPANY, source_type="inventory_adjustment", source_id=count.id
            )
        ) == 2
        trail = read_trail(session, entity="physical_count")
        assert trail and trail[0].entity_id == str(count.id), trail
        assert any(line.variance == Decimal("1.000000") for line in counted(session, count))
        print("the entries name the count, and the count is on the audit trail")

        # 5 + 6 — above the threshold, approval comes first
        big = start_count(session, company_id=COMPANY, location=bin_one, actor="alice")
        record_count(session, big, item=bolt, counted_quantity=108)
        session.commit()
        assert adjustment_value(session, big) == Decimal("90.000000"), adjustment_value(session, big)
        refusal = _refused(lambda: post_adjustment(session, big), ApprovalRequiredError)
        session.rollback()
        assert session.scalar(select(func.count()).select_from(StockLedgerEntry)) == 4, (
            "the refused adjustment wrote a movement"
        )
        print(f"an above-threshold adjustment waits for approval: {refusal[:52]}…")

        request = start_approval(
            session,
            company_id=COMPANY,
            doc_type="inventory_adjustment",
            document_id=big.id,
            amount=adjustment_value(session, big),
        )
        session.commit()
        assert request is not None and request.state == "pending"
        decide(
            session, request, actor="carol", action=APPROVE, role="controller", reason="counted twice"
        )
        session.commit()
        approved = post_adjustment(session, big, approval=request)
        session.commit()
        assert [entry.quantity for entry in approved] == [Decimal("9.000000")], approved
        _refused(lambda: post_adjustment(session, big, approval=request), CountAlreadyPostedError)
        session.rollback()
        assert session.get(PhysicalCount, big.id).state == "posted"
        print("the approved adjustment posted once, and posting it again is refused")

    engine.dispose()
    print("ok — a count produces a variance, and the adjustment needs approval when configured")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
