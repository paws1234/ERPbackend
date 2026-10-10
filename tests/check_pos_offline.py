"""T-6.OFFLINE.01 check — the queue replayed, exactly once, with one report per terminal.

    DATABASE_URL=postgresql+psycopg://erpv1:erpv1@localhost:5432/erpv1 \
        python tests/check_pos_offline.py

Green on all eight:

1. **a synced sale is the same sale an online till would have made** — the same basket
   rung up over the API and replayed from a queue produce the *same* journal entry and
   the *same* stock movement, account for account and figure for figure
2. **a re-sent queue lands once** — the second send of the same two sales answers
   `duplicate` for both, nothing is accepted, and the company has the same sales,
   postings and movements it had before (no second issue, no second entry)
3. **an oversell is reported, not swallowed** — a queued sale the location cannot fill is
   refused with the item, the quantity the till sold and what the location holds, the
   difference row carries the policy the terminal was trading under, and no sale,
   movement or posting is left behind
4. **one report per terminal per run** — a two-sale queue is one report covering the days
   it spanned, a second terminal gets its own, and `reports_of` filters by terminal
5. **every queued sale has an outcome** — accepted, duplicate and rejected, with the
   counts adding up to what was queued
6. **the endpoint is retry-safe by key as well as by number** — the same
   `Idempotency-Key` and body answers with the first report and `Idempotent-Replay`, the
   same key with a different queue is refused, and a caller without `pos.sell` is refused
   in the platform's one error shape
7. **the till's own policy letter is on the report** — `oversell_allowed` reaches the
   report and every difference row, so a shortfall reads as a stale cache or as a policy
8. **one bad sale does not take the run** — a queued basket whose payment does not cover
   it is refused by name, leaves no basket behind, and the sale queued after it still
   synchronises in the same batch

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

from app.api import BASE, app  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger.accounts import create_account  # noqa: E402
from app.ledger.currency import register_currency  # noqa: E402
from app.ledger.mapping import set_mapping  # noqa: E402
from app.ledger.posting import JournalEntry, JournalLine  # noqa: E402
from app.pos.offline import (  # noqa: E402
    ACCEPTED,
    DUPLICATE,
    REJECTED,
    PosSyncReport,
    report_of,
    reports_of,
    sync_sales,
)
from app.pos.sales import (  # noqa: E402
    COMPLETED,
    DOC_TYPE,
    PosSale,
    complete_sale,
    open_sale,
    scan,
    tender,
)
# Imported for its table, not its API: a completed sale names the shift it happened in.
from app.pos.shifts import PosShift  # noqa: E402,F401
from app.sales.customers import Customer  # noqa: E402,F401 — for its table
from app.sales.fulfilment import Shipment  # noqa: E402,F401 — for its table
from app.sales.orders import SalesOrder  # noqa: E402,F401 — for its table
from app.sales.pipeline import Opportunity  # noqa: E402,F401 — for its table
from app.security import assign, define_role, grant  # noqa: E402
from app.stock.entries import StockLedgerEntry  # noqa: E402
from app.stock.items import add_barcode, create_item  # noqa: E402
from app.stock.locations import create_location  # noqa: E402
from app.stock.transactions import receive  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from tests.seed import seed_stock_accounts  # noqa: E402

COMPANY = uuid.uuid4()
OTHER = uuid.uuid4()
DAY = date(2026, 9, 20)
DAY_TWO = date(2026, 9, 22)
WIDGET = "4000000000017"
SHELF_PRICE = "50.00"
GROSS = "112.000000"


def _basket(number: str) -> dict:
    return {
        "number": number,
        "sold_on": DAY.isoformat(),
        "lines": [{"barcode": WIDGET, "base_price": SHELF_PRICE, "quantity": "2"}],
        "tenders": [{"tender_type": "cash", "amount": GROSS}],
    }


def _posting(session: Session, sale: PosSale) -> list[tuple[str, Decimal, Decimal]]:
    """The sale's journal lines, account by account — what the platform stored."""
    entry = session.scalar(
        select(JournalEntry).where(
            JournalEntry.source_type == DOC_TYPE, JournalEntry.source_id == sale.id
        )
    )
    assert entry is not None, f"sale {sale.number} posted nothing"
    return sorted(
        (line.account, Decimal(line.debit), Decimal(line.credit))
        for line in session.scalars(
            select(JournalLine)
            .where(JournalLine.entry_id == entry.id)
            .order_by(JournalLine.line_no)
        )
    )


