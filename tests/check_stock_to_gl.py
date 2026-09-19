"""T-1.INV.07 check — stock movements post to the GL, and the two reconcile.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/erpv1 \
        python tests/check_stock_to_gl.py

Green on all six:

1. a receipt posts a balanced entry (inventory debited, the receipt's counterpart
   credited) for exactly the movement's value, and an issue posts the mirror image
2. a transfer posts both halves against the inventory account, so it is traceable
   and the inventory total does not move
3. an inventory adjustment posts its variance through the same path
4. the reconciliation reports zero difference between the GL inventory account and
   the stock ledger over the period — §6 metric 2
5. a movement whose posting is skipped is **reported** as a difference, not
   accepted silently — and posting it clears the difference
6. a source type with no mapped counterpart is refused, every stock posting names
   its document, and the ledger-integrity gate is green

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger.currency import register_currency, store_rate  # noqa: E402
from app.ledger.gl import account_balance, entries_for_source  # noqa: E402
from app.ledger.mapping import set_mapping  # noqa: E402
from app.ledger.posting import JournalEntry  # noqa: E402
from app.stock.entries import on_hand, record_movement  # noqa: E402
from app.stock.gl_posting import (  # noqa: E402
    StockPostingError,
    account_key_for,
    post_movement_to_gl,
    reconcile,
)
from app.stock.items import create_item  # noqa: E402
from app.stock.locations import create_location  # noqa: E402
from app.stock.transactions import issue, receive, transfer  # noqa: E402
from tests.check_ledger_integrity import ledger_gate  # noqa: E402
from tests.seed import seed_accounts  # noqa: E402

COMPANY = uuid.uuid4()
START, END = date(2026, 9, 1), date(2026, 9, 30)
DAY = date(2026, 9, 17)


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
                code="STOCK-GL",
                name="Stock to GL check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()
        seed_accounts(session, company_id=COMPANY)
        register_currency(session, company_id=COMPANY, code="PHP", name="Philippine Peso")
        register_currency(session, company_id=COMPANY, code="USD", name="US Dollar")
        store_rate(
            session,
            company_id=COMPANY,
            base_currency="PHP",
            currency="USD",
            on=DAY,
            rate="58.5",
        )
        # inventory, the receipt's counterpart (goods received not invoiced), the
        # issue's counterpart (cost of goods sold) and the adjustment's.
        set_mapping(session, company_id=COMPANY, key="inventory", account_code="1200")
        set_mapping(session, company_id=COMPANY, key="stock_receipt", account_code="2000")
        set_mapping(session, company_id=COMPANY, key="stock_issue", account_code="5000")
        set_mapping(session, company_id=COMPANY, key="stock_adjustment", account_code="5900")
        session.commit()
        item = create_item(
            session, company_id=COMPANY, sku="BOLT", name="Bolt", base_uom="each",
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
        bin_two = create_location(
            session, company_id=COMPANY, code="B2", name="Bin 2", location_type="bin",
            parent_id=aisle.id,
        )
        session.commit()

        # 1 — a receipt and an issue post their own entries
        receipt_doc = uuid.uuid4()
        receive(
            session, item=item, location=bin_one, uom="each", quantity=100,
            value=Decimal("1000.00"), currency="PHP", source_type="goods_receipt",
            source_id=receipt_doc, posting_date=DAY,
        )
        session.commit()
        receipt_postings = entries_for_source(
            session, company_id=COMPANY, source_type="goods_receipt", source_id=receipt_doc
        )
        assert len(receipt_postings) == 1, receipt_postings
        sides = {line.account: (line.debit, line.credit) for line in receipt_postings[0].lines}
        assert sides["1200"] == (Decimal("1000.000000"), Decimal(0)), sides
        assert sides["2000"] == (Decimal(0), Decimal("1000.000000")), sides

        issue_doc = uuid.uuid4()
        issue(session, item=item, location=bin_one, uom="each", quantity=10, currency="PHP",
              source_type="stock_issue", source_id=issue_doc, posting_date=DAY)
        session.commit()
        issue_sides = {
            line.account: (line.debit, line.credit)
            for line in entries_for_source(
                session, company_id=COMPANY, source_type="stock_issue", source_id=issue_doc
            )[0].lines
        }
        assert issue_sides["1200"] == (Decimal(0), Decimal("100.000000")), issue_sides
        assert issue_sides["5000"] == (Decimal("100.000000"), Decimal(0)), issue_sides
        print("a receipt debits inventory and credits its counterpart; an issue is the mirror")

        # 2 — a transfer touches inventory on both sides
        transfer_doc = uuid.uuid4()
        transfer(session, item=item, from_location=bin_one, to_location=bin_two, uom="each",
                 quantity=30, currency="PHP", source_type="stock_transfer",
                 source_id=transfer_doc, posting_date=DAY)
        session.commit()
        transfer_postings = entries_for_source(
            session, company_id=COMPANY, source_type="stock_transfer", source_id=transfer_doc
        )
        assert len(transfer_postings) == 2, transfer_postings
        for posting in transfer_postings:
            assert {line.account for line in posting.lines} == {"1200"}, posting.lines
        print("a transfer posts inside the inventory account, so the total is unmoved")

        # 3 — an adjustment posts its variance through the same path
        variance = record_movement(
            session, item=item, location=bin_one, quantity=-5, value=Decimal("-50.00"),
            currency="PHP", source_type="inventory_adjustment", source_id=uuid.uuid4(),
            posting_date=DAY,
        )
        post_movement_to_gl(session, entry=variance)
        session.commit()
        print("an adjustment's variance posts to inventory against the shrinkage account")

        receive(
            session, item=item, location=bin_one, uom="each", quantity=1,
            value=Decimal("100.00"), currency="USD", source_type="goods_receipt",
            source_id=uuid.uuid4(), posting_date=DAY,
        )
        session.commit()

        # 4 — the two agree, including foreign-currency stock converted to base
        report = reconcile(session, company_id=COMPANY, start=START, end=END)
        assert report["balanced"], report
        assert report["difference"] == "0.000000", report
        assert report["stock_value"] == "6700.000000", report
        assert account_balance(session, company_id=COMPANY, account_code="1200") == Decimal(
            "6700.000000"
        ), "the GL inventory account disagrees with the ledger it was posted from"
        print(f"stock {report['stock_value']} = GL {report['gl_value']}; difference 0")

        # 5 — a movement with no posting is a reported difference
        skipped = record_movement(
            session, item=item, location=bin_one, quantity=10, value=Decimal("100.00"),
            currency="PHP", source_type="goods_receipt", source_id=uuid.uuid4(),
            posting_date=DAY,
        )
        session.commit()
        mismatch = reconcile(session, company_id=COMPANY, start=START, end=END)
        assert not mismatch["balanced"], mismatch
        assert mismatch["difference"] == "-100.000000", mismatch
        assert on_hand(session, company_id=COMPANY, item_id=item.id)["value"] == Decimal(
            "1050.000000"
        )
        print(f"a movement with no posting is reported: difference {mismatch['difference']}")
        post_movement_to_gl(session, entry=skipped)
        session.commit()
        assert reconcile(session, company_id=COMPANY, start=START, end=END)["balanced"]

        # 6 — an unmapped source type is refused, and every posting names its document
        try:
            account_key_for("stock_count")
        except StockPostingError as exc:
            refusal = str(exc)
        else:
            raise AssertionError("an unknown movement source type was accepted")
        postings = session.scalars(select(JournalEntry)).all()
        assert postings and all(entry.source_id for entry in postings), postings
        with engine.connect() as connection:
            assert ledger_gate(connection) == 0, "the ledger-integrity gate is not green"
        print(f"an unmapped source type is refused ({refusal[:44]}…); the gate is green")

    engine.dispose()
    print("ok — every movement posts, and the GL inventory account reconciles to it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
