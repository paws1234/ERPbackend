"""T-4.WO.01 check — the work order, its requirements and the version it pins.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_work_orders.py

Green on all five:

1. a work order raised from a released BOM has **every requirement the explosion has**,
   up-lifted at every level, and says so in its own levels and paths
2. **a later BOM edit leaves it unchanged**: the item's BOM is revised and changed, and
   the order's requirements and route still match the version it pinned
3. a released BOM with **no routing** produces an order that reports the absence rather
   than assuming a route, and an item with **no released BOM at all is refused**
4. **status moves are transitions** — the legal step is taken and an illegal jump is
   refused with the steps that do follow
5. every move is **on the audit trail** with the before and the after, and the order's
   own requirement list reconciles to the explosion of the pin
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.audit import read_trail  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.manufacturing.bom import (  # noqa: E402
    add_line,
    create_bom,
    lines_of,
    release,
    revise,
)
from app.manufacturing.routing import add_operation  # noqa: E402
from app.manufacturing.work_orders import (  # noqa: E402
    IN_PROGRESS,
    NoBomError,
    PLANNED,
    RELEASED,
    WorkOrderStateError,
    advance,
    bom_of,
    create_work_order,
    missing_route,
    reconcile_requirements,
    requirements_of,
    route_of,
)
from app.stock.items import create_item  # noqa: E402

COMPANY = uuid.uuid4()
DAY = date(2026, 9, 1)
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
                code="WO-CHECK",
                name="Work order check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()

        def item(sku: str):
            made = create_item(
                session,
                company_id=COMPANY,
                sku=sku,
                name=sku.title(),
                base_uom="each",
                traceability_mode="none",
            )
            session.flush()
            return made

        bicycle = item("BICYCLE")
        frame = item("FRAME")
        wheel = item("WHEEL")
        tube = item("TUBE")
        alloy = item("ALLOY")
        session.commit()

        # The same shape as T-4.BOM.01's own check: a scrape on the frame's line
        # carries into the tube and the billet, and the frame is built from three tubes.
        bicycle_bom = create_bom(session, company_id=COMPANY, item=bicycle)
        add_line(session, bicycle_bom, item=frame, quantity="1", scrap_percent="5")
        add_line(session, bicycle_bom, item=wheel, quantity="2", scrap_percent="0")
        frame_bom = create_bom(session, company_id=COMPANY, item=frame)
        add_line(session, frame_bom, item=tube, quantity="3")
        tube_bom = create_bom(session, company_id=COMPANY, item=tube)
        add_line(session, tube_bom, item=alloy, quantity="2")
        add_operation(session, bicycle_bom, name="Cut", work_center_code="CUT",
                      setup_minutes="15", run_minutes="2")
        add_operation(session, bicycle_bom, name="Weld", work_center_code="WELD",
                      setup_minutes="20", run_minutes="3")
        session.commit()
        for bom in (frame_bom, tube_bom, bicycle_bom):
            release(session, bom)
        session.commit()

        # 1 — the requirements are the explosion, including scrap at every level
        order = create_work_order(
            session, company_id=COMPANY, item=bicycle, quantity=ORDERED,
            number="WO-1", created_on=DAY, due_on=date(2026, 9, 30),
        )
        session.commit()
        stored = {
            session.get(type(frame), row.item_id).sku: Decimal(row.quantity_required)
            for row in requirements_of(session, order)
        }
        assert stored == {
            "FRAME": Decimal("10.500000"),
            "WHEEL": Decimal("20.000000"),
            "TUBE": Decimal("31.500000"),
            "ALLOY": Decimal("63.000000"),
        }, stored
        levels = {session.get(type(frame), row.item_id).sku: row.level for row in requirements_of(session, order)}
        assert levels == {"FRAME": 1, "WHEEL": 1, "TUBE": 2, "ALLOY": 3}, levels
        paths = {row.path for row in requirements_of(session, order)}
        assert "BICYCLE → FRAME → TUBE → ALLOY" in paths, paths
        reconciled = reconcile_requirements(session, order)
        assert reconciled["matched"] is True, reconciled
        assert order.status == PLANNED, order.status
        assert [row.name for row in route_of(session, order)] == ["Cut", "Weld"], route_of(
            session, order
        )
        assert [row.planned_quantity for row in route_of(session, order)] == [
            ORDERED, ORDERED
        ], route_of(session, order)
        print(
            f"1. {order.number} for {ORDERED} bicycles carries {len(stored)} components"
            f" — {stored['FRAME']} frames (5 % scrapped), {stored['WHEEL']} wheels,"
            f" {stored['TUBE']} tubes and {stored['ALLOY']} billets — and its two"
            f" operations over {ORDERED} units each, all reconciled to the explosion"
        )

        # 2 — a later BOM edit cannot reach back into the job
        pinned = bom_of(session, order)
        assert pinned.version == bicycle_bom.version, (pinned.version, bicycle_bom.version)
        revised = revise(session, bicycle_bom)
        session.commit()
        add_line(session, revised, item=wheel, quantity="3")
        release(session, revised)
        session.commit()
        after = {
            session.get(type(frame), row.item_id).sku: Decimal(row.quantity_required)
            for row in requirements_of(session, order)
        }
        assert after == stored, (after, stored)
        assert reconcile_requirements(session, order)["matched"] is True, (
            reconcile_requirements(session, order)
        )
        assert [row.name for row in route_of(session, order)] == ["Cut", "Weld"], (
            "the pinned route moved with the BOM"
        )
        print(
            f"2. the bicycle's BOM was revised to v{revised.version} and its wheel line"
            f" changed from 2 to 3; {order.number} still names v{pinned.version}, still"
            f" requires {stored['WHEEL']} wheels and still routes the two operations it"
            " was raised with — an edit to the item is a new version, never a change to"
            " a job already planned"
        )

        # 3 — a route nobody wrote, and an item that cannot be built at all
        plain = item("PLAIN")
        plain_bom = create_bom(session, company_id=COMPANY, item=plain)
        add_line(session, plain_bom, item=alloy, quantity="1")
        release(session, plain_bom)
        session.commit()
        routeless = create_work_order(
            session, company_id=COMPANY, item=plain, quantity="4", number="WO-2",
            created_on=DAY,
        )
        session.commit()
        assert missing_route(session, routeless) is True, route_of(session, routeless)
        assert route_of(session, routeless) == [], route_of(session, routeless)
        orphan = item("ORPHAN")
        session.commit()
        said_no_bom = _refused(
            lambda: create_work_order(
                session, company_id=COMPANY, item=orphan, quantity="1", number="WO-3",
                created_on=DAY,
            ),
            NoBomError,
        )
        session.rollback()
        assert "nothing to expand" in said_no_bom, said_no_bom
        print(
            f"3. WO-2 was raised against a BOM with no routing and reports the absence"
            f" ({missing_route(session, routeless)} rather than an implied route), while"
            f" an item with no released BOM is refused ({said_no_bom[:44]}…)"
        )

        # 4 — status moves are transitions
        released_order = advance(session, order, status=RELEASED)
        session.commit()
        assert released_order.status == RELEASED, released_order.status
        started = advance(session, order, status=IN_PROGRESS)
        session.commit()
        assert started.status == IN_PROGRESS, started.status
        said_jump = _refused(
            lambda: advance(session, order, status=PLANNED), WorkOrderStateError
        )
        session.rollback()
        order = session.get(type(order), order.id)
        said_unknown = _refused(
            lambda: advance(session, order, status="finished"), WorkOrderStateError
        )
        session.rollback()
        order = session.get(type(order), order.id)
        print(
            f"4. {order.number} went planned → released → {order.status}; going back to"
            f" planned was refused ({said_jump[:46]}…) and so was a status nobody defines"
            f" ({said_unknown[:46]}…)"
        )

        # 5 — the moves are on the trail, with the before and the after
        trail = [
            row
            for row in read_trail(session, entity="work_order", entity_id=order.id)
            if row.action == "update"
        ]
        statuses = [
            (row.before_values.get("status"), row.after_values.get("status")) for row in trail
        ]
        assert (PLANNED, RELEASED) in statuses, statuses
        assert (RELEASED, IN_PROGRESS) in statuses, statuses
        assert reconcile_requirements(session, order)["matched"] is True, "the pin moved"
        print(
            f"5. the trail holds {len(trail)} status moves for {order.number} —"
            f" {statuses} — recorded by the table's own trigger, with the requirement"
            f" list still matching v{bom_of(session, order).version}"
        )

    print("\ncheck_work_orders: all assertions green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