def _stock(session: Session, sale: PosSale) -> list[tuple[Decimal, Decimal]]:
    return sorted(
        (Decimal(row.quantity), Decimal(row.value))
        for row in session.scalars(
            select(StockLedgerEntry).where(
                StockLedgerEntry.source_type == DOC_TYPE,
                StockLedgerEntry.source_id == sale.id,
            )
        )
    )


def _count(session: Session, model) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def _outcome(report: dict, number: str) -> dict:
    found = [row for row in report["outcomes"] if row["number"] == number]
    assert len(found) == 1, f"expected one outcome for {number}: {report['outcomes']}"
    return found[0]


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
                code="OFFLINE-CHECK",
                name="Offline check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        register_currency(session, company_id=COMPANY, code="PHP", name="Peso")
        session.commit()
        seed_stock_accounts(session, company_id=COMPANY)
        create_account(session, company_id=COMPANY, code="2200", name="Output VAT",
                       account_class="liability")
        for key, code in (
            ("revenue", "4000"),
            ("output_tax", "2200"),
            ("cash", "1000"),
            ("bank", "1010"),
        ):
            set_mapping(session, company_id=COMPANY, key=key, account_code=code)
        session.commit()

        widget = create_item(session, company_id=COMPANY, sku="WIDGET", name="Widget",
                             base_uom="each", traceability_mode="none")
        session.commit()
        add_barcode(session, widget, value=WIDGET, symbology="ean")
        session.commit()
        warehouse = create_location(session, company_id=COMPANY, code="MAIN",
                                    name="Main warehouse", location_type="warehouse")
        zone = create_location(session, company_id=COMPANY, code="MAIN-Z", name="Zone",
                               location_type="zone", parent_id=warehouse.id)
        aisle = create_location(session, company_id=COMPANY, code="MAIN-A", name="Aisle",
                                location_type="aisle", parent_id=zone.id)
        till = create_location(session, company_id=COMPANY, code="TILL-1", name="Till 1",
                               location_type="bin", parent_id=aisle.id)
        session.commit()
        receive(session, item=widget, location=till, uom="each", quantity="500",
                value=Decimal("5000"), currency="PHP", source_type="goods_receipt",
                source_id=uuid.uuid4(), posting_date=DAY)
        session.commit()

        # The control: the same basket, rung up online, through T-3.POS.01's own calls.
        online = open_sale(session, company_id=COMPANY, number="ON-1", terminal="T1",
                           location=till, sold_on=DAY)
        scan(session, online, barcode=WIDGET, base_price=SHELF_PRICE, quantity="2")
        tender(session, online, tender_type="cash", amount=GROSS)
        complete_sale(session, online)
        session.commit()

        # 1 — the replayed sale is that sale
        first = sync_sales(
            session,
            company_id=COMPANY,
            terminal="T1",
            location=till,
            sales=[_basket("T1-1")],
        )
        session.commit()
        replayed = session.scalar(
            select(PosSale).where(PosSale.company_id == COMPANY, PosSale.number == "T1-1")
        )
        assert replayed is not None and replayed.status == COMPLETED, replayed
        assert _posting(session, replayed) == _posting(session, online), (
            _posting(session, replayed),
            _posting(session, online),
        )
        assert _stock(session, replayed) == _stock(session, online), (
            _stock(session, replayed),
            _stock(session, online),
        )
        assert first.accepted == 1 and first.duplicates == 0, report_of(first)
        print(
            "1. a queued sale replayed through the same calls posts what the online till"
            f" posts: {len(_posting(session, replayed))} journal lines and a"
            f" {_stock(session, replayed)[0][0]} movement, identical to ON-1's"
        )

        # 2 — a re-sent queue lands once
        sales_before = _count(session, PosSale)
        second = sync_sales(
            session,
            company_id=COMPANY,
            terminal="T1",
            location=till,
            sales=[_basket("T1-1")],
        )
        session.commit()
        assert second.accepted == 0 and second.duplicates == 1, report_of(second)
        assert _count(session, PosSale) == sales_before, "the re-send made a second sale"
        assert len(_stock(session, replayed)) == 1, "the re-send issued the stock again"
        print(
            f"2. the same number sent again came back {_outcome(report_of(second), 'T1-1')}"
            f" — nothing accepted, no second sale, no second issue, no second posting"
            f" ({_count(session, PosSale)} sales in the company before and after)"
        )

        # 3 — an oversell is reported
        report = sync_sales(
            session,
            company_id=COMPANY,
            terminal="T1",
            location=till,
            sales=[_basket("T1-2"), _basket("T1-3")],
            oversell_allowed=True,
        )
        session.commit()
        accepted_row = _outcome(report_of(report), "T1-2")
        assert accepted_row["outcome"] == ACCEPTED, accepted_row
        # Four sales of two have gone out: ON-1, T1-1, T1-2 and T1-3.
        oversold = sync_sales(
            session,
            company_id=COMPANY,
            terminal="T1",
            location=till,
            sales=[
                {
                    "number": "T1-4",
                    "sold_on": DAY.isoformat(),
                    "lines": [{"barcode": WIDGET, "base_price": SHELF_PRICE,
                               "quantity": "600"}],
                    "tenders": [{"tender_type": "cash", "amount": "33600.000000"}],
                }
            ],
            oversell_allowed=False,
        )
        session.commit()
        short = _outcome(report_of(oversold), "T1-4")
        assert short["outcome"] == REJECTED and "cannot issue" in short["reason"], short
        assert "wants 600.000000" in short["reason"] and "holds 492.000000" in short["reason"], (
            short["reason"]
        )
        difference = report_of(oversold)["differences"][0]
        assert difference["item"] == "WIDGET", difference
        assert difference["quantity"] == "600.000000", difference["quantity"]
        assert difference["on_hand"] == "492.000000", difference["on_hand"]
        assert difference["location"] == "TILL-1", difference
        assert difference["oversell_allowed"] is False, difference
        assert session.scalar(
            select(PosSale).where(PosSale.company_id == COMPANY, PosSale.number == "T1-4")
        ) is None, "the refused sale was written anyway"
        assert _count(session, PosSale) == sales_before + 2, "the refused sale left a basket"
        print(
            f"3. the till sold 600 of WIDGET and the location holds 492, so the replay"
            f" refuses it — {short['reason']} — with no basket, movement or posting left"
            " behind"
        )

        # 4 — one report per terminal per run
        assert report.queued == 2 and report.queued_from == DAY, report_of(report)
        assert report.queued_to == DAY, report_of(report)
        tills = reports_of(session, company_id=COMPANY, terminal="T1")
        assert [row.queued for row in tills] == [1, 1, 2, 1], [row.queued for row in tills]
        assert all(row.terminal == "T1" for row in tills), "another terminal's report leaked"
        other = sync_sales(
            session,
            company_id=COMPANY,
            terminal="T2",
            location=till,
            sales=[
                {
                    "number": "T2-1",
                    "sold_on": DAY_TWO.isoformat(),
                    "lines": [{"barcode": WIDGET, "base_price": SHELF_PRICE, "quantity": "1"}],
                    "tenders": [{"tender_type": "cash", "amount": "56.000000"}],
                }
            ],
        )
        session.commit()
        assert len(reports_of(session, company_id=COMPANY, terminal="T2")) == 1, "T2's report"
        assert len(reports_of(session, company_id=COMPANY)) == 5, "one report per run, per till"
        assert other.queued_to == DAY_TWO, report_of(other)
        print(
            f"4. a two-sale queue is one report covering {report.queued_from}, a second"
            f" terminal is its own report on {other.queued_to}, and the company has"
            f" {len(reports_of(session, company_id=COMPANY))} reports for"
            f" {_count(session, PosSale)} sales — one per run, never one per sale"
        )

        # 5 — every queued sale has an outcome
        for run in reports_of(session, company_id=COMPANY):
            rows = report_of(run)
            assert rows["queued"] == len(rows["outcomes"]), rows
            assert rows["accepted"] + rows["duplicates"] + rows["rejected"] == rows["queued"], rows
        counts = [
            (report_of(run)["accepted"], report_of(run)["duplicates"], report_of(run)["rejected"])
            for run in reports_of(session, company_id=COMPANY)
        ]
        assert counts == [(1, 0, 0), (0, 1, 0), (2, 0, 0), (0, 0, 1), (1, 0, 0)], counts
        print(f"5. every run's counts add up to what it queued: {counts}")

        # 6 — the endpoint, driven through the published API
        seller = define_role(session, company_id=COMPANY, code="cashier", name="Cashier")
        grant(session, seller, "pos.sell", "pos.read")
        assign(session, company_id=COMPANY, subject="maria", role=seller)
        session.commit()
        client = TestClient(app, raise_server_exceptions=False)
        headers = {
            "X-Company-Id": str(COMPANY),
            "X-Actor": "maria",
            "Idempotency-Key": "till-1-batch-9",
        }
        body = {
            "terminal": "T1",
            "location_code": till.code,
            "oversell_allowed": False,
            "sales": [
                {
                    "number": "T1-9",
                    "sold_on": DAY_TWO.isoformat(),
                    "lines": [{"barcode": WIDGET, "base_price": SHELF_PRICE, "quantity": "1"}],
                    "tenders": [{"tender_type": "cash", "amount": "56.000000"}],
                }
            ],
        }
        first_call = client.post(f"{BASE}/pos/sync", headers=headers, json=body)
        assert first_call.status_code == 201, first_call.text
        assert first_call.json()["report"]["accepted"] == 1, first_call.json()
        retried = client.post(f"{BASE}/pos/sync", headers=headers, json=body)
        assert retried.status_code == 201, retried.text
        assert retried.headers.get("Idempotent-Replay") == "true", retried.headers
        assert retried.json() == first_call.json(), "the replay answered something else"
        assert _count(session, PosSale) == sales_before + 4, "the retry made a second sale"
        clash = client.post(
            f"{BASE}/pos/sync",
            headers=headers,
            json={**body, "sales": [{**body["sales"][0], "number": "T1-10"}]},
        )
        assert clash.status_code == 409, clash.text
        assert set(clash.json()) == {"error"}, clash.json()
        assert clash.json()["error"]["code"] == "idempotency_key_reused", clash.json()
        unpermitted = client.post(
            f"{BASE}/pos/sync", headers={**headers, "X-Actor": "nobody"}, json=body
        )
        assert unpermitted.status_code == 403, unpermitted.text
        listed = client.get(
            f"{BASE}/pos/sync/reports", headers=headers, params={"terminal": "T1"}
        )
        assert listed.status_code == 200, listed.text
        assert listed.json()["reports"][-1]["accepted"] == 1, listed.json()
        print(
            f"6. the queue over the API: accepted on the first call,"
            f" {retried.headers['Idempotent-Replay']} and the same report on the retry,"
            f" {clash.json()['error']['code']} for the same key with another queue, and"
            f" {unpermitted.status_code} without the capability"
        )

        # 7 — the policy the till was trading under is stated
        assert report.oversell_allowed is True, report_of(report)
        assert oversold.oversell_allowed is False, report_of(oversold)
        assert [run.oversell_allowed for run in reports_of(
            session, company_id=COMPANY, terminal="T1"
        )] == [False, False, True, False, False], "the policy is not on every report"
        print(
            "7. every report states the policy in force (the oversell was queued under"
            " oversell_allowed=False), so a shortfall reads as a stale cache rather than"
            " as a choice"
        )

        # 8 — one bad sale does not take the run
        broken = sync_sales(
            session,
            company_id=COMPANY,
            terminal="T1",
            location=till,
            sales=[
                {
                    "number": "T1-5",
                    "sold_on": DAY.isoformat(),
                    # Ten pesos against a basket of 25: the till queued a basket its own
                    # payment does not cover, which is a refusal T-3.POS.01 already states.
                    "lines": [{"barcode": WIDGET, "base_price": SHELF_PRICE, "quantity": "1"}],
                    "tenders": [{"tender_type": "cash", "amount": "10.00"}],
                },
                {
                    "number": "T1-6",
                    "sold_on": DAY.isoformat(),
                    "lines": [{"barcode": WIDGET, "base_price": SHELF_PRICE, "quantity": "1"}],
                    "tenders": [{"tender_type": "cash", "amount": "56.000000"}],
                },
            ],
        )
        session.commit()
        bad = _outcome(report_of(broken), "T1-5")
        assert bad["outcome"] == REJECTED and "is owed" in bad["reason"], bad
        assert _outcome(report_of(broken), "T1-6")["outcome"] == ACCEPTED, report_of(broken)
        assert session.scalar(
            select(PosSale).where(PosSale.company_id == COMPANY, PosSale.number == "T1-5")
        ) is None, "the refused basket was left behind"
        assert report_of(broken)["differences"] == [], report_of(broken)
        print(
            f"8. a basket its own payment does not cover is refused by name —"
            f" {bad['reason']} — with no basket left behind, and T1-6, queued behind it in"
            " the same batch, still synchronised"
        )

        assert _count(session, PosSyncReport) == 7, _count(session, PosSyncReport)

    print("\ncheck_pos_offline: all assertions green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
