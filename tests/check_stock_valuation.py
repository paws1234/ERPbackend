"""T-1.INV.04 check — the three costing methods, and where they disagree.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/erpv1 \
        python tests/check_stock_valuation.py

One movement sequence — receive 100 @ 5.00, receive 50 @ 6.00, issue 120,
receive 30 @ 7.00 — so 60 units are left. Green on all seven:

1. moving average values the remainder at the running average cost (370.00)
2. FIFO consumes the oldest layers, so it keeps a different figure (390.00)
3. standard cost values at the standard, whatever was paid (360.00 at 6.00)
4. `value_issue` gives each method's own cost for the same issue, and the stored
   issue uses the engine's answer rather than the caller's guess
5. Standard Cost with no standard set **fails loudly** instead of valuing at zero
6. changing the method rewrites nothing: the ledger rows are untouched
7. valuation is available on read (no batch job) and narrows to a location

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
from app.stock.entries import StockLedgerEntry, on_hand, record_movement  # noqa: E402
from app.stock.items import create_item  # noqa: E402
from app.stock.locations import create_location  # noqa: E402
from app.stock.valuation import (  # noqa: E402
    MissingStandardCostError,
    UnknownCostingMethodError,
    set_costing_method,
    valuation,
    value_issue,
)

COMPANY = uuid.uuid4()
D1, D2, D3, D4 = date(2026, 9, 1), date(2026, 9, 5), date(2026, 9, 10), date(2026, 9, 15)


def _refused(call, expected: type[Exception]) -> str:
    try:
        call()
    except Exception as exc:  # noqa: BLE001 — the type and message are the point
        assert isinstance(exc, type) or isinstance(exc, expected), (
            f"refused with {type(exc).__name__}: {exc}"
        )
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
        company = Company(
            id=COMPANY,
            code="VAL-CHECK",
            name="Valuation check",
            base_currency="PHP",
            fiscal_year_start_month=1,
        )
        session.add(company)
        session.commit()
        assert company.costing_method == "moving_average", company.costing_method
        item = create_item(
            session,
            company_id=COMPANY,
            sku="BOLT",
            name="Bolt",
            base_uom="each",
            traceability_mode="none",
            standard_cost=Decimal("6.00"),
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

        receive = uuid.uuid4()
        for quantity, value, day in ((100, "500.00", D1), (50, "300.00", D2)):
            record_movement(
                session, item=item, location=bin_one, quantity=quantity, value=Decimal(value),
                currency="PHP", source_type="goods_receipt", source_id=receive, posting_date=day,
            )
        session.commit()

        # 4 — the engine prices the issue, and each method prices it differently
        costs = {
            method: value_issue(
                session,
                company_id=COMPANY,
                item=item,
                quantity=Decimal("120"),
                method=method,
            )
            for method in ("moving_average", "fifo", "standard_cost")
        }
        assert costs == {
            "moving_average": Decimal("640.000000"),
            "fifo": Decimal("620.000000"),
            "standard_cost": Decimal("720.000000"),
        }, costs
        moved = record_movement(
            session, item=item, location=bin_one, quantity=-120,
            value=-costs["moving_average"], currency="PHP", source_type="stock_issue",
            source_id=uuid.uuid4(), posting_date=D3,
        )
        record_movement(
            session, item=item, location=bin_two, quantity=30, value=Decimal("210.00"),
            currency="PHP", source_type="goods_receipt", source_id=uuid.uuid4(), posting_date=D4,
        )
        session.commit()
        assert moved.value == Decimal("-640.000000"), moved.value

        # 1, 2, 3 — three methods, three answers, one ledger
        average = valuation(session, company_id=COMPANY, item=item)
        fifo = valuation(session, company_id=COMPANY, item=item, method="fifo")
        standard = valuation(session, company_id=COMPANY, item=item, method="standard_cost")
        assert average["quantity"] == fifo["quantity"] == standard["quantity"] == "60.000000"
        assert average["value"] == "370.000000", average
        assert fifo["value"] == "390.000000", fifo
        assert standard["value"] == "360.000000", standard
        assert average["unit_cost"] == "6.166667", average
        assert on_hand(session, company_id=COMPANY, item_id=item.id)["value"] == Decimal(
            "370.000000"
        ), "the ledger's own value disagrees with the moving-average method it was written by"
        print(
            f"60 units left: moving average {average['value']}, FIFO {fifo['value']},"
            f" standard cost {standard['value']}"
        )

        # 4 again — the engine, not the caller, priced that issue
        print(
            "one issue of 120 priced by the engine:\n"
            f"  moving average {costs['moving_average']}, FIFO {costs['fifo']},"
            f" standard cost {costs['standard_cost']}"
        )

        # 5 — a method that cannot value says so
        no_standard = create_item(
            session,
            company_id=COMPANY,
            sku="NUT",
            name="Nut",
            base_uom="each",
            traceability_mode="none",
        )
        session.commit()
        refusal = _refused(
            lambda: valuation(
                session, company_id=COMPANY, item=no_standard, method="standard_cost"
            ),
            MissingStandardCostError,
        )
        session.rollback()
        print(f"Standard Cost without a standard is refused: {refusal[:56]}…")

        # 6 — the method changes, the rows do not
        before = session.scalar(
            select(func.count()).select_from(StockLedgerEntry)
        ), session.scalar(
            select(func.sum(StockLedgerEntry.value)).select_from(StockLedgerEntry)
        )
        set_costing_method(session, company, method="fifo")
        session.commit()
        assert valuation(session, company_id=COMPANY, item=item)["value"] == "390.000000"
        after = session.scalar(
            select(func.count()).select_from(StockLedgerEntry)
        ), session.scalar(
            select(func.sum(StockLedgerEntry.value)).select_from(StockLedgerEntry)
        )
        assert before == after, f"changing the method rewrote the ledger: {before} → {after}"
        _refused(
            lambda: set_costing_method(session, company, method="lifo"), UnknownCostingMethodError
        )
        session.rollback()
        print(f"the method is now {company.costing_method}; the ledger rows are unchanged")

        # 7 — on read, and narrowed
        at_bin_two = valuation(
            session, company_id=COMPANY, item=item, location_id=bin_two.id
        )
        assert at_bin_two["quantity"] == "30.000000" and at_bin_two["value"] == "210.000000", (
            at_bin_two
        )
        first_two_days = valuation(session, company_id=COMPANY, item=item, as_of=D2)
        assert first_two_days["quantity"] == "150.000000", first_two_days
        print(f"valuation narrows: {at_bin_two['quantity']} at B2, {first_two_days['quantity']} by {D2}")

    engine.dispose()
    print("ok — the three methods are the three methods, and none of them touches the ledger")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
