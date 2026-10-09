"""T-4.WO.02 check — job cards, the time they book and the output they record.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_job_cards.py

Green on all five:

1. two operations, four bookings: the **total booked time is the sum of the entries**,
   per card and per order, and the time splits into its setup and run parts
2. **produced and rejected are recorded apart** and reconcile to the work order's own
   quantity — the difference is stated while the job runs, not discovered at the close
3. **an overrun past the operation's limit is refused unless somebody accepts it**, and
   the acceptance is kept on the entry with who owned it and why
4. a **closed card takes no more time**, and a correction is a **new entry** naming the
   entry it corrects — the booking it corrects is unchanged beside it
5. the booked time is reported **per work centre**, which is what the costing prices
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

from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.manufacturing.bom import add_line, create_bom, release  # noqa: E402
from app.manufacturing.job_cards import (  # noqa: E402
    CLOSED,
    CardClosedError,
    JobCardError,
    OverrunNotAcknowledged,
    book_time,
    booked_time,
    card_minutes,
    cards_of,
    close_card,
    correct_entry,
    entries_of,
    open_card,
    output_of,
    operation_booked_minutes,
    overrun_limit,
    planned_minutes_of,
    time_by_work_center,
)
from app.manufacturing.routing import add_operation  # noqa: E402
from app.manufacturing.work_orders import create_work_order  # noqa: E402
from app.stock.items import create_item  # noqa: E402

COMPANY = uuid.uuid4()
DAY = date(2026, 9, 14)
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
                code="JC-CHECK",
                name="Job card check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()

        widget = create_item(session, company_id=COMPANY, sku="WIDGET", name="Widget",
                             base_uom="each", traceability_mode="none")
        blank = create_item(session, company_id=COMPANY, sku="BLANK", name="Blank",
                            base_uom="each", traceability_mode="none")
        session.commit()
        bom = create_bom(session, company_id=COMPANY, item=widget)
        add_line(session, bom, item=blank, quantity="1")
        # Cut: 15 to set up and 10 a unit → 115 planned for 10. Weld: 15 + 3 a unit → 45.
        add_operation(session, bom, name="Cut", work_center_code="CUT",
                      setup_minutes="15", run_minutes="10")
        add_operation(session, bom, name="Weld", work_center_code="WELD",
                      setup_minutes="15", run_minutes="3")
        release(session, bom)
        session.commit()
        order = create_work_order(
            session, company_id=COMPANY, item=widget, quantity=ORDERED,
            number="WO-1", created_on=DAY, due_on=date(2026, 9, 20),
        )
        session.commit()

        first = open_card(session, order, operation_sequence=1, operator="ana", on=DAY)
        second = open_card(session, order, operation_sequence=1, operator="ben", on=DAY)
        third = open_card(session, order, operation_sequence=2, operator="ana", on=DAY)
        session.commit()

        # 1 — four bookings, and every total is their sum
        book_time(session, first, setup_minutes="15", run_minutes="60",
                  produced_quantity="6", recorded_by="ana")
        book_time(session, second, setup_minutes="0", run_minutes="45",
                  produced_quantity="5", rejected_quantity="1", recorded_by="ben")
        session.commit()
        assert card_minutes(session, first) == {
            "setup_minutes": Decimal("15.000000"),
            "run_minutes": Decimal("60.000000"),
            "total_minutes": Decimal("75.000000"),
        }, card_minutes(session, first)
        assert card_minutes(session, second)["total_minutes"] == Decimal("45.000000"), (
            card_minutes(session, second)
        )
        assert planned_minutes_of(first) == Decimal("115.000000"), planned_minutes_of(first)
        assert overrun_limit(first) == Decimal("126.500000"), overrun_limit(first)
        print(
            f"1. three cards, {len(entries_of(session, first)) + len(entries_of(session, second))}"
            f" bookings on step 1 so far: {card_minutes(session, first)} on ana's card and"
            f" {card_minutes(session, second)['total_minutes']} on ben's — the card's"
            " total is the sum of its entries, split into the setup and the run"
        )

        # 2 — produced and rejected apart, reconciled to the order
        output = output_of(session, order)
        assert output == {
            "produced": Decimal("11.000000"),
            "rejected": Decimal("1.000000"),
            "net": Decimal("10.000000"),
            "ordered": ORDERED,
            "difference": Decimal("0.000000"),
        }, output
        print(
            f"2. the two benches reported {output['produced']} produced and"
            f" {output['rejected']} rejected — a net of {output['net']} against the"
            f" {output['ordered']} ordered, with a difference of"
            f" {output['difference']} stated while the job is still running"
        )

        # 3 — an overrun nobody owns is refused; one somebody owns is kept
        said_unowned = _refused(
            lambda: book_time(session, second, run_minutes="10", recorded_by="ben"),
            OverrunNotAcknowledged,
        )
        session.rollback()
        second = session.get(type(second), second.id)
        overrun = book_time(
            session, second, run_minutes="10", recorded_by="ben",
            acknowledge_overrun=True, overrun_reason="the die needed re-cutting",
        )
        session.commit()
        assert overrun.overrun_acknowledged_by == "ben", overrun.overrun_acknowledged_by
        assert overrun.overrun_reason == "the die needed re-cutting", overrun.overrun_reason
        assert card_minutes(session, second)["total_minutes"] == Decimal("55.000000"), (
            card_minutes(session, second)
        )
        assert "take the operation to" in said_unowned, said_unowned
        print(
            f"3. ten more minutes on step 1 was refused ({said_unowned[:48]}…) and the"
            f" same booking accepted once ben owned it"
            f" ({overrun.overrun_acknowledged_by}: {overrun.overrun_reason}) — the"
            f" operation now stands at"
            f" {operation_booked_minutes(session, order, sequence=1)} minutes against a"
            f" planned {planned_minutes_of(first)} and a limit of {overrun_limit(first)}"
        )

        # 4 — a closed card, and a correction that is a new entry
        book_time(session, third, setup_minutes="15", run_minutes="30", recorded_by="ana")
        session.commit()
        close_card(session, third, actor="maria")
        session.commit()
        said_closed = _refused(
            lambda: book_time(session, third, run_minutes="5", recorded_by="ana"),
            CardClosedError,
        )
        session.rollback()
        third = session.get(type(third), third.id)
        original = entries_of(session, third)[0]
        before = (original.setup_minutes, original.run_minutes, original.produced_quantity)
        correction = correct_entry(
            session, original, actor="maria", reason="the run timer was started late",
            run_minutes="4",
        )
        session.commit()
        original = session.get(type(original), original.id)
        assert (original.setup_minutes, original.run_minutes, original.produced_quantity) == before, (
            "the booking it corrects was rewritten"
        )
        assert correction.corrects_entry_id == original.id, correction.corrects_entry_id
        assert correction.recorded_by == "maria" and correction.note, correction
        assert int(third.status == CLOSED) == 1, third.status
        print(
            f"4. the closed card refused more time ({said_closed[:48]}…), and the"
            f" correction is a {len(entries_of(session, third))}nd entry naming the one it"
            f" corrects — whose own {before[1]} run minutes are unchanged beside it, with"
            " maria's reason on the new row"
        )

        # 5 — the totals, and the work centres the time belongs to
        booked = booked_time(session, order)
        per_card = sum(
            (card_minutes(session, card)["total_minutes"] for card in cards_of(session, order)),
            Decimal(0),
        )
        # Step 1: 15 + 60 + 45 + the 10-minute overrun = 130. Step 2: 15 + 30 plus the
        # 4-minute correction = 49. Nothing else was booked.
        assert booked["total_minutes"] == per_card == Decimal("179.000000"), (
            booked["total_minutes"],
            per_card,
        )
        assert booked["setup_minutes"] == Decimal("30.000000"), booked
        assert booked["run_minutes"] == Decimal("149.000000"), booked
        assert booked["entries"] == 5, booked
        assert [(row["sequence"], row["work_center"]) for row in booked["per_operation"]] == [
            (1, "CUT"),
            (2, "WELD"),
        ], booked["per_operation"]
        assert time_by_work_center(session, order) == {
            "CUT": Decimal("130.000000"),
            "WELD": Decimal("49.000000"),
        }, time_by_work_center(session, order)
        print(
            f"5. the order has booked {booked['total_minutes']} minutes over"
            f" {booked['entries']} entries ({booked['setup_minutes']} setup,"
            f" {booked['run_minutes']} run), equal to the sum of its cards:"
            f" {time_by_work_center(session, order)} by work centre — the figure T-4.WO.05"
            " prices at each centre's dated rate"
        )

    print("\ncheck_job_cards: all assertions green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
