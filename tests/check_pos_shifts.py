"""T-3.POS.03 check — the retail shift: open, trade, count, close.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_pos_shifts.py

Green on all eleven:

1. a shift opens with its float, and **a second shift cannot be opened on one terminal**
   — refused by the service *and* by the partial unique index behind it
2. while the company manages drawers, **a sale cannot be completed outside an open
   shift**, and once the shift is open the sale is stamped with it — and a basket opened
   inside a shift that is then **closed** is not added to it afterwards
3. a company that does **not** state the policy sells without a shift — unstated is the
   ordinary shop, not a refusal
4. closing records the **expected cash derived from the shift's own sales and movements**
   against what was counted, with the variance between them
5. a **non-zero variance needs a reason** and is refused without one; a reason is
   recorded with it, and a zero variance needs none
6. a **closed shift takes no more sales** — its sale window has ended
7. the shift's totals are derived every time: sales, net, tax, gross, the tenders kept
   apart, the movements, the change paid — so they cannot drift from the sales
8. the opening float is counted **once** — a float also recorded as a drawer movement is
   not added a second time, and a withdrawal that merely reads like a float still left
   the drawer; the day's **second shift on one till** does not wear the first shift's
   movements
9. the policy itself is **readable over the API** — the stated answer comes back on the
   company profile the till reads, and withdrawing it reads back as unstated
10. with no drawer policy, a basket opened on a shift and completed after it **closed**
   is not that shift's sale — the signed-off Z-Report cannot be restated

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.exc import DBAPIError
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.company import (  # noqa: E402
    Company,
    cash_drawer_required_for,
    set_cash_drawer_required,
)
from app.db import Base  # noqa: E402
from app.ledger.accounts import create_account  # noqa: E402
from app.ledger.currency import register_currency  # noqa: E402
from app.ledger.mapping import set_mapping  # noqa: E402
from app.pos.drawer import paid_out  # noqa: E402
from app.pos.sales import (  # noqa: E402
    CARD,
    CASH,
    PosSale,
    complete_sale,
    open_sale,
    scan,
    tender,
)
from app.pos.shifts import (  # noqa: E402
    CLOSED,
    OPEN,
    VarianceReasonRequired,
    ShiftAlreadyOpenError,
    ShiftError,
    close_shift,
    closed_shifts,
    current_shift,
    open_shift,
    require_open_shift,
    shift_sales,
    shift_totals,
)
from app.api import BASE, app  # noqa: E402
from app.sales.customers import create_customer  # noqa: E402
from app.sales.fulfilment import Shipment  # noqa: E402,F401 — for its table
from app.sales.orders import SalesOrder  # noqa: E402,F401 — for its table
from app.sales.pipeline import Opportunity  # noqa: E402,F401 — for its table
from app.stock.items import add_barcode, create_item  # noqa: E402
from app.stock.locations import create_location  # noqa: E402
from app.security import assign, define_role, grant  # noqa: E402
from app.stock.transactions import receive  # noqa: E402
from tests.seed import seed_stock_accounts  # noqa: E402

COMPANY = uuid.uuid4()
DAY = date(2026, 9, 22)
BARCODE = "4000000000208"
VAT = Decimal("1.12")
TERMINAL = "T1"


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
            Company(id=COMPANY, code="POS-SHIFT", name="POS shifts", base_currency="PHP",
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

        def ring_up(number: str, *, cash=None, card=None) -> PosSale:
            sale = open_sale(session, company_id=COMPANY, number=number, terminal=TERMINAL,
                             location=till, sold_on=DAY)
            session.flush()
            scan(session, sale, barcode=BARCODE, base_price="100.00")
            session.commit()
            if cash is not None:
                tender(session, sale, tender_type=CASH, amount=cash)
            if card is not None:
                tender(session, sale, tender_type=CARD, amount=card)
            session.commit()
            return sale

        # 3 — with no policy stated, a till sells without a shift
        assert cash_drawer_required_for(session, company_id=COMPANY) is False
        no_shift = ring_up("POS-NOSHIFT", cash="120.00")
        complete_sale(session, no_shift)
        session.commit()
        assert no_shift.shift_id is None, no_shift.shift_id
        print(
            "3. with the policy unstated a sale completes with no shift at all"
            f" ({no_shift.number}, shift {no_shift.shift_id}) — the ordinary shop"
        )

        # 2 — with the policy on, trading outside a shift is refused
        set_cash_drawer_required(session, session.get(Company, COMPANY), required=True)
        session.commit()
        assert cash_drawer_required_for(session, company_id=COMPANY) is True
        orphan = ring_up("POS-ORPHAN", cash="120.00")
        outside = _refused(lambda: complete_sale(session, orphan), ShiftError)
        session.rollback()
        assert "no open shift" in outside, outside

        # 1 — opening one, and not a second
        shift = open_shift(session, company_id=COMPANY, terminal=TERMINAL,
                           opening_float="500.00", actor="maria", on=DAY)
        session.commit()
        assert shift.status == OPEN and shift.opening_float == Decimal("500.000000")
        assert current_shift(session, company_id=COMPANY, terminal=TERMINAL).id == shift.id
        again = _refused(
            lambda: open_shift(session, company_id=COMPANY, terminal=TERMINAL,
                               opening_float="100.00", actor="maria", on=DAY),
            ShiftAlreadyOpenError,
        )
        session.rollback()
        session.add(
            type(shift)(
                company_id=COMPANY, terminal=TERMINAL, status=OPEN,
                opening_float=Decimal("1"), opened_on=DAY,
                opened_at=shift.opened_at, opened_by="hand-written",
            )
        )
        try:
            session.commit()
        except DBAPIError as exc:
            assert "uq_pos_shift_open_terminal" in str(exc), exc
            session.rollback()
        else:
            raise AssertionError("the database accepted two open shifts on one terminal")
        print(
            f"1. the shift opened with a 500.00 float, a second on the same terminal was"
            f" refused ({again[:44]}…), and the partial unique index refuses a"
            " hand-written one too"
        )
        print(f"2. with the policy on, a sale outside a shift is refused ({outside[:44]}…)")

        # 2b + 7 — trade on the shift, and read its totals
        on_shift_cash = ring_up("POS-S1", cash="150.00")
        complete_sale(session, on_shift_cash)
        session.commit()
        assert on_shift_cash.shift_id == shift.id, on_shift_cash.shift_id
        on_shift_card = ring_up("POS-S2", card=str(gross))
        complete_sale(session, on_shift_card)
        session.commit()
        assert {sale.number for sale in shift_sales(session, shift)} == {"POS-S1", "POS-S2"}, (
            [sale.number for sale in shift_sales(session, shift)]
        )
        paid_out(session, company_id=COMPANY, terminal=TERMINAL, amount="60.00",
                 reason="courier", actor="maria", on=DAY)
        session.commit()
        totals = shift_totals(session, shift)
        assert totals["sales"] == 2, totals
        assert totals["gross"] == (2 * gross).quantize(Decimal("0.000001")), totals
        assert totals["net"] == Decimal("200.000000"), totals["net"]
        assert totals["tax"] == (2 * (gross - Decimal("100"))).quantize(
            Decimal("0.000001")
        ), totals
        assert totals["tenders"][CASH]["tendered"] == Decimal("150.000000"), totals["tenders"]
        assert totals["tenders"][CARD]["applied"] == gross, totals["tenders"]
        assert totals["movements"] == Decimal("-60.000000"), totals
        # What the drawer holds: the 500.00 float, the 150.00 the cash sale put in less
        # the 38.00 change it gave back (a net of the sale's own 112.00), less the 60.00
        # paid out. The card's 112.00 went to the bank and is not in the drawer.
        expected = (
            Decimal("500") + gross - Decimal("60")
        ).quantize(Decimal("0.000001"))
        assert totals["expected_cash"] == expected, (totals["expected_cash"], expected)
        print(
            f"7. the shift's totals are derived: {totals['sales']} sales worth"
            f" {totals['gross']} ({totals['net']} net and {totals['tax']} tax), tenders"
            f" {totals['tenders']}, movements {totals['movements']}, expected cash"
            f" {totals['expected_cash']}"
        )

        # 5 — the variance needs a reason
        unexplained = _refused(
            lambda: close_shift(session, shift, counted_cash="600.00", actor="maria"),
            VarianceReasonRequired,
        )
        session.rollback()
        shift = current_shift(session, company_id=COMPANY, terminal=TERMINAL)
        assert shift.status == OPEN, "a refused close closed the shift"
        short = Decimal("600.00") - expected
        close_shift(session, shift, counted_cash="600.00", actor="maria",
                    reason="till short after a busy lunch")
        session.commit()

        # 4 — the recorded figures
        assert shift.status == CLOSED
        assert shift.expected_cash == expected, (shift.expected_cash, expected)
        assert shift.counted_cash == Decimal("600.000000"), shift.counted_cash
        assert shift.variance == short, (shift.variance, short)
        assert shift.variance_reason == "till short after a busy lunch"
        assert shift.closed_by == "maria" and shift.closed_at is not None
        assert [row.id for row in closed_shifts(session, company_id=COMPANY, on=DAY)] == [
            shift.id
        ], closed_shifts(session, company_id=COMPANY, on=DAY)
        print(
            f"4. closing against a counted 600.00 recorded the expected"
            f" {shift.expected_cash} and the variance {shift.variance} — and an"
            f" unexplained variance was refused first ({unexplained[:44]}…)"
        )

        # 4b — a zero variance needs no reason
        clean = open_shift(session, company_id=COMPANY, terminal="T2", opening_float="0",
                           actor="maria", on=DAY)
        session.commit()
        balanced = shift_totals(session, clean)["expected_cash"]
        close_shift(session, clean, counted_cash=str(balanced), actor="maria")
        session.commit()
        assert clean.variance == Decimal("0.000000") and clean.variance_reason is None
        print(
            f"4b. a shift that balanced ({balanced}) closed with a zero variance and no"
            " reason asked for"
        )

        # 6 — a closed shift takes no more sales
        late = ring_up("POS-LATE", cash="120.00")
        session.commit()
        assert late.shift_id is None, "a sale on a closed shift was stamped with it"
        print(
            "6. a sale rung after the close found no open shift on that terminal"
            " (POS-LATE), so the closed shift's window is closed"
        )

        # 8 — the opening float is not counted twice
        float_movement = paid_out(session, company_id=COMPANY, terminal="T3", amount="10.00",
                                  reason="float for the drawer", actor="maria", on=DAY)
        session.commit()
        third = open_shift(session, company_id=COMPANY, terminal="T3",
                           opening_float="100.00", actor="maria", on=DAY)
        session.commit()
        totals_three = shift_totals(session, third)
        assert totals_three["opening_float"] == Decimal("100.000000"), totals_three
        assert totals_three["movements"] == Decimal("-10.000000"), totals_three
        assert totals_three["expected_cash"] == Decimal("90.000000"), totals_three
        print(
            f"8. a shift opened with a 100.00 float and a 10.00 paid-out expects"
            f" {totals_three['expected_cash']} — the float is its own figure, not a"
            " movement added on top"
        )

        # 8c — a withdrawal described as a float still left the drawer
        described = paid_out(session, company_id=COMPANY, terminal="T3", amount="5.00",
                             reason="opening float", actor="maria", on=DAY)
        session.commit()
        after_withdrawal = shift_totals(session, third)
        assert after_withdrawal["movements"] == Decimal("-15.000000"), after_withdrawal
        assert after_withdrawal["expected_cash"] == Decimal("85.000000"), after_withdrawal
        print(
            f"8c. a 5.00 paid-out whose reason happens to read"
            f" {described.reason!r} is still money that left the drawer"
            f" ({after_withdrawal['expected_cash']} expected), because what is the"
            " shift's own float is its kind and its reason, not the words alone"
        )

        # 8b — the day's second shift on one till does not wear the first's movements
        morning = open_shift(session, company_id=COMPANY, terminal="T4",
                             opening_float="100.00", actor="maria", on=DAY)
        session.commit()
        paid_out(session, company_id=COMPANY, terminal="T4", amount="10.00",
                 reason="morning courier", actor="maria", on=DAY)
        session.commit()
        closed = close_shift(session, morning, counted_cash="90.00", actor="maria")
        session.commit()
        assert closed.variance == Decimal("0.000000"), closed.variance
        evening = open_shift(session, company_id=COMPANY, terminal="T4",
                             opening_float="200.00", actor="maria", on=DAY,
                             at=closed.closed_at)
        session.commit()
        evening_totals = shift_totals(session, evening)
        assert evening_totals["movements"] == Decimal("0.000000"), evening_totals
        assert evening_totals["expected_cash"] == Decimal("200.000000"), evening_totals
        assert close_shift(
            session, evening, counted_cash="200.00", actor="maria"
        ).variance == Decimal("0.000000")
        session.commit()
        assert shift_totals(session, closed)["movements"] == Decimal("-10.000000"), (
            shift_totals(session, closed)
        )
        print(
            "8b. the same till's second shift opened on its own 200.00 and closed on it"
            " with no variance: the 10.00 the first shift paid out is the first shift's"
            " movement, not the second's"
        )

        # 9 — the till reads the policy it trades under, over the API
        owner = define_role(session, company_id=COMPANY, code="owner", name="Owner")
        grant(session, owner, "company.read", "company.configure", "pos.shift")
        assign(session, company_id=COMPANY, subject="maria", role=owner)
        session.commit()
        client = TestClient(app, raise_server_exceptions=False)
        headers = {"X-Company-Id": str(COMPANY), "X-Actor": "maria"}
        # 9b — a shift's float that is not a number is a refusal, not a fault
        nonsense = client.post(
            f"{BASE}/pos/shifts", headers=headers,
            json={"terminal": "T9", "opening_float": "abc", "actor": "maria"},
        )
        assert nonsense.status_code == 422, nonsense.text
        assert nonsense.json()["error"]["code"] == "shift_error", nonsense.text
        profile = client.get(f"{BASE}/companies/current", headers=headers)
        assert profile.status_code == 200, profile.text
        assert profile.json()["cash_drawer_required"] is True, profile.text
        withdrawn = client.post(
            f"{BASE}/pos/cash-drawer-policy", headers=headers, json={"required": None}
        )
        assert withdrawn.status_code == 200, withdrawn.text
        assert withdrawn.json()["cash_drawer_required"] is None, withdrawn.text
        reread = client.get(f"{BASE}/companies/current", headers=headers).json()
        assert reread["cash_drawer_required"] is None, reread
        restored = client.post(
            f"{BASE}/pos/cash-drawer-policy", headers=headers, json={"required": True}
        )
        assert restored.status_code == 200, restored.text
        print(
            f"9b. an opening float of 'abc' is refused in the one error shape"
            f" ({nonsense.json()['error']['code']}) — a 500 would be a fault, not a"
            " refusal"
        )
        print(
            "9. the policy a till trades under is readable from the API — the stated"
            " 'shifts required' comes back on the company profile, withdrawing it reads"
            " back as unstated rather than as a different answer"
        )

        # 10 — with no drawer policy, a basket whose shift closed is not that shift's sale
        set_cash_drawer_required(session, session.get(Company, COMPANY), required=None)
        session.commit()
        assert cash_drawer_required_for(session, company_id=COMPANY) is False
        evening = open_shift(session, company_id=COMPANY, terminal="T5",
                             opening_float="100.00", actor="maria", on=DAY)
        session.commit()
        stranded = open_sale(session, company_id=COMPANY, number="POS-STRANDED",
                             terminal="T5", location=till, sold_on=DAY)
        session.commit()
        assert stranded.shift_id == evening.id, stranded.shift_id
        signed_off = shift_totals(session, evening)
        close_shift(session, evening, counted_cash="100.00", actor="maria")
        session.commit()
        scan(session, stranded, barcode=BARCODE, base_price="100.00")
        tender(session, stranded, tender_type=CASH, amount="120.00")
        session.commit()
        complete_sale(session, stranded)
        session.commit()
        assert stranded.shift_id is None, (
            "a sale completed after its shift closed was added to that shift"
        )
        after = shift_totals(session, evening)
        assert after["gross"] == signed_off["gross"] and after["sales"] == signed_off["sales"], (
            after, signed_off
        )
        assert stranded.number not in {sale.number for sale in shift_sales(session, evening)}
        print(
            f"10. a basket opened on a shift and completed after it closed carries no"
            f" shift at all: the closed shift still reads {after['gross']}, so a signed"
            " off Z-Report cannot be restated by a later sale"
        )


    print("\ncheck_pos_shifts: all assertions green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
