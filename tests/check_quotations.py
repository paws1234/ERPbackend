"""T-3.SALES.03 check — quotations: priced lines, validity, and the order they become.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/erpv1 \
        python tests/check_quotations.py

Green on all six:

1. **a quotation prices its lines when it is written**, recording the price, the day it
   was fixed (`priced_on`) and the rule that produced it — a price a person stated
   records no rule, which is an honest null rather than an invented code; a document may
   only name a **registered** currency (T-1.ACCT.05), and a line number is used once
2. **re-pricing restates every line**, with the window the new prices hold for: a
   partial re-price is refused, a re-price naming a line that is not there is refused,
   and a window that has already closed is refused — the plan names no quotation
   validity, so no default is invented, and a quotation with no window never expires.
   A re-price restates the **rule** with the price, so a line can never keep the rule
   that produced a price it no longer has
3. **an expired quotation cannot be converted**: the refusal names re-pricing as the
   fix, the card is not ordered, and after a re-price the same conversion succeeds
4. **conversion produces an order whose lines are identical** to the quotation's, linked
   back to it, carrying the same currency and the same rule per line — and a quotation
   that prices nothing cannot become an order, because the service is a caller too even
   though the boundary refuses an empty line list
5. **a quotation converts once** — refused by the service, refused again by the schema's
   own partial unique index for a hand-written second order — and a converted quotation
   can neither be re-priced nor have a line added, because it is no longer an offer
6. **the whole path is drivable through the published API**, refusals included:
   `POST /api/v1/quotations`, `GET /api/v1/quotations/{number}`,
   `POST .../reprice` and `POST .../order` — and a quotation with **no lines** is
   refused at the boundary, because nothing could ever convert it, as is a number the
   company already uses

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api import BASE, app  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger.currency import UnknownCurrencyError  # noqa: E402
from app.sales.customers import create_customer  # noqa: E402
from app.sales.orders import (  # noqa: E402
    DuplicateOrderError,
    IncompleteOrderError,
    SalesOrder,
    convert_quotation_to_order,
    order_for_quotation,
    order_total,
)
from app.sales.quotations import (  # noqa: E402
    ConvertedQuotationError,
    DuplicateLineError,
    ExpiredQuotationError,
    InvalidQuotationError,
    add_line,
    create_quotation,
    expired,
    line_amount,
    lines_of,
    reprice_quotation,
)
from app.security import assign, define_role, grant  # noqa: E402

COMPANY = uuid.uuid4()
OCT = date(2026, 10, 1)
# Never hard-coded: a check that pins "today" is a check that rots (the multi-currency
# lesson). `priced_on` defaults to the day the line is written, so the assertion has to
# ask the clock what that is.
TODAY = datetime.now(timezone.utc).date().isoformat()


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


def _line_facts(line) -> tuple:
    """Everything about a line that a conversion has to carry across unchanged."""
    return (
        line.line_no,
        line.description,
        line.item_id,
        line.quantity,
        line.uom,
        line.unit_price,
        line.rule_code,
        line.priced_on,
    )


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
                code="QUOTE-CHECK",
                name="Quote check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()
        acme = create_customer(
            session,
            company_id=COMPANY,
            party_code="ACME",
            name="Acme Retail",
            payment_terms_days=30,
        )
        session.commit()

        # 1 — priced lines, with what fixed the price
        quote = create_quotation(
            session,
            company_id=COMPANY,
            customer_id=acme.id,
            number="Q-1001",
            issued_on=OCT,
            valid_until=date(2026, 10, 31),
        )
        session.commit()
        first = add_line(
            session,
            quote,
            line_no=1,
            description="Widget",
            quantity="10",
            unit_price="5.25",
            uom="pcs",
            rule_code="TIER-A",
            priced_on=OCT,
        )
        second = add_line(
            session,
            quote,
            line_no=2,
            description="Freight",
            quantity="1",
            unit_price="250.50",
            priced_on=OCT,
        )
        session.commit()
        assert first.rule_code == "TIER-A", first.rule_code
        assert second.rule_code is None, "a hand-stated price recorded a rule"
        assert first.priced_on == OCT and second.priced_on == OCT
        assert line_amount(first) == Decimal("52.50"), line_amount(first)
        assert [line.line_no for line in lines_of(session, quote)] == [1, 2]
        said = _refused(
            lambda: add_line(
                session, quote, line_no=1, description="Again", quantity="1", unit_price="1"
            ),
            DuplicateLineError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: add_line(
                session, quote, line_no=3, description="Negative", quantity="-1", unit_price="1"
            ),
            InvalidQuotationError,
        )
        session.rollback()
        # a document may only name a currency this company has registered (T-1.ACCT.05)
        said += " | " + _refused(
            lambda: create_quotation(
                session,
                company_id=COMPANY,
                customer_id=acme.id,
                number="Q-XXX",
                currency="XXX",
            ),
            UnknownCurrencyError,
        )
        session.rollback()
        print(
            f"1. two lines priced at {first.unit_price!r} (rule {first.rule_code!r}) and"
            f" {second.unit_price!r} (no rule, stated by hand), fixed on {first.priced_on};"
            f" a reused line number, a negative quantity and an unregistered currency"
            f" are refused: {said}"
        )

        # 2 — re-pricing restates every line, with a window that has not closed
        said = _refused(
            lambda: reprice_quotation(
                session,
                quote,
                prices={1: Decimal("6.00")},
                valid_until=date(2026, 12, 31),
                on=OCT,
            ),
            InvalidQuotationError,
        )
        session.rollback()
        assert "2" in said, f"the refusal did not name the line it wanted: {said}"
        session.refresh(quote)
        assert lines_of(session, quote)[0].unit_price == Decimal("5.250000"), (
            "the refused partial re-price changed a price anyway"
        )
        said += " | " + _refused(
            lambda: reprice_quotation(
                session,
                quote,
                prices={1: Decimal("6.00"), 2: Decimal("260.00"), 9: Decimal("1")},
                valid_until=date(2026, 12, 31),
                on=OCT,
            ),
            InvalidQuotationError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: reprice_quotation(
                session,
                quote,
                prices={1: Decimal("6.00"), 2: Decimal("260.00")},
                valid_until=OCT,
                on=OCT,
            ),
            InvalidQuotationError,
        )
        session.rollback()
        print(f"2a. a partial re-price, an unknown line and a window that has closed are refused: {said}")

        reprice_quotation(
            session,
            quote,
            prices={1: Decimal("6.00"), 2: Decimal("260.00")},
            rules={1: "TIER-B"},
            valid_until=date(2026, 12, 31),
            on=date(2026, 10, 2),
        )
        session.commit()
        session.refresh(quote)
        priced = lines_of(session, quote)
        assert [line.unit_price for line in priced] == [Decimal("6.000000"), Decimal("260.000000")]
        assert all(line.priced_on == date(2026, 10, 2) for line in priced), "a re-price left a stale date"
        assert priced[0].rule_code == "TIER-B", "the re-price kept the rule behind the old price"
        assert quote.valid_until == date(2026, 12, 31)
        assert not expired(quote, on=date(2026, 10, 2))
        # ... and a re-price that names **no** rule clears the one it had: a price can
        # never keep the rule that produced a different price
        reprice_quotation(
            session,
            quote,
            prices={1: Decimal("7.00"), 2: Decimal("270.00")},
            valid_until=date(2027, 1, 31),
            on=date(2026, 10, 3),
        )
        session.commit()
        assert all(line.rule_code is None for line in lines_of(session, quote)), (
            "a re-price that named no rule left a stale one behind"
        )
        assert all(line.priced_on == date(2026, 10, 3) for line in lines_of(session, quote))
        # a quotation with no window never expires — nothing is invented to close it
        open_ended = create_quotation(
            session, company_id=COMPANY, customer_id=acme.id, number="Q-OPEN"
        )
        session.commit()
        assert open_ended.valid_until is None
        assert not expired(open_ended, on=date(2030, 1, 1)), "an unstated window expired anyway"
        print("2b. the whole quotation re-priced onto 2026-10-02 with a new window and a new rule")

        # 3 — an expired quotation cannot be converted without re-pricing
        stale = create_quotation(
            session,
            company_id=COMPANY,
            customer_id=acme.id,
            number="Q-STALE",
            issued_on=date(2026, 8, 1),
            valid_until=date(2026, 9, 1),
        )
        session.commit()
        add_line(
            session,
            stale,
            line_no=1,
            description="Old price",
            quantity="2",
            unit_price="10.00",
            priced_on=date(2026, 8, 1),
        )
        session.commit()
        assert expired(stale, on=date(2026, 10, 2))
        said = _refused(
            lambda: convert_quotation_to_order(
                session, stale, number="SO-STALE", on=date(2026, 10, 2)
            ),
            ExpiredQuotationError,
        )
        session.rollback()
        assert "re-price" in said, f"the refusal did not say what fixes it: {said}"
        assert order_for_quotation(session, stale) is None, "the refused conversion ordered anyway"
        # ... and re-pricing it is what makes the same conversion possible
        reprice_quotation(
            session,
            stale,
            prices={1: Decimal("11.00")},
            valid_until=date(2026, 12, 31),
            on=date(2026, 10, 2),
        )
        session.commit()
        recovered = convert_quotation_to_order(
            session, stale, number="SO-STALE", on=date(2026, 10, 2)
        )
        session.commit()
        assert recovered.number == "SO-STALE"
        print(f"3. an expired quotation is refused (\u2026{said[-70:]}) and converts once re-priced")

        # 4 — the order's lines are the quotation's, carried across and linked back
        add_line(
            session,
            quote,
            line_no=3,
            description="Install",
            quantity="3",
            unit_price="99.99",
            priced_on=OCT,
        )
        session.commit()
        order = convert_quotation_to_order(session, quote, number="SO-1001", on=date(2026, 10, 5))
        session.commit()
        assert order.quotation_id == quote.id, "the order does not link back to the quotation"
        assert order.quotation.number == "Q-1001"
        assert order.customer_id == acme.id and order.ordered_on == date(2026, 10, 5)
        assert order.currency == quote.currency
        assert [_line_facts(line) for line in order.lines] == [
            _line_facts(line) for line in lines_of(session, quote)
        ], "the order's lines are not the quotation's"
        assert order_total(order) == sum(
            (line_amount(line) for line in lines_of(session, quote)), Decimal(0)
        )
        print(
            f"4. SO-1001 carries all {len(order.lines)} lines of Q-1001 identically"
            f" (total {order_total(order)}) and links back to it"
        )

        # 4b — a quotation that prices nothing has nothing to order. The boundary refuses
        # an empty line list, but the service is a caller too, and this is the refusal
        # that keeps a header-only document from becoming a header-only order.
        nothing = create_quotation(
            session, company_id=COMPANY, customer_id=acme.id, number="Q-NOTHING"
        )
        session.commit()
        said = _refused(
            lambda: convert_quotation_to_order(session, nothing, number="SO-NOTHING"),
            IncompleteOrderError,
        )
        session.rollback()
        print(f"4b. a quotation with no lines cannot become an order: {said}")

        # 5 — once, and no longer an offer afterwards
        said = _refused(
            lambda: convert_quotation_to_order(session, quote, number="SO-1002", on=date(2026, 10, 6)),
            DuplicateOrderError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: reprice_quotation(
                session,
                quote,
                prices={1: Decimal("1"), 2: Decimal("1"), 3: Decimal("1")},
                valid_until=date(2027, 1, 1),
            ),
            ConvertedQuotationError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: add_line(
                session, quote, line_no=4, description="Late", quantity="1", unit_price="1"
            ),
            ConvertedQuotationError,
        )
        session.rollback()
        # the schema refuses a hand-written second order for the same quotation
        try:
            session.execute(
                insert(SalesOrder.__table__).values(
                    id=uuid.uuid4(),
                    company_id=COMPANY,
                    quotation_id=quote.id,
                    customer_id=acme.id,
                    number="SO-SNEAK",
                    ordered_on=date(2026, 10, 7),
                )
            )
            session.commit()
        except DBAPIError as exc:
            assert "uq_sales_order_quotation" in str(exc), exc
            session.rollback()
        else:
            raise AssertionError("the database accepted a second order for one quotation")
        print(f"5. a second conversion is refused ({said[:70]}\u2026), and so is a hand-written one")

        # 6 — the API drives the same path, refusals included
        role = define_role(session, company_id=COMPANY, code="seller", name="Seller")
        grant(
            session,
            role,
            "quotation.read",
            "quotation.write",
            "order.write",
        )
        assign(session, company_id=COMPANY, subject="maria", role=role)
        session.commit()

    client = TestClient(app, raise_server_exceptions=False)
    headers = {"X-Company-Id": str(COMPANY), "X-Actor": "maria"}

    raised = client.post(
        f"{BASE}/quotations",
        headers=headers,
        json={
            "number": "Q-API",
            "customer_code": "ACME",
            "issued_on": "2026-10-01",
            "valid_until": "2026-12-31",
            "lines": [
                {"line_no": 1, "description": "Widget", "quantity": "4", "unit_price": "2.50",
                 "uom": "pcs", "rule_code": "TIER-A", "priced_on": "2026-10-01"},
                {"line_no": 2, "description": "Freight", "quantity": "1", "unit_price": "75.00"},
            ],
        },
    )
    assert raised.status_code == 201, raised.text
    body = raised.json()
    assert body["customer_code"] == "ACME" and body["expired"] is False, body
    assert [line["unit_price"] for line in body["lines"]] == ["2.500000", "75.000000"], body["lines"]
    assert body["lines"][0]["rule_code"] == "TIER-A" and body["lines"][1]["rule_code"] is None
    assert body["lines"][0]["priced_on"] == "2026-10-01", body["lines"][0]
    assert body["lines"][1]["priced_on"] == TODAY, (
        f"an unstated priced_on read {body['lines'][1]['priced_on']!r}, not the day the"
        " line was written"
    )
    assert body["total"] == "85.000000", body["total"]
    read_back = client.get(f"{BASE}/quotations/Q-API", headers=headers)
    assert read_back.status_code == 200 and read_back.json() == body, read_back.text

    refused = _refusal(
        client.post(
            f"{BASE}/quotations/Q-API/reprice",
            headers=headers,
            json={"valid_until": "2027-01-31", "prices": [{"line_no": 1, "unit_price": "3.00"}]},
        ),
        "quotation_error",
    )
    ordered = client.post(
        f"{BASE}/quotations/Q-API/order", headers=headers, json={"number": "SO-API", "on": "2026-10-05"}
    )
    assert ordered.status_code == 201, ordered.text
    placed = ordered.json()
    assert placed["quotation"] == "Q-API" and placed["customer_code"] == "ACME", placed
    assert [line["unit_price"] for line in placed["lines"]] == ["2.500000", "75.000000"], placed["lines"]
    assert placed["total"] == "85.000000", placed["total"]
    twice = _refusal(
        client.post(
            f"{BASE}/quotations/Q-API/order", headers=headers, json={"number": "SO-API-2"}
        ),
        "order_error",
    )
    converted_refusal = _refusal(
        client.post(
            f"{BASE}/quotations/Q-STALE/order", headers=headers, json={"number": "SO-API-STALE"}
        ),
        "order_error",
    )

    # An expired offer over the API: the refusal, the re-price, and the conversion that
    # then goes through — the criterion, driven end to end rather than only in the service.
    lapsed = client.post(
        f"{BASE}/quotations",
        headers=headers,
        json={
            "number": "Q-API-LAPSED",
            "customer_code": "ACME",
            "issued_on": "2026-08-01",
            "valid_until": "2026-09-01",
            "lines": [
                {"line_no": 1, "description": "Widget", "quantity": "2", "unit_price": "9.00"}
            ],
        },
    )
    assert lapsed.status_code == 201, lapsed.text
    assert lapsed.json()["expired"] is True, lapsed.json()
    lapsed_refusal = _refusal(
        client.post(
            f"{BASE}/quotations/Q-API-LAPSED/order", headers=headers, json={"number": "SO-LAPSED"}
        ),
        "quotation_error",
    )
    renewed = client.post(
        f"{BASE}/quotations/Q-API-LAPSED/reprice",
        headers=headers,
        json={
            "valid_until": "2026-12-31",
            "prices": [{"line_no": 1, "unit_price": "9.50", "rule_code": "TIER-B"}],
        },
    )
    assert renewed.status_code == 200, renewed.text
    assert renewed.json()["expired"] is False, renewed.json()
    assert renewed.json()["lines"][0]["rule_code"] == "TIER-B", renewed.json()["lines"]
    recovered = client.post(
        f"{BASE}/quotations/Q-API-LAPSED/order", headers=headers, json={"number": "SO-LAPSED"}
    )
    assert recovered.status_code == 201, recovered.text
    assert recovered.json()["total"] == "19.000000", recovered.json()

    forbidden = client.post(
        f"{BASE}/quotations",
        headers={**headers, "X-Actor": "stranger"},
        json={
            "number": "Q-NOPE",
            "customer_code": "ACME",
            "lines": [{"line_no": 1, "description": "x", "quantity": "1", "unit_price": "1"}],
        },
    )
    assert forbidden.status_code == 403, forbidden.text
    assert _shape_error(forbidden)["code"] == "forbidden"
    # a number this company already uses is refused, not silently filed twice
    reused = client.post(
        f"{BASE}/quotations",
        headers=headers,
        json={
            "number": "Q-API",
            "customer_code": "ACME",
            "lines": [{"line_no": 1, "description": "x", "quantity": "1", "unit_price": "1"}],
        },
    )
    assert _shape_error(reused)["code"] == "quotation_error", reused.text
    assert "Q-API" in _shape_error(reused)["message"], reused.text
    # a quotation with no lines is refused at the boundary: nothing could ever convert it
    empty = client.post(
        f"{BASE}/quotations",
        headers=headers,
        json={"number": "Q-EMPTY", "customer_code": "ACME", "lines": []},
    )
    assert empty.status_code == 422, empty.text
    assert _shape_error(empty)["code"] == "invalid_request", empty.text
    print(
        f"6. the API priced Q-API (total {body['total']}), refused a partial re-price"
        f" ({refused[:40]}\u2026), converted it to SO-API once ({twice[:40]}\u2026),"
        f" refused the converted one ({converted_refusal[:40]}\u2026), refused the"
        f" expired Q-API-LAPSED ({lapsed_refusal[:50]}\u2026), converted it after a"
        " re-price, and answered 403 to a stranger"
    )

    print("check_quotations: all assertions green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
