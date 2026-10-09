"""T-4.BOM.02 check — the routing's sequence, its times and where its components go.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_routing.py

Green on all five:

1. a **four-operation routing** is validated and displayed in order, with one component
   assigned to an operation and the rest reported as the BOM's own
2. **a sequence cannot repeat or leave a hole** — both are refused, and the refusal
   names the step that was expected
3. times state their **basis**: setup is per batch, the run is per unit, and a run time
   the planner timed for a batch of four is restated per unit without losing what was
   stated (the arithmetic of a batch is hand-checkable)
4. a **released BOM's routing is frozen** with it — the route changes by revision
5. an operation with **no work centre is reported** as unassigned rather than quietly
   loading nobody's capacity
"""

from __future__ import annotations

import os
import sys
import uuid
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.manufacturing.bom import add_line, create_bom, lines_of, release  # noqa: E402
from app.manufacturing.routing import (  # noqa: E402
    DuplicateOperationError,
    RoutingLockedError,
    SequenceGapError,
    add_operation,
    assign_component,
    operation_at,
    operation_minutes,
    operations,
    routing,
    routing_minutes,
    run_basis_minutes,
    sequence_check,
    unassigned,
)
from app.stock.items import create_item  # noqa: E402

COMPANY = uuid.uuid4()
BATCH = Decimal("10")


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
                code="ROUTE-CHECK",
                name="Routing check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()

        def item(sku: str):
            return create_item(
                session,
                company_id=COMPANY,
                sku=sku,
                name=sku.title(),
                base_uom="each",
                traceability_mode="none",
            )

        bicycle = item("BICYCLE")
        frame = item("FRAME")
        wheel = item("WHEEL")
        paint = item("PAINT-KIT")
        session.commit()

        bom = create_bom(session, company_id=COMPANY, item=bicycle, memo="routing")
        frame_line = add_line(session, bom, item=frame, quantity="1")
        wheel_line = add_line(session, bom, item=wheel, quantity="2")
        paint_line = add_line(session, bom, item=paint, quantity="0.2")
        session.commit()

        # 1 — four operations, attached to the BOM, one of them consuming a component
        cut = add_operation(
            session, bom, name="Cut tube", work_center_code="CUT",
            setup_minutes="15", run_minutes="2",
        )
        weld = add_operation(
            session, bom, name="Weld frame", work_center_code="WELD",
            setup_minutes="20", run_minutes="12", run_per_units=4,
        )
        spray = add_operation(
            session, bom, name="Paint", work_center_code="PAINT",
            setup_minutes="30", run_minutes="5",
        )
        assemble = add_operation(session, bom, name="Assemble", setup_minutes="10",
                                 run_minutes="8")
        session.commit()
        assign_component(session, bom, line=paint_line, operation=spray)
        assign_component(session, bom, line=frame_line, operation=None)
        session.commit()

        shown = routing(session, bom)
        assert [step["sequence"] for step in shown["operations"]] == [1, 2, 3, 4], shown
        assert [step["operation"] for step in shown["operations"]] == [
            "Cut tube", "Weld frame", "Paint", "Assemble"
        ], shown["operations"]
        assert [step["work_center"] for step in shown["operations"]] == [
            "CUT", "WELD", "PAINT", None
        ], shown["operations"]
        # The component assigned to Paint is under Paint, and the two the job consumes
        # generally are still on the BOM — neither group is silent about the other.
        assert shown["operations"][2]["components"] == [
            {"line_no": paint_line.line_no, "item_id": paint.id, "quantity": Decimal("0.200000")}
        ], shown["operations"][2]
        assert {row["line_no"] for row in shown["general_components"]} == {
            frame_line.line_no, wheel_line.line_no
        }, shown["general_components"]
        print(
            f"1. the routing is {len(shown['operations'])} operations in order —"
            " Cut tube (CUT), Weld frame (WELD), Paint (PAINT), Assemble (unassigned) —"
            f" with the paint kit consumed at step 3 and {len(shown['general_components'])}"
            " components belonging to the BOM generally"
        )

        # 2 — dense and unique, or refused
        said_duplicate = _refused(
            lambda: add_operation(session, bom, name="Polish", run_minutes="1", sequence=2),
            DuplicateOperationError,
        )
        session.rollback()
        bom = session.get(type(bom), bom.id)
        said_gap = _refused(
            lambda: add_operation(session, bom, name="Polish", run_minutes="1", sequence=7),
            SequenceGapError,
        )
        session.rollback()
        bom = session.get(type(bom), bom.id)
        add_operation(session, bom, name="Polish", work_center_code="PACK",
                     run_minutes="1")
        session.commit()
        check = sequence_check(session, bom)
        assert check == {"sequences": [1, 2, 3, 4, 5], "dense": True, "duplicates": []}, check
        print(
            f"2. a repeated sequence was refused ({said_duplicate[:52]}…) and so was a"
            f" hole ({said_gap[:52]}…); appending gave {check['sequences']}, dense from 1"
            " with no duplicate"
        )

        # 3 — the basis of each time is stated, and a batch's run time is restated
        assert shown["operations"][0]["setup_basis"] == "per_batch", shown["operations"][0]
        assert shown["operations"][0]["run_basis"] == "per_unit", shown["operations"][0]
        # Weld was timed at 12 minutes for 4 units, so 3 minutes a unit — and the figure
        # it was stated as is still recoverable.
        assert weld.run_minutes_per_unit == Decimal("3.000000"), weld.run_minutes_per_unit
        assert weld.run_basis_units == 4, weld.run_basis_units
        assert run_basis_minutes(weld) == Decimal("12.000000"), run_basis_minutes(weld)
        # Hand-checked for a batch of 10: 15 + 2×10 = 35, 20 + 3×10 = 50, 30 + 5×10 = 80,
        # 10 + 8×10 = 90 — the setup once, the run per unit — and the Polish step added
        # in the section above contributes 0 + 1×10 = 10.
        assert operation_minutes(weld, BATCH) == Decimal("50.000000"), operation_minutes(weld, BATCH)
        assert routing_minutes(session, bom, quantity=BATCH) == Decimal("265.000000"), (
            routing_minutes(session, bom, quantity=BATCH)
        )
        assert operations(session, bom)[1].sequence == 2, operations(session, bom)
        assert operation_at(session, bom, 4).name == "Assemble", operation_at(session, bom, 4)
        print(
            f"3. setup is per batch and the run per unit — Weld was timed {run_basis_minutes(weld)}"
            f" minutes for {weld.run_basis_units} units and is stored as"
            f" {weld.run_minutes_per_unit} a unit; a batch of {BATCH} takes"
            f" {routing_minutes(session, bom, quantity=BATCH)} minutes across the"
            f" {len(operations(session, bom))} operations, setup counted once each"
        )

        # 4 — releasing the BOM freezes the route with it
        release(session, bom)
        session.commit()
        said_locked = _refused(
            lambda: add_operation(session, bom, name="Pack", run_minutes="2"),
            RoutingLockedError,
        )
        session.rollback()
        bom = session.get(type(bom), bom.id)
        said_locked_assign = _refused(
            lambda: assign_component(
                session, bom, line=lines_of(session, bom)[0], operation=None
            ),
            RoutingLockedError,
        )
        session.rollback()
        assert "frozen" in said_locked, said_locked
        assert "frozen" in said_locked_assign, said_locked_assign
        print(
            f"4. the released BOM's routing is frozen: another operation"
            f" ({said_locked[:46]}…) and a re-assignment of a component"
            f" ({said_locked_assign[:46]}…) are both refused — the route changes by"
            " revising the BOM"
        )

        # 5 — an operation nobody has assigned to a work centre is reported
        loose = unassigned(session, bom)
        assert loose == [{"sequence": 4, "operation": "Assemble"}], loose
        print(
            "5. Assemble names no work centre and is reported unassigned — capacity"
            " planning says so rather than loading it onto nobody"
        )

    print("\ncheck_routing: all assertions green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
