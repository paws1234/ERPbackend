"""T-3.POS.06 — measuring an online POS sale against §6 metric 6's < 2 s budget.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_pos_latency.py

Metric 6 is *"POS transaction latency < 2 s"*, and a metric is only measured, never
assumed — so this is a **measurement**, not an optimisation: nothing here changes the
sale's path, and a failure is a finding for the owner of whatever made it slow.

What it does:

1. builds a **realistic dataset** — a catalogue of forty items with barcodes, stock in
   the till, a customer with a tier and a handful of pricing rules — so the pricing
   engine has rules to consider and the issue has stock to value, rather than an
   empty database where everything is fast for the wrong reason
2. times a **complete sale**: scan (barcode → item → engine price → pack tax) → tender
   → complete (stock issue + balanced posting) → receipt, each sale committed on its
   own connection, which is what a till actually does
3. reports the **distribution**, not one run: the count, the minimum, the median, the
   95th percentile and the maximum, with the budget they are compared against
4. compares the **95th percentile** with the budget and fails the check if it is over —
   a single slow run is what a customer experiences, and hiding it behind an average is
   how a latency budget stops meaning anything
5. is **repeatable**: the dataset is built from fixed values, the second pass runs on
   the same database the first one left, and both distributions are reported so a
   regression is visible as a number rather than as a feeling

`POS_LATENCY_BUDGET` overrides the budget in seconds (default 2.0), and the sale count
is fixed so two runs on the same machine are comparable.

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import math
import os
import statistics
import sys
import time
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger.accounts import create_account  # noqa: E402
from app.ledger.currency import register_currency  # noqa: E402
from app.ledger.mapping import set_mapping  # noqa: E402
from app.pos.drawer import paid_out  # noqa: E402
from app.pos.reports import day_report, shift_report  # noqa: E402
from app.pos.sales import (  # noqa: E402
    CARD,
    CASH,
    complete_sale,
    open_sale,
    receipt,
    scan,
    tender,
)
from app.pos.shifts import close_shift, open_shift  # noqa: E402
from app.sales.customers import create_customer, set_customer_tier  # noqa: E402
from app.sales.fulfilment import Shipment  # noqa: E402,F401 — for its table
from app.sales.orders import SalesOrder  # noqa: E402,F401 — for its table
from app.sales.pipeline import Opportunity  # noqa: E402,F401 — for its table
from app.sales.pricing import define_rule  # noqa: E402
from app.stock.items import add_barcode, create_item  # noqa: E402
from app.stock.locations import create_location  # noqa: E402
from app.stock.transactions import receive  # noqa: E402
from tests.seed import seed_stock_accounts  # noqa: E402

COMPANY = uuid.uuid4()
DAY = date(2026, 9, 25)
BUDGET = Decimal(os.environ.get("POS_LATENCY_BUDGET", "2.0"))
CATALOGUE = 40
SALES = 60
LINES_PER_SALE = 3


def _percentile(values: list[float], fraction: float) -> float:
    """The value at `fraction` of a sorted sample, by the nearest-rank method.

    The rank is `ceil(fraction * n)` — the smallest sample that covers the fraction —
    clamped to the ends, so the 95th of 60 samples is the 57th, not the 58th.
    """
    ordered = sorted(values)
    rank = max(1, min(len(ordered), math.ceil(fraction * len(ordered))))
    return ordered[rank - 1]


def _report(label: str, milliseconds: list[float]) -> dict:
    """The distribution as it is printed and asserted on — no average alone."""
    summary = {
        "runs": len(milliseconds),
        "min_ms": min(milliseconds),
        "median_ms": statistics.median(milliseconds),
        "p95_ms": _percentile(milliseconds, 0.95),
        "max_ms": max(milliseconds),
        "mean_ms": statistics.fmean(milliseconds),
    }
    print(
        f"{label}: {summary['runs']} complete sales — min"
        f" {summary['min_ms']:.0f} ms, median {summary['median_ms']:.0f} ms, p95"
        f" {summary['p95_ms']:.0f} ms, max {summary['max_ms']:.0f} ms, mean"
        f" {summary['mean_ms']:.0f} ms (budget {BUDGET * 1000:.0f} ms)"
    )
    return summary


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
            Company(id=COMPANY, code="POS-LATENCY", name="POS latency",
                    base_currency="PHP", fiscal_year_start_month=1)
        )
        register_currency(session, company_id=COMPANY, code="PHP", name="Peso")
        session.commit()
        seed_stock_accounts(session, company_id=COMPANY)
        create_account(session, company_id=COMPANY, code="2200", name="Output VAT",
                       account_class="liability")
        for key, code in (("receivables", "1100"), ("revenue", "4000"),
                          ("output_tax", "2200"), ("cash", "1000"), ("bank", "1010")):
            set_mapping(session, company_id=COMPANY, key=key, account_code=code)
        session.commit()

        # 1 — a catalogue worth pricing against: forty items, barcodes, stock in the till.
        catalogue = []
        for index in range(CATALOGUE):
            item = create_item(session, company_id=COMPANY, sku=f"SKU-{index:03d}",
                               name=f"Item {index:03d}", base_uom="each",
                               traceability_mode="none")
            add_barcode(session, item, value=f"40000000{index:05d}", symbology="ean")
            catalogue.append(item)
        session.commit()
        warehouse = create_location(session, company_id=COMPANY, code="MAIN",
                                    name="Main", location_type="warehouse")
        zone = create_location(session, company_id=COMPANY, code="MAIN-Z", name="Zone",
                               location_type="zone", parent_id=warehouse.id)
        aisle = create_location(session, company_id=COMPANY, code="MAIN-A", name="Aisle",
                                location_type="aisle", parent_id=zone.id)
        till = create_location(session, company_id=COMPANY, code="TILL-1", name="Till 1",
                               location_type="bin", parent_id=aisle.id)
        session.commit()
        for item in catalogue:
            receive(session, item=item, location=till, uom="each", quantity="1000",
                    value=Decimal("40000"), currency="PHP", source_type="goods_receipt",
                    source_id=uuid.uuid4(), posting_date=DAY)
        session.commit()

        acme = create_customer(session, company_id=COMPANY, party_code="ACME",
                               name="Acme Retail", payment_terms_days=30)
        set_customer_tier(session, acme, tier="GOLD")
        # Rules the engine has to consider and order, on three dimensions.
        define_rule(session, company_id=COMPANY, code="ANY-5", name="Any 5%",
                    discount_type="percent", discount_value="5", priority=50)
        define_rule(session, company_id=COMPANY, code="GOLD-10", name="Gold 10%",
                    tier="GOLD", discount_type="percent", discount_value="10", priority=10)
        define_rule(session, company_id=COMPANY, code="BULK-15", name="Bulk 15%",
                    min_quantity="5", discount_type="percent", discount_value="15",
                    priority=20)
        define_rule(session, company_id=COMPANY, code="ITEM-20", name="Item 20%",
                    item_id=catalogue[0].id, discount_type="percent", discount_value="20",
                    priority=1)
        session.commit()
        shift = open_shift(session, company_id=COMPANY, terminal="T1", opening_float="1000",
                           actor="maria", on=DAY)
        session.commit()

        def one_sale(index: int) -> float:
            """One complete sale, timed the way a till experiences it: end to end."""
            started = time.perf_counter()
            sale = open_sale(session, company_id=COMPANY, number=f"POS-L{index:04d}",
                             terminal="T1", location=till, customer=acme, sold_on=DAY)
            session.flush()
            for offset in range(LINES_PER_SALE):
                item = catalogue[(index * LINES_PER_SALE + offset) % CATALOGUE]
                scan(session, sale, barcode=f"40000000{(index * LINES_PER_SALE + offset) % CATALOGUE:05d}",
                     base_price="120.00", quantity="2")
            session.commit()
            tender(session, sale, tender_type=CASH, amount=str(sale.gross_amount))
            session.commit()
            complete_sale(session, sale)
            session.commit()
            receipt(sale)
            return (time.perf_counter() - started) * 1000

        # 2 + 3 — the measurement, over a realistic sale, reported as a distribution
        first_pass = _report(
            f"pass 1 (catalogue {CATALOGUE}, {LINES_PER_SALE} lines a sale)",
            [one_sale(index) for index in range(SALES)],
        )
        second_pass = _report(
            "pass 2 (same database, same dataset)",
            [one_sale(SALES + index) for index in range(SALES)],
        )

        # 4 — the budget, on the tail rather than the average
        budget_ms = float(BUDGET) * 1000
        over = [
            value for value in (first_pass["p95_ms"], second_pass["p95_ms"])
            if value > budget_ms
        ]
        assert not over, (
            f"the 95th percentile exceeded the {budget_ms:.0f} ms budget in"
            f" {len(over)} pass(es): {over}"
        )
        assert first_pass["runs"] == SALES and second_pass["runs"] == SALES

        # 5 — the day is still whole after all that trading, and the report says so
        totals = shift_report(session, shift)
        assert totals["ties"] is True, totals
        assert totals["sales"] == 2 * SALES, totals["sales"]
        assert totals["tenders_applied"] == totals["gross"], totals
        paid_out(session, company_id=COMPANY, terminal="T1", amount="10.00",
                 reason="coffee money", actor="maria", on=DAY)
        session.commit()
        shift = session.get(type(shift), shift.id)
        close_shift(
            session, shift, actor="maria",
            counted_cash=str(shift_report(session, shift)["drawer"]["expected"]),
        )
        session.commit()
        day = day_report(session, company_id=COMPANY, on=DAY)
        assert day["sales"] == 2 * SALES, day["sales"]
        assert day["gross"] == totals["gross"], (day["gross"], totals["gross"])
        print(
            f"5. the two passes traded {2 * SALES} sales worth {totals['gross']} — the"
            f" shift's Z-Report still ties with no rounding gap and the day report agrees"
        )

        print(
            f"\ncheck_pos_latency: all assertions green — p95 {first_pass['p95_ms']:.0f} ms"
            f" and {second_pass['p95_ms']:.0f} ms against a {budget_ms:.0f} ms budget,"
            f" measured over {SALES} complete sales a pass on a {CATALOGUE}-item catalogue"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
