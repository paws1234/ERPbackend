"""T-6.HARD.01 — the load test: the five ledger-heavy paths §4 Phase 6 names, measured.

    DATABASE_URL=postgresql+psycopg://erpv1:erpv1@localhost:5432/erpv1 \
        python tools/load_test.py [--items 40] [--postings 400] [--sales 60] \
            [--concurrency 4] [--repeats 3] [--record LOAD-TEST.md]

The plan names the paths — *posting, stock ledger reads, statement generation, POS checkout,
MRP runs* — and §6 metric 6 states the one figure: **POS checkout under 2 s**. Everything else
here is a target this task states itself, in one table (:data:`TARGETS_MS`), because a
"measured" path with nothing to measure it against is a stopwatch, not a verification. A path
that misses its target is a **finding with its cause**, and the cause is read from
:data:`CAUSES` — a human's sentence — so a failure is recorded rather than explained away.

Four decisions:

* **The dataset is stated, not implied.** The item count, the postings already in the ledger,
  the catalogue, the concurrency and the repeats are all printed with the figures and written
  into the recorded report: a latency with no dataset beside it cannot be compared with
  anything, including itself next month.
* **The distribution is the answer, not the average.** Minimum, median, 95th percentile and
  maximum are all reported, and the **95th percentile is what is compared with the target** —
  a single slow transaction is what a customer experiences, and an average is how a budget
  stops meaning anything (the rule T-3.POS.06 set for POS latency).
* **Concurrency is real.** Each worker gets its own connection and its own transaction, so
  "at this load" means several tills, desks and runs at once rather than one request repeated
  in a loop.
* **The numbers are recorded in the repository.** `--record` writes `LOAD-TEST.md` with the
  command, the dataset, the table and the findings, and the file's own check
  (`tests/check_load.py`) fails if a named path has no figure there.

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import argparse
import math
import os
import statistics
import sys
import threading
import time
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Imported for their tables, not their APIs: the paths read the customer's exposure
# (invoices, gateway payments) and the order book's own tables, so the one schema has to
# carry them before `create_all`.
from app.ar.gateway import GatewayPayment  # noqa: E402,F401
from app.ar.invoices import CustomerInvoice  # noqa: E402,F401
from app.company import Company, set_credit_check_mode  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger.accounts import create_account  # noqa: E402
from app.ledger.currency import register_currency  # noqa: E402
from app.ledger.mapping import set_mapping  # noqa: E402
from app.ledger.posting import post_journal_entry  # noqa: E402
from app.ledger.statements import balance_sheet, profit_and_loss, trial_balance  # noqa: E402
from app.manufacturing.mrp import run_mrp  # noqa: E402
from app.pos.sales import CASH, complete_sale, open_sale, receipt, scan, tender  # noqa: E402
from app.pos.shifts import open_shift  # noqa: E402
from app.procurement.orders import PurchaseOrderLine  # noqa: E402,F401 — for its table
from app.sales.customers import create_customer, set_customer_tier  # noqa: E402
from app.sales.fulfilment import Shipment  # noqa: E402,F401 — for its table
from app.sales.orders import confirm_order, convert_quotation_to_order  # noqa: E402
from app.sales.pipeline import Opportunity  # noqa: E402,F401 — for its table
from app.sales.pricing import define_rule  # noqa: E402
from app.sales.quotations import add_line as quote_line  # noqa: E402
from app.sales.quotations import create_quotation  # noqa: E402
from app.stock.entries import movements, on_hand  # noqa: E402
from app.stock.items import Item, add_barcode, create_item  # noqa: E402
from app.stock.locations import create_location, location_by_code  # noqa: E402
from app.stock.transactions import receive  # noqa: E402
from tests.seed import seed_stock_accounts  # noqa: E402

COMPANY = uuid.uuid4()
DAY = date(2026, 6, 1)
HORIZON_DAYS = 28
BUCKET_DAYS = 7
LINES_PER_SALE = 3

# The five paths §4 Phase 6 names, and the target each is held to in milliseconds. Only the
# POS figure is the plan's (§6 metric 6, `pos_latency_budget`); the rest are stated here, in
# the open, so "it failed its target" is a sentence a reader can check rather than assume.
TARGETS_MS: dict[str, float] = {
    "posting": 100.0,
    "stock_ledger_reads": 250.0,
    "statement_generation": 2000.0,
    "pos_checkout": 2000.0,
    "mrp_run": 5000.0,
}

# What each named path does, said once, so the report, the tool and the check agree.
DESCRIPTIONS: dict[str, str] = {
    "posting": "one balanced journal entry posted through T-0.CORE.01's primitive",
    "stock_ledger_reads": "on-hand and the movement history for three items — the ledger sum",
    "statement_generation": "trial balance, profit and loss and balance sheet for the period",
    "pos_checkout": "one complete sale: scan ×3, tender, complete (stock issue + posting), receipt",
    "mrp_run": "one net-requirements run over the horizon, against the order book and stock",
}

# The one figure the plan itself states (§6 metric 6 / `pos_latency_budget`).
POS_BUDGET_MS = 2000.0

# A finding's cause is a human's sentence, written down where the finding is read. A path over
# its target with nothing stated is reported as exactly that — never as a pass.
UNSTATED = "cause not yet stated — recorded as an open finding"

# The causes already known, where a path is over its target: filled in by whoever investigates
# one, so the next run's report repeats what was found rather than rediscovering it. Empty
# because the first recorded run found nothing over target — a miss with nothing stated here
# is recorded as an open finding and never as a pass.
CAUSES: dict[str, str] = {}


def percentile(values: list[float], fraction: float) -> float:
    """The value at `fraction` of a sample, by the nearest-rank method (T-3.POS.06's rule)."""
    ordered = sorted(values)
    rank = max(1, min(len(ordered), math.ceil(fraction * len(ordered))))
    return ordered[rank - 1]


def distribution(milliseconds: list[float]) -> dict:
    """A sample as it is printed and judged — never one number on its own."""
    return {
        "runs": len(milliseconds),
        "min_ms": min(milliseconds),
        "median_ms": statistics.median(milliseconds),
        "p95_ms": percentile(milliseconds, 0.95),
        "max_ms": max(milliseconds),
        "mean_ms": statistics.fmean(milliseconds),
    }


def measure(
    call: Callable[[Session], None],
    *,
    engine,
    repeats: int,
    concurrency: int,
) -> list[float]:
    """Time `call` `repeats` times on each of `concurrency` workers, in milliseconds.

    Each worker opens its own session on its own connection, so the sample is a sample of the
    path under load rather than of one session in a loop. A worker's exception is the caller's:
    a load test that swallows an error measures how fast a refusal happens.
    """
    if repeats < 1 or concurrency < 1:
        raise ValueError("a load test asks for at least one repeat and one worker")
    samples: list[float] = []
    failures: list[BaseException] = []
    lock = threading.Lock()
    barrier = threading.Barrier(concurrency)

    def worker() -> None:
        barrier.wait()
        try:
            with Session(engine) as session:
                for _ in range(repeats):
                    started = time.perf_counter()
                    call(session)
                    taken = (time.perf_counter() - started) * 1000
                    with lock:
                        samples.append(taken)
        except BaseException as exc:  # noqa: BLE001 — re-raised below, never swallowed
            with lock:
                failures.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(concurrency)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if failures:
        # A worker's fault is the caller's: a load test that swallows an error measures how
        # fast a refusal happens, which is not the path anybody asked about.
        raise failures[0]
    if len(samples) != repeats * concurrency:
        raise RuntimeError(
            f"expected {repeats * concurrency} samples, measured {len(samples)}"
        )
    return samples


def run_load(
    paths: dict[str, Callable[[Session], None]],
    *,
    engine,
    dataset: dict,
    repeats: int,
    concurrency: int,
    causes: dict[str, str] | None = None,
) -> dict:
    """Every named path, measured at the stated size and load, with its findings."""
    rows: list[dict] = []
    for name, call in paths.items():
        target = TARGETS_MS.get(name)
        if target is None:
            raise ValueError(f"{name!r} is not one of the paths this task names")
        took = measure(call, engine=engine, repeats=repeats, concurrency=concurrency)
        rows.append(
            {
                "path": name,
                "does": DESCRIPTIONS.get(name, ""),
                "target_ms": target,
                **distribution(took),
            }
        )
        rows[-1]["within_target"] = rows[-1]["p95_ms"] <= target
    report = {
        "dataset": dataset,
        "concurrency": concurrency,
        "repeats": repeats,
        "paths": rows,
        "findings": findings(rows, causes or {}),
    }
    return report


def findings(rows: list[dict], causes: dict[str, str]) -> list[dict]:
    """Every path whose 95th percentile is over its target, with the cause it is recorded with."""
    out: list[dict] = []
    for row in rows:
        if row["p95_ms"] > row["target_ms"]:
            out.append(
                {
                    "path": row["path"],
                    "measured_ms": row["p95_ms"],
                    "target_ms": row["target_ms"],
                    "cause": causes.get(row["path"], UNSTATED),
                }
            )
    return out


def build_dataset(session: Session, *, items: int, postings: int) -> dict:
    """A dataset worth measuring against, and the context the paths read.

    Deliberately not empty: a catalogue to price against, stock in the till to issue, pricing
    rules for the engine to consider and order, a confirmed order for MRP to net, and postings
    already in the ledger so a statement has volume to read.
    """
    session.add(
        Company(
            id=COMPANY,
            code="LOAD-TEST",
            name="Load test",
            base_currency="PHP",
            fiscal_year_start_month=1,
        )
    )
    register_currency(session, company_id=COMPANY, code="PHP", name="Peso")
    session.commit()
    # The order book needs a stated credit-check mode before an order can be confirmed
    # (T-3.SALES.04); a load test is not a test of the credit policy.
    set_credit_check_mode(session, session.get(Company, COMPANY), mode="off")
    session.commit()
    seed_stock_accounts(session, company_id=COMPANY)
    create_account(session, company_id=COMPANY, code="2200", name="Output VAT",
                   account_class="liability")
    for key, code in (
        ("receivables", "1100"),
        ("revenue", "4000"),
        ("output_tax", "2200"),
        ("cash", "1000"),
        ("bank", "1010"),
    ):
        set_mapping(session, company_id=COMPANY, key=key, account_code=code)
    session.commit()

    catalogue = []
    for index in range(items):
        item = create_item(
            session, company_id=COMPANY, sku=f"SKU-{index:03d}", name=f"Item {index:03d}",
            base_uom="each", traceability_mode="none",
        )
        add_barcode(session, item, value=f"40000000{index:05d}", symbology="ean")
        catalogue.append(item)
    session.commit()
    warehouse = create_location(session, company_id=COMPANY, code="MAIN", name="Main",
                                location_type="warehouse")
    zone = create_location(session, company_id=COMPANY, code="MAIN-Z", name="Zone",
                           location_type="zone", parent_id=warehouse.id)
    aisle = create_location(session, company_id=COMPANY, code="MAIN-A", name="Aisle",
                            location_type="aisle", parent_id=zone.id)
    till = create_location(session, company_id=COMPANY, code="TILL-1", name="Till 1",
                           location_type="bin", parent_id=aisle.id)
    session.commit()
    for item in catalogue:
        receive(session, item=item, location=till, uom="each", quantity="10000",
                value=Decimal("400000"), currency="PHP", source_type="goods_receipt",
                source_id=uuid.uuid4(), posting_date=DAY)
    session.commit()

    customer = create_customer(session, company_id=COMPANY, party_code="ACME",
                               name="Acme Retail", payment_terms_days=30)
    set_customer_tier(session, customer, tier="GOLD")
    define_rule(session, company_id=COMPANY, code="ANY-5", name="Any 5%",
                discount_type="percent", discount_value="5", priority=50)
    define_rule(session, company_id=COMPANY, code="GOLD-10", name="Gold 10%", tier="GOLD",
                discount_type="percent", discount_value="10", priority=10)
    define_rule(session, company_id=COMPANY, code="BULK-15", name="Bulk 15%", min_quantity="5",
                discount_type="percent", discount_value="15", priority=20)
    session.commit()

    # A ledger with volume in it: the postings a statement has to read.
    for index in range(postings):
        post_journal_entry(
            session,
            company_id=COMPANY,
            posting_date=DAY,
            currency="PHP",
            memo=f"load test posting {index}",
            source_type="load_test",
            source_id=uuid.uuid4(),
            lines=[
                {"account": "1000", "debit": Decimal("125.50")},
                {"account": "4000", "credit": Decimal("125.50")},
            ],
        )
    session.commit()

    # Demand for MRP to net, and a shift for the till to trade in.
    quote = create_quotation(session, company_id=COMPANY, customer_id=customer.id,
                             number="Q-LOAD", issued_on=DAY, valid_until=date(2026, 12, 31))
    session.flush()
    quote_line(session, quote, line_no=1, description="Item 000",
               quantity=str(items), unit_price="120.00", uom="each",
               item_id=catalogue[0].id, priced_on=DAY)
    session.flush()
    order = convert_quotation_to_order(session, quote, number="SO-LOAD", on=DAY)
    confirm_order(session, order, actor="load")
    open_shift(session, company_id=COMPANY, terminal="T1", opening_float="1000",
               actor="load", on=DAY)
    session.commit()

    return {
        "items": items,
        "postings_in_ledger": postings,
        "catalogue": len(catalogue),
        "customers": 1,
        "demand_source": "one confirmed order",
    }


def paths_for(session: Session) -> dict[str, Callable[[Session], None]]:
    """The five paths, bound to the dataset that was just built."""
    catalogue = sorted(
        session.scalars(select_item(session)), key=lambda item: item.sku
    )
    codes = {
        item.id: row.value for item in catalogue for row in item.barcodes
    }
    counter = threadsafe_counter()

    def posting(worker: Session) -> None:
        post_journal_entry(
            worker,
            company_id=COMPANY,
            posting_date=DAY,
            currency="PHP",
            memo=f"load {counter()}",
            source_type="load_test",
            source_id=uuid.uuid4(),
            lines=[
                {"account": "1000", "debit": Decimal("10.25")},
                {"account": "4000", "credit": Decimal("10.25")},
            ],
        )
        worker.commit()

    def stock_reads(worker: Session) -> None:
        for item in catalogue[:3]:
            on_hand(worker, company_id=COMPANY, item_id=item.id)
            movements(worker, company_id=COMPANY, item_id=item.id)

    def statements(worker: Session) -> None:
        trial_balance(worker, company_id=COMPANY, start=DAY, end=DAY)
        profit_and_loss(worker, company_id=COMPANY, start=DAY, end=DAY)
        balance_sheet(worker, company_id=COMPANY, as_of=DAY)

    def checkout(worker: Session) -> None:
        index = counter()
        sale = open_sale(
            worker, company_id=COMPANY, number=f"POS-L{index:06d}", terminal="T1",
            location=till(worker), sold_on=DAY,
        )
        for offset in range(LINES_PER_SALE):
            item = catalogue[(index * LINES_PER_SALE + offset) % len(catalogue)]
            scan(worker, sale, barcode=codes[item.id], base_price="120.00", quantity="2")
        worker.commit()
        tender(worker, sale, tender_type=CASH, amount=str(sale.gross_amount))
        worker.commit()
        complete_sale(worker, sale)
        worker.commit()
        receipt(sale)

    def mrp(worker: Session) -> None:
        run_mrp(worker, company_id=COMPANY, start=DAY, horizon_days=HORIZON_DAYS,
                bucket_days=BUCKET_DAYS, demand_sources=("sales_orders",), run_on=DAY)
        worker.commit()

    return {
        "posting": posting,
        "stock_ledger_reads": stock_reads,
        "statement_generation": statements,
        "pos_checkout": checkout,
        "mrp_run": mrp,
    }


def threadsafe_counter():
    """A counter several workers share: sale numbers and memos have to be unique under load."""
    lock = threading.Lock()
    state = {"n": 0}

    def take() -> int:
        with lock:
            state["n"] += 1
            return state["n"]

    return take


def till(session: Session):
    """The till the sale issues from — one location, read by its code."""
    return location_by_code(session, company_id=COMPANY, code="TILL-1")


def select_item(session: Session):
    """The dataset's catalogue, as the paths read it."""
    return select(Item).where(Item.company_id == COMPANY)


def table(report: dict) -> str:
    """The measured table as markdown — what goes in the report and in the record."""
    lines = [
        "| Path | What it does | Runs | Min | Median | p95 | Max | Target | Within |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in report["paths"]:
        lines.append(
            f"| `{row['path']}` | {row['does']} | {row['runs']} | {row['min_ms']:.0f} ms |"
            f" {row['median_ms']:.0f} ms | {row['p95_ms']:.0f} ms | {row['max_ms']:.0f} ms |"
            f" {row['target_ms']:.0f} ms | {'yes' if row['within_target'] else '**no**'} |"
        )
    return "\n".join(lines)


def record(report: dict, path: Path, *, command: str) -> Path:
    """Write the recorded report: the command, the dataset, the table and the findings."""
    dataset = report["dataset"]
    body = [
        "# Load test — the ledger-heavy paths (T-6.HARD.01)",
        "",
        "**Recorded by**: `tools/load_test.py`. **Reproduce with**:",
        "",
        "```",
        command,
        "```",
        "",
        "## The dataset the figures belong to",
        "",
        f"- items in the catalogue: **{dataset['items']}** (each with a barcode and stock)",
        f"- postings already in the ledger: **{dataset['postings_in_ledger']}**",
        f"- demand for MRP: {dataset['demand_source']}",
        "- a customer with a tier and three pricing rules the engine has to order",
        f"- concurrency: **{report['concurrency']}** workers, each with its own connection and"
        " transaction",
        f"- repeats per worker: **{report['repeats']}**",
        "",
        "## The figures",
        "",
        table(report),
        "",
        "The **95th percentile** is what is compared with the target: a single slow transaction"
        " is what a customer experiences, and an average is how a budget stops meaning"
        " anything (the rule T-3.POS.06 set for POS latency). Only `pos_checkout`'s target is"
        " the plan's own (§6 metric 6, `pos_latency_budget`); the others are stated in"
        " `tools/load_test.py`'s `TARGETS_MS` so a miss is checkable rather than a feeling.",
        "",
        "## Findings",
        "",
    ]
    if report["findings"]:
        for finding in report["findings"]:
            body.append(
                f"- `{finding['path']}`: measured **{finding['measured_ms']:.0f} ms** against"
                f" {finding['target_ms']:.0f} ms — {finding['cause']}"
            )
    else:
        body.append(
            "None: every named path is within its target at the stated dataset and load."
        )
    body.append("")
    tightest = max(report["paths"], key=lambda row: row["p95_ms"] / row["target_ms"])
    body.append(
        f"The closest path to its target is `{tightest['path']}`: **{tightest['p95_ms']:.0f}"
        f" ms** of a {tightest['target_ms']:.0f} ms target"
        f" ({tightest['p95_ms'] / tightest['target_ms'] * 100:.0f} %). Everything else has more"
        " headroom, so that is where the next dataset belongs."
    )
    body.append("")
    path.write_text("\n".join(body))
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure the load the plan names")
    parser.add_argument("--items", type=int, default=40)
    parser.add_argument("--postings", type=int, default=400)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--record", default="LOAD-TEST.md")
    parser.add_argument(
        "--only", default="", help="a comma-separated subset of the paths, for a quick run"
    )
    args = parser.parse_args()

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
        dataset = build_dataset(session, items=args.items, postings=args.postings)
        everything = paths_for(session)
    chosen = (
        {name: everything[name] for name in args.only.split(",") if name in everything}
        if args.only
        else everything
    )
    if not chosen:
        print(f"nothing to measure; the paths are {', '.join(TARGETS_MS)}", file=sys.stderr)
        return 2

    report = run_load(
        chosen,
        engine=engine,
        dataset={**dataset, "load_test": "tools/load_test.py"},
        repeats=args.repeats,
        concurrency=args.concurrency,
        causes=CAUSES,
    )
    print(table(report))
    for finding in report["findings"]:
        print(
            f"FINDING {finding['path']}: {finding['measured_ms']:.0f} ms against"
            f" {finding['target_ms']:.0f} ms — {finding['cause']}"
        )
    if args.record:
        written = record(
            report,
            ROOT / args.record,
            command=(
                "DATABASE_URL=postgresql+psycopg://… python tools/load_test.py"
                f" --items {args.items} --postings {args.postings} --repeats {args.repeats}"
                f" --concurrency {args.concurrency}"
            ),
        )
        print(f"recorded in {written}")
    return 1 if report["findings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
