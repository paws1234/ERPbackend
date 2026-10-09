"""T-3.POS.04 check — the Z-Report, its voids and refunds, and the day beside it.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_pos_zreport.py

Green on all nine:

1. the shift's Z-Report **ties to its constituent sales with no rounding gap** — the
   tenders applied add up to the gross, and the net plus the tax is the gross exactly
2. the tax is broken out **as the pack applied it**, line by line, and the report's tax
   is the sum of the sales' own tax
3. **voids and refunds are separate lines**: an abandoned basket took nothing, a refunded
   sale gave its money back — counted and valued apart from each other and from the sales
4. a **refund reverses what the sale did**: the stock is back on the shelf at the value it
   left at, and a reversing entry is posted — a new entry, never an edit
5. a **void needs a reason and an actor**, and a sale that is already void cannot be
   voided twice
6. the **day report is the sum of the shift reports exactly** — each of the day's lines
   *is* that shift's own report for the day (`shift_report(shift, on=day)`), so the
   equality is arithmetic over one function rather than a rounding promise, and the
   trade of a till with no shift at all is its own bucket
7. **reprinting a closed shift's report reproduces it**, because nothing is stored to
   drift: the sales are the shift's own rows and a closed shift takes no more
8. a shift report carries the drawer's own count, variance and reason

9. a till left **trading past midnight** is reported on the day it traded, in its own
   shift's line — never in the bucket for the trade no shift took — and the shift's day
   slices add back to its whole report, with the opening float stated once

10. a refund made **after** the shift closed belongs to the day it was made: the closed
   shift's report is byte for byte what it signed off, the refund is stated on its own
   day, and the shift that paid the cash back expects exactly its float less that cash

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.company import Company, set_cash_drawer_required  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger.accounts import create_account  # noqa: E402
from app.ledger.currency import register_currency  # noqa: E402
from app.ledger.mapping import set_mapping  # noqa: E402
from app.ledger.posting import JournalEntry, JournalLine  # noqa: E402
from app.pos.drawer import paid_out  # noqa: E402
from app.pos.reports import (  # noqa: E402
    ReportError,
    VoidNotAllowed,
    day_report,
    shift_report,
    void_sale,
)
from app.pos.sales import (  # noqa: E402
    CARD,
    CASH,
    VOID,
    complete_sale,
    open_sale,
    scan,
    tender,
)
from app.pos.shifts import (  # noqa: E402
    CLOSED,
    close_shift,
    open_shift,
    shift_totals,
)
from app.sales.customers import create_customer  # noqa: E402,F401 — the sales chain's tables
from app.sales.fulfilment import Shipment  # noqa: E402,F401 — for its table
from app.sales.orders import SalesOrder  # noqa: E402,F401 — for its table
from app.sales.pipeline import Opportunity  # noqa: E402,F401 — for its table
from app.stock.entries import StockLedgerEntry, on_hand  # noqa: E402
from app.stock.items import add_barcode, create_item  # noqa: E402
from app.stock.locations import create_location  # noqa: E402
from app.stock.transactions import receive  # noqa: E402
from tests.seed import seed_stock_accounts  # noqa: E402

COMPANY = uuid.uuid4()
DAY = date(2026, 9, 23)
BARCODE = "4000000000307"
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


def _lines(session: Session, entry: JournalEntry) -> list[JournalLine]:
    return list(
        session.scalars(
            select(JournalLine)
            .where(JournalLine.entry_id == entry.id)
            .order_by(JournalLine.line_no)
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
            Company(id=COMPANY, code="POS-Z", name="POS Z", base_currency="PHP",
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
        set_cash_drawer_required(session, session.get(Company, COMPANY), required=True)
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
        unit_cost = Decimal("40.000000")

        shift = open_shift(session, company_id=COMPANY, terminal=TERMINAL,
                           opening_float="200.00", actor="maria", on=DAY)
        session.commit()

        def ring_up(number: str, *, cash=None, card=None, abandon=False):
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
            if abandon:
                return sale
            complete_sale(session, sale)
            session.commit()
            return sale

        kept_cash = ring_up("POS-K1", cash="150.00")
        kept_card = ring_up("POS-K2", card=str(gross))
        refunded = ring_up("POS-R1", cash=str(gross))
        abandoned = ring_up("POS-V1", abandon=True)
        session.commit()
        paid_out(session, company_id=COMPANY, terminal=TERMINAL, amount="25.00",
                 reason="ice", actor="maria", on=DAY)
        session.commit()

        # 5 — a void needs a reason and an actor
        no_reason = _refused(
            lambda: void_sale(session, abandoned, reason="  ", actor="maria"),
            ReportError,
        )
        session.rollback()
        no_actor = _refused(
            lambda: void_sale(session, abandoned, reason="customer walked away",
                              actor=""),
            ReportError,
        )
        session.rollback()
        print(
            f"5. a void with no reason ({no_reason[:34]}…) and one with no actor"
            f" ({no_actor[:34]}…) are each refused"
        )

        # 4 — the abandoned basket is a void, the completed sale is a refund
        void_sale(session, abandoned, reason="customer walked away", actor="maria",
                  on=DAY)
        session.commit()
        assert abandoned.status == VOID and abandoned.reversal_entry_id is None
        stock_before = on_hand(session, company_id=COMPANY, item_id=item.id,
                               location_id=till.id)["quantity"]
        original_entry = session.get(JournalEntry, refunded.journal_entry_id)
        void_sale(session, refunded, reason="wrong size", actor="maria", on=DAY)
        session.commit()
        assert refunded.status == VOID, refunded.status
        assert refunded.void_reason == "wrong size" and refunded.voided_by == "maria"
        reversal = session.get(JournalEntry, refunded.reversal_entry_id)
        assert reversal is not None, "the refund posted no reversing entry"
        original = {row.account: row.debit - row.credit for row in _lines(session, original_entry)}
        mirror = {row.account: row.debit - row.credit for row in _lines(session, reversal)}
        assert {account: -value for account, value in original.items()} == mirror, (
            original, mirror
        )
        stock_after = on_hand(session, company_id=COMPANY, item_id=item.id,
                              location_id=till.id)["quantity"]
        assert stock_after == stock_before + 1, (stock_before, stock_after)
        # Back at the value it left at: the issue took 40.00 of inventory, the return
        # puts the same 40.00 back.
        returned = session.scalars(
            select(StockLedgerEntry).where(
                StockLedgerEntry.source_type == "pos_sale_refund",
                StockLedgerEntry.source_id == refunded.id,
            )
        ).all()
        assert len(returned) == 1 and returned[0].value == unit_cost, [
            row.value for row in returned
        ]
        twice = _refused(
            lambda: void_sale(session, refunded, reason="again", actor="maria"),
            VoidNotAllowed,
        )
        session.rollback()
        print(
            f"4. the refund put the goods back ({stock_before} → {stock_after} on hand at"
            f" {returned[0].value}, the value they left at) and posted a reversing entry"
            f" that is the sale's own mirrored — and voiding it twice is refused"
            f" ({twice[:38]}…)"
        )

        # 1 + 2 — the shift's report ties to its sales
        report = shift_report(session, shift)
        assert report["ties"] is True, report
        assert report["tenders_applied"] == report["gross"], report
        assert report["net"] + report["tax"] == report["gross"], report
        assert report["net"] == Decimal("300.000000"), report["net"]
        assert report["tax"] == Decimal("36.000000"), report["tax"]
        assert report["gross"] == Decimal("336.000000"), report["gross"]
        # Three rung-up sales, and the one that was refunded is still one of them: the
        # till took that money and handed it back, which the refund line states.
        assert report["sales"] == 3, report["sales"]
        assert report["tenders"][CASH]["applied"] == 2 * gross, report["tenders"]
        assert report["tenders"][CARD]["applied"] == gross, report["tenders"]
        print(
            f"1. the Z-Report ties with no gap: {report['sales']} sales, net"
            f" {report['net']} + tax {report['tax']} = {report['gross']}, and the"
            f" tenders applied add up to exactly that ({report['tenders_applied']})"
        )
        print(
            f"2. the tax is the pack's own, broken out per line and summed"
            f" ({report['tax']}) — the same rule an invoice for these goods charges"
        )

        # 3 — voids and refunds apart
        assert report["voids"] == {
            "count": 1, "value": Decimal("112.000000"), "sales": [abandoned.number]
        }, report["voids"]
        assert report["refunds"] == {
            "count": 1, "value": Decimal("112.000000"), "sales": [refunded.number]
        }, report["refunds"]
        print(
            f"3. voids ({report['voids']['count']} worth {report['voids']['value']}) and"
            f" refunds ({report['refunds']['count']} worth {report['refunds']['value']})"
            " are separate lines, each naming its sale: an abandoned basket was never a"
            f" sale, and the refunded one is counted among the {report['sales']} with the"
            " money that went back stated beside it"
        )

        # 8 — the drawer section
        close_shift(session, shift, counted_cash="300.00", actor="maria",
                    reason="short by the price of a coffee")
        session.commit()
        closed = shift_report(session, shift)
        assert closed["drawer"]["counted"] == Decimal("300.000000"), closed["drawer"]
        assert closed["drawer"]["expected"] == shift.expected_cash, closed["drawer"]
        assert closed["drawer"]["variance"] == shift.variance, closed["drawer"]
        assert closed["drawer"]["variance_reason"] == "short by the price of a coffee"
        assert closed["drawer"]["opening_float"] == Decimal("200.000000"), closed["drawer"]
        print(
            f"8. the report carries the drawer's own count ({closed['drawer']['counted']}),"
            f" the expected {closed['drawer']['expected']}, the variance"
            f" {closed['drawer']['variance']} and the reason for it"
        )

        # 7 — reprinting it reproduces it
        assert shift_report(session, shift) == closed, "a closed shift's report moved"
        print(
            "7. reprinting the closed shift's Z-Report reproduced every figure — nothing"
            " is stored to drift, and the closed shift takes no more sales"
        )

        # 6 — the day report is the sum of the shifts, and the shiftless trade has its own bucket
        afternoon = open_shift(session, company_id=COMPANY, terminal="T2",
                               opening_float="100.00", actor="jose", on=DAY)
        session.commit()
        second = open_sale(session, company_id=COMPANY, number="POS-T2", terminal="T2",
                           location=till, sold_on=DAY)
        session.flush()
        scan(session, second, barcode=BARCODE, base_price="100.00", quantity="2")
        session.commit()
        tender(session, second, tender_type=CASH, amount="250.00")
        session.commit()
        complete_sale(session, second)
        session.commit()
        close_shift(session, afternoon, counted_cash=str(
            shift_totals(session, afternoon)["expected_cash"]
        ), actor="jose")
        session.commit()
        day = day_report(session, company_id=COMPANY, on=DAY)
        assert len(day["shifts"]) == 2, day["shifts"]
        summed_gross = sum((row["gross"] for row in day["shifts"]), day["shiftless"]["gross"])
        summed_tax = sum((row["tax"] for row in day["shifts"]), day["shiftless"]["tax"])
        assert day["gross"] == summed_gross.quantize(Decimal("0.000001")), (
            day["gross"], summed_gross
        )
        assert day["tax"] == summed_tax.quantize(Decimal("0.000001")), (day["tax"], summed_tax)
        assert day["sales"] == sum(row["sales"] for row in day["shifts"]), day
        # The finding's own comparison: the day's lines **are** the shifts' reports, the
        # same function rather than a recount kept agreeing with it by hand.
        for row, seen in zip(day["shifts"], (shift, afternoon)):
            whole = shift_report(session, seen)
            assert row["gross"] == whole["gross"], (row, whole)
            assert row["net"] == whole["net"] and row["tax"] == whole["tax"], (row, whole)
            assert row["tenders"] == whole["tenders"], (row, whole)
            assert row["movements"] == whole["movements"], (row, whole)
            assert row["expected_cash"] == whole["expected_cash"], (row, whole)
            assert row["refunds"] == whole["refunds"]["sales"], (row, whole)
        assert day["shifts"][0]["tenders"] == closed["tenders"], "a shift's line moved"
        # The first shift rang up three sales (one of them since refunded, which is
        # still a sale it took), the afternoon one two.
        day_gross = ((3 * gross) + (2 * gross)).quantize(Decimal("0.000001"))
        assert day["gross"] == day_gross, (day["gross"], day_gross)
        print(
            f"6. the day report added {len(day['shifts'])} shifts to {day['gross']}"
            f" ({day['tax']} tax, {day['sales']} sales) — exactly the sum of their own"
            f" figures, with the void/refund lines beside them"
        )

        # 9 — a till left trading past midnight: the day it traded owns it
        overnight = open_shift(session, company_id=COMPANY, terminal="T3",
                               opening_float="50.00", actor="jose", on=DAY)
        session.commit()
        tomorrow = DAY + timedelta(days=1)
        late = open_sale(session, company_id=COMPANY, number="POS-LATE", terminal="T3",
                         location=till, sold_on=tomorrow)
        session.flush()
        scan(session, late, barcode=BARCODE, base_price="100.00")
        session.commit()
        tender(session, late, tender_type=CASH, amount="112.00")
        session.commit()
        complete_sale(session, late)
        session.commit()
        assert late.shift_id == overnight.id, late.shift_id
        paid_out(session, company_id=COMPANY, terminal="T3", amount="10.00",
                 reason="next-day courier", actor="jose", on=tomorrow)
        session.commit()
        next_day = day_report(session, company_id=COMPANY, on=tomorrow)
        assert len(next_day["shifts"]) == 1, next_day["shifts"]
        carried = next_day["shifts"][0]
        assert carried["terminal"] == "T3" and carried["sales"] == 1, carried
        # Not the bucket for the trade no shift took: the shift that was still trading
        # is the one whose report counts the sale, so the day says so in that line.
        assert next_day["shiftless"]["sales"] == 0, next_day["shiftless"]
        assert next_day["shiftless"]["movements"] == Decimal("0.000000"), next_day["shiftless"]
        assert next_day["sales"] == 1 and next_day["gross"] == gross, next_day
        assert next_day["expected_cash"] == gross - Decimal("10"), next_day
        # The line is the shift's own report for that day, and the shift's days add back
        # to its whole report — the property the day report exists to have.
        overnight_tomorrow = shift_report(session, overnight, on=tomorrow)
        overnight_day = shift_report(session, overnight, on=DAY)
        whole = shift_report(session, overnight)
        assert carried["gross"] == overnight_tomorrow["gross"] == gross, (carried, whole)
        assert carried["movements"] == overnight_tomorrow["movements"], (carried, whole)
        assert (overnight_day["gross"] + overnight_tomorrow["gross"]) == whole["gross"], (
            overnight_day, overnight_tomorrow, whole
        )
        assert (
            overnight_day["movements"] + overnight_tomorrow["movements"]
        ) == whole["movements"], (overnight_day, overnight_tomorrow, whole)
        # The float entered the drawer on the day it opened: stated once, not on both.
        assert overnight_day["opening_float"] == Decimal("50.000000"), overnight_day
        assert overnight_tomorrow["opening_float"] == Decimal("0.000000"), overnight_tomorrow
        assert day_report(session, company_id=COMPANY, on=DAY)["sales"] == day["sales"], (
            "the earlier day's report changed"
        )
        print(
            f"9. the till left trading past midnight is reported on the day it traded"
            f" ({next_day['gross']} on {tomorrow}, in T3's own shift line — nothing in"
            f" the shiftless bucket the trade did not come from), the shift's two days"
            f" add back to its whole {whole['gross']}, and {DAY}'s own report is"
            " unchanged — revenue is neither lost nor in two days at once"
        )

        # 10 — a refund made later belongs to the day it was made
        refund_day = DAY + timedelta(days=2)
        closed_before = shift_report(session, shift)
        later = open_shift(session, company_id=COMPANY, terminal=TERMINAL,
                           opening_float="200.00", actor="jose", on=refund_day)
        session.commit()
        void_sale(session, kept_cash, reason="till error", actor="jose", on=refund_day)
        session.commit()
        assert shift_report(session, shift) == closed_before, (
            "a later refund restated a closed shift's report"
        )
        later_report = shift_report(session, later)
        assert later_report["sales"] == 0 and later_report["gross"] == Decimal(0), (
            later_report
        )
        assert later_report["refunds"] == {
            "count": 1, "value": gross, "sales": [kept_cash.number]
        }, later_report["refunds"]
        # The shift that refunded it never sold anything: its 200.00 float less the
        # 112.00 the customer was handed back is what its drawer should hold.
        assert later_report["drawer"]["expected"] == Decimal("88.000000"), (
            later_report["drawer"]
        )
        day_after = day_report(session, company_id=COMPANY, on=refund_day)
        assert day_after["refunds"] == {"count": 1, "value": gross}, day_after["refunds"]
        assert day_after["sales"] == 0 and day_after["gross"] == Decimal(0), day_after
        assert day_after["shifts"][0]["refunds"] == [kept_cash.number], day_after["shifts"]
        print(
            f"10. {kept_cash.number} was refunded on {refund_day}: that day states the"
            f" {later_report['refunds']['value']} that went back, its shift's drawer"
            f" expects {later_report['drawer']['expected']} (200.00 float less the cash"
            f" handed over), and the closed shift's {closed_before['gross']} is byte for"
            " byte what it signed off"
        )

    print("\ncheck_pos_zreport: all assertions green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
