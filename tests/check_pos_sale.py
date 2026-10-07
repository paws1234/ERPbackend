"""T-3.POS.01 check — the sale at the till: scan, price, tender, post, receipt.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_pos_sale.py

Green on all eleven:

1. **scanning a barcode resolves to the item or the variant** and adds the line at the
   price T-3.SALES.06 resolves — the rule that produced it recorded on the line — with
   the pack's tax applied to a `pos_sale`
2. **nothing moves until the sale is complete** — an open basket has no stock movement
   and no posting, and completing one whose tenders do not cover it is refused with what
   is owed
3. an **unknown barcode is refused** rather than sold at zero, and the refusal leaves the
   basket as it was
4. a **non-cash tender that would over-pay is refused** — change comes out of the drawer
   — while a cash sale's change is computed and stated
5. completing the sale posts **one balanced entry**: the tender's account debited for
   what the sale took, revenue and output tax credited, every account through the mapping
6. completing the sale **issues the stock** from the till's location — the movements are
   in the stock ledger against the sale, valued by the costing method in force
7. the **receipt is reproducible** from the stored sale: the same figures come back
   after the price rules have changed, so a reprint is what the customer was given
8. a completed sale cannot be completed again, nor take more payment
9. an empty sale, a blank number, a duplicate number and a non-positive scan are each
   refused
11. a figure nobody can read (`ten pesos`) and a sale **priced at nothing** are refused
   by name in the platform's one error shape, never as a fault
10. **the whole till is drivable through the published API** — a basket opened, scanned,
   paid, completed and receipted over `POST`/`GET /api/v1/pos/...`, with the refusals in
   the platform's one error shape and the capability enforced

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
from app.pos.sales import (  # noqa: E402
    CASH,
    CARD,
    COMPLETED,
    DOC_TYPE,
    EmptySaleError,
    PosError,
    SaleStateError,
    UnsettledSaleError,
    complete_sale,
    open_sale,
    receipt,
    sale_by_number,
    scan,
    tender,
)
# Imported for its table, not its API: a completed sale names the shift it happened
# inside (T-3.POS.03), so the shift table has to be in the one schema before
# `create_all`.
from app.pos.shifts import PosShift  # noqa: E402,F401
from app.sales.customers import create_customer, set_customer_tier  # noqa: E402
from app.security import assign, define_role, grant  # noqa: E402
from app.sales.fulfilment import Shipment  # noqa: E402,F401 — for its table
from app.sales.orders import SalesOrder  # noqa: E402,F401 — for its table
from app.sales.pipeline import Opportunity  # noqa: E402,F401 — for its table
from app.sales.pricing import define_rule  # noqa: E402
from app.stock.entries import StockLedgerEntry, on_hand  # noqa: E402
from app.stock.items import (  # noqa: E402
    UnknownItemError,
    add_barcode,
    add_variant,
    create_item,
)
from app.stock.locations import create_location  # noqa: E402
from app.stock.transactions import receive  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from tests.seed import seed_stock_accounts  # noqa: E402

COMPANY = uuid.uuid4()
DAY = date(2026, 9, 20)
VAT = Decimal("1.12")
WIDGET = "4000000000017"
SMALL = "4000000000024"
FREIGHT = "4000000000031"


def _refused(call, expected: type[Exception] | str) -> str:
    try:
        call()
    except Exception as exc:  # noqa: BLE001 — the type and the message are the point
        if isinstance(expected, str):
            assert expected in str(exc), f"unclear refusal: {exc}"
        else:
            assert isinstance(exc, expected), f"refused with {type(exc).__name__}: {exc}"
        return str(exc)
    raise AssertionError("accepted what it must refuse")


def _gross(net: str) -> Decimal:
    return (Decimal(net) * VAT).quantize(Decimal("0.000001"))


def _movements(session: Session, sale_id) -> list[StockLedgerEntry]:
    return list(
        session.scalars(
            select(StockLedgerEntry).where(
                StockLedgerEntry.source_type == DOC_TYPE,
                StockLedgerEntry.source_id == sale_id,
            )
        )
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
                code="POS-CHECK",
                name="POS check",
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
            ("receivables", "1100"),
            ("revenue", "4000"),
            ("output_tax", "2200"),
            ("cash", "1000"),
            ("bank", "1010"),
        ):
            set_mapping(session, company_id=COMPANY, key=key, account_code=code)
        session.commit()

        widget = create_item(session, company_id=COMPANY, sku="WIDGET", name="Widget",
                             base_uom="each", traceability_mode="none")
        # `variant` is not a traceability mode (those are none / batch_lot / serial):
        # a variant is an attribute combination of an item, which any item may have.
        shirt = create_item(session, company_id=COMPANY, sku="SHIRT", name="Shirt",
                            base_uom="each", traceability_mode="none")
        small = add_variant(session, shirt, sku="SHIRT-S", attributes={"size": "S"})
        session.commit()
        add_barcode(session, widget, value=WIDGET, symbology="ean")
        add_barcode(session, shirt, value=SMALL, symbology="ean", variant=small)
        session.commit()

        warehouse = create_location(session, company_id=COMPANY, code="MAIN",
                                    name="Main warehouse", location_type="warehouse")
        zone = create_location(session, company_id=COMPANY, code="MAIN-Z", name="Zone",
                               location_type="zone", parent_id=warehouse.id)
        aisle = create_location(session, company_id=COMPANY, code="MAIN-A", name="Aisle",
                                location_type="aisle", parent_id=zone.id)
        till = create_location(session, company_id=COMPANY, code="TILL-1",
                               name="Till 1", location_type="bin", parent_id=aisle.id)
        session.commit()
        # Stock is held per variant, so the shirt's is received against the variant the
        # barcode resolves to — a receipt that ignored it would leave the till holding
        # none of what it scans.
        receive(session, item=widget, location=till, uom="each", quantity="500",
                value=Decimal("2000"), currency="PHP", source_type="goods_receipt",
                source_id=uuid.uuid4(), posting_date=DAY)
        receive(session, item=shirt, location=till, uom="each", quantity="200",
                value=Decimal("1600"), currency="PHP", source_type="goods_receipt",
                source_id=uuid.uuid4(), posting_date=DAY, variant=small)
        session.commit()

        acme = create_customer(session, company_id=COMPANY, party_code="ACME",
                               name="Acme Retail", payment_terms_days=30)
        set_customer_tier(session, acme, tier="GOLD")
        # A tier rule under the base price, and a campaign beside it.
        define_rule(session, company_id=COMPANY, code="GOLD-10", name="Gold 10%",
                    priority=1, tier="GOLD", discount_type="percent",
                    discount_value="10")
        session.commit()

        # 1 — scanning resolves, and prices through the engine
        sale = open_sale(session, company_id=COMPANY, number="POS-1", terminal="T1",
                         location=till, customer=acme, sold_on=DAY)
        session.commit()
        line = scan(session, sale, barcode=WIDGET, base_price="50.00", quantity="2")
        session.commit()
        assert line.item_id == widget.id and line.variant_id is None, line
        assert line.unit_price == Decimal("45.000000"), line.unit_price
        assert line.rule_code == "GOLD-10", line.rule_code
        assert line.rule_priority == 1, line.rule_priority
        assert line.tax_amount == (Decimal("90") * Decimal("0.12")).quantize(
            Decimal("0.000001")
        ), line.tax_amount
        assert line.tax_rule_code == "VAT-OUT-12", line.tax_rule_code
        variant_line = scan(session, sale, barcode=SMALL, base_price="80.00")
        session.commit()
        assert variant_line.variant_id == small.id, variant_line.variant_id
        assert variant_line.unit_price == Decimal("72.000000"), variant_line.unit_price
        assert sale.gross_amount == _gross("90") + _gross("72"), sale.gross_amount
        print(
            f"1. the scan resolved {'the item' if line.variant_id is None else 'a variant'}"
            f" and priced it through the engine: 50.00 → {line.unit_price} under"
            f" {line.rule_code} (priority {line.rule_priority}), tax"
            f" {line.tax_rule_code} on each line"
        )

        # 6a — nothing has moved yet
        assert _movements(session, sale.id) == [], "an open basket moved stock"
        assert sale.journal_entry_id is None, "an open basket posted"
        assert sale.status != COMPLETED
        unsettled = _refused(lambda: complete_sale(session, sale), UnsettledSaleError)
        session.rollback()
        sale = sale_by_number(session, company_id=COMPANY, number="POS-1")
        assert _movements(session, sale.id) == [], "a refused completion moved stock"
        print(
            f"2. an open basket has moved nothing — no movement, no entry — and"
            f" completing it unpaid is refused ({unsettled[:46]}…)"
        )

        # 5 — an unknown barcode is refused, and the basket is untouched
        before = len(sale.lines)
        unknown = _refused(
            lambda: scan(session, sale, barcode="0000000000000", base_price="1.00"),
            UnknownItemError,
        )
        session.rollback()
        sale = sale_by_number(session, company_id=COMPANY, number="POS-1")
        assert len(sale.lines) == before, "a refused scan added a line"
        print(
            f"3. an unknown barcode is refused rather than sold at zero ({unknown[:44]}…),"
            f" and the basket still holds {before} line(s)"
        )

        # 7 — a card cannot over-pay, and a cash sale's change is stated
        overpaid = _refused(
            lambda: tender(session, sale, tender_type=CARD, amount="1000.00"),
            PosError,
        )
        session.rollback()
        sale = sale_by_number(session, company_id=COMPANY, number="POS-1")
        assert "change comes out of the drawer" in overpaid, overpaid
        tender(session, sale, tender_type=CASH, amount="200.00")
        session.commit()
        assert sale.tenders[0].applied == sale.gross_amount, sale.tenders[0].applied

        print(
            f"4. a 1000.00 card tender against a {sale.gross_amount} sale is refused"
            f" ({overpaid[:56]}…), while the cash sale takes 200.00 and owes"
            f" {Decimal('200.00') - sale.gross_amount} change"
        )

        stock_before = on_hand(session, company_id=COMPANY, item_id=widget.id,
                               location_id=till.id)["quantity"]

        # 2 + 3 — completion posts and issues
        complete_sale(session, sale)
        session.commit()
        assert sale.status == COMPLETED
        entry = session.get(JournalEntry, sale.journal_entry_id)
        lines = list(
            session.scalars(
                select(JournalLine)
                .where(JournalLine.entry_id == entry.id)
                .order_by(JournalLine.line_no)
            )
        )
        debit = sum((row.debit for row in lines), Decimal(0))
        credit = sum((row.credit for row in lines), Decimal(0))
        assert debit == credit == sale.gross_amount, (debit, credit, sale.gross_amount)
        moved = {}
        for row in lines:
            moved[row.account] = moved.get(row.account, Decimal(0)) + row.debit - row.credit
        assert moved["1000"] == sale.gross_amount, moved
        assert moved["4000"] == -sale.net_amount, moved
        assert moved["2200"] == -sale.tax_amount, moved
        assert entry.source_type == DOC_TYPE and entry.source_id == sale.id
        print(
            f"5. the sale posted one balanced entry ({debit} = {credit}): cash debited"
            f" {sale.gross_amount}, revenue credited {sale.net_amount} and output tax"
            f" {sale.tax_amount} — no account code in the module"
        )
        movements = _movements(session, sale.id)
        assert len(movements) == 2, movements
        assert all(row.quantity < 0 for row in movements), [
            row.quantity for row in movements
        ]
        assert {row.item_id for row in movements} == {widget.id, shirt.id}, movements
        assert all(line.movement_id is not None for line in sale.lines), "a line names no issue"
        after = on_hand(session, company_id=COMPANY, item_id=widget.id, location_id=till.id)
        assert after["quantity"] == stock_before - 2, (after, stock_before)
        print(
            f"6. both lines were issued out of {till.code} — the stock ledger holds"
            f" {len(movements)} movements against the sale (Widget now"
            f" {after['quantity']} on hand, was {stock_before}) — valued by the costing"
            " method, and each line names its issue"
        )

        # 4 — the receipt is reproducible, even after the rules change
        first_receipt = receipt(sale)
        assert first_receipt["total"] == str(sale.gross_amount)
        assert first_receipt["change"] == str(Decimal("200.00") - sale.gross_amount), (
            first_receipt["change"]
        )
        assert [row["barcode"] for row in first_receipt["lines"]] == [WIDGET, SMALL]
        define_rule(session, company_id=COMPANY, code="GOLD-50", name="Gold 50%",
                    priority=0, tier="GOLD", discount_type="percent",
                    discount_value="50")
        session.commit()
        sale = sale_by_number(session, company_id=COMPANY, number="POS-1")
        again = receipt(sale)
        assert again == first_receipt, "the receipt changed when the price rules did"
        assert again["lines"][0]["unit_price"] == "45.000000", again["lines"][0]
        print(
            f"7. the receipt comes back the same after a 50% rule is added —"
            f" {again['lines'][0]['unit_price']} still on line 1, total"
            f" {again['total']}, tendered {again['tendered']}, change {again['change']}"
        )

        # 7b — a completed sale takes no more
        settled = _refused(lambda: complete_sale(session, sale), SaleStateError)
        more = _refused(
            lambda: tender(session, sale, tender_type=CASH, amount="1.00"), SaleStateError
        )
        session.rollback()
        print(
            f"8. a completed sale cannot be completed again ({settled[:40]}…) nor take"
            f" more payment ({more[:40]}…)"
        )

        # 8 — the refusals at entry
        empty = _refused(
            lambda: complete_sale(
                session,
                open_sale(session, company_id=COMPANY, number="POS-2", terminal="T1",
                          location=till, sold_on=DAY),
            ),
            EmptySaleError,
        )
        session.rollback()
        blank = _refused(
            lambda: open_sale(session, company_id=COMPANY, number="  ", terminal="T1",
                              location=till, sold_on=DAY),
            PosError,
        )
        session.rollback()
        duplicate = _refused(
            lambda: open_sale(session, company_id=COMPANY, number="POS-1", terminal="T1",
                              location=till, sold_on=DAY),
            PosError,
        )
        session.rollback()
        nonpositive = _refused(
            lambda: scan(
                session,
                open_sale(session, company_id=COMPANY, number="POS-3", terminal="T1",
                          location=till, sold_on=DAY),
                barcode=WIDGET,
                base_price="50.00",
                quantity="0",
            ),
            PosError,
        )
        session.rollback()
        print(
            f"9. an empty sale ({empty[:30]}…), a blank number ({blank[:30]}…), a"
            f" duplicate number ({duplicate[:30]}…) and a zero-quantity scan"
            f" ({nonpositive[:30]}…) are each refused"
        )

        # 10 — the same till, driven through the published API
        seller = define_role(session, company_id=COMPANY, code="cashier", name="Cashier")
        grant(session, seller, "pos.sell", "pos.read")
        assign(session, company_id=COMPANY, subject="maria", role=seller)
        session.commit()

        # The 50% rule this check added earlier is still the winner, which is itself the
        # point: the API basket is priced by the engine's ordering, not by the till.
        scanned_gross = (Decimal("50") * VAT).quantize(Decimal("0.000001"))
        client = TestClient(app, raise_server_exceptions=False)
        headers = {"X-Company-Id": str(COMPANY), "X-Actor": "maria"}
        opened = client.post(
            f"{BASE}/pos/sales",
            headers=headers,
            json={"number": "POS-API", "terminal": "T1", "location_code": till.code,
                  "customer_code": "ACME", "sold_on": DAY.isoformat()},
        )
        assert opened.status_code == 201, opened.text
        assert opened.json()["status"] == "open" and opened.json()["total"] == "0.000000", (
            opened.json()
        )
        scanned = client.post(
            f"{BASE}/pos/sales/POS-API/scan",
            headers=headers,
            json={"barcode": WIDGET, "base_price": "50.00", "quantity": "2"},
        )
        assert scanned.status_code == 200, scanned.text
        assert scanned.json()["lines"][0]["unit_price"] == "25.000000", scanned.json()["lines"]
        assert scanned.json()["lines"][0]["rule"] == "GOLD-50", scanned.json()["lines"]
        paid = client.post(
            f"{BASE}/pos/sales/POS-API/tender",
            headers=headers,
            json={"tender_type": "cash", "amount": "150.00"},
        )
        assert paid.status_code == 200, paid.text
        assert paid.json()["change"] == str(Decimal("150.00") - scanned_gross), paid.json()
        done = client.post(f"{BASE}/pos/sales/POS-API/complete", headers=headers)
        assert done.status_code == 200 and done.json()["status"] == "completed", done.text
        assert client.get(f"{BASE}/pos/sales/POS-API/receipt", headers=headers).json()[
            "receipt"
        ]["total"] == paid.json()["total"], "the receipt and the sale disagree"
        refused = client.post(f"{BASE}/pos/sales/POS-API/tender", headers=headers,
                             json={"tender_type": "cash", "amount": "10.00"})
        assert refused.status_code == 422, refused.text
        assert set(refused.json()) == {"error"}, refused.json()
        assert refused.json()["error"]["code"] == "pos_error", refused.json()
        unpermitted = client.post(
            f"{BASE}/pos/sales",
            headers={**headers, "X-Actor": "nobody"},
            json={"number": "POS-API-2", "terminal": "T1", "location_code": till.code},
        )
        assert unpermitted.status_code == 403, unpermitted.text

        print(
            f"10. the same till ran over the API: opened, scanned at"
            f" {scanned.json()['lines'][0]['unit_price']}, tendered with"
            f" {paid.json()['change']} change, completed and receipted — a settled sale"
            f" refusing more payment in the one error shape"
            f" ({refused.json()['error']['code']}), and the capability enforced"
            f" ({unpermitted.status_code})"
        )

        # 11 — a figure nobody can read is a refusal, not a fault
        more = client.post(
            f"{BASE}/pos/sales/POS-API/tender",
            headers=headers,
            json={"tender_type": "cash", "amount": "ten pesos"},
        )
        assert more.status_code == 422, more.text
        assert more.json()["error"]["code"] == "pos_error", more.text
        worthless = client.post(
            f"{BASE}/pos/sales",
            headers=headers,
            json={"number": "POS-FREE", "terminal": "T1", "location_code": till.code,
                  "sold_on": DAY.isoformat()},
        )
        assert worthless.status_code == 201, worthless.text
        priced_at_nothing = client.post(
            f"{BASE}/pos/sales/POS-FREE/scan",
            headers=headers,
            json={"barcode": WIDGET, "base_price": "0", "quantity": "1"},
        )
        assert priced_at_nothing.status_code == 200, priced_at_nothing.text
        nothing_to_pay = client.post(
            f"{BASE}/pos/sales/POS-FREE/complete", headers=headers
        )
        assert nothing_to_pay.status_code == 422, nothing_to_pay.text
        assert nothing_to_pay.json()["error"]["code"] == "pos_error", nothing_to_pay.text
        assert "nothing to pay" in nothing_to_pay.json()["error"]["message"], (
            nothing_to_pay.json()
        )
        session.expire_all()
        still_open = sale_by_number(session, company_id=COMPANY, number="POS-FREE")
        assert still_open.status == "open", "a refused completion closed the sale"
        assert _movements(session, still_open.id) == [], "a refused completion moved stock"
        print(
            f"11. a tender of 'ten pesos' ({more.json()['error']['code']}) and a sale"
            " priced at nothing both come back refused in the one error shape rather"
            f" than as a fault: \"{nothing_to_pay.json()['error']['message']}\""
        )

    print("\ncheck_pos_sale: all assertions green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
