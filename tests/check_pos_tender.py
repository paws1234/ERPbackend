"""T-3.POS.02 check — tendering and the drawer.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_pos_tender.py

Green on all eight:

1. a sale settles **only when the tenders cover it**: an under-paid sale is refused with
   what is owed, and a card that would over-pay is refused because change comes out of
   the drawer
2. **change is computed correctly** — a cash sale's change is what was handed over less
   what the sale took, and the drawer's expected cash follows it
3. a **split payment records each tender separately with its own settlement path** — cash
   to the drawer's account and the card to the bank's, each with its own line, and the
   terminal's reference carried with it
4. **drawer movements outside a sale are recorded with a reason** and move the drawer's
   expected cash: a paid-in raises it, a paid-out lowers it
5. a movement with no reason, no actor or a non-positive amount is **refused** — cash
   that left the drawer unexplained cannot be counted by anybody
6. the **tender breakdown** states what each path took, kept apart rather than lumped
   into one number
7. the drawer's expected cash is **derived from the documents** every time — from the
   sales, their change and their movements — so it cannot drift from what they say

5b. an amount nobody can read (`zzz`, `NaN`, `Infinity`) is refused by name, from the
   service and over the API in the platform's one error shape

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, select
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.company import Company  # noqa: E402
from app.api import BASE, app  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger.accounts import create_account  # noqa: E402
from app.ledger.currency import register_currency  # noqa: E402
from app.ledger.mapping import set_mapping  # noqa: E402
from app.ledger.posting import JournalEntry, JournalLine  # noqa: E402
from app.security import assign, define_role, grant  # noqa: E402
from app.pos.drawer import (  # noqa: E402
    DrawerError,
    cash_sales_total,
    change_paid,
    drawer_state,
    movement_total,
    movements_for,
    paid_in,
    paid_out,
    tender_breakdown,
)
from app.pos.sales import (  # noqa: E402
    CARD,
    CASH,
    GATEWAY,
    PosError,
    PosSale,
    UnsettledSaleError,
    complete_sale,
    open_sale,
    scan,
    tender,
)
# Imported for its table, not its API: a completed sale names the shift it happened
# inside (T-3.POS.03), so the shift table has to be in the one schema before
# `create_all`.
from app.pos.shifts import PosShift  # noqa: E402,F401
from app.sales.customers import create_customer  # noqa: E402
from app.sales.fulfilment import Shipment  # noqa: E402,F401 — for its table
from app.sales.orders import SalesOrder  # noqa: E402,F401 — for its table
from app.sales.pipeline import Opportunity  # noqa: E402,F401 — for its table
from app.stock.items import add_barcode, create_item  # noqa: E402
from app.stock.locations import create_location  # noqa: E402
from app.stock.transactions import receive  # noqa: E402
from tests.seed import seed_stock_accounts  # noqa: E402

COMPANY = uuid.uuid4()
DAY = date(2026, 9, 21)
BARCODE = "4000000000109"
VAT = Decimal("1.12")


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


def _paid(session, till, number: str, *, cash=None, card=None, gateway=None):
    """Ring up one sale of a single priced line and pay it however is asked."""
    sale = open_sale(session, company_id=COMPANY, number=number, terminal="T1",
                     location=till, sold_on=DAY)
    session.flush()
    scan(session, sale, barcode=BARCODE, base_price="100.00")
    session.commit()
    for kind, amount in ((CASH, cash), (CARD, card), (GATEWAY, gateway)):
        if amount is not None:
            tender(session, sale, tender_type=kind, amount=amount,
                   reference=None if kind == CASH else f"{kind.upper()}-1")
    session.commit()
    complete_sale(session, sale)
    session.commit()
    return sale


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
            Company(id=COMPANY, code="POS-TENDER", name="POS tender", base_currency="PHP",
                    fiscal_year_start_month=1)
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
        session.commit()
        receive(session, item=item, location=till, uom="each", quantity="100",
                value=Decimal("4000"), currency="PHP", source_type="goods_receipt",
                source_id=uuid.uuid4(), posting_date=DAY)
        session.commit()

        gross = (Decimal("100") * VAT).quantize(Decimal("0.000001"))

        # 1 — under-tender refused, over-tender on a card refused
        unpaid = open_sale(session, company_id=COMPANY, number="POS-U1", terminal="T1",
                           location=till, sold_on=DAY)
        session.flush()
        scan(session, unpaid, barcode=BARCODE, base_price="100.00")
        session.commit()
        tender(session, unpaid, tender_type=CASH, amount="10.00")
        session.commit()
        owed = _refused(lambda: complete_sale(session, unpaid), UnsettledSaleError)
        session.rollback()
        card_over = _refused(
            lambda: tender(
                session,
                open_sale(session, company_id=COMPANY, number="POS-U2", terminal="T1",
                          location=till, sold_on=DAY),
                tender_type=CARD,
                amount="10000.00",
            ),
            PosError,
        )
        session.rollback()
        assert "change comes out of the drawer" in card_over or "over-pay" in card_over, (
            card_over
        )
        print(
            f"1. an under-paid sale is refused with what it is owed ({owed[:44]}…) and a"
            f" card that would over-pay is refused ({card_over[:44]}…)"
        )

        # 2 — change, and what it does to the drawer
        cash_sale = _paid(session, till, "POS-CASH", cash="120.00")
        assert cash_sale.gross_amount == gross, cash_sale.gross_amount
        change = Decimal("120.00") - gross
        assert change_paid([cash_sale]) == change, change_paid([cash_sale])
        assert cash_sales_total([cash_sale]) == Decimal("120.000000"), cash_sales_total(
            [cash_sale]
        )
        state = drawer_state([cash_sale], [])
        assert state["expected"] == gross, state
        assert state["change_paid"] == change, state
        print(
            f"2. the 120.00 cash sale took {gross} and gave {change} back — the drawer's"
            f" expected cash is {state['expected']}, which is the sale, not the note"
        )

        # 3 — a split payment, each tender on its own path
        split = open_sale(session, company_id=COMPANY, number="POS-SPLIT", terminal="T1",
                          location=till, sold_on=DAY)
        session.flush()
        scan(session, split, barcode=BARCODE, base_price="100.00")
        session.commit()
        tender(session, split, tender_type=CASH, amount="50.00")
        tender(session, split, tender_type=CARD, amount=str(gross - Decimal("50")),
               reference="AUTH-99")
        session.commit()
        complete_sale(session, split)
        session.commit()
        assert [row.tender_type for row in split.tenders] == [CASH, CARD], split.tenders
        entry = session.get(JournalEntry, split.journal_entry_id)
        lines = list(
            session.scalars(
                select(JournalLine).where(JournalLine.entry_id == entry.id)
            )
        )
        by_account: dict[str, Decimal] = {}
        for row in lines:
            by_account[row.account] = by_account.get(row.account, Decimal(0)) + row.debit
        assert by_account["1000"] == Decimal("50.000000"), by_account
        assert by_account["1010"] == (gross - Decimal("50")), by_account
        # The authorisation is on the tender, and the entry is found from the sale it
        # posted for — so a card settling to the bank is traceable from either end.
        assert split.tenders[1].reference == "AUTH-99", split.tenders[1].reference
        assert entry.source_type == "pos_sale" and entry.source_id == split.id, entry
        print(
            f"3. the split sale recorded cash {by_account['1000']} to the drawer's account"
            f" and the card {by_account['1010']} to the bank's — separate tenders,"
            " separate paths, with the card's authorisation on its own tender row"
        )

        # 4 + 6 — movements move the drawer, and the breakdown is per tender
        paid_out(session, company_id=COMPANY, terminal="T1", amount="200.00",
                 reason="window cleaner", actor="maria", on=DAY)
        paid_in(session, company_id=COMPANY, terminal="T1", amount="500.00",
                reason="opening float", actor="maria", on=DAY)
        session.commit()
        rows = movements_for(session, company_id=COMPANY, terminal="T1", on=DAY)
        assert {row.movement_type for row in rows} == {"paid_out", "paid_in"}, rows
        assert all(row.actor == "maria" for row in rows), rows
        assert movement_total(rows) == Decimal("300.000000"), movement_total(rows)
        sales = [cash_sale, split]
        state = drawer_state(sales, rows)
        expected = cash_sales_total(sales) - change_paid(sales) + Decimal("300")
        assert state["expected"] == expected, state
        breakdown = tender_breakdown(sales)
        assert breakdown == {
            CASH: {"tendered": Decimal("170.000000"),
                   "applied": Decimal("50.000000") + gross},
            CARD: {"tendered": (gross - Decimal("50")).quantize(Decimal("0.000001")),
                   "applied": (gross - Decimal("50")).quantize(Decimal("0.000001"))},
        }, breakdown
        print(
            f"4. a 200.00 paid-out (window cleaner) and a 500.00 paid-in (opening float)"
            f" leave the drawer's expected cash at {state['expected']}, and the movements"
            f" name who made them"
        )
        print(
            f"6. the breakdown keeps the paths apart — cash {breakdown[CASH]}, card"
            f" {breakdown[CARD]}"
        )

        # 5 — the refusals
        no_reason = _refused(
            lambda: paid_out(session, company_id=COMPANY, terminal="T1", amount="10.00",
                             reason="   ", actor="maria", on=DAY),
            DrawerError,
        )
        session.rollback()
        no_actor = _refused(
            lambda: paid_in(session, company_id=COMPANY, terminal="T1", amount="10.00",
                            reason="float", actor="", on=DAY),
            DrawerError,
        )
        session.rollback()
        negative = _refused(
            lambda: paid_in(session, company_id=COMPANY, terminal="T1", amount="0",
                            reason="float", actor="maria", on=DAY),
            DrawerError,
        )
        session.rollback()
        print(
            f"5. a movement with no reason ({no_reason[:32]}…), no actor"
            f" ({no_actor[:32]}…) and a non-positive amount ({negative[:34]}…) are each"
            " refused"
        )

        # 5b — an amount nobody can read is refused, not raised further in
        unreadable = _refused(
            lambda: paid_out(session, company_id=COMPANY, terminal="T1", amount="zzz",
                             reason="typo", actor="maria", on=DAY),
            DrawerError,
        )
        session.rollback()
        for figure in ("NaN", "Infinity"):
            _refused(
                lambda figure=figure: paid_out(
                    session, company_id=COMPANY, terminal="T1", amount=figure,
                    reason="not a figure", actor="maria", on=DAY,
                ),
                DrawerError,
            )
            session.rollback()
        # And the same refusal over the API, in the platform's one error shape: a
        # mistyped figure is the till's mistake, not the platform's fault.
        owner = define_role(session, company_id=COMPANY, code="cashier", name="Cashier")
        grant(session, owner, "company.read", "pos.drawer", "pos.read")
        assign(session, company_id=COMPANY, subject="maria", role=owner)
        session.commit()
        client = TestClient(app, raise_server_exceptions=False)
        headers = {"X-Company-Id": str(COMPANY), "X-Actor": "maria"}
        over_the_api = client.post(
            f"{BASE}/pos/drawer-movements",
            headers=headers,
            json={"terminal": "T1", "movement_type": "paid_out", "amount": "zzz",
                  "reason": "typo", "actor": "maria"},
        )
        assert over_the_api.status_code == 422, over_the_api.text
        assert over_the_api.json()["error"]["code"] == "drawer_error", over_the_api.text
        assert set(over_the_api.json()) == {"error"}, over_the_api.json()
        print(
            f"5b. an unreadable amount is refused wherever it is stated — \"{unreadable}\""
            f" from the service, and {over_the_api.status_code}"
            f" ({over_the_api.json()['error']['code']}) over the API rather than a fault"
        )

        # 7 — the expected figure is derived, not stored
        again = drawer_state(
            list(session.scalars(select(PosSale).where(
                PosSale.company_id == COMPANY, PosSale.status == "completed"
            ))),
            movements_for(session, company_id=COMPANY, terminal="T1", on=DAY),
        )
        assert again == state, (again, state)
        print(
            f"7. the same figure comes back from the documents alone ({again['expected']})"
            " — nothing here keeps a balance beside them"
        )

    print("\ncheck_pos_tender: all assertions green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
