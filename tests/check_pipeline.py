"""T-3.SALES.02 check — the opportunity pipeline: configured stages, recorded movement, the board.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/erpv1 \
        python tests/check_pipeline.py

Green on all nine:

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
7. one company's board cannot borrow another company's stage, nor be filed under another
   company's customer
8. the review findings of 2026-09-30 are held in the tree: tenant consistency on creation
   **and** on a quotation's references, an **append-only** movement trail, a reopened card
   that reads as open again, and a converted quotation that carries the customer's currency
9. **the board can be driven through the published API** — §9 adds a stage, creates a card,
   moves it, loses it and converts a win through `POST /api/v1/…`, and asserts that the actor
   and the instant a move records come from the **request** (`X-Actor` and the server's clock)
   rather than from the body; a withheld field stays withheld in a mutation's answer too

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import create_engine, insert, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api import BASE, app  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger.currency import register_currency  # noqa: E402
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
from app.sales.quotations import InvalidQuotationError, Quotation, create_quotation  # noqa: E402
from app.security import assign, define_role, grant, restrict  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

COMPANY = uuid.uuid4()
OTHER = uuid.uuid4()


def _shape_error(response) -> dict:
    """The platform's one error shape, or a failure that says what arrived instead."""
    body = response.json()
    assert isinstance(body, dict) and set(body) == {"error"}, f"not the one error shape: {body}"
    assert set(body["error"]) == {"code", "message", "details"}, body
    return body["error"]


