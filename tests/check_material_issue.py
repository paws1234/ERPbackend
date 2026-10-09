"""T-4.WO.03 check — issuing material to a work order, and the WIP it books.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_material_issue.py

Green on all five:

1. a **partial issue** leaves the remaining requirement visible, derived from the issue
   rows rather than kept beside them
2. the issue writes a **stock ledger row** and a **balanced WIP posting** at the costing
   method's value — inventory down, work in progress up, by the same amount
3. an **over-issue is refused**, and accepted once somebody overrides it — with the
   actor and the reason kept on the issue row
4. an item the job **does not call for** is refused, and so is more than the store holds
5. the **WIP the order is carrying** is the sum of its issues, which is what the
   finished-goods receipt clears
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
from app.manufacturing.issues import (  # noqa: E402
    NotRequiredError,
    OverIssueError,
    issued_to_wip,
    issues_of,
    issue_material,
    issue_lines,
    location_stock,
    outstanding,
)
from app.manufacturing.work_orders import create_work_order  # noqa: E402
from app.stock.entries import movements_for_source  # noqa: E402
from app.stock.items import create_item  # noqa: E402
from app.stock.locations import create_location  # noqa: E402
from app.stock.transactions import InsufficientStockError, receive  # noqa: E402
from tests.seed import seed_stock_accounts  # noqa: E402

COMPANY = uuid.uuid4()
DAY = date(2026, 9, 15)
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


def _entry(session: Session, *, source_id):
    """The journal entry one issue posted."""
    return session.scalar(
        select(JournalEntry).where(
            JournalEntry.source_type == "work_order_issue",
            JournalEntry.source_id == source_id,
        )
    )


def _touched(session: Session, entry: JournalEntry) -> dict[str, Decimal]:
    """What an entry did to each account, in the accounts' own codes."""
    out: dict[str, Decimal] = {}
    for line in session.scalars(
        select(JournalLine).where(JournalLine.entry_id == entry.id)
    ):
        signed = Decimal(line.debit) - Decimal(line.credit)
        out[line.account] = (out.get(line.account, Decimal(0)) + signed).quantize(
            Decimal("0.000001")
        )
    return out


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
        company = Company(
            id=COMPANY,
            code="ISSUE-CHECK",
            name="Issue check",
            base_currency="PHP",
            fiscal_year_start_month=1,
        )
        session.add(company)
        session.commit()
        seed_stock_accounts(session, company_id=COMPANY)
        # The WIP account the mapping key points at: the counterpart of a material
        # issue, cleared by the finished-goods receipt (T-4.WO.04).
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
        add_line(session, bom, item=blank, quantity="1")
        release(session, bom)
        warehouse = create_location(session, company_id=COMPANY, code="MAIN", name="Main",
                                    location_type="warehouse")
        zone = create_location(session, company_id=COMPANY, code="MAIN-Z", name="Zone",
                               location_type="zone", parent_id=warehouse.id)
        aisle = create_location(session, company_id=COMPANY, code="MAIN-Z-1", name="Aisle",
                                location_type="aisle", parent_id=zone.id)
        bin_a1 = create_location(session, company_id=COMPANY, code="MAIN-Z-1-A",
                                 name="Bin A", location_type="bin", parent_id=aisle.id)
        session.commit()
        # 30 blanks at 300 in total: ten a unit, so every issue below is exactly 10 a unit.
        receive(session, item=blank, location=bin_a1, uom="each", quantity="30",
                value=Decimal("300"), currency="PHP", source_type="goods_receipt",
                source_id=uuid.uuid4(), posting_date=DAY)
        session.commit()
        order = create_work_order(
            session, company_id=COMPANY, item=widget, quantity=ORDERED, number="WO-1",
            created_on=DAY, due_on=date(2026, 9, 30),
        )
        session.commit()

        # 1 — a partial issue, and the remaining requirement
        first = issue_material(session, order, item=blank, location=bin_a1, quantity="6",
                              on=DAY, actor="ana")
        session.commit()
        remaining = outstanding(session, order)
        assert remaining == [
            {
                "item": "BLANK",
                "level": 1,
                "required": Decimal("10.000000"),
                "issued": Decimal("6.000000"),
                "remaining": Decimal("4.000000"),
                "uom": "each",
            }
        ], remaining
        print(
            f"1. {first.quantity} of BLANK issued of the {remaining[0]['required']} the"
            f" job requires: {remaining[0]['remaining']} left, and the figure comes from"
            f" the {len(issues_of(session, order))} issue row rather than a balance kept"
            " beside it"
        )

        # 2 — the ledger row and the balanced WIP posting, at the costing method's value
        movements = movements_for_source(
            session, company_id=COMPANY, source_type="work_order_issue", source_id=first.id
        )
        assert len(movements) == 1, movements
        assert movements[0].quantity == Decimal("-6.000000"), movements[0].quantity
        assert movements[0].value == Decimal("-60.000000"), movements[0].value
        entry = _entry(session, source_id=first.id)
        assert entry is not None, "the issue posted no entry"
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
        assert debits == credits == Decimal("60.000000"), (debits, credits)
        touched = _touched(session, entry)
        assert touched == {"1200": Decimal("-60.000000"), "1230": Decimal("60.000000")}, touched
        assert location_stock(session, item=blank, location=bin_a1) == Decimal("24.000000"), (
            location_stock(session, item=blank, location=bin_a1)
        )
        print(
            f"2. the issue wrote one stock row ({movements[0].quantity} at"
            f" {movements[0].value}) and one balanced entry — inventory"
            f" {touched['1200']}, work in progress {touched['1230']} — valued at the"
            f" costing method's ten a unit, and the bin holds"
            f" {location_stock(session, item=blank, location=bin_a1)}"
        )

        # 3 — past the tolerance: refused, then overridden and recorded
        within = issue_material(session, order, item=blank, location=bin_a1, quantity="4.25",
                               on=DAY, actor="ana")
        session.commit()
        said_over = _refused(
            lambda: issue_material(session, order, item=blank, location=bin_a1,
                                   quantity="1", on=DAY, actor="ana"),
            OverIssueError,
        )
        session.rollback()
        order = session.get(type(order), order.id)
        overridden = issue_material(
            session, order, item=blank, location=bin_a1, quantity="1", on=DAY,
            actor="maria", override=True, override_reason="the last length was short",
        )
        session.commit()
        assert overridden.overridden is True, overridden.overridden
        assert overridden.override_actor == "maria", overridden.override_actor
        assert overridden.override_reason == "the last length was short", overridden
        final = outstanding(session, order)[0]
        assert final["issued"] == Decimal("11.250000"), final
        assert final["remaining"] == Decimal("-1.250000"), final
        assert within.quantity == Decimal("4.250000"), within.quantity
        print(
            f"3. a fourth issue would have taken the job to 11.25 against a requirement"
            f" of 10 and a 5 % ceiling of 10.5: refused ({said_over[:44]}…), then"
            f" accepted once maria owned it ({overridden.override_reason}) — the issue"
            f" row carries the override, and the requirement now reads"
            f" {final['issued']} issued"
        )

        # 4 — what the job did not call for, and what the store does not hold
        said_not_required = _refused(
            lambda: issue_material(session, order, item=widget, location=bin_a1,
                                   quantity="1", on=DAY, actor="ana"),
            NotRequiredError,
        )
        session.rollback()
        order = session.get(type(order), order.id)
        said_empty = _refused(
            lambda: issue_material(session, order, item=blank, location=bin_a1,
                                   quantity="30", on=DAY, actor="ana", override=True,
                                   override_reason="a lot comes off at once"),
            InsufficientStockError,
        )
        session.rollback()
        order = session.get(type(order), order.id)
        assert "does not call for" in said_not_required, said_not_required
        assert "would take it" in said_empty, said_empty
        print(
            f"4. a WIDGET issue was refused — the job requires BLANK, not the thing it"
            f" makes ({said_not_required[:40]}…) — and so was thirty blanks from a bin"
            f" holding"
            f" {location_stock(session, item=blank, location=bin_a1)} ({said_empty[:40]}…)"
        )

        # 5 — the WIP the order carries, summed from its issues
        wip = issued_to_wip(session, order)
        lines = issue_lines(session, order)
        assert wip["issues"] == 3, wip
        assert wip["value"] == Decimal("112.500000"), wip
        assert wip["overridden"] == ["the last length was short"], wip
        assert [row["value"] for row in lines] == [
            Decimal("60.000000"),
            Decimal("42.500000"),
            Decimal("10.000000"),
        ], lines
        assert sum(row["value"] for row in lines) == wip["value"], lines
        print(
            f"5. the order is carrying {wip['value']} of material in work in progress"
            f" over {wip['issues']} issues"
            f" ({[str(row['value']) for row in lines]}) — the figure T-4.WO.04 clears"
            " when the finished goods are received"
        )

    print("\ncheck_material_issue: all assertions green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
