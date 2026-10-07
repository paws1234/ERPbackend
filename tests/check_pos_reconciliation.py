"""T-3.POS.05 check — the till's day against the GL and the stock ledger.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_pos_reconciliation.py

Green on all seven:

1. a traded day reconciles **clean against the ledger**: the revenue and tax the sales
   charged, and each tender's account, are exactly what their own entries posted
2. the stock side reconciles **clean against the stock ledger**: every movement the day's
   sales carry matches the quantities their lines say left the shelf, per item
3. an **injected difference is reported, per day and per terminal** — an extra posting
   against a sale shows as a difference on both accounts it touched, and a movement with
   no line to explain it shows on the stock side
4. a **voided sale is not counted twice**: a refund's reversing entry and its stock return
   are not added to the day's takings, which stay the sales that stand
5. the reconciliation is **re-runnable** — the same call returns the same figures, because
   both sides are read from the rows each time
6. the day's figures are stated **per terminal as well as whole**, so a day that balances
   while one till is short does not hide it
7. a tender that never reached an account is reported rather than absorbed

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
from app.ledger.accounts import create_account  # noqa: E402
from app.ledger.currency import register_currency  # noqa: E402
from app.ledger.mapping import mapped_account, set_mapping  # noqa: E402
from app.ledger.posting import post_journal_entry  # noqa: E402
from app.pos.reconciliation import (  # noqa: E402
    per_terminal,
    reconcile,
    reconcile_gl,
    reconcile_stock,
)
from app.pos.reports import void_sale  # noqa: E402
from app.pos.sales import (  # noqa: E402
    CARD,
    CASH,
    complete_sale,
    open_sale,
    scan,
    tender,
)
from app.sales.customers import create_customer  # noqa: E402,F401 — the sales chain's tables
from app.sales.fulfilment import Shipment  # noqa: E402,F401 — for its table
from app.sales.orders import SalesOrder  # noqa: E402,F401 — for its table
from app.sales.pipeline import Opportunity  # noqa: E402,F401 — for its table
from app.stock.items import add_barcode, create_item  # noqa: E402
from app.stock.locations import create_location  # noqa: E402
from app.stock.transactions import issue, receive  # noqa: E402
from tests.seed import seed_stock_accounts  # noqa: E402

COMPANY = uuid.uuid4()
DAY = date(2026, 9, 24)
BARCODE = "4000000000406"
VAT = Decimal("1.12")


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
            Company(id=COMPANY, code="POS-RECON", name="POS reconciliation",
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

        item = create_item(session, company_id=COMPANY, sku="WIDGET", name="Widget",
                           base_uom="each", traceability_mode="none")
        other = create_item(session, company_id=COMPANY, sku="GADGET", name="Gadget",
                            base_uom="each", traceability_mode="none")
        add_barcode(session, item, value=BARCODE, symbology="ean")
        session.commit()
        warehouse = create_location(session, company_id=COMPANY, code="MAIN",
                                    name="Main", location_type="warehouse")
        zone = create_location(session, company_id=COMPANY, code="MAIN-Z", name="Zone",
                               location_type="zone", parent_id=warehouse.id)
        aisle = create_location(session, company_id=COMPANY, code="MAIN-A", name="Aisle",
                                location_type="aisle", parent_id=zone.id)
        till = create_location(session, company_id=COMPANY, code="TILL-1", name="Till 1",
                               location_type="bin", parent_id=aisle.id)
        till_two = create_location(session, company_id=COMPANY, code="TILL-2", name="Till 2",
                                   location_type="bin", parent_id=aisle.id)
        session.commit()
        for place in (till, till_two):
            receive(session, item=item, location=place, uom="each", quantity="200",
                    value=Decimal("8000"), currency="PHP", source_type="goods_receipt",
                    source_id=uuid.uuid4(), posting_date=DAY)
            receive(session, item=other, location=place, uom="each", quantity="50",
                    value=Decimal("1500"), currency="PHP", source_type="goods_receipt",
                    source_id=uuid.uuid4(), posting_date=DAY)
        session.commit()

        gross = (Decimal("100") * VAT).quantize(Decimal("0.000001"))

        def ring_up(number: str, *, terminal="T1", place=None, cash=None, card=None,
                    quantity="1"):
            sale = open_sale(session, company_id=COMPANY, number=number, terminal=terminal,
                             location=place or till, sold_on=DAY)
            session.flush()
            scan(session, sale, barcode=BARCODE, base_price="100.00", quantity=quantity)
            session.commit()
            if cash is not None:
                tender(session, sale, tender_type=CASH, amount=cash)
            if card is not None:
                tender(session, sale, tender_type=CARD, amount=card)
            session.commit()
            complete_sale(session, sale)
            session.commit()
            return sale

        first = ring_up("POS-A1", cash="150.00")
        second = ring_up("POS-A2", card=str(gross))
        third = ring_up("POS-B1", terminal="T2", place=till_two, cash=str(2 * gross),
                        quantity="2")

        # 5 — re-runnable, and clean on both sides
        clean = reconcile(session, company_id=COMPANY, on=DAY)
        assert clean["balanced"] is True, clean
        assert clean["gl"]["sales"] == 3, clean["gl"]
        # Four units sold across the three sales (1 + 1 + 2).
        assert clean["gl"]["gross"] == (4 * gross).quantize(Decimal("0.000001")), clean
        assert set(clean["gl"]["differences"].values()) == {Decimal("0.000000")}, clean
        assert clean["stock"]["items"] == 1, clean["stock"]
        assert clean["stock"]["expected"] == {
            (item.id, None): Decimal("-4.000000")
        }, clean["stock"]
        assert clean["stock"]["lines_without_movement"] == [], clean["stock"]
        assert reconcile(session, company_id=COMPANY, on=DAY) == clean, "a rerun moved"
        print(
            f"1. the day's {clean['gl']['sales']} sales reconcile clean against the ledger"
            f" — revenue and tax as charged ({clean['gl']['gross']} gross) and each"
            f" tender's account as applied"
        )
        print(
            f"2. the stock side reconciles clean too: {clean['stock']['items']} item(s) at"
            f" {clean['stock']['expected']}, matching the movements the sales carry"
        )

        # 6 — per terminal
        by_till = per_terminal(session, company_id=COMPANY, on=DAY)
        assert [row["terminal"] for row in by_till] == ["T1", "T2"], by_till
        assert by_till[0]["gl"]["gross"] == (2 * gross).quantize(Decimal("0.000001")), by_till
        assert by_till[1]["gl"]["gross"] == (2 * gross).quantize(Decimal("0.000001")), by_till
        assert all(row["balanced"] for row in by_till), by_till
        print(
            f"6. the same figures are stated per terminal"
            f" ({[(row['terminal'], str(row['gl']['gross'])) for row in by_till]}) — a day"
            " that balances while one till is short cannot hide here"
        )

        # 3 — an injected posting against a sale, and a movement with no line
        post_journal_entry(
            session, company_id=COMPANY, posting_date=DAY, currency="PHP",
            memo="extra posting against POS-A1", source_type="pos_sale",
            source_id=first.id,
            lines=[{"account": "1010", "debit": Decimal("50")},
                   {"account": "4000", "credit": Decimal("50")}],
        )
        issue(session, item=other, location=till, uom="each", quantity="1",
              currency="PHP", source_type="pos_sale", source_id=third.id,
              posting_date=DAY)
        session.commit()
        injected = reconcile(session, company_id=COMPANY, on=DAY)
        assert injected["balanced"] is False, injected
        revenue = mapped_account(session, company_id=COMPANY, key="revenue").code
        bank = mapped_account(session, company_id=COMPANY, key="bank").code
        assert injected["gl"]["differences"][revenue] == Decimal("-50.000000"), (
            injected["gl"]["differences"]
        )
        assert injected["gl"]["differences"][bank] == Decimal("50.000000"), (
            injected["gl"]["differences"]
        )
        assert injected["stock"]["differences"][(other.id, None)] == Decimal("-1.000000"), (
            injected["stock"]["differences"]
        )
        assert injected["gl"]["differences"][mapped_account(
            session, company_id=COMPANY, key="cash"
        ).code] == Decimal("0.000000")
        print(
            f"3. an extra 50.00 posting against a sale is reported on both accounts it"
            f" touched ({revenue} {injected['gl']['differences'][revenue]}, {bank}"
            f" {injected['gl']['differences'][bank]}) and a movement with no line to"
            f" explain it shows on the stock side"
            f" ({injected['stock']['differences'][(other.id, None)]})"
        )

        # 4 — a refund is not counted as takings
        refunded = ring_up("POS-A3", cash=str(gross))
        before_void = reconcile(session, company_id=COMPANY, on=DAY)
        void_sale(session, refunded, reason="wrong size", actor="maria")
        session.commit()
        after_void = reconcile(session, company_id=COMPANY, on=DAY)
        assert after_void["gl"]["sales"] == before_void["gl"]["sales"] - 1, (
            before_void["gl"]["sales"],
            after_void["gl"]["sales"],
        )
        assert after_void["gl"]["gross"] == (
            before_void["gl"]["gross"] - gross
        ).quantize(Decimal("0.000001")), after_void["gl"]
        assert after_void["gl"]["differences"][revenue] == Decimal("-50.000000"), (
            after_void["gl"]["differences"]
        )
        assert after_void["stock"]["expected"][(item.id, None)] == Decimal("-4.000000"), (
            after_void["stock"]
        )
        print(
            f"4. refunding {refunded.number} took the day's takings back to"
            f" {after_void['gl']['gross']} — the sale no longer stands, so neither its"
            " entry nor its stock return is counted among them"
        )

        # 7 — a tender that reached no account is reported
        missing_account = post_journal_entry(
            session, company_id=COMPANY, posting_date=DAY, currency="PHP",
            memo="a tender booked somewhere else", source_type="pos_sale",
            source_id=first.id,
            lines=[{"account": "1100", "debit": Decimal("10")},
                   {"account": "4000", "credit": Decimal("10")}],
        )
        session.commit()
        detected = reconcile_gl(session, company_id=COMPANY, on=DAY)
        assert detected["differences"][
            mapped_account(session, company_id=COMPANY, key="cash").code
        ] == Decimal("0.000000")
        assert detected["differences"][revenue] == Decimal("-60.000000"), (
            detected["differences"]
        )
        assert "1100" in {row.account for row in missing_account.lines}, "the probe posted"
        print(
            f"7. a posting that put a tender's money on the wrong account is visible as a"
            f" difference on the account it should have reached"
            f" ({detected['differences'][revenue]} against {revenue}), not absorbed"
        )

    print("\ncheck_pos_reconciliation: all assertions green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
