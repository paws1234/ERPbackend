"""T-1.X.GATE — the Phase 1 exit check.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/erpv1 \
        python tests/check_phase1_exit.py

The plan's Phase 1 exit criteria (amended 2026-09-17) are *"ability to post balanced
entries and maintain accurate stock valuation"* and *"batch/lot and serial identity
is enforced on every movement of a tracked item"*. This script builds one dataset
of every Phase 1 kind of work and then verifies each criterion of the gate's own
list against it:

1. balanced entries post from more than one source module through a single
   interface, and an unbalanced one is refused — §6 metric 1
2. the ledger-integrity check reports zero imbalances over the whole dataset
3. stock valuation is accurate under each costing method, after receipts, issues,
   transfers and adjustments
4. stock valuation matches the GL inventory account on the same period — §6 metric 2
5. period locking prevents a back-dated posting into a closed period
6. a foreign-currency document posts with its dated rate, and realized/unrealized
   gain/loss post balanced entries
7. the Trial Balance balances and the Balance Sheet balances for the dataset
8. batch/lot and serial identity is enforced on every movement of a tracked item,
   and untracked items are unaffected

It writes nothing outside the scratch database and asserts nothing it does not
compute.

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import os
import pathlib
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
from app.ledger.fx_gain_loss import revalue_open_balance, settle_document  # noqa: E402
from app.ledger.gl import account_balance  # noqa: E402
from app.ledger.mapping import set_mapping  # noqa: E402
from app.ledger.periods import PeriodLockedError, lock_period  # noqa: E402
from app.ledger.posting import (  # noqa: E402
    JournalEntry,
    UnbalancedEntryError,
    post_journal_entry,
)
from app.ledger.statements import balance_sheet, trial_balance  # noqa: E402
from app.stock.batches import create_batch  # noqa: E402
from app.stock.counts import post_adjustment, record_count, start_count  # noqa: E402
from app.stock.entries import on_hand  # noqa: E402
from app.stock.gl_posting import reconcile  # noqa: E402
from app.stock.items import TraceabilityError, create_item  # noqa: E402
from app.stock.locations import create_location  # noqa: E402
from app.stock.serials import add_serial  # noqa: E402
from app.stock.transactions import issue, receive, transfer  # noqa: E402
from app.stock.valuation import valuation, value_issue  # noqa: E402
from app.workflow import configure  # noqa: E402
from tests.check_ledger_integrity import ledger_gate  # noqa: E402
from tests.check_posting_interface import writers_in  # noqa: E402
from tests.seed import seed_stock_accounts  # noqa: E402

COMPANY = uuid.uuid4()
D1, D2 = date(2026, 9, 5), date(2026, 9, 20)
START, END = date(2026, 9, 1), date(2026, 9, 30)
BOOKED, HIGHER = Decimal("58.5000000000"), Decimal("59.7500000000")
CHECKS: list[str] = []


def _bin(session: Session, code: str, parent: str, kind: str = "bin") -> object:
    return create_location(
        session, company_id=COMPANY, code=code, name=code, location_type=kind, parent_id=parent
    )


def main() -> int:  # noqa: C901 — it is a gate: one function per criterion, in order
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is required (a scratch Postgres)", file=sys.stderr)
        return 2

    engine = create_engine(url)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    Base.metadata.create_all(engine)
    app_root = pathlib.Path(__file__).resolve().parent.parent
    app_files = {
        str(path.relative_to(app_root)): path.read_text()
        for path in sorted((app_root / "app").rglob("*.py"))
    }

    with Session(engine) as session:
        session.add(
            Company(
                id=COMPANY,
                code="PHASE1-GATE",
                name="Phase 1 exit check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()
        seed_stock_accounts(session, company_id=COMPANY)
        set_mapping(session, company_id=COMPANY, key="fx_gain", account_code="4910")
        set_mapping(session, company_id=COMPANY, key="fx_loss", account_code="5990")
        configure(
            session,
            company_id=COMPANY,
            doc_type="inventory_adjustment",
            name="Adjustments",
            levels=[(Decimal("5000"), "controller")],
        )
        register_currency(session, company_id=COMPANY, code="PHP", name="Philippine Peso")
        register_currency(session, company_id=COMPANY, code="USD", name="US Dollar")
        for on, rate in ((D1, BOOKED), (D2, HIGHER)):
            store_rate(
                session, company_id=COMPANY, base_currency="PHP", currency="USD", on=on,
                rate=rate, today=D2,
            )
        session.commit()

        bolt = create_item(
            session, company_id=COMPANY, sku="BOLT", name="Bolt", base_uom="each",
            traceability_mode="none", standard_cost=Decimal("6.00"),
        )
        loose = create_item(
            session, company_id=COMPANY, sku="NUT", name="Nut", base_uom="each",
            traceability_mode="none",
        )
        milk = create_item(
            session, company_id=COMPANY, sku="MILK", name="Milk", base_uom="each",
            traceability_mode="batch_lot",
        )
        pump = create_item(
            session, company_id=COMPANY, sku="PUMP", name="Pump", base_uom="each",
            traceability_mode="serial",
        )
        lot = create_batch(session, item=milk, code="MILK-1", expiry_date=date(2027, 1, 31))
        unit = add_serial(session, item=pump, code="SN-1")
        warehouse = create_location(
            session, company_id=COMPANY, code="WH1", name="Main", location_type="warehouse"
        )
        zone = _bin(session, "WH1-Z", warehouse.id, "zone")
        aisle = _bin(session, "WH1-Z-A", zone.id, "aisle")
        bin_one = _bin(session, "B1", aisle.id)
        bin_two = _bin(session, "B2", aisle.id)
        session.commit()

        # ---- the dataset: every Phase 1 kind of work, once ----
        receive(
            session, item=bolt, location=bin_one, uom="each", quantity=100,
            value=Decimal("1000.00"), currency="PHP", source_type="goods_receipt",
            source_id=uuid.uuid4(), posting_date=D1,
        )
        issue(
            session, item=bolt, location=bin_one, uom="each", quantity=10, currency="PHP",
            source_type="stock_issue", source_id=uuid.uuid4(), posting_date=D1,
        )
        transfer(
            session, item=bolt, from_location=bin_one, to_location=bin_two, uom="each",
            quantity=30, currency="PHP", source_type="stock_transfer", source_id=uuid.uuid4(),
            posting_date=D1,
        )
        count = start_count(session, company_id=COMPANY, location=bin_one, actor="alice")
        record_count(session, count, item=bolt, counted_quantity=55)
        post_adjustment(session, count)
        receive(
            session, item=milk, location=bin_one, uom="each", quantity=8,
            value=Decimal("400.00"), currency="PHP", source_type="goods_receipt",
            source_id=uuid.uuid4(), posting_date=D1, batch=lot,
        )
        issue(
            session, item=milk, location=bin_one, uom="each", quantity=3, currency="PHP",
            source_type="stock_issue", source_id=uuid.uuid4(), posting_date=D1, batch=lot,
        )
        receive(
            session, item=pump, location=bin_one, uom="each", quantity=1,
            value=Decimal("900.00"), currency="PHP", source_type="goods_receipt",
            source_id=uuid.uuid4(), posting_date=D1, serial=unit,
        )
        receive(
            session, item=loose, location=bin_one, uom="each", quantity=5,
            value=Decimal("50.00"), currency="PHP", source_type="goods_receipt",
            source_id=uuid.uuid4(), posting_date=D1,
        )
        # a manual entry from the ledger module, and a foreign-currency document
        post_journal_entry(
            session, company_id=COMPANY, posting_date=D1, currency="PHP",
            source_type="manual", source_id=uuid.uuid4(),
            lines=[{"account": "1000", "debit": Decimal("5000.00")},
                   {"account": "3000", "credit": Decimal("5000.00")}],
        )
        vendor_bill = uuid.uuid4()
        post_journal_entry(
            session, company_id=COMPANY, posting_date=D1, currency="USD",
            source_type="supplier_invoice", source_id=vendor_bill,
            lines=[{"account": "5000", "debit": Decimal("100.00")},
                   {"account": "2000", "credit": Decimal("100.00")}],
        )
        settle_document(
            session, company_id=COMPANY, document_type="supplier_invoice",
            document_id=vendor_bill, account_code="2000", settlement_rate="59.75",
            settlement_date=D2,
        )
        # an open foreign receivable, which the period end revalues (the settlement
        # above belongs to the payable, and re-measuring the same movement twice is
        # not what either calculation is for)
        sales_invoice = uuid.uuid4()
        post_journal_entry(
            session, company_id=COMPANY, posting_date=D1, currency="USD",
            source_type="sales_invoice", source_id=sales_invoice,
            lines=[{"account": "1100", "debit": Decimal("100.00")},
                   {"account": "4000", "credit": Decimal("100.00")}],
        )
        revalue_open_balance(
            session, company_id=COMPANY, account_code="1100", currency="USD", as_of=D2
        )
        session.commit()

        # 1 — one interface, several modules, refusals included
        writers = writers_in(app_files, "app")
        assert writers == ["app/ledger/posting.py"], f"another writer exists: {writers}"
        try:
            post_journal_entry(
                session, company_id=COMPANY, posting_date=D2, currency="PHP",
                lines=[{"account": "1000", "debit": Decimal("1")},
                       {"account": "4000", "credit": Decimal("2")}],
            )
        except UnbalancedEntryError as exc:
            refusal = str(exc)
        else:
            raise AssertionError("an unbalanced entry was accepted")
        session.rollback()
        CHECKS.append(f"one writer ({writers[0]}); unbalanced refused ({refusal[:34]}…)")

        # 2 — the gate itself
        with engine.connect() as connection:
            assert ledger_gate(connection) == 0, "the ledger has an imbalance"
        CHECKS.append("ledger-gate: zero imbalances over the whole dataset")

        # 3 — the three methods, after receipts, issues, transfers and adjustments
        average = valuation(session, company_id=COMPANY, item=bolt)
        fifo = valuation(session, company_id=COMPANY, item=bolt, method="fifo")
        standard = valuation(session, company_id=COMPANY, item=bolt, method="standard_cost")
        assert on_hand(session, company_id=COMPANY, item_id=bolt.id)["quantity"] == Decimal(85)
        assert (average["quantity"], fifo["quantity"], standard["quantity"]) == (
            "85.000000", "85.000000", "85.000000",
        ), (average, fifo, standard)
        assert average["value"] == "850.000000", average
        assert fifo["value"] == "850.000000", fifo
        assert standard["value"] == "510.000000", standard
        assert value_issue(
            session, company_id=COMPANY, item=bolt, quantity=Decimal("5")
        ) == Decimal("50.000000")
        CHECKS.append(
            f"85 bolt: moving average {average['value']}, FIFO {fifo['value']},"
            f" standard cost {standard['value']}"
        )

        # 4 — valuation against the GL inventory account
        stock_value = on_hand(session, company_id=COMPANY)["value"]
        report = reconcile(session, company_id=COMPANY, start=START, end=END)
        assert report["balanced"], report
        assert account_balance(session, company_id=COMPANY, account_code="1200") == stock_value
        CHECKS.append(f"valuation {report['stock_value']} = GL 1200; difference {report['difference']}")

        # 5 — a closed period takes no posting
        lock_period(
            session, company_id=COMPANY, year=2026, month=9, actor="alice",
            reason="September is reported",
        )
        session.commit()
        try:
            receive(
                session, item=loose, location=bin_one, uom="each", quantity=1,
                value=Decimal("10.00"), currency="PHP", source_type="goods_receipt",
                source_id=uuid.uuid4(), posting_date=D2,
            )
        except PeriodLockedError as exc:
            locked = str(exc)
        else:
            raise AssertionError("a posting landed inside a closed period")
        session.rollback()
        assert on_hand(session, company_id=COMPANY, item_id=loose.id)["quantity"] == Decimal(5)
        CHECKS.append(f"2026-09 locked: {locked[:44]}…")

        # 6 — the dated rate, and both gain/loss postings balanced
        realised = [
            entry
            for entry in session.scalars(select(JournalEntry))
            if entry.source_type == "supplier_invoice_fx_settlement"
        ]
        assert realised, "the settlement posted nothing"
        debits = sum((line.debit for line in realised[0].lines), Decimal(0))
        credits = sum((line.credit for line in realised[0].lines), Decimal(0))
        assert debits == credits == Decimal("125.000000"), (debits, credits)
        assert account_balance(session, company_id=COMPANY, account_code="5990") == Decimal(
            "125.000000"
        ), "the realized loss is not where the settlement put it"
        # debit-positive: an expense is positive, so a credited gain is negative.
        assert account_balance(session, company_id=COMPANY, account_code="4910") == Decimal(
            "-125.000000"
        ), "the unrealized gain is not where the revaluation put it"
        CHECKS.append(
            f"USD document at the dated rate: realized {debits} loss, unrealized 125.000000 gain"
        )

        # 7 — the statements
        trial = trial_balance(session, company_id=COMPANY, start=START, end=END)
        sheet = balance_sheet(session, company_id=COMPANY, as_of=END)
        assert trial["balanced"] and trial["total_debit"] == trial["total_credit"], trial
        assert sheet["balanced"], sheet
        CHECKS.append(
            f"trial balance {trial['total_debit']} = {trial['total_credit']};"
            f" balance sheet {sheet['total_assets']} = {sheet['total_liabilities']}"
            f" + {sheet['total_equity']}"
        )

        # 8 — identity on every movement of a tracked item, untracked unaffected
        try:
            issue(
                session, item=milk, location=bin_one, uom="each", quantity=1, currency="PHP",
                source_type="stock_issue", source_id=uuid.uuid4(), posting_date=D1,
            )
        except TraceabilityError as exc:
            batch_refusal = str(exc)
        else:
            raise AssertionError("a batch-tracked item moved without a batch")
        session.rollback()
        try:
            issue(
                session, item=pump, location=bin_one, uom="each", quantity=1, currency="PHP",
                source_type="stock_issue", source_id=uuid.uuid4(), posting_date=D1,
            )
        except TraceabilityError as exc:
            serial_refusal = str(exc)
        else:
            raise AssertionError("a serial-tracked item moved without its unit")
        session.rollback()
        assert on_hand(session, company_id=COMPANY, item_id=milk.id, batch_id=lot.id)[
            "quantity"
        ] == Decimal(5)
        assert on_hand(session, company_id=COMPANY, item_id=bolt.id)["quantity"] == Decimal(85)
        CHECKS.append(
            f"batch and serial identity enforced ({batch_refusal[:26]}… / {serial_refusal[:26]}…)"
        )

    engine.dispose()
    for index, line in enumerate(CHECKS, start=1):
        print(f"{index}. {line}")
    print("ok — every Phase 1 exit criterion holds in the tree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
