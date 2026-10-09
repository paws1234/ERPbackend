"""T-4.WO.04 check — production receipts, the material they consume and the WIP they clear.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_finished_goods.py

Green on all five:

1. a **partial receipt consumes its share** of the components and takes its share out of
   work in progress, leaving the balance derived rather than stored
2. the **final receipt consumes exactly the BOM requirement** and takes the residue, so
   the WIP account ends the job at **zero**
3. the **stock ledger and the ledger** both show it: the material leaves inventory, the
   goods arrive in it, and WIP is debited and credited by the same 200
4. a receipt whose consumption **nobody has issued** is refused with the shortfall
   named, and so is one that would report **more than the order was raised for**
5. the **consumption report** states what was required, issued and consumed, whether it
   is inside the tolerance, and where WIP stands
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
from app.ledger.mapping import set_mapping  # noqa: E402
from app.ledger.posting import JournalEntry, JournalLine  # noqa: E402
from app.manufacturing.bom import add_line, create_bom, release  # noqa: E402
from app.manufacturing.issues import issued_value, issue_material, outstanding  # noqa: E402
from app.manufacturing.receipts import (  # noqa: E402
    ConsumptionNotCovered,
    OverReceiptError,
    WorkOrderNotRunning,
    consumption_report,
    receipt_lines,
    receive_finished_goods,
    wip_balance,
)
from app.manufacturing.work_orders import (  # noqa: E402
    IN_PROGRESS,
    RELEASED,
    advance,
    create_work_order,
)
from app.stock.entries import (  # noqa: E402
    StockLedgerEntry,
    movements_for_source,
    on_hand,
)
from app.stock.items import create_item  # noqa: E402
from app.stock.locations import create_location  # noqa: E402
from app.stock.transactions import receive  # noqa: E402
from tests.seed import seed_stock_accounts  # noqa: E402

COMPANY = uuid.uuid4()
DAY = date(2026, 9, 16)
ORDERED = Decimal("10")


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


def _balance(session: Session, code: str) -> Decimal:
    """What the ledger holds on one account, from its own lines."""
    total = Decimal(0)
    for line in session.scalars(
        select(JournalLine).join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
    ):
        if line.account == code:
            total += Decimal(line.debit) - Decimal(line.credit)
    return total.quantize(Decimal("0.000001"))


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
                id=COMPANY, code="FG-CHECK", name="Finished goods check",
                base_currency="PHP", fiscal_year_start_month=1,
            )
        )
        session.commit()
        seed_stock_accounts(session, company_id=COMPANY)
        create_account(session, company_id=COMPANY, code="1230", name="Work in Progress",
                       account_class="asset")
        set_mapping(session, company_id=COMPANY, key="work_in_progress", account_code="1230")
        session.commit()

        widget = create_item(session, company_id=COMPANY, sku="WIDGET", name="Widget",
                             base_uom="each", traceability_mode="none")
        blank = create_item(session, company_id=COMPANY, sku="BLANK", name="Blank",
                            base_uom="each", traceability_mode="none")
        session.commit()
        bom = create_bom(session, company_id=COMPANY, item=widget)
        add_line(session, bom, item=blank, quantity="2")
        release(session, bom)
        warehouse = create_location(session, company_id=COMPANY, code="MAIN", name="Main",
                                    location_type="warehouse")
        zone = create_location(session, company_id=COMPANY, code="MAIN-Z", name="Zone",
                               location_type="zone", parent_id=warehouse.id)
        aisle = create_location(session, company_id=COMPANY, code="MAIN-Z-1", name="Aisle",
                                location_type="aisle", parent_id=zone.id)
        store = create_location(session, company_id=COMPANY, code="MAIN-Z-1-A", name="Bin A",
                                location_type="bin", parent_id=aisle.id)
        finished = create_location(session, company_id=COMPANY, code="MAIN-Z-2-A",
                                   name="Bin B", location_type="bin", parent_id=aisle.id)
        session.commit()
        # 40 blanks at 400: ten a unit, so the material for one widget is 20.
        receive(session, item=blank, location=store, uom="each", quantity="40",
                value=Decimal("400"), currency="PHP", source_type="goods_receipt",
                source_id=uuid.uuid4(), posting_date=DAY)
        session.commit()
        order = create_work_order(
            session, company_id=COMPANY, item=widget, quantity=ORDERED, number="WO-1",
            created_on=DAY, due_on=date(2026, 9, 30),
        )
        session.commit()
        said_early = _refused(
            lambda: receive_finished_goods(session, order, location=finished, quantity="1",
                                           on=DAY, backflush_location=store),
            WorkOrderNotRunning,
        )
        session.rollback()
        order = session.get(type(order), order.id)
        for status in (RELEASED, IN_PROGRESS):
            advance(session, order, status=status)
        session.commit()

        # 1 — a partial receipt consumes its share and takes its share of WIP
        first = receive_finished_goods(
            session, order, location=finished, quantity="4", on=DAY,
            backflush_location=store, actor="ana",
        )
        session.commit()
        # 4 of 10 widgets take 8 of the 20 blanks = 80 of material; the receipt takes
        # 4/10 of what is issued (80) = 32, leaving 48 in WIP for the rest of the job.
        assert first.value == Decimal("32.000000"), first.value
        assert first.completes is False, first.completes
        assert issued_value(session, order) == Decimal("80.000000"), issued_value(session, order)
        assert wip_balance(session, order) == Decimal("48.000000"), wip_balance(session, order)
        partial = outstanding(session, order)[0]
        assert (partial["issued"], partial["remaining"]) == (
            Decimal("8.000000"),
            Decimal("12.000000"),
        ), partial
        assert on_hand(session, company_id=COMPANY, item_id=widget.id,
                       location_id=finished.id)["quantity"] == Decimal("4.000000"), "widgets missing"
        print(
            f"1. receiving 4 of {ORDERED} widgets drew {partial['issued']} of the"
            f" {partial['required']} blanks it takes and {first.value} of the"
            f" {issued_value(session, order)} of material issued for the job — the WIP"
            f" balance is {wip_balance(session, order)}, derived from the issues and the"
            " receipts rather than kept beside them"
        )

        # 2 — the final receipt consumes exactly the requirement and clears WIP
        final = receive_finished_goods(
            session, order, location=finished, quantity="6", on=DAY,
            backflush_location=store, actor="ana",
        )
        session.commit()
        assert final.value == Decimal("168.000000"), final.value
        assert final.completes is True, final.completes
        assert wip_balance(session, order) == Decimal("0.000000"), wip_balance(session, order)
        whole = outstanding(session, order)[0]
        assert (whole["issued"], whole["remaining"]) == (
            Decimal("20.000000"),
            Decimal("0.000000"),
        ), whole
        assert on_hand(session, company_id=COMPANY, item_id=widget.id,
                       location_id=finished.id)["quantity"] == ORDERED, "the output is not there"
        assert receipt_lines(session, order)[1]["completes"] is True, receipt_lines(session, order)
        print(
            f"2. the final receipt of 6 consumed the last {whole['issued']} blanks and took"
            f" the remaining {final.value} out of work in progress: the account is at"
            f" {wip_balance(session, order)} with {ORDERED} widgets in stock — the"
            " requirement is issued in full and nothing is left unreconciled"
        )

        # 3 — the ledger, by hand: the material leaves inventory and comes back as goods
        issue_movements = list(
            session.scalars(
                select(StockLedgerEntry).where(
                    StockLedgerEntry.source_type == "work_order_issue"
                )
            )
        )
        receipt_movements = movements_for_source(
            session, company_id=COMPANY, source_type="work_order_receipt", source_id=final.id
        )
        assert len(issue_movements) == 2, len(issue_movements)
        assert len(receipt_movements) == 1, receipt_movements
        assert receipt_movements[0].value == Decimal("168.000000"), receipt_movements[0].value
        assert _balance(session, "1200") == Decimal("400.000000"), _balance(session, "1200")
        assert _balance(session, "1230") == Decimal("0.000000"), _balance(session, "1230")
        entries = list(
            session.scalars(
                select(JournalEntry).where(JournalEntry.source_type == "work_order_receipt")
            )
        )
        assert len(entries) == 2, entries
        for entry in entries:
            debits = sum(
                (Decimal(line.debit) for line in session.scalars(
                    select(JournalLine).where(JournalLine.entry_id == entry.id))),
                Decimal(0),
            )
            credits = sum(
                (Decimal(line.credit) for line in session.scalars(
                    select(JournalLine).where(JournalLine.entry_id == entry.id))),
                Decimal(0),
            )
            assert debits == credits, (entry.id, debits, credits)
        print(
            f"3. inventory stands at {_balance(session, '1200')} — the 40 blanks' 400 back"
            f" as 10 widgets — and work in progress at {_balance(session, '1230')}; the"
            f" two receipts posted {len(entries)} balanced entries and named their own"
            " stock rows as documents"
        )

        # 4 — a receipt nobody has drawn for, and one past the order
        second = create_work_order(
            session, company_id=COMPANY, item=widget, quantity="2", number="WO-2",
            created_on=DAY,
        )
        session.commit()
        advance(session, second, status=RELEASED)
        advance(session, second, status=IN_PROGRESS)
        session.commit()
        said_uncovered = _refused(
            lambda: receive_finished_goods(session, second, location=finished, quantity="2",
                                           on=DAY),
            ConsumptionNotCovered,
        )
        session.rollback()
        second = session.get(type(second), second.id)
        first = session.get(type(first), first.id)
        order = session.get(type(order), order.id)
        said_too_much = _refused(
            lambda: receive_finished_goods(session, order, location=finished, quantity="1",
                                           on=DAY, backflush_location=store),
            OverReceiptError,
        )
        session.rollback()
        also_early = said_early
        assert "BLANK 4.000000" in said_uncovered, said_uncovered
        assert "does not make more than it was raised for" in said_too_much, said_too_much
        assert "is being worked" in also_early, also_early
        print(
            f"4. WO-2's receipt was refused because nobody had issued its material"
            f" ({said_uncovered[:48]}…) and WO-1 refused a seventh widget"
            f" ({said_too_much[:48]}…); a receipt before the job started is refused too"
            f" ({also_early[:40]}…)"
        )

        # 5 — the report a cost accountant reads before closing the job
        report = consumption_report(session, order)
        assert report["received"] == ORDERED, report
        assert report["wip"] == Decimal("0.000000"), report
        assert report["within_tolerance"] is True, report
        assert report["complete"] is True, report
        assert report["required"][0]["issued"] == Decimal("20.000000"), report["required"]
        print(
            f"5. the consumption report: received {report['received']} of"
            f" {report['ordered']}, WIP {report['wip']}, inside tolerance"
            f" {report['within_tolerance']}, complete {report['complete']} — every"
            f" requirement issued exactly ({report['required'][0]['issued']} of"
            f" {report['required'][0]['required']})"
        )

    print("\ncheck_finished_goods: all assertions green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
