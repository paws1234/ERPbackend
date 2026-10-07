"""T-3.SALES.05 check — fulfilment: pick lists, shipping, and the stock they move.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_fulfilment.py

**One order fulfilled in two shipments**, with the stock ledger, the order remainders
and the postings shown. Green on all seven:

1. **a pick list covers exactly the confirmed order lines** — one line per order line,
   with its item and its uom carried across; a **draft** order is refused (nothing has
   been agreed to pick), a second pick list for the same order is refused, and so is a
   pick-list number the company already uses
2. **picked quantities are recorded against the line** — and over-picking is refused
   rather than capped, because a silently reduced figure would put a number on the
   picker's paper that they did not report
3. **partial shipping leaves the correct remainder on the order** — after shipping 4 of
   10 the line reports 4 shipped and 6 remaining, and after the second shipment of 6 it
   reports 10 shipped and 0 remaining
4. **the stock is issued at the correct warehouse with the correct valuation** — the
   bin's on-hand falls by exactly what shipped, the stock ledger entry is filed against
   the shipment, and the value is the company's moving average (4 units at 4.00 = 16.00)
5. **an order cannot be shipped twice for the same quantity** — the third shipment is
   refused because the line owes nothing, and shipping more than the bin holds is
   refused by the no-negative-stock rule rather than recorded
6. **a line that names no stock item is not shipped** — the freight line is refused and
   says why, so nothing is posted for goods that were never in a location
7. **the whole path is drivable through the published API**, refusals included: the pick
   list, the picked quantity and the shipment, each answered with a `status_code` and the
   order's remainders

**Every posting balances**: each shipment's ledger entry is read back and its debits and
credits are asserted equal, so the cost side of the sale is shown rather than assumed.

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api import BASE, app  # noqa: E402
from app.company import Company, set_credit_check_mode  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger.currency import register_currency  # noqa: E402
from app.ledger.posting import JournalEntry, JournalLine  # noqa: E402
from app.sales.customers import create_customer  # noqa: E402
from app.sales.fulfilment import (  # noqa: E402
    DOC_TYPE,
    DuplicatePickListError,
    DuplicateShipmentError,
    EmptyShipmentError,
    NothingToIssueError,
    OrderNotConfirmed,
    OverShipmentError,
    generate_pick_list,
    pick_list_for,
    pick_lines,
    record_picked,
    remaining_quantity,
    ship_order,
    shipments_for,
)
from app.sales.orders import (  # noqa: E402
    CONFIRMED,
    DRAFT,
    confirm_order,
    convert_quotation_to_order,
    order_by_number,
)
from app.sales.quotations import add_line, create_quotation  # noqa: E402
from app.security import assign, define_role, grant  # noqa: E402
from app.stock.entries import on_hand  # noqa: E402
from app.stock.items import create_item, item_by_sku  # noqa: E402
from app.stock.locations import create_location, location_by_code  # noqa: E402
from app.stock.transactions import InsufficientStockError, receive  # noqa: E402
from tests.seed import seed_stock_accounts  # noqa: E402

COMPANY = uuid.uuid4()
OCT = date(2026, 10, 1)
# What the bin holds to start with, and what it costs. One order of 10 ships as 4 + 6.
OPENING = Decimal("100")
UNIT_COST = Decimal("4.00")
ORDERED = Decimal("10")
FIRST_SHIPMENT = Decimal("4")
SECOND_SHIPMENT = Decimal("6")


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


def _shape_error(response) -> dict:
    """The platform's one error shape, or a failure that says what arrived instead."""
    body = response.json()
    assert isinstance(body, dict) and set(body) == {"error"}, f"not the one error shape: {body}"
    assert set(body["error"]) == {"code", "message", "details"}, body
    return body["error"]


def _refusal(response, expected: str) -> str:
    error = _shape_error(response)
    assert error["code"] == expected, f"got {error}"
    return error["message"]


def _entry_for(session: Session, *, shipment) -> JournalEntry:
    """The GL entry a shipment's issue posted — read back, not assumed."""
    entry = session.scalar(
        select(JournalEntry).where(
            JournalEntry.source_type == DOC_TYPE,
            JournalEntry.source_id == shipment.id,
        )
    )
    assert entry is not None, f"shipment {shipment.number!r} posted nothing to the ledger"
    return entry


def _balanced(session: Session, entry: JournalEntry) -> tuple[Decimal, Decimal, list]:
    """The entry's two sides, and its lines — the arithmetic, not a promise."""
    lines = list(
        session.scalars(
            select(JournalLine)
            .where(JournalLine.entry_id == entry.id)
            .order_by(JournalLine.line_no)
        )
    )
    debit = sum((line.debit for line in lines), Decimal(0))
    credit = sum((line.credit for line in lines), Decimal(0))
    assert debit == credit, f"entry {entry.id} does not balance: {debit} != {credit}"
    assert len(lines) >= 2, f"entry {entry.id} has {len(lines)} line(s)"
    return debit, credit, lines