def _refusal(response, expected: str) -> str:
    """A refusal over HTTP: the status, the code and the message it came back with."""
    error = _shape_error(response)
    assert error["code"] == expected, f"got {error}"
    return error["message"]


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

        # 4c — a card cannot be *opened* in the loss column: ending is a move, with a
        # reason, and a card created there would end before it began
        said = _refused(
            lambda: create_opportunity(
                session,
                company_id=COMPANY,
                customer=acme,
                name="Born lost",
                owner="jo",
                stage=lost,
            ),
            InvalidPipelineError,
        )
        session.rollback()
        print(f"4c. a deal cannot open in the loss column: {said}")

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

        # 8 — the review findings of 2026-09-30: one tenant, one chain, one trail
        other_customer = create_customer(
            session,
            company_id=OTHER,
            party_code="OTHER-CUST",
            name="Other Co",
            payment_terms_days=5,
        )
        second_buyer = create_customer(
            session,
            company_id=COMPANY,
            party_code="SECOND",
            name="Second Buyer",
            payment_terms_days=15,
        )
        session.commit()
        said = _refused(
            lambda: create_opportunity(
                session,
                company_id=COMPANY,
                customer=other_customer,
                name="Cross tenant",
                owner="jo",
            ),
            InvalidPipelineError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: create_opportunity(
                session,
                company_id=COMPANY,
                customer=acme,
                name="Cross stage",
                owner="jo",
                stage=other_stage,
            ),
            UnknownStageError,
        )
        session.rollback()
        print(f"8a. an opportunity cannot borrow another company's customer or stage: {said}")

        # ... and a quotation's references must be one company's and one customer's
        said = _refused(
            lambda: create_quotation(
                session,
                company_id=COMPANY,
                customer_id=other_customer.id,
                number="QUO-CROSS",
            ),
            InvalidQuotationError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: create_quotation(
                session,
                company_id=COMPANY,
                customer_id=second_buyer.id,
                number="QUO-CROSS2",
                opportunity_id=deal.id,
            ),
            InvalidQuotationError,
        )
        session.rollback()
        print(f"8b. a quotation cannot name another company's customer, nor another"
              f" customer's win: {said}")

        # 8c — the movement trail is append-only, in the database
        for operation, statement in (
            (
                "UPDATE",
                update(OpportunityMove)
                .where(OpportunityMove.id == loss_move.id)
                .values(reason="rewritten"),
            ),
            (
                "DELETE",
                OpportunityMove.__table__.delete().where(
                    OpportunityMove.id == loss_move.id
                ),
            ),
        ):
            try:
                session.execute(statement)
                session.commit()
            except DBAPIError as exc:
                assert "is append-only" in str(exc), exc
                session.rollback()
            else:
                raise AssertionError(f"the movement trail allowed a {operation}")
        print("8c. the movement trail refuses both a rewrite and a deletion")

        # 8d — reopening clears the current state, and keeps every move
        reopened = create_opportunity(
            session, company_id=COMPANY, customer=acme, name="Reopen me", owner="jo"
        )
        session.commit()
        move_opportunity(session, reopened, to_stage=lost, actor="jo", reason="went cold", at=when)
        session.commit()
        session.refresh(reopened)
        assert reopened.closed_at is not None and reopened.lost_reason == "went cold"
        move_opportunity(session, reopened, to_stage=qualified, actor="jo", at=when)
        session.commit()
        session.refresh(reopened)
        assert reopened.closed_at is None, "reopening left the card stamped closed"
        assert reopened.lost_reason is None, "reopening left a stale loss reason"
        reopened_trail = list(
            session.scalars(
                select(OpportunityMove).where(OpportunityMove.opportunity_id == reopened.id)
            )
        )
        assert len(reopened_trail) == 3, f"the trail holds {len(reopened_trail)} moves, not 3"
        print("8d. a reopened card reads as open again, and its trail keeps all three moves")

        # 8e — a conversion carries the customer's own currency across
        register_currency(session, company_id=COMPANY, code="USD", name="US Dollar")
        session.commit()
        usd_buyer = create_customer(
            session,
            company_id=COMPANY,
            party_code="US-BUYER",
            name="US Buyer",
            payment_terms_days=30,
            transaction_currency="USD",
        )
        session.commit()
        export_deal = create_opportunity(
            session, company_id=COMPANY, customer=usd_buyer, name="Export deal", owner="jo"
        )
        session.commit()
        move_opportunity(session, export_deal, to_stage=won, actor="jo", at=when)
        session.commit()
        session.refresh(export_deal)
        export_quote = convert_to_quotation(session, export_deal, number="QUO-USD")
        session.commit()
        assert export_quote.currency == "USD", (
            f"the conversion stored {export_quote.currency!r}, not the customer's USD"
        )
        print(f"8e. the converted quotation carries the customer's currency ({export_quote.currency})")

        # 9 — the board can be **driven** through the published API (T-3.SALES.02)
        # Everything above is the domain layer. This section is the boundary: the
        # Kanban is only drivable if the stages and the cards have paths, and the
        # actor and the instant a move records have to come from the request.
        seller = define_role(session, company_id=COMPANY, code="seller", name="Seller")
        grant(
            session,
            seller,
            "pipeline.read",
            "pipeline.configure",
            "opportunity.write",
            "quotation.write",
        )
        assign(session, company_id=COMPANY, subject="maria", role=seller)
        # The restricted viewer from §5 also needs the read capability, so the board
        # can be asked for **through the API** as the role the restriction applies to.
        grant(session, role, "pipeline.read", "opportunity.write")
        session.commit()

    client = TestClient(app, raise_server_exceptions=False)
    headers = {"X-Company-Id": str(COMPANY), "X-Actor": "maria"}
    intern = {**headers, "X-Actor": "intern"}
    stages_url = f"{BASE}/pipeline/stages"
    cards_url = f"{BASE}/opportunities"

    # 9a — a stage is added through the API, and a clash is refused in the one shape
    added = client.post(
        stages_url, headers=headers, json={"name": "Negotiation", "position": 6}
    )
    assert added.status_code == 201, added.text
    assert added.json() == {
        "name": "Negotiation",
        "position": 6,
        "is_won": False,
        "is_lost": False,
    }, added.json()
    said = _refusal(
        client.post(
            stages_url, headers=headers, json={"name": "Negotiation", "position": 6}
        ),
        "pipeline_error",
    )
    board_names = [
        column["stage"]["name"]
        for column in client.get(f"{BASE}/pipeline/board", headers=headers).json()
    ]
    assert board_names == ["Lead", "Qualified", "Pilot", "Won", "Lost", "Negotiation"], board_names
    print(f"9a. a stage added over the API joins the board, and a duplicate is refused: {said}")

    # 9b — a card is created over the API, and its opening move names the requester
    created = client.post(
        cards_url,
        headers=headers,
        json={
            "customer_code": "ACME",
            "name": "API deal",
            "owner": "jo",
            "value": "5000.00",
            "expected_close": "2026-12-31",
        },
    )
    assert created.status_code == 201, created.text
    card = created.json()
    assert card["card"]["name"] == "API deal", card
    assert card["card"]["value"] == "5000.000000", card["card"]
    assert card["stage"] == "Lead", card
    assert card["move"]["actor"] == "maria", card["move"]
    assert card["move"]["from_stage"] is None and card["move"]["to_stage"] == "Lead"
    assert card["closed_at"] is None
    deal_id = card["card"]["id"]
    print(f"9b. the API created a card ({deal_id[:8]}…) whose opening move names {card['move']['actor']!r}")

    # 9c — a move records the request's actor and the **server's** instant
    started = datetime.now(timezone.utc)
    moved = client.post(
        f"{cards_url}/{deal_id}/moves", headers=headers, json={"to_stage": "Qualified"}
    )
    assert moved.status_code == 200, moved.text
    step = moved.json()["move"]
    assert step["from_stage"] == "Lead" and step["to_stage"] == "Qualified", step
    assert step["actor"] == "maria", step
    stamped = datetime.fromisoformat(step["moved_at"])
    assert stamped.tzinfo is not None, step["moved_at"]
    assert stamped >= started, f"the instant was not the server's: {step['moved_at']}"
    assert moved.json()["stage"] == "Qualified"
    print(f"9c. the move recorded {step['actor']!r} at {step['moved_at']} — both from the request, not the body")

    # 9d — a loss without a reason is refused; with one it is recorded on card and move
    said = _refusal(
        client.post(
            f"{cards_url}/{deal_id}/loss", headers=headers, json={"reason": ""}
        ),
        "pipeline_error",
    )
    still = client.get(f"{BASE}/pipeline/board", headers=headers).json()
    standing = next(
        column["stage"]["name"]
        for column in still
        for one in column["cards"]
        if one["name"] == "API deal"
    )
    assert standing == "Qualified", f"the refused loss moved the card to {standing}"
    ended = client.post(
        f"{cards_url}/{deal_id}/loss",
        headers=headers,
        json={"reason": "Chose a competitor"},
    )
    assert ended.status_code == 200, ended.text
    assert ended.json()["stage"] == "Lost", ended.json()
    assert ended.json()["card"]["lost_reason"] == "Chose a competitor", ended.json()
    assert ended.json()["closed_at"] is not None
    print(f"9d. a blank reason is refused ({said[:60]}…) and the real one lands on the card")

    # 9e — a won card converts once, over the API
    won_card = client.post(
        cards_url, headers=headers, json={"customer_code": "ACME", "name": "API win", "owner": "jo"}
    )
    assert won_card.status_code == 201, won_card.text
    won_id = won_card.json()["card"]["id"]
    said = _refusal(
        client.post(
            f"{cards_url}/{won_id}/quotation", headers=headers, json={"number": "QUO-API-0"}
        ),
        "pipeline_error",
    )
    client.post(f"{cards_url}/{won_id}/moves", headers=headers, json={"to_stage": "Won"})
    quoted = client.post(
        f"{cards_url}/{won_id}/quotation",
        headers=headers,
        json={"number": "QUO-API-1", "issued_on": "2026-10-05"},
    )
    assert quoted.status_code == 201, quoted.text
    assert quoted.json()["customer_code"] == "ACME", quoted.json()
    assert quoted.json()["number"] == "QUO-API-1" and quoted.json()["issued_on"] == "2026-10-05"
    again = _refusal(
        client.post(
            f"{cards_url}/{won_id}/quotation", headers=headers, json={"number": "QUO-API-2"}
        ),
        "pipeline_error",
    )
    print(f"9e. a non-won card is refused ({said[:40]}…), the win produced QUO-API-1, and a second is refused")

    # 9f — an unknown card and an unauthorised caller are both named, not defaulted
    said = _refusal(
        client.post(
            f"{cards_url}/{uuid.uuid4()}/moves", headers=headers, json={"to_stage": "Lead"}
        ),
        "pipeline_error",
    )
    nobody = client.post(
        cards_url,
        headers={**headers, "X-Actor": "stranger"},
        json={"customer_code": "ACME", "name": "Nope", "owner": "jo"},
    )
    assert nobody.status_code == 403, nobody.text
    assert _shape_error(nobody)["code"] == "forbidden", nobody.text
    print(f"9f. an unknown card is refused by name ({said[:40]}…) and an unrolled caller gets 403")

    # 9g — the board's cards carry the id they are addressed by, and a withheld field
    # stays withheld in a mutation's answer too
    as_intern = client.get(f"{BASE}/pipeline/board", headers=intern).json()
    intern_cards = [one for column in as_intern for one in column["cards"]]
    assert intern_cards, "the restricted viewer's board came back empty"
    assert all("value" not in one for one in intern_cards), "a withheld value reached the board"
    assert all("id" in one for one in intern_cards), "the card id is not addressable"
    minted = client.post(
        cards_url,
        headers=intern,
        json={"customer_code": "ACME", "name": "Intern deal", "owner": "intern", "value": "77"},
    )
    assert minted.status_code == 201, minted.text
    assert "value" not in minted.json()["card"], (
        "a mutation answered with a value the board withholds"
    )
    assert minted.json()["card"]["name"] == "Intern deal"
    print(
        f"9g. {len(intern_cards)} cards carry an id while `value` is withheld — on the board"
        " and on a mutation's answer alike"
    )

    print("check_pipeline: all assertions green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
