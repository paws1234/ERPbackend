"""T-4.BOM.01 check — the multi-level BOM, its scrap, and the loops it refuses.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_bom.py

Green on all five:

1. a **three-level BOM explodes to the right quantities**, with scrap applied at every
   level (a 5 % uplift on a level-1 line multiplies through the levels below it)
2. **scrap of zero is not "unset"**: a line somebody stated takes no uplift and says
   `0`, a line nobody stated takes no uplift and says `None` — the two are told apart
3. a **circular BOM is rejected**, and so is **a component that is its own ancestor**
   (the refusal names the loop rather than the explosion discovering it)
4. a **released BOM does not change**: its lines are frozen, and the change is a new
   version — carrying the same make-up — while the old version still explodes as it did
5. the explosion's **total per item** is the sum of its levels, so what to buy and
   where it is consumed cannot disagree

**Scratch database only**: it drops and recreates the public schema.
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
from app.manufacturing.bom import (  # noqa: E402
    DRAFT,
    lines_of,
    RELEASED,
    BomLockedError,
    CircularBomError,
    add_line,
    create_bom,
    explode,
    release,
    revise,
)
from app.stock.items import create_item  # noqa: E402

COMPANY = uuid.uuid4()
BUILT = Decimal("10")


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


def _level(result: dict, sku: str) -> dict:
    """The level row for one item — the walk's entry, by SKU."""
    rows = [row for row in result["levels"] if row["item"] == sku]
    assert rows, f"{sku} is not in {[row['item'] for row in result['levels']]}"
    return rows[0]


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
                code="BOM-CHECK",
                name="BOM check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()

        def item(sku: str, name: str):
            return create_item(
                session,
                company_id=COMPANY,
                sku=sku,
                name=name,
                base_uom="each",
                traceability_mode="none",
            )

        bicycle = item("BICYCLE", "Bicycle")
        frame = item("FRAME", "Frame")
        wheel = item("WHEEL", "Wheel")
        tube = item("TUBE", "Tube")
        tyre = item("TYRE", "Tyre")
        spoke = item("SPOKE", "Spoke")
        alloy = item("ALLOY", "Alloy billet")
        session.commit()

        # The bicycle: one frame, 5 % of which is scrapped making it, and two wheels
        # nobody has stated a scrap figure for beyond the zero somebody did state.
        bicycle_bom = create_bom(session, company_id=COMPANY, item=bicycle, memo="v1")
        add_line(session, bicycle_bom, item=frame, quantity="1", scrap_percent="5")
        add_line(session, bicycle_bom, item=wheel, quantity="2", scrap_percent="0")
        # A frame is three tubes, with no scrap figure stated at all.
        frame_bom = create_bom(session, company_id=COMPANY, item=frame)
        add_line(session, frame_bom, item=tube, quantity="3")
        # A wheel is a tyre (10 % scrapped) and five spokes (nothing stated).
        wheel_bom = create_bom(session, company_id=COMPANY, item=wheel)
        add_line(session, wheel_bom, item=tyre, quantity="1", scrap_percent="10")
        add_line(session, wheel_bom, item=spoke, quantity="5")
        # ...and a tube is two billets, so the tree is four levels deep.
        tube_bom = create_bom(session, company_id=COMPANY, item=tube)
        add_line(session, tube_bom, item=alloy, quantity="2")
        for bom in (frame_bom, wheel_bom, tube_bom):
            release(session, bom)
        session.commit()

        # 1 — three levels, scrap at every one of them
        result = explode(session, bicycle_bom, quantity=BUILT)
        # 10 bicycles take 10 frames, 5 % scrapped = 10.5, and 2 wheels each = 20 with
        # nothing uplifted. A frame is 3 tubes, so 10.5 × 3 = 31.5 — the frame's own
        # scrap carried down — and a tube is 2 billets, so 63. A wheel takes 1 tyre
        # (10 % scrapped, 22) and 5 spokes (100).
        expected = {
            "FRAME": Decimal("10.5"),
            "WHEEL": Decimal("20"),
            "TUBE": Decimal("31.5"),
            "TYRE": Decimal("22"),
            "SPOKE": Decimal("100"),
            "ALLOY": Decimal("63"),
        }
        assert result["required"] == expected, result["required"]
        assert _level(result, "WHEEL")["level"] == 1, _level(result, "WHEEL")
        assert _level(result, "TUBE")["level"] == 2, _level(result, "TUBE")
        assert _level(result, "TYRE")["level"] == 2, _level(result, "TYRE")
        assert _level(result, "ALLOY")["level"] == 3, _level(result, "ALLOY")
        assert _level(result, "ALLOY")["path"] == "BICYCLE → FRAME → TUBE → ALLOY", (
            _level(result, "ALLOY")["path"]
        )
        # The uplift is the line's own, applied at the level it belongs to: the tyre is
        # 1 per wheel and the wheel's line — not the tyre's — is the scrapped one.
        assert _level(result, "TYRE")["quantity"] == (Decimal("20") * Decimal("1.10")), (
            _level(result, "TYRE")
        )
        # The frame's scrap is what the tube's requirement is built on: 10 × 1.05 × 3.
        assert _level(result, "TUBE")["quantity"] == (Decimal("10.5") * Decimal("3")), (
            _level(result, "TUBE")
        )
        print(
            f"1. 10 bicycles explode to {len(result['levels'])} component lines over 4"
            f" levels: frames {result['required']['FRAME']} (1 each, 5 % scrapped),"
            f" wheels {result['required']['WHEEL']} (2 each), tubes"
            f" {result['required']['TUBE']} (3 per scrapped frame), tyres"
            f" {result['required']['TYRE']} (10 % of their own), spokes"
            f" {result['required']['SPOKE']} and billets {result['required']['ALLOY']}"
            f" — 10.5 frames carrying into 31.5 tubes and 63 billets: scrap multiplied"
            " through the levels, not added up at the end"
        )

        # 2 — a stated zero and an unstated figure are different facts
        assert _level(result, "WHEEL")["scrap_percent"] == Decimal("0.0000"), (
            _level(result, "WHEEL")
        )
        assert _level(result, "WHEEL")["quantity"] == Decimal("20"), _level(result, "WHEEL")
        assert _level(result, "FRAME")["scrap_percent"] == Decimal("5.0000"), (
            _level(result, "FRAME")
        )
        assert _level(result, "TUBE")["scrap_percent"] is None, _level(result, "TUBE")
        assert _level(result, "SPOKE")["scrap_percent"] is None, _level(result, "SPOKE")
        assert _level(result, "TYRE")["scrap_percent"] == Decimal("10.0000"), (
            _level(result, "TYRE")
        )
        print(
            "2. the wheel's line states 0 % and takes no uplift (20, not 21), the"
            " tube's states nothing and also takes none — the report keeps the two"
            " apart (0.0000 against None), so an unfilled figure is never read as a"
            " decision that there is none"
        )

        # 3 — a component that is its own ancestor, and a BOM that loops
        said_self = _refused(
            lambda: add_line(session, bicycle_bom, item=bicycle, quantity="1"),
            CircularBomError,
        )
        session.rollback()
        # A released BOM is frozen, so the loop is offered to a *draft* revision of it:
        # the make-up it carries is what must not be closed into a ring.
        tube_draft = revise(session, tube_bom)
        session.commit()
        said_ancestor = _refused(
            lambda: add_line(session, tube_draft, item=frame, quantity="1"),
            CircularBomError,
        )
        session.rollback()
        tube_draft = session.get(type(tube_bom), tube_draft.id)
        said_deep = _refused(
            lambda: add_line(session, tube_draft, item=bicycle, quantity="1"),
            "already made from",
        )
        session.rollback()
        assert "cannot be a component of its own BOM" in said_self, said_self
        assert "already made from" in said_ancestor, said_ancestor
        assert "already made from" in said_deep, said_deep
        print(
            f"3. a component that is its own ancestor is refused: the bicycle cannot be"
            f" its own component ({said_self[:44]}…), a frame cannot be a component of"
            f" the tube it is made of ({said_ancestor[:44]}…), and neither can the"
            f" bicycle two levels above it ({said_deep[:44]}…)"
        )

        # 4 — a released BOM is frozen; the change is a new version
        said_locked = _refused(
            lambda: add_line(
                session, session.get(type(wheel_bom), wheel_bom.id), item=tyre, quantity="2"
            ),
            BomLockedError,
        )
        session.rollback()
        wheel_bom = session.get(type(wheel_bom), wheel_bom.id)
        assert wheel_bom.status == RELEASED, wheel_bom.status
        fresh = revise(session, wheel_bom)
        session.commit()
        assert fresh.status == DRAFT and fresh.version == wheel_bom.version + 1, (
            fresh.status,
            fresh.version,
        )
        assert [(line.item_id, line.quantity) for line in lines_of(session, fresh)] == [
            (line.item_id, line.quantity) for line in lines_of(session, wheel_bom)
        ], "the revision did not carry the make-up over"
        after = explode(session, bicycle_bom, quantity=BUILT)
        assert after["required"] == expected, (
            "the version still in use changed when a revision was drafted"
        )
        # The revision is the one that can be changed, and it does not disturb v1.
        add_line(session, fresh, item=tyre, quantity="0.5")
        session.commit()
        assert explode(session, bicycle_bom, quantity=BUILT)["required"] == expected, "v1 moved"
        # 10 units × 1 tyre (10 % scrapped = 11) plus 10 × 0.5 with nothing stated = 5.
        assert explode(session, fresh, quantity=BUILT)["required"]["TYRE"] == Decimal("16"), (
            explode(session, fresh, quantity=BUILT)["required"]
        )
        print(
            f"4. changing the wheel BOM in use was refused ({said_locked[:44]}…); the"
            f" revision is v{fresh.version} as a draft carrying the same make-up, and"
            f" v{wheel_bom.version} — the version in use — still explodes to exactly the"
            f" same {result['required']['TYRE']} tyres after the new version is edited"
        )

        # 5 — the walk and the total agree
        walked: dict[str, Decimal] = {}
        for row in result["levels"]:
            walked[row["item"]] = walked.get(row["item"], Decimal(0)) + row["quantity"]
        assert {sku: amount.quantize(Decimal("0.000001")) for sku, amount in walked.items()} == (
            result["required"]
        ), (walked, result["required"])
        print(
            f"5. the totals equal the sum of the levels for all"
            f" {len(result['required'])} items — the list that says where a component is"
            " consumed and the figure that says how much to buy are the same arithmetic"
        )

    print("\ncheck_bom: all assertions green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