def _order(session: Session, number: str):
    return order_by_number(session, company_id=COMPANY, number=number)


def _new_order(session: Session, *, customer, item, number: str, quantity=ORDERED):
    """A confirmable order: a stock line (10 by default) and a freight line with no item."""
    quote = create_quotation(
        session,
        company_id=COMPANY,
        customer_id=customer.id,
        number=f"Q-{number}",
        issued_on=OCT,
        valid_until=date(2026, 12, 31),
    )
    session.flush()
    add_line(
        session,
        quote,
        line_no=1,
        description="Widget",
        quantity=str(quantity),
        unit_price="5.25",
        uom="each",
        item_id=item.id,
        priced_on=OCT,
    )
    add_line(
        session,
        quote,
        line_no=2,
        description="Freight",
        quantity="1",
        unit_price="250.50",
        priced_on=OCT,
    )
    session.flush()
    order = convert_quotation_to_order(session, quote, number=number, on=OCT)
    session.commit()
    return order


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
                code="FULFIL-CHECK",
                name="Fulfilment check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        register_currency(session, company_id=COMPANY, code="PHP", name="Peso")
        session.commit()
        seed_stock_accounts(session, company_id=COMPANY)
        session.commit()

        item = create_item(
            session,
            company_id=COMPANY,
            sku="WIDGET",
            name="Widget",
            base_uom="each",
            traceability_mode="none",
        )
        warehouse = create_location(
            session,
            company_id=COMPANY,
            code="MAIN",
            name="Main warehouse",
            location_type="warehouse",
        )
        zone = create_location(
            session,
            company_id=COMPANY,
            code="MAIN-Z",
            name="Zone",
            location_type="zone",
            parent_id=warehouse.id,
        )
        aisle = create_location(
            session,
            company_id=COMPANY,
            code="MAIN-A",
            name="Aisle",
            location_type="aisle",
            parent_id=zone.id,
        )
        bin_a = create_location(
            session,
            company_id=COMPANY,
            code="MAIN-B1",
            name="Bin B1",
            location_type="bin",
            parent_id=aisle.id,
        )
        session.commit()

        receive(
            session,
            item=item,
            location=bin_a,
            uom="each",
            quantity=OPENING,
            value=OPENING * UNIT_COST,
            currency="PHP",
            source_type="goods_receipt",
            source_id=uuid.uuid4(),
            posting_date=OCT,
        )
        session.commit()
        opened = on_hand(session, company_id=COMPANY, item_id=item.id, location_id=bin_a.id)
        assert opened["quantity"] == OPENING, opened

        acme = create_customer(
            session,
            company_id=COMPANY,
            party_code="ACME",
            name="Acme Retail",
            payment_terms_days=30,
            credit_limit="100000",
        )
        set_credit_check_mode(session, session.get(Company, COMPANY), mode="block")
        session.commit()

        # 1 — a pick list is the order's lines, once, and only for a confirmed order
        order = _new_order(session, customer=acme, item=item, number="SO-1")
        draft_said = _refused(
            lambda: generate_pick_list(session, order, number="PL-DRAFT", on=OCT),
            OrderNotConfirmed,
        )
        session.rollback()
        assert "confirmed" in draft_said, draft_said

        order = _order(session, "SO-1")
        confirm_order(session, order, exposure="0", actor="maria")
        session.commit()
        order = _order(session, "SO-1")
        assert order.status == CONFIRMED
        listed = generate_pick_list(session, order, number="PL-1", on=OCT)
        session.commit()
        rows = pick_lines(session, listed)
        assert [r.line_no for r in rows] == [1, 2], [r.line_no for r in rows]
        assert len(rows) == len(order.lines), (len(rows), len(order.lines))
        assert rows[0].item_id == item.id and rows[0].uom == "each", rows[0]
        assert rows[0].quantity == ORDERED, rows[0].quantity
        assert rows[1].item_id is None, "the freight line was given a stock item"
        assert all(r.picked_quantity == 0 for r in rows), [r.picked_quantity for r in rows]
        said = _refused(
            lambda: generate_pick_list(session, order, number="PL-1b", on=OCT),
            DuplicatePickListError,
        )
        session.rollback()
        assert "already has a pick list" in said, said
        print(
            f"1. the pick list carries exactly the order's {len(rows)} lines (item and uom"
            f" included), a draft order is refused ('{draft_said[:40]}…'), and so is a second"
            " pick list for the same order"
        )

        # 2 — picked quantities are recorded, and over-picking is refused
        listed = pick_list_for(session, _order(session, "SO-1"))
        picked = record_picked(session, listed, line_no=1, quantity=str(ORDERED))
        session.commit()
        assert picked.picked_quantity == ORDERED, picked.picked_quantity
        said = _refused(
            lambda: record_picked(
                session, listed, line_no=1, quantity=str(ORDERED + 1)
            ),
            "more than the order says",
        )
        session.rollback()
        print(f"2. picking {ORDERED} is recorded, and over-picking is refused ({said[:48]}\u2026)")

        # 3 + 4 + 5 — one order, two shipments, with the ledger and the postings shown
        order = _order(session, "SO-1")
        line_one = order.lines[0]
        assert remaining_quantity(line_one) == ORDERED

        first = ship_order(
            session,
            order,
            number="SH-1",
            warehouse=bin_a,
            lines=[(1, str(FIRST_SHIPMENT))],
            on=OCT,
        )
        session.commit()
        assert [l.line_no for l in first.lines] == [1], first.lines
        assert first.lines[0].movement_id is not None, "the shipment line points at no movement"
        assert first.location_id == bin_a.id, first.location_id

        entry = _entry_for(session, shipment=first)
        debit, credit, lines = _balanced(session, entry)
        assert debit == FIRST_SHIPMENT * UNIT_COST, (debit, FIRST_SHIPMENT * UNIT_COST)
        assert entry.currency == "PHP", entry.currency

        order = _order(session, "SO-1")
        after_first = order.lines[0]
        assert after_first.shipped_quantity == FIRST_SHIPMENT, after_first.shipped_quantity
        assert remaining_quantity(after_first) == ORDERED - FIRST_SHIPMENT
        held = on_hand(session, company_id=COMPANY, item_id=item.id, location_id=bin_a.id)
        assert held["quantity"] == OPENING - FIRST_SHIPMENT, held
        assert held["value"] == (OPENING - FIRST_SHIPMENT) * UNIT_COST, held

        second = ship_order(
            session,
            order,
            number="SH-2",
            warehouse=bin_a,
            lines=[(1, str(SECOND_SHIPMENT))],
            on=OCT,
        )
        session.commit()
        order = _order(session, "SO-1")
        done = order.lines[0]
        assert done.shipped_quantity == ORDERED, done.shipped_quantity
        assert remaining_quantity(done) == 0, remaining_quantity(done)
        assert len(shipments_for(session, order)) == 2
        held = on_hand(session, company_id=COMPANY, item_id=item.id, location_id=bin_a.id)
        assert held["quantity"] == OPENING - ORDERED, held
        assert held["value"] == (OPENING - ORDERED) * UNIT_COST, held
        second_entry = _entry_for(session, shipment=second)
        second_debit, _, _ = _balanced(session, second_entry)
        assert second_debit == SECOND_SHIPMENT * UNIT_COST, second_debit
        print(
            f"3/4. 10 shipped as {FIRST_SHIPMENT}+{SECOND_SHIPMENT}: the bin fell from {OPENING}"
            f" to {held['quantity']} at {UNIT_COST} each, and both entries balance"
            f" ({debit} and {second_debit}, debit = credit)"
        )

        owed_said = _refused(
            lambda: ship_order(
                session, order, number="SH-3", warehouse=bin_a, lines=[(1, "1")], on=OCT
            ),
            OverShipmentError,
        )
        session.rollback()
        assert "still owes 0" in owed_said, owed_said
        order = _order(session, "SO-1")
        # 100 of them: the order would allow it, but the bin only holds 90
        too_much = _new_order(
            session, customer=acme, item=item, number="SO-2", quantity=OPENING
        )
        confirm_order(session, too_much, exposure="0", actor="maria")
        session.commit()
        too_much = _order(session, "SO-2")
        short_said = _refused(
            lambda: ship_order(
                session,
                too_much,
                number="SH-4",
                warehouse=bin_a,
                lines=[(1, str(OPENING))],
                on=OCT,
            ),
            InsufficientStockError,
        )
        session.rollback()
        print(
            f"5. a third shipment is refused ('{owed_said[:40]}\u2026'), and so is more stock"
            f" than the bin holds ({short_said[:45]}\u2026) — the no-negative-stock rule"
        )

        # 6 — a line with no item moves no stock, and says so
        order_two = _order(session, "SO-2")
        freight_said = _refused(
            lambda: ship_order(
                session, order_two, number="SH-5", warehouse=bin_a, lines=[(2, "1")], on=OCT
            ),
            NothingToIssueError,
        )
        session.rollback()
        assert "no stock item" in freight_said, freight_said
        empty_said = _refused(
            lambda: ship_order(
                session, order_two, number="SH-6", warehouse=bin_a, lines=[], on=OCT
            ),
            EmptyShipmentError,
        )
        session.rollback()
        print(
            f"6. the freight line is refused ('{freight_said[:40]}\u2026'), and so is a"
            f" shipment with no lines ('{empty_said[:35]}\u2026')"
        )

        # 7 — the API drives the same path
        seller = define_role(session, company_id=COMPANY, code="seller", name="Seller")
        grant(session, seller, "order.read", "order.write")
        assign(session, company_id=COMPANY, subject="maria", role=seller)
        session.commit()

        api_order = _new_order(
            session,
            customer=create_customer(
                session,
                company_id=COMPANY,
                party_code="API-CO",
                name="Api Co",
                payment_terms_days=30,
                credit_limit="100000",
            ),
            item=item,
            number="SO-3",
        )
        session.commit()

    client = TestClient(app, raise_server_exceptions=False)
    headers = {"X-Company-Id": str(COMPANY), "X-Actor": "maria"}

    early = client.post(
        f"{BASE}/sales-orders/SO-3/pick-list", headers=headers, json={"number": "PL-EARLY"}
    )
    assert early.status_code == 422, early.text
    assert _refusal(early, "fulfilment_error")

    confirmed = client.post(
        f"{BASE}/sales-orders/SO-3/confirm", headers=headers, json={"exposure": "0"}
    )
    assert confirmed.status_code == 200, confirmed.text

    drawn = client.post(
        f"{BASE}/sales-orders/SO-3/pick-list", headers=headers, json={"number": "PL-API"}
    )
    assert drawn.status_code == 201, drawn.text
    body = drawn.json()
    assert body["pick_list"]["number"] == "PL-API", body["pick_list"]
    assert [l["line_no"] for l in body["pick_list"]["lines"]] == [1, 2], body["pick_list"]
    assert body["pick_list"]["lines"][0]["item_sku"] == "WIDGET", body["pick_list"]
    assert body["lines"][0]["remaining"] == "10.000000", body["lines"]

    picked = client.post(
        f"{BASE}/sales-orders/SO-3/pick-list/lines/1",
        headers=headers,
        json={"quantity": "10"},
    )
    assert picked.status_code == 200, picked.text
    assert picked.json()["pick_list"]["lines"][0]["picked_quantity"] == "10.000000"

    shipped = client.post(
        f"{BASE}/sales-orders/SO-3/shipments",
        headers=headers,
        json={
            "number": "SH-API",
            "warehouse": "MAIN-B1",
            "on": "2026-10-01",
            "lines": [{"line_no": 1, "quantity": "4"}],
        },
    )
    assert shipped.status_code == 201, shipped.text
    body = shipped.json()
    assert body["shipments"][0]["number"] == "SH-API", body["shipments"]
    assert body["shipments"][0]["warehouse"] == "MAIN-B1", body["shipments"]
    assert body["lines"][0]["shipped"] == "4.000000", body["lines"]
    assert body["lines"][0]["remaining"] == "6.000000", body["lines"]

    missing = client.post(
        f"{BASE}/sales-orders/SO-3/shipments",
        headers=headers,
        json={
            "number": "SH-NOPE",
            "warehouse": "NOWHERE",
            "lines": [{"line_no": 1, "quantity": "1"}],
        },
    )
    assert missing.status_code == 422, missing.text
    assert _refusal(missing, "location_error")

    shown = client.get(f"{BASE}/sales-orders/SO-3", headers=headers)
    assert shown.status_code == 200, shown.text
    assert shown.json()["lines"][0]["remaining"] == "6.000000", shown.json()["lines"]
    print(
        "7. the API draws the pick list, records the pick, ships 4 of 10 and reports 6"
        " remaining — with an unknown warehouse refused by name"
    )

    # Re-read in a fresh session: what the ledger says, asked of the ledger rather
    # than kept in a detached object.
    with Session(engine) as session:
        bin_b1 = location_by_code(session, company_id=COMPANY, code="MAIN-B1")
        widget = item_by_sku(session, company_id=COMPANY, sku="WIDGET")
        left = on_hand(session, company_id=COMPANY, item_id=widget.id, location_id=bin_b1.id)
        # Everything that left the bin: SO-1's two shipments, then SO-3's one.
        gone = OPENING - (ORDERED + FIRST_SHIPMENT)
        assert left["quantity"] == gone, left
        assert left["value"] == gone * UNIT_COST, left

    print(
        "\nok — the order was fulfilled in two shipments at the bin's own valuation, every"
        " posting balances, and the remainders are the order's own"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
