"""T-3.SALES.02 check — the opportunity pipeline: configured stages, recorded movement, the board.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/erpv1 \
        python tests/check_pipeline.py

Green on all six:

1. **stages are rows, not code** — a company defines its own columns, in its own
   order, and `is_won`/`is_lost` are what give them meaning; a duplicate name or a
   duplicate position is refused, a stage that still holds a card cannot be removed,
   and a company with no board at all cannot be given an opportunity
2. **movement records who and when** — creating a card writes its opening move, every
   drag writes another, and the trail carries the from-stage, the to-stage, the actor
   and the instant, in order
3. **a won opportunity converts once** — the quotation carries the *customer row*
   across (not a copy of its details) and links back to the opportunity; a second
   conversion is refused by the service, and the database's own partial unique index
   refuses a hand-written second quotation
4. **a loss carries its reason** — moving into the loss column without one is
   refused, with one it is recorded on both the move and the card, and a card in a
   non-won stage cannot be converted
5. **the board respects field-level permissions** — a value the viewer's role may not
   read is **absent** from the card rather than blank, and a viewer without the
   restriction sees it
6. the board comes back in column order with each card in its own column

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import create_engine, insert, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.sales.customers import create_customer  # noqa: E402
from app.sales.pipeline import (  # noqa: E402
    AlreadyConvertedError,
    DuplicateStageError,
    InvalidPipelineError,
    LostReasonRequiredError,
    NotWonError,
    OpportunityMove,
    PipelineStage,
    StageInUseError,
    UnknownStageError,
    board,
    convert_to_quotation,
    create_opportunity,
    define_stage,
    lose_opportunity,
    move_opportunity,
    remove_stage,
    stage_by_name,
    stages,
)
from app.sales.quotations import Quotation  # noqa: E402
from app.security import assign, define_role, restrict  # noqa: E402

COMPANY = uuid.uuid4()
OTHER = uuid.uuid4()


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
        for company_id, code in ((COMPANY, "PIPE-CHECK"), (OTHER, "OTHER")):
            session.add(
                Company(
                    id=company_id,
                    code=code,
                    name=f"{code} company",
                    base_currency="PHP",
                    fiscal_year_start_month=1,
                )
            )
        session.commit()

        # a board with no stages cannot take a deal
        acme = create_customer(
            session, company_id=COMPANY, party_code="ACME", name="Acme Retail", payment_terms_days=30
        )
        session.commit()
        said = _refused(
            lambda: create_opportunity(
                session, company_id=COMPANY, customer=acme, name="First deal", owner="jo"
            ),
            InvalidPipelineError,
        )
        session.rollback()
        print(f"1a. an opportunity without a board is refused: {said}")

        # 1 — stages configured as rows
        lead = define_stage(session, company_id=COMPANY, name="Lead", position=0)
        qualified = define_stage(session, company_id=COMPANY, name="Qualified", position=1)
        # positions 3 and 4 on purpose: the gap at 2 is where a later stage goes
        won = define_stage(session, company_id=COMPANY, name="Won", position=3, is_won=True)
        lost = define_stage(session, company_id=COMPANY, name="Lost", position=4, is_lost=True)
        session.commit()
        assert [stage.name for stage in stages(session, company_id=COMPANY)] == [
            "Lead",
            "Qualified",
            "Won",
            "Lost",
        ], "the board is not in its stated order"
        said = _refused(
            lambda: define_stage(session, company_id=COMPANY, name="Qualified", position=9),
            DuplicateStageError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: define_stage(session, company_id=COMPANY, name="Pilot", position=1),
            DuplicateStageError,
        )
        session.rollback()
        # a hand-written both-won-and-lost stage is refused by the database
        try:
            session.execute(
                insert(PipelineStage.__table__).values(
                    id=uuid.uuid4(),
                    company_id=COMPANY,
                    name="Contradiction",
                    position=7,
                    is_won=True,
                    is_lost=True,
                )
            )
            session.commit()
        except DBAPIError as exc:
            assert "ck_pipeline_stage_won_or_lost" in str(exc), exc
            session.rollback()
        else:
            raise AssertionError("the database accepted a stage that is both won and lost")
        # ... and a stage between two others is a row, not a rewrite
        pilot = define_stage(session, company_id=COMPANY, name="Pilot", position=2)
        session.commit()
        assert [stage.name for stage in stages(session, company_id=COMPANY)][2] == "Pilot"
        print(
            f"1b. four stages configured as rows, then a fifth inserted between two others"
            f" without touching them; a duplicate name or position is refused ({said})"
        )

        # 2 — movement records who and when
        deal = create_opportunity(
            session,
            company_id=COMPANY,
            customer=acme,
            name="Warehouse fit-out",
            owner="jo",
            value="120000.00",
            expected_close=date(2026, 11, 30),
            at=datetime(2026, 9, 30, 8, 0, tzinfo=timezone.utc),
        )
        session.commit()
        assert deal.stage_id == lead.id, "a new deal did not open in the first stage"
        assert len(deal.moves) == 1, "the opening move was not recorded"
        when = datetime(2026, 9, 30, 9, 15, tzinfo=timezone.utc)
        move_opportunity(session, deal, to_stage=qualified, actor="maria", at=when)
        session.commit()
        session.refresh(deal)
        trail = list(session.scalars(
            select(OpportunityMove)
            .where(OpportunityMove.opportunity_id == deal.id)
            .order_by(OpportunityMove.moved_at)
        ))
        assert len(trail) == 2, f"the trail holds {len(trail)} moves, not 2"
        assert trail[0].from_stage_id is None and trail[0].to_stage_id == lead.id
        assert trail[1].from_stage_id == lead.id and trail[1].to_stage_id == qualified.id
        assert trail[1].actor == "maria", trail[1].actor
        assert trail[1].moved_at == when, trail[1].moved_at
        assert deal.stage_id == qualified.id
        print(
            f"2. the card's trail records {len(trail)} moves with from/to, the actor"
            f" ({trail[1].actor!r}) and the instant"
        )

        # 4 — a loss carries its reason
        said = _refused(
            lambda: move_opportunity(session, deal, to_stage=lost, actor="maria"),
            LostReasonRequiredError,
        )
        session.rollback()
        assert deal.stage_id == qualified.id, "the refused loss moved the card anyway"
        # ... and a card in a non-won stage cannot be converted
        said += " | " + _refused(
            lambda: convert_to_quotation(session, deal, number="QUO-NOTWON"),
            NotWonError,
        )
        session.rollback()
        print(f"4a. a loss without a reason is refused, and so is converting a non-won card: {said}")

        # 3 — a won opportunity converts once
        move_opportunity(session, deal, to_stage=won, actor="maria", at=when)
        session.commit()
        session.refresh(deal)
        assert deal.closed_at == when, "the win did not stamp the close"
        quotation = convert_to_quotation(session, deal, number="QUO-1001", issued_on=date(2026, 10, 1))
        session.commit()
        assert quotation.customer_id == acme.id, "the customer row was not carried across"
        assert quotation.opportunity_id == deal.id, "the quotation does not link back to the win"
        assert quotation.number == "QUO-1001" and quotation.issued_on == date(2026, 10, 1)
        said = _refused(
            lambda: convert_to_quotation(session, deal, number="QUO-1002"),
            AlreadyConvertedError,
        )
        session.rollback()
        # the database refuses a hand-written second quotation for the same win too
        try:
            session.execute(
                insert(Quotation.__table__).values(
                    id=uuid.uuid4(),
                    company_id=COMPANY,
                    customer_id=acme.id,
                    number="QUO-SNEAK",
                    opportunity_id=deal.id,
                    issued_on=date(2026, 10, 2),
                    created_at=datetime(2026, 10, 2, tzinfo=timezone.utc),
                )
            )
            session.commit()
        except DBAPIError as exc:
            assert "uq_quotation_opportunity" in str(exc), exc
            session.rollback()
        else:
            raise AssertionError("the database accepted a second quotation for one win")
        print(
            f"3. the win produced {quotation.number!r} carrying customer"
            f" {acme.party.code!r}; a second conversion is refused ({said}), and so is a"
            " hand-written second quotation"
        )

        # 4b — a loss, with its reason, recorded on the move and the card
        also = create_opportunity(
            session, company_id=COMPANY, customer=acme, name="Second deal", owner="jo", value="500"
        )
        session.commit()
        loss_move = lose_opportunity(
            session, also, actor="jo", reason="Budget withdrawn for the year", at=when
        )
        session.commit()
        session.refresh(also)
        assert also.lost_reason == "Budget withdrawn for the year", also.lost_reason
        assert loss_move.reason == also.lost_reason
        assert also.stage_id == lost.id and also.closed_at == when
        # ... and a stage that still holds a card cannot be removed
        said = _refused(lambda: remove_stage(session, lost), StageInUseError)
        session.rollback()
        assert also.lost_reason == "Budget withdrawn for the year", "the refused removal clobbered the card"
        # an unknown stage is named, not silently defaulted
        said += " | " + _refused(
            lambda: stage_by_name(session, company_id=COMPANY, name="Nonexistent"),
            UnknownStageError,
        )
        session.rollback()
        print(f"4b. the loss recorded its reason on card and move; removal is refused ({said})")

        # 5 — the board respects field-level permissions
        role = define_role(session, company_id=COMPANY, code="junior", name="Junior")
        restrict(session, role, entity="opportunity", field="value", can_read=False)
        assign(session, company_id=COMPANY, subject="intern", role=role)
        session.commit()
        columns = board(session, company_id=COMPANY, subject="intern")
        cards = [card for column in columns for card in column["cards"]]
        assert cards, "the board came back empty"
        assert all("value" not in card for card in cards), "a restricted value reached the board"
        assert all("name" in card for card in cards), "the restriction hid more than the value"
        open_columns = board(session, company_id=COMPANY, subject="maria")
        open_cards = [card for column in open_columns for card in column["cards"]]
        assert any("value" in card for card in open_cards), "an unrestricted viewer lost the value"
        print(
            f"5. the junior role's board omits `value` from all {len(cards)} cards while"
            f" an unrestricted viewer sees it on {sum(1 for c in open_cards if 'value' in c)}"
        )

        # 6 — the board is columns in order, cards in their own column
        assert [column["stage"]["name"] for column in open_columns] == [
            "Lead",
            "Qualified",
            "Pilot",
            "Won",
            "Lost",
        ], [column["stage"]["name"] for column in open_columns]
        won_column = next(c for c in open_columns if c["stage"]["is_won"])
        lost_column = next(c for c in open_columns if c["stage"]["is_lost"])
        assert [card["name"] for card in won_column["cards"]] == ["Warehouse fit-out"]
        assert [card["name"] for card in lost_column["cards"]] == ["Second deal"]
        print("6. the board is its columns in order, each card in the column it moved to")

        # 7 — another company's stage cannot be used on this board
        other_stage = define_stage(session, company_id=OTHER, name="Lead", position=0)
        session.commit()
        said = _refused(
            lambda: move_opportunity(session, deal, to_stage=other_stage, actor="maria"),
            UnknownStageError,
        )
        session.rollback()
        print(f"7. a stage from another company's board is refused: {said}")

    print("check_pipeline: all assertions green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
