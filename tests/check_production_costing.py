"""T-4.WO.05 check — the job costed by hand, the variance posted, the goods carrying it.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_production_costing.py

Green on all five:

1. the cost is **material plus booked time at the dated rates**, hand-checked, and the
   scrap the job consumed is inside the material rather than lost
2. the **finished goods carry the job's own cost** — the receipts' material plus the
   labour they could not yet know
3. the **variance against the standard** is computed and posted, both entries balance,
   and the applied account ends stating what the standard expected the job to use
4. **costing twice posts once**: a second call returns the recorded figures and the
   ledger has no second entry
5. a job whose output is **not all received** is refused, and so is an item with **no
   standard cost** to be judged against
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date, datetime, timezone
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
from app.manufacturing.costing import (  # noqa: E402
    LABOUR_KEY,
    VARIANCE_KEY,
    WorkOrderNotComplete,
    cost_summary,
    cost_work_order,
    costing_of,
    counted,
    labour_breakdown,
)
from app.manufacturing.job_cards import book_time, close_card, open_card  # noqa: E402
from app.manufacturing.receipts import receive_finished_goods  # noqa: E402
from app.manufacturing.routing import add_operation  # noqa: E402
from app.manufacturing.work_centers import create_work_center, rate_on, set_rate  # noqa: E402
from app.manufacturing.work_orders import (  # noqa: E402
    IN_PROGRESS,
    RELEASED,
    advance,
    create_work_order,
)
from app.stock.items import create_item  # noqa: E402
from app.stock.locations import create_location  # noqa: E402
from app.stock.transactions import receive  # noqa: E402
from app.stock.valuation import MissingStandardCostError  # noqa: E402
from tests.seed import seed_stock_accounts  # noqa: E402

COMPANY = uuid.uuid4()
DAY = date(2026, 9, 17)
ORDERED = Decimal("10")
STANDARD = Decimal("50")


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


def _lines(session: Session, entry: JournalEntry) -> list[dict]:
    rows = session.scalars(
        select(JournalLine).where(JournalLine.entry_id == entry.id).order_by(JournalLine.line_no)
    )
    return [
        {
            "account": row.account,
            "debit": Decimal(row.debit).quantize(Decimal("0.000001")),
            "credit": Decimal(row.credit).quantize(Decimal("0.000001")),
        }
        for row in rows
    ]


def _balance(session: Session, code: str) -> Decimal:
    total = sum(
        (
            Decimal(line.debit) - Decimal(line.credit)
            for line in session.scalars(select(JournalLine))
            if line.account == code
        ),
        Decimal(0),
    )
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
            Company(id=COMPANY, code="COST-CHECK", name="Costing check",
                    base_currency="PHP", fiscal_year_start_month=1)
        )
        session.commit()
        seed_stock_accounts(session, company_id=COMPANY)
        create_account(session, company_id=COMPANY, code="1230", name="Work in Progress",
                       account_class="asset")
        create_account(session, company_id=COMPANY, code="2150", name="Labour Applied",
                       account_class="liability")
        create_account(session, company_id=COMPANY, code="5950", name="Production Variance",
                       account_class="expense")
        set_mapping(session, company_id=COMPANY, key="work_in_progress", account_code="1230")
        set_mapping(session, company_id=COMPANY, key=LABOUR_KEY, account_code="2150")
        set_mapping(session, company_id=COMPANY, key=VARIANCE_KEY, account_code="5950")
        session.commit()

        cutting = create_work_center(session, company_id=COMPANY, code="CUT",
                                    name="Cutting bench", capacity_minutes="480",
                                    capacity_period="day", downtime_percent="10")
        welding = create_work_center(session, company_id=COMPANY, code="WELD",
                                     name="Welding bay", capacity_minutes="1400",
                                     capacity_period="week")
        set_rate(session, cutting, effective_from=date(2026, 1, 1), hourly_rate="250.00")
        set_rate(session, welding, effective_from=date(2026, 1, 1), hourly_rate="410.00")
        widget = create_item(session, company_id=COMPANY, sku="WIDGET", name="Widget",
                             base_uom="each", traceability_mode="none",
                             standard_cost=STANDARD)
        blank = create_item(session, company_id=COMPANY, sku="BLANK", name="Blank",
                            base_uom="each", traceability_mode="none")
        plain = create_item(session, company_id=COMPANY, sku="PLAIN", name="Plain",
                            base_uom="each", traceability_mode="none")
        session.commit()
        bom = create_bom(session, company_id=COMPANY, item=widget)
        add_line(session, bom, item=blank, quantity="2")
        add_operation(session, bom, name="Cut", work_center_code="CUT",
                      setup_minutes="15", run_minutes="10")
        add_operation(session, bom, name="Weld", work_center_code="WELD",
                      setup_minutes="15", run_minutes="3")
        release(session, bom)
        plain_bom = create_bom(session, company_id=COMPANY, item=plain)
        add_line(session, plain_bom, item=blank, quantity="1")
        add_operation(session, plain_bom, name="Cut", work_center_code="CUT",
                      setup_minutes="5", run_minutes="1")
        release(session, plain_bom)
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
        receive(session, item=blank, location=store, uom="each", quantity="60",
                value=Decimal("600"), currency="PHP", source_type="goods_receipt",
                source_id=uuid.uuid4(), posting_date=DAY)
        session.commit()

        order = create_work_order(session, company_id=COMPANY, item=widget,
                                  quantity=ORDERED, number="WO-1", created_on=DAY,
                                  due_on=date(2026, 9, 30))
        session.commit()
        advance(session, order, status=RELEASED)
        advance(session, order, status=IN_PROGRESS)
        session.commit()

        # 120 minutes on the cutting bench is exactly two hours at 250 = 500; 45 minutes
        # of welding is three quarters of an hour at 410 = 307.50.
        # Booked on the day the job ran: a booking's own day is what the rate is read
        # at, so the check states it rather than leaving it to the clock.
        cut_at = datetime(2026, 9, 17, 9, 0, tzinfo=timezone.utc)
        weld_at = datetime(2026, 9, 17, 14, 0, tzinfo=timezone.utc)
        cut_card = open_card(session, order, operation_sequence=1, operator="ana", on=DAY)
        book_time(session, cut_card, setup_minutes="15", run_minutes="105",
                  produced_quantity="4", recorded_by="ana", booked_at=cut_at)
        close_card(session, cut_card, actor="ana")
        session.commit()
        weld_card = open_card(session, order, operation_sequence=2, operator="ben", on=DAY)
        book_time(session, weld_card, setup_minutes="15", run_minutes="30",
                  produced_quantity="4", recorded_by="ben", booked_at=weld_at)
        close_card(session, weld_card, actor="ben")
        session.commit()
        receive_finished_goods(session, order, location=finished, quantity="4", on=DAY,
                               backflush_location=store, actor="ana")
        receive_finished_goods(session, order, location=finished, quantity="6", on=DAY,
                               backflush_location=store, actor="ana")
        session.commit()

        # 1 — material plus booked time at the dated rates
        figures = cost_summary(session, order)
        rows = {row["work_center"]: row for row in figures["labour_rows"]}
        assert figures["material"] == Decimal("200.000000"), figures
        assert rows["CUT"]["minutes"] == Decimal("120.000000"), rows["CUT"]
        assert rows["CUT"]["hourly_rate"] == Decimal("250.000000"), rows["CUT"]
        assert rows["CUT"]["cost"] == Decimal("500.000000"), rows["CUT"]
        assert rows["WELD"]["minutes"] == Decimal("45.000000"), rows["WELD"]
        assert rows["WELD"]["cost"] == Decimal("307.500000"), rows["WELD"]
        assert rows["WELD"]["booked_on"] == DAY, rows["WELD"]
        assert figures["labour"] == Decimal("807.500000"), figures
        assert figures["total"] == Decimal("1007.500000"), figures
        # 20 blanks at ten a unit is the material, and the requirement already carried
        # any scrap: what the job consumed is what it cost.
        assert figures["expected"] == (STANDARD * ORDERED), figures
        assert figures["variance"] == Decimal("507.500000"), figures
        print(
            f"1. material {figures['material']} (20 blanks at ten, scrap already inside"
            f" the requirement) plus labour {figures['labour']} —"
            f" {rows['CUT']['minutes']} minutes on CUT at"
            f" {rows['CUT']['hourly_rate']} = {rows['CUT']['cost']},"
            f" {rows['WELD']['minutes']} on WELD = {rows['WELD']['cost']} — gives"
            f" {figures['total']} against a standard of {figures['expected']}"
        )

        # 2 — before the posting, the goods carry only the material the receipts knew
        assert figures["capitalised"] == Decimal("200.000000"), figures
        cost = cost_work_order(session, order)
        session.commit()
        summary = cost_summary(session, order)
        assert summary["costed"] is True, summary
        assert summary["finished_goods"] == Decimal("1007.500000"), summary
        print(
            f"2. the receipts had capitalised {figures['capitalised']} of material; the"
            f" costing added the {figures['labour']} of labour they could not yet know,"
            f" and the finished goods now stand at {summary['finished_goods']} — the"
            " job's own cost, not a material-only figure"
        )

        # 3 — the variance, posted and balanced, with the applied account at standard
        entry = session.get(JournalEntry, cost.entry_id)
        lines = _lines(session, entry)
        # Inventory takes the 807.50 of labour the receipts could not know; the applied
        # account is credited with the 300 the standard allowed the output (500 standard
        # less the 200 of material already capitalised); and the 507.50 the job cost
        # beyond that is credited to the variance account.
        assert lines == [
            {"account": "1200", "debit": Decimal("807.500000"), "credit": Decimal("0.000000")},
            {"account": "2150", "debit": Decimal("0.000000"), "credit": Decimal("300.000000")},
            {"account": "5950", "debit": Decimal("0.000000"), "credit": Decimal("507.500000")},
        ], lines
        assert sum(line["debit"] for line in lines) == sum(
            line["credit"] for line in lines
        ), lines
        assert _balance(session, "2150") == Decimal("-300.000000"), _balance(session, "2150")
        assert _balance(session, "5950") == Decimal("-507.500000"), _balance(session, "5950")
        # Inventory, by hand: 600 of blanks received, 200 issued to the job, 200 of
        # finished goods received back, and the 807.50 of labour the costing added.
        assert _balance(session, "1200") == Decimal("1407.500000"), _balance(session, "1200")
        assert summary["finished_goods"] == Decimal("1007.500000"), summary
        print(
            f"3. the costing posted one entry that balances: inventory debited the"
            f" {figures['labour']} of labour (the account stands at"
            f" {_balance(session, '1200')}: 600 of blanks, 200 issued, 200 of goods back,"
            f" 807.50 of labour), the applied account credited"
            f" {-_balance(session, '2150')} — what the standard allowed the output (500"
            f" less the 200 of material the receipts carried) — and the variance account"
            f" credited {-_balance(session, '5950')}, the difference between the two"
        )

        # 4 — costing twice posts once
        def order_entries() -> int:
            return len(
                list(
                    session.scalars(
                        select(JournalEntry).where(JournalEntry.source_id == order.id)
                    )
                )
            )

        before_entries = order_entries()
        again = cost_work_order(session, order)
        session.commit()
        after_entries = order_entries()
        assert again.id == cost.id, (again.id, cost.id)
        assert after_entries == before_entries == 1, (before_entries, after_entries)
        assert counted(session, company_id=COMPANY) == 1, counted(session, company_id=COMPANY)
        assert costing_of(session, order).total_value == Decimal("1007.500000"), (
            costing_of(session, order).total_value
        )
        print(
            f"4. recosting returned the same recorded figures ({again.total_value}) and"
            f" the ledger still holds {after_entries} entry for the order — one"
            " costing per job, so a retry cannot post twice"
        )

        # 5 — a job with output outstanding, and an item with no standard
        short = create_work_order(session, company_id=COMPANY, item=widget, quantity="4",
                                  number="WO-2", created_on=DAY)
        session.commit()
        advance(session, short, status=RELEASED)
        advance(session, short, status=IN_PROGRESS)
        session.commit()
        said_incomplete = _refused(
            lambda: cost_work_order(session, short), WorkOrderNotComplete
        )
        session.rollback()
        plain_order = create_work_order(session, company_id=COMPANY, item=plain,
                                        quantity="2", number="WO-3", created_on=DAY)
        session.commit()
        advance(session, plain_order, status=RELEASED)
        advance(session, plain_order, status=IN_PROGRESS)
        session.commit()
        plain_card = open_card(session, plain_order, operation_sequence=1, operator="ana",
                               on=DAY)
        book_time(session, plain_card, setup_minutes="5", run_minutes="2",
                  produced_quantity="2", recorded_by="ana", booked_at=cut_at)
        close_card(session, plain_card, actor="ana")
        session.commit()
        receive_finished_goods(session, plain_order, location=finished, quantity="2",
                               on=DAY, backflush_location=store, actor="ana")
        session.commit()
        said_standard = _refused(
            lambda: cost_work_order(session, plain_order), MissingStandardCostError
        )
        session.rollback()
        assert "received 0.000000" in said_incomplete, said_incomplete
        assert "states no standard cost" in said_standard, said_standard
        assert len(labour_breakdown(session, order)) == 2, labour_breakdown(session, order)
        assert rate_on(session, cutting, on=DAY) == Decimal("250.000000"), "the dated rate moved"
        print(
            f"5. WO-2 was refused before its output arrived ({said_incomplete[:44]}…) and"
            f" WO-3 for having nothing to be judged against"
            f" ({said_standard[:44]}…) — a job is costed when it is finished, against a"
            " standard somebody stated"
        )

    print("\ncheck_production_costing: all assertions green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
