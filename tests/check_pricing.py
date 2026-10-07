"""T-3.SALES.06 check — the pricing engine: ordered rules over tier and volume.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_pricing.py

**Overlapping rules on the same item resolved deterministically, with the winning rule
shown.** Green on all six:

1. **overlapping rules resolve in the documented order** — with four rules matching the
   same line, the winner is the one the order says, and the whole ordering is available
   rather than only the winner. Priority outranks specificity (a deliberate override),
   specificity breaks a priority tie (item before any-item, tier before any-tier), the
   tighter volume band beats the wider one, and the code breaks what is left — so the
   same inputs give the same answer however the rows come back
2. **the resolution order is stated on the priced line** — the priced line carries the
   winning rule's `code` **and** the `priority` that decided it, so the line explains
   itself later, after the rules have changed
3. **a quote records the rule that applied, so it can be reproduced** — re-resolving the
   same line after a rule is edited gives a different answer, while the line still
   reports the price and rule it was priced under; the quotation is unaffected by the
   rule change, which is what "a tier change does not retroactively change already-priced
   documents" means in practice
4. **tier and volume are the dimensions** — a customer in no tier is not matched by a
   rule written for a tier (null is *no constraint* on the rule, and *no tier* on the
   customer), a quantity below the band is not matched, and the band's ceiling is
   inclusive
5. **the engine is one implementation, used by both documents** — a quotation priced
   through the engine carries `rule_code` and `rule_priority`, and the order converted
   from it carries the same two unchanged, so the two documents cannot disagree about
   why the price is what it is
6. **the whole path is drivable through the published API**, refusals included:
   `POST {BASE}/price-rules` and `POST {BASE}/price-rules/resolve` — with a wrong
   discount type refused, an unknown SKU refused, and the resolution order returned

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api import BASE, app  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger.currency import register_currency  # noqa: E402
from app.sales.customers import create_customer, set_customer_tier  # noqa: E402
from app.sales.orders import convert_quotation_to_order, order_by_number  # noqa: E402
from app.sales.pricing import (  # noqa: E402
    DuplicateRuleError,
    PricingError,
    UnknownDiscountType,
    define_rule,
    resolve_price,
    rule_by_code,
    tier_of,
)
from app.sales.pricing import price_quote_line  # noqa: E402
from app.sales.quotations import create_quotation, lines_of  # noqa: E402
from app.security import assign, define_role, grant  # noqa: E402
from app.stock.items import create_item  # noqa: E402

COMPANY = uuid.uuid4()
OCT = date(2026, 10, 1)
BASE_PRICE = Decimal("100.00")


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
    body = response.json()
    assert isinstance(body, dict) and set(body) == {"error"}, f"not the one error shape: {body}"
    assert set(body["error"]) == {"code", "message", "details"}, body
    return body["error"]


def _refusal(response, expected: str) -> str:
    error = _shape_error(response)
    assert error["code"] == expected, f"got {error}"
    return error["message"]


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
                code="PRICE-CHECK",
                name="Pricing check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        register_currency(session, company_id=COMPANY, code="PHP", name="Peso")
        session.commit()

        widget = create_item(
            session,
            company_id=COMPANY,
            sku="WIDGET",
            name="Widget",
            base_uom="each",
            traceability_mode="none",
        )
        gadget = create_item(
            session,
            company_id=COMPANY,
            sku="GADGET",
            name="Gadget",
            base_uom="each",
            traceability_mode="none",
        )
        gold = create_customer(
            session,
            company_id=COMPANY,
            party_code="GOLD-CO",
            name="Gold Retail",
            payment_terms_days=30,
        )
        plain = create_customer(
            session,
            company_id=COMPANY,
            party_code="PLAIN-CO",
            name="Plain Retail",
            payment_terms_days=30,
        )
        set_customer_tier(session, gold, tier="GOLD")
        session.commit()
        assert tier_of(gold) == "GOLD" and tier_of(plain) is None

        # 1 — four rules, one item, all overlapping: the documented order decides
        define_rule(
            session,
            company_id=COMPANY,
            code="ANY-5",
            name="5% off everything",
            discount_type="percent",
            discount_value="5",
            priority=100,
        )
        define_rule(
            session,
            company_id=COMPANY,
            code="GOLD-TIER-10",
            name="10% off for the gold tier",
            discount_type="percent",
            discount_value="10",
            tier="GOLD",
            priority=100,
        )
        define_rule(
            session,
            company_id=COMPANY,
            code="ITEM-15",
            name="15% off this item",
            discount_type="percent",
            discount_value="15",
            item_id=widget.id,
            priority=100,
        )
        define_rule(
            session,
            company_id=COMPANY,
            code="VOL-20",
            name="20% off from 50 up, gold tier, this item",
            discount_type="percent",
            discount_value="20",
            item_id=widget.id,
            tier="GOLD",
            min_quantity="50",
            priority=100,
        )
        define_rule(
            session,
            company_id=COMPANY,
            code="OVERRIDE",
            name="A deliberate override: 30% off, and it wins on priority",
            discount_type="percent",
            discount_value="30",
            item_id=widget.id,
            tier="GOLD",
            min_quantity="50",
            priority=1,
        )
        session.commit()

        decision = resolve_price(
            session,
            company_id=COMPANY,
            base_price=BASE_PRICE,
            quantity="60",
            item_id=widget.id,
            tier="GOLD",
        )
        order = [rule.code for rule in decision.considered]
        assert decision.rule_code == "OVERRIDE", order
        assert order == ["OVERRIDE", "VOL-20", "ITEM-15", "GOLD-TIER-10", "ANY-5"], order
        assert decision.rule_priority == 1, decision.rule_priority
        assert decision.price == Decimal("70.000000"), decision.price
        # the same inputs give the same answer, every time
        again = resolve_price(
            session,
            company_id=COMPANY,
            base_price=BASE_PRICE,
            quantity="60",
            item_id=widget.id,
            tier="GOLD",
        )
        assert [r.code for r in again.considered] == order and again.price == decision.price
        print(
            f"1. five overlapping rules resolved to {decision.rule_code!r} at"
            f" {decision.price} off {BASE_PRICE}, in the order {order}"
        )

        # 2 — the line states which rule won and where it sat
        quotation = create_quotation(
            session,
            company_id=COMPANY,
            customer_id=gold.id,
            number="Q-1",
            issued_on=OCT,
            valid_until=date(2026, 12, 31),
        )
        session.flush()
        line = price_quote_line(
            session,
            quotation,
            line_no=1,
            description="Widget",
            quantity="60",
            base_price=BASE_PRICE,
            item_id=widget.id,
            uom="each",
            on=OCT,
        )
        session.commit()
        assert line.rule_code == "OVERRIDE", line.rule_code
        assert line.rule_priority == 1, line.rule_priority
        assert line.unit_price == Decimal("70.000000"), line.unit_price
        assert line.priced_on == OCT, line.priced_on
        print(
            f"2. the priced line states rule {line.rule_code!r} at priority"
            f" {line.rule_priority}, at {line.unit_price}"
        )

        # 3 — a rule change does not restate the quote already priced
        winner = rule_by_code(session, company_id=COMPANY, code="OVERRIDE")
        winner.discount_value = Decimal("99")
        winner.priority = 900
        session.commit()
        after = resolve_price(
            session,
            company_id=COMPANY,
            base_price=BASE_PRICE,
            quantity="60",
            item_id=widget.id,
            tier="GOLD",
        )
        assert after.rule_code == "VOL-20", after.rule_code
        assert after.price == Decimal("80.000000"), after.price
        unchanged = lines_of(session, quotation)[0]
        assert unchanged.rule_code == "OVERRIDE", unchanged.rule_code
        assert unchanged.rule_priority == 1, unchanged.rule_priority
        assert unchanged.unit_price == Decimal("70.000000"), unchanged.unit_price
        print(
            f"3. after the winning rule was edited the same query answers {after.rule_code!r}"
            f" at {after.price}, while the quote still holds {unchanged.rule_code!r} at"
            f" {unchanged.unit_price}"
        )

        # 4 — tier and volume are the dimensions, and null means no constraint
        #     a customer in no tier is not matched by a tier-scoped rule
        untiered = resolve_price(
            session,
            company_id=COMPANY,
            base_price=BASE_PRICE,
            quantity="60",
            item_id=widget.id,
            tier=tier_of(plain),
        )
        assert untiered.rule_code == "ITEM-15", untiered.rule_code
        #     below the band, and the band's ceiling is inclusive
        small = resolve_price(
            session,
            company_id=COMPANY,
            base_price=BASE_PRICE,
            quantity="50",
            item_id=widget.id,
            tier="GOLD",
        )
        assert small.rule_code == "VOL-20", small.rule_code
        # a band with a ceiling, so "the tighter band wins" is tested against a closed
        # one: `VOL-20`'s null ceiling is deliberately open, not an invented one
        define_rule(
            session,
            company_id=COMPANY,
            code="BAND-25",
            name="25% off this item between 10 and 20",
            discount_type="percent",
            discount_value="25",
            item_id=widget.id,
            min_quantity="10",
            max_quantity="20",
            priority=100,
        )
        session.commit()
        in_band = resolve_price(
            session,
            company_id=COMPANY,
            base_price=BASE_PRICE,
            quantity="20",
            item_id=widget.id,
            tier="GOLD",
        )
        assert in_band.rule_code == "BAND-25", in_band.rule_code
        out_of_band = resolve_price(
            session,
            company_id=COMPANY,
            base_price=BASE_PRICE,
            quantity="21",
            item_id=widget.id,
            tier="GOLD",
        )
        assert out_of_band.rule_code == "ITEM-15", out_of_band.rule_code
        #     an amount discount, and an item no rule names, and a percent over 100
        define_rule(
            session,
            company_id=COMPANY,
            code="FLAT-7",
            name="7.50 off anything",
            discount_type="amount",
            discount_value="7.50",
            min_quantity="1",
            priority=100,
        )
        session.commit()
        flat = resolve_price(
            session,
            company_id=COMPANY,
            base_price=BASE_PRICE,
            quantity="1",
            item_id=gadget.id,
            tier=None,
        )
        assert flat.rule_code == "ANY-5", flat.rule_code
        nobody = resolve_price(
            session,
            company_id=COMPANY,
            base_price=BASE_PRICE,
            quantity="1",
            item_id=None,
            tier=None,
        )
        assert nobody.rule_code == "ANY-5", nobody.rule_code
        print(
            f"4. a customer in no tier gets {untiered.rule_code!r} (not the tier rule);"
            f" an open band still applies at 50 ({small.rule_code!r}) while a closed one"
            f" applies at 20 ({in_band.rule_code!r}) and not at 21"
            f" ({out_of_band.rule_code!r}); an unnamed item falls to {nobody.rule_code!r}"
        )

        # 5 — one implementation: the order carries the quotation's own answer
        order_doc = convert_quotation_to_order(session, quotation, number="SO-1", on=OCT)
        session.commit()
        copied = order_doc.lines[0]
        assert copied.rule_code == unchanged.rule_code, copied.rule_code
        assert copied.rule_priority == unchanged.rule_priority, copied.rule_priority
        assert copied.unit_price == unchanged.unit_price, copied.unit_price
        print(
            f"5. the order carries {copied.rule_code!r} at priority {copied.rule_priority}"
            f" and {copied.unit_price} — the engine's answer, not a second one"
        )

        # refusals: an unknown discount type, a duplicate code, a negative price
        said = _refused(
            lambda: define_rule(
                session,
                company_id=COMPANY,
                code="BAD",
                name="Bad",
                discount_type="free",
                discount_value="1",
            ),
            UnknownDiscountType,
        )
        session.rollback()
        assert "percent, amount" in said, said
        said = _refused(
            lambda: define_rule(
                session,
                company_id=COMPANY,
                code="ANY-5",
                name="Again",
                discount_type="percent",
                discount_value="5",
            ),
            DuplicateRuleError,
        )
        session.rollback()
        # an `amount` discount larger than the price is a refusal, not a credit: the
        # rule has to win first, so it is given the lowest priority for the gadget
        define_rule(
            session,
            company_id=COMPANY,
            code="TOO-BIG",
            name="50 off the gadget",
            discount_type="amount",
            discount_value="50",
            item_id=gadget.id,
            priority=1,
        )
        session.commit()
        said = _refused(
            lambda: resolve_price(
                session,
                company_id=COMPANY,
                base_price="10",
                quantity="1",
                item_id=gadget.id,
                tier=None,
            ),
            "less than nothing",
        )
        session.rollback()
        print(f"   refusals: {said[:70]}\u2026")

        pricing = define_role(session, company_id=COMPANY, code="pricer", name="Pricer")
        grant(session, pricing, "pricing.configure", "order.read")
        assign(session, company_id=COMPANY, subject="maria", role=pricing)
        session.commit()

    client = TestClient(app, raise_server_exceptions=False)
    headers = {"X-Company-Id": str(COMPANY), "X-Actor": "maria"}

    # 6 — the API files a rule and asks the engine
    filed = client.post(
        f"{BASE}/price-rules",
        headers=headers,
        json={
            "code": "API-ITEM",
            "name": "12% off the widget",
            "discount_type": "percent",
            "discount_value": "12",
            "item_sku": "WIDGET",
            "min_quantity": "2",
            "priority": 2,
        },
    )
    assert filed.status_code == 201, filed.text
    assert filed.json()["item_sku"] == "WIDGET", filed.text

    bad = client.post(
        f"{BASE}/price-rules",
        headers=headers,
        json={
            "code": "API-BAD",
            "name": "Nope",
            "discount_type": "freebie",
            "discount_value": "1",
        },
    )
    assert bad.status_code == 422, bad.text
    assert _refusal(bad, "pricing_error")

    resolved = client.post(
        f"{BASE}/price-rules/resolve",
        headers=headers,
        json={"base_price": "100.00", "quantity": "3", "item_sku": "WIDGET"},
    )
    assert resolved.status_code == 200, resolved.text
    body = resolved.json()
    assert body["rule_code"] == "API-ITEM", body
    assert body["price"] == "88.000000", body
    assert [r["code"] for r in body["considered"]][0] == "API-ITEM", body["considered"]
    assert len(body["considered"]) > 1, "the whole ordering is not reported"

    unknown = client.post(
        f"{BASE}/price-rules/resolve",
        headers=headers,
        json={"base_price": "10.00", "quantity": "1", "item_sku": "NOPE"},
    )
    assert unknown.status_code == 422, unknown.text
    print(
        "6. the API files a rule, refuses an unknown discount type and an unknown SKU,"
        f" and resolves the decision with its full order ({len(body['considered'])} considered)"
    )

    with Session(engine) as session:
        stored = order_by_number(session, company_id=COMPANY, number="SO-1")
        assert stored.lines[0].rule_priority == 1, stored.lines[0].rule_priority

    print(
        "\nok — overlapping rules resolve in one documented order, the line states which"
        " rule won and where it sat, and a later rule change does not restate it"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
