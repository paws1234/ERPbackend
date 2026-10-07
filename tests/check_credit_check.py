"""T-3.SALES.04 check — sales orders: confirmation and the order-time credit decision.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_credit_check.py

Green on all seven:

1. **a company that has stated no credit-check mode cannot confirm an order** — plan §8
   leaves the mode undecided, so there is no default to apply and no policy to invent:
   the refusal names the fix, and the order stays a draft. `off` is a *policy* ("check
   nothing") and unstated is not `off`, which is why the two are asserted apart
2. **`block` mode refuses the confirmation that breaches the limit** and lets the one
   that does not through, ever: the refusal names the limit it breached and the order is
   still a draft with no decision recorded against it
3. **`warn` mode confirms only with a recorded acknowledgement** — refused without one,
   and the acknowledgement is written down naming the actor who gave it, because a
   breach somebody accepted has to be attributable
4. **the decision shows the limit, the exposure and the order value that produced it**,
   as they stood at confirmation, including the honest null for a customer with no limit
   agreed
5. **changing the mode, or the limit, does not alter a decision already taken** — the
   recorded mode, limit and amounts are read back unchanged afterwards, and the decision
   is append-only: a hand-written UPDATE and DELETE are both refused by the database
6. **the exposure is validated, not trusted** — a float, an infinity and a negative are
   each refused rather than compared, and the order is not confirmed by the attempt
7. **the whole path is drivable through the published API**, refusals included:
   `POST /api/v1/companies/current/credit-check-mode`, `POST /api/v1/sales-orders/{number}`
   `/confirm` and `GET /api/v1/sales-orders/{number}`

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api import BASE, app  # noqa: E402
from app.company import (  # noqa: E402
    Company,
    UnknownCreditCheckMode,
    credit_check_mode_of,
    set_credit_check_mode,
)
from app.db import Base  # noqa: E402
from app.sales.customers import (  # noqa: E402
    create_customer,
    customer_by_code,
    set_credit_limit,
)
from app.sales.orders import (  # noqa: E402
    BREACHED,
    CONFIRMED,
    DRAFT,
    WITHIN_LIMIT,
    CreditAcknowledgementRequired,
    CreditError,
    CreditLimitExceeded,
    NoCreditCheckMode,
    OrderAlreadyConfirmed,
    confirm_order,
    convert_quotation_to_order,
    credit_decision_for,
    order_by_number,
)
from app.sales.quotations import add_line, create_quotation  # noqa: E402
from app.security import assign, define_role, grant  # noqa: E402

COMPANY = uuid.uuid4()
OCT = date(2026, 10, 1)
# The order is 10 x 5.25 + 250.50 = 303.00, so an exposure of 500 is comfortably inside
# a 1000 limit and an exposure of 800 is not.
BREACHING_EXPOSURE = "800"
SAFE_EXPOSURE = "500"


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


def _order(session: Session, number: str):
    """The order as it now stands — re-read, so a rollback cannot leave a stale object."""
    return order_by_number(session, company_id=COMPANY, number=number)


def _new_order(session: Session, *, customer, number: str):
    """One confirmable order: a quotation with two lines, converted."""
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
        quantity="10",
        unit_price="5.25",
        uom="pcs",
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
        company = Company(
            id=COMPANY,
            code="CREDIT-CHECK",
            name="Credit check",
            base_currency="PHP",
            fiscal_year_start_month=1,
        )
        session.add(company)
        session.commit()

        # The column has no default: a fresh company has stated no policy at all.
        assert company.credit_check_mode is None, company.credit_check_mode
        assert credit_check_mode_of(session, company_id=COMPANY) is None

        acme = create_customer(
            session,
            company_id=COMPANY,
            party_code="ACME",
            name="Acme Retail",
            payment_terms_days=30,
            credit_limit="1000",
        )
        nocap = create_customer(
            session,
            company_id=COMPANY,
            party_code="NOCAP",
            name="No Cap Ltd",
            payment_terms_days=30,
        )
        session.commit()

        # 1 — no stated mode is a refusal, not a silent policy
        draft = _new_order(session, customer=acme, number="SO-1")
        assert draft.status == DRAFT, draft.status
        said = _refused(
            lambda: confirm_order(
                session, draft, exposure=SAFE_EXPOSURE, actor="maria"
            ),
            NoCreditCheckMode,
        )
        session.rollback()
        assert "no credit-check mode" in said, said
        assert _order(session, "SO-1").status == DRAFT, "an unstated mode confirmed anyway"
        assert credit_decision_for(session, _order(session, "SO-1")) is None, (
            "a refused confirmation recorded a decision"
        )
        said = _refused(
            lambda: set_credit_check_mode(session, company, mode="maybe"),
            UnknownCreditCheckMode,
        )
        session.rollback()
        assert "off, warn, block" in said, said
        print(f"1. an unstated mode refuses the confirmation ({said[:60]}\u2026), and so does an unknown one")

        # 2 — `block` refuses the breach, and only the breach
        set_credit_check_mode(session, company, mode="block")
        session.commit()
        safe = _order(session, "SO-1")
        decision = confirm_order(session, safe, exposure=SAFE_EXPOSURE, actor="maria")
        session.commit()
        assert decision.outcome == WITHIN_LIMIT, decision.outcome
        assert safe.status == CONFIRMED, safe.status
        assert safe.confirmed_by == "maria" and safe.confirmed_at is not None

        blocked = _new_order(session, customer=acme, number="SO-2")
        said = _refused(
            lambda: confirm_order(
                session, blocked, exposure=BREACHING_EXPOSURE, actor="maria"
            ),
            CreditLimitExceeded,
        )
        session.rollback()
        assert "1000" in said and "1103.00" in said, said
        after = _order(session, "SO-2")
        assert after.status == DRAFT, "a blocked order was confirmed"
        assert credit_decision_for(session, after) is None, "a blocked order recorded a decision"
        print(f"2. block refuses the breach ({said[:60]}\u2026) and only the breach")

        # 3 — `warn` needs the acknowledgement, and records it
        set_credit_check_mode(session, company, mode="warn")
        session.commit()
        warned = _order(session, "SO-2")
        said = _refused(
            lambda: confirm_order(
                session, warned, exposure=BREACHING_EXPOSURE, actor="maria"
            ),
            CreditAcknowledgementRequired,
        )
        session.rollback()
        assert _order(session, "SO-2").status == DRAFT, "warn confirmed without an acknowledgement"
        warned = _order(session, "SO-2")
        acknowledged = confirm_order(
            session,
            warned,
            exposure=BREACHING_EXPOSURE,
            actor="maria",
            acknowledge_breach=True,
        )
        session.commit()
        assert acknowledged.outcome == BREACHED, acknowledged.outcome
        assert acknowledged.acknowledged_by == "maria", acknowledged.acknowledged_by
        assert warned.status == CONFIRMED and warned.confirmed_by == "maria"
        print(f"3. warn refuses without an acknowledgement ({said[:50]}\u2026), and records the one given")

        # 4 — the decision carries the three numbers that produced it
        assert acknowledged.mode == "warn", acknowledged.mode
        assert acknowledged.limit_amount == Decimal("1000.000000"), acknowledged.limit_amount
        assert acknowledged.exposure == Decimal(BREACHING_EXPOSURE), acknowledged.exposure
        assert acknowledged.order_value == Decimal("303.000000"), acknowledged.order_value
        assert acknowledged.exposure + acknowledged.order_value == Decimal("1103.000000")
        assert acknowledged.decided_at is not None

        # a customer with no limit agreed records the honest null, not zero
        uncapped = _new_order(session, customer=nocap, number="SO-3")
        open_ended = confirm_order(
            session, uncapped, exposure="999999", actor="maria"
        )
        session.commit()
        assert open_ended.limit_amount is None, open_ended.limit_amount
        assert open_ended.outcome == WITHIN_LIMIT, (
            "a customer with no limit agreed was judged against one"
        )
        print(
            "4. the decision shows mode warn, limit 1000.000000, exposure 800.000000 and value"
            " 303.000000, and null for a customer with no limit agreed"
        )

        # 5 — history is not restated by the things that produced it
        set_credit_check_mode(session, company, mode="off")
        set_credit_limit(session, acme, limit="10")
        session.commit()
        reread = credit_decision_for(session, _order(session, "SO-2"))
        assert reread.mode == "warn", reread.mode
        assert reread.limit_amount == Decimal("1000.000000"), reread.limit_amount
        assert reread.exposure == Decimal("800.000000") and reread.order_value == Decimal("303.000000")
        assert reread.outcome == BREACHED and reread.acknowledged_by == "maria"

        # and the decision cannot be edited or removed by hand either
        for statement in (
            f"UPDATE credit_decision SET exposure = 0 WHERE id = '{reread.id}'",
            f"DELETE FROM credit_decision WHERE id = '{reread.id}'",
        ):
            try:
                session.execute(text(statement))
                session.commit()
            except DBAPIError as exc:
                assert "append-only" in str(exc), exc
                session.rollback()
            else:
                raise AssertionError(f"the database allowed: {statement}")
        print(
            "5. changing the mode to off and the limit to 10 left the recorded decision at"
            " warn/1000.000000/800.000000, and a hand-written UPDATE and DELETE were refused"
        )

        # 6 — a stated exposure that is not an amount is refused, not compared
        off_balance = _new_order(session, customer=acme, number="SO-4")
        said = []
        for bad in (500.5, "Infinity", "-1"):
            said.append(
                _refused(
                    lambda bad=bad: confirm_order(
                        session, off_balance, exposure=bad, actor="maria"
                    ),
                    CreditError,
                )
            )
            session.rollback()
        assert _order(session, "SO-4").status == DRAFT, "an invalid exposure confirmed the order"
        # `off` is a policy, not an absence of one: it confirms and still writes the decision
        settled = confirm_order(
            session, off_balance, exposure=BREACHING_EXPOSURE, actor="maria"
        )
        session.commit()
        assert settled.outcome == BREACHED, settled.outcome
        assert settled.acknowledged_by is None, "off mode recorded an acknowledgement nobody gave"
        said_again = _refused(
            lambda: confirm_order(
                session, _order(session, "SO-4"), exposure="1", actor="maria"
            ),
            OrderAlreadyConfirmed,
        )
        session.rollback()
        print(
            f"6. a float, an infinity and a negative exposure are all refused, 'off' records"
            f" without enforcing, and a second confirmation is refused ({said_again[:40]}\u2026)"
        )

        # 7 — the API drives the same path, refusals included
        # Section 5 cut this customer's limit to 10 to prove the decision does not move;
        # put it back, so what follows is the API path and not that assertion again.
        set_credit_limit(session, acme, limit="1000")
        applier = define_role(
            session, company_id=COMPANY, code="seller", name="Seller"
        )
        grant(session, applier, "order.read", "order.write", "company.configure", "company.read")
        assign(session, company_id=COMPANY, subject="maria", role=applier)
        session.commit()

    client = TestClient(app, raise_server_exceptions=False)
    headers = {"X-Company-Id": str(COMPANY), "X-Actor": "maria"}

    stated = client.post(
        f"{BASE}/companies/current/credit-check-mode",
        headers=headers,
        json={"mode": "block"},
    )
    assert stated.status_code == 200, stated.text
    assert stated.json()["credit_check_mode"] == "block", stated.text
    bad_mode = client.post(
        f"{BASE}/companies/current/credit-check-mode",
        headers=headers,
        json={"mode": "nonsense"},
    )
    assert bad_mode.status_code == 422, bad_mode.text
    assert _refusal(bad_mode, "unknown_credit_check_mode")

    with Session(engine) as session:
        api_order = _new_order(
            session,
            customer=customer_by_code(session, company_id=COMPANY, code="ACME"),
            number="SO-5",
        )
        assert api_order.status == DRAFT

    over = client.post(
        f"{BASE}/sales-orders/SO-5/confirm",
        headers=headers,
        json={"exposure": BREACHING_EXPOSURE},
    )
    assert over.status_code == 422, over.text
    assert _refusal(over, "order_error")

    fine = client.post(
        f"{BASE}/sales-orders/SO-5/confirm",
        headers=headers,
        json={"exposure": SAFE_EXPOSURE},
    )
    assert fine.status_code == 200, fine.text
    body = fine.json()
    assert body["status"] == "confirmed" and body["confirmed_by"] == "maria", body
    assert body["credit_decision"]["mode"] == "block", body
    assert body["credit_decision"]["breached"] is False, body
    assert body["credit_decision"]["exposure"] == "500.000000", body
    assert body["credit_decision"]["order_value"] == "303.000000", body
    assert body["credit_decision"]["limit"] == "1000.000000", body

    shown = client.get(f"{BASE}/sales-orders/SO-5", headers=headers)
    assert shown.status_code == 200, shown.text
    assert shown.json()["credit_decision"] == body["credit_decision"], shown.text
    assert client.get(f"{BASE}/sales-orders/SO-404", headers=headers).status_code == 422
    print(
        "7. the API states the mode, refuses a breach with the platform's one error shape, and"
        " serves the recorded decision back unchanged"
    )

    print(
        "\nok — the order-time credit decision is applied per the company's stated mode,"
        " recorded with the numbers that produced it, and not restated afterwards"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
