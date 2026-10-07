"""T-3.SALES.07 check — campaigns and coupons: scoped codes with a life.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_campaigns.py

**One coupon redeemed to its limit and then refused, plus an out-of-window rejection.**
Green on all six:

1. **a campaign is a dimension of the engine, not a second engine** — a rule scoped to a
   campaign applies when the line is priced under it and not otherwise, and it takes part
   in T-3.SALES.06's one documented resolution order
2. **a coupon applies only within its validity window** — the day before it opens is
   refused, the opening and closing days are inclusive, and the day after it closes is
   refused; an **unknown** code is refused naming the code it could not find
3. **and only up to its usage limit** — one coupon with a limit of one is redeemed, then
   the second attempt is refused with the limit and the count in the message, and the
   unlimited coupon beside it keeps working
4. **a coupon cannot be stacked beyond the configured allowance** — with two coupons of
   allowance 2 and one of allowance 1, the third on a document is refused, and the
   strictest allowance on the document is the one that governs
5. **redemption is recorded against the document that used it, and cannot be edited
   away** — the redemption names the document's type and id, a second redemption of the
   same code on the same document is refused, and a hand-written UPDATE and DELETE on the
   record are both refused by the database
6. **the whole path is drivable through the published API**, refusals included:
   `POST {BASE}/coupons` and `POST {BASE}/coupons/{code}/redeem`

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api import BASE, app  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.sales.campaigns import (  # noqa: E402
    CouponExhaustedError,
    CouponExpiredError,
    CouponNotYetValidError,
    CouponStackingRefused,
    DuplicateCouponError,
    UnknownCouponError,
    coupon_by_code,
    coupon_discount,
    define_coupon,
    redeem_coupon,
    redemption_count,
    redemptions_for,
)
from app.sales.pricing import define_rule, resolve_price  # noqa: E402
from app.security import assign, define_role, grant  # noqa: E402

COMPANY = uuid.uuid4()
OTHER = uuid.uuid4()
OPEN = date(2026, 10, 1)
CLOSE = date(2026, 10, 31)
BASE_PRICE = Decimal("200.00")
DOC_A = uuid.uuid4()
DOC_B = uuid.uuid4()
DOC_C = uuid.uuid4()


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
        for company_id, code in ((COMPANY, "COUPON-CHECK"), (OTHER, "OTHER-CO")):
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

        # 1 — a campaign is a scope the engine already resolves over
        define_rule(
            session,
            company_id=COMPANY,
            code="XMAS-10",
            name="10% off in the Christmas campaign",
            discount_type="percent",
            discount_value="10",
            campaign="XMAS",
            priority=100,
        )
        define_rule(
            session,
            company_id=COMPANY,
            code="ANY-5",
            name="5% off everything",
            discount_type="percent",
            discount_value="5",
            priority=100,
        )
        session.commit()
        in_campaign = resolve_price(
            session, company_id=COMPANY, base_price=BASE_PRICE, quantity="1", campaign="XMAS"
        )
        out_of_campaign = resolve_price(
            session, company_id=COMPANY, base_price=BASE_PRICE, quantity="1", campaign="EASTER"
        )
        none_stated = resolve_price(
            session, company_id=COMPANY, base_price=BASE_PRICE, quantity="1"
        )
        assert in_campaign.rule_code == "XMAS-10", in_campaign.rule_code
        assert out_of_campaign.rule_code == "ANY-5", out_of_campaign.rule_code
        assert none_stated.rule_code == "ANY-5", none_stated.rule_code
        assert in_campaign.price == Decimal("180.000000"), in_campaign.price
        print(
            f"1. under campaign XMAS the engine picks {in_campaign.rule_code!r} at"
            f" {in_campaign.price}; under EASTER and under none it falls to"
            f" {none_stated.rule_code!r}"
        )

        # 2 + 3 + 4 — the coupons themselves
        launch = define_coupon(
            session,
            company_id=COMPANY,
            code="LAUNCH50",
            name="50 off the launch",
            campaign="XMAS",
            discount_type="amount",
            discount_value="50",
            stacking_allowance=2,
            valid_from=OPEN,
            valid_until=CLOSE,
            max_redemptions=1,
        )
        open_ended = define_coupon(
            session,
            company_id=COMPANY,
            code="ALWAYS5",
            name="5% off, always",
            campaign="EVERGREEN",
            discount_type="percent",
            discount_value="5",
            stacking_allowance=2,
        )
        tight = define_coupon(
            session,
            company_id=COMPANY,
            code="TIGHT",
            name="10% off, and only ever one coupon at a time",
            campaign="EVERGREEN",
            discount_type="percent",
            discount_value="10",
            stacking_allowance=1,
        )
        session.commit()
        assert coupon_discount(launch, BASE_PRICE) == Decimal("50.000000")

        before = _refused(
            lambda: redeem_coupon(
                session,
                launch,
                document_type="quotation",
                document_id=DOC_A,
                base_price=BASE_PRICE,
                on=date(2026, 9, 30),
            ),
            CouponNotYetValidError,
        )
        session.rollback()
        assert "opens on 2026-10-01" in before, before
        after = _refused(
            lambda: redeem_coupon(
                session,
                launch,
                document_type="quotation",
                document_id=DOC_A,
                base_price=BASE_PRICE,
                on=date(2026, 11, 1),
            ),
            CouponExpiredError,
        )
        session.rollback()
        assert "closed on 2026-10-31" in after, after
        unknown = _refused(
            lambda: coupon_by_code(session, company_id=COMPANY, code="NOPE"),
            UnknownCouponError,
        )
        session.rollback()
        assert "NOPE" in unknown, unknown
        print(
            f"2. the window is enforced at both ends ({before[:44]}\u2026 / {after[:44]}\u2026),"
            f" and an unknown code is refused naming it ({unknown[:44]}\u2026)"
        )

        # the opening and closing days are inclusive: redeem on the closing day
        first = redeem_coupon(
            session,
            launch,
            document_type="quotation",
            document_id=DOC_A,
            base_price=BASE_PRICE,
            on=CLOSE,
        )
        session.commit()
        assert first.discount_amount == Decimal("50.000000"), first.discount_amount
        assert redemption_count(session, launch) == 1
        exhausted = _refused(
            lambda: redeem_coupon(
                session,
                launch,
                document_type="quotation",
                document_id=DOC_B,
                base_price=BASE_PRICE,
                on=OPEN,
            ),
            CouponExhaustedError,
        )
        session.rollback()
        assert "allows 1 redemption(s) and has used 1" in exhausted, exhausted
        # the coupon beside it, with no limit stated, keeps working
        second = redeem_coupon(
            session,
            open_ended,
            document_type="quotation",
            document_id=DOC_B,
            base_price=BASE_PRICE,
            on=OPEN,
        )
        session.commit()
        assert second.discount_amount == Decimal("10.000000"), second.discount_amount
        print(
            f"3. the closing day is inclusive and the limit bites ({exhausted[:52]}\u2026);"
            f" an uncapped coupon beside it still applies ({second.discount_amount})"
        )

        # stacking: ALWAYS5 and LAUNCH-exhausted aside, DOC_B already carries ALWAYS5
        # (allowance 2). Adding TIGHT (allowance 1) makes the strictest allowance 1.
        stacked = _refused(
            lambda: redeem_coupon(
                session,
                tight,
                document_type="quotation",
                document_id=DOC_B,
                base_price=BASE_PRICE,
                on=OPEN,
            ),
            CouponStackingRefused,
        )
        session.rollback()
        assert "strictest allowance is 1" in stacked, stacked
        twice = _refused(
            lambda: redeem_coupon(
                session,
                open_ended,
                document_type="quotation",
                document_id=DOC_B,
                base_price=BASE_PRICE,
                on=OPEN,
            ),
            CouponStackingRefused,
        )
        session.rollback()
        assert "already on this document" in twice, twice
        # on a fresh document with room, the same tight coupon is fine
        third = redeem_coupon(
            session,
            tight,
            document_type="quotation",
            document_id=DOC_C,
            base_price=BASE_PRICE,
            on=OPEN,
        )
        session.commit()
        assert third.discount_amount == Decimal("20.000000"), third.discount_amount
        print(
            f"4. a second coupon is refused where the allowance is 1"
            f" ('{stacked[:48]}\u2026'), and the same coupon is fine on a document with room"
        )

        # 5 — the redemption names the document, and is history
        standing = redemptions_for(
            session, document_type="quotation", document_id=DOC_B
        )
        assert len(standing) == 1, [r.id for r in standing]
        assert standing[0].document_type == "quotation"
        assert standing[0].document_id == DOC_B
        assert standing[0].coupon.code == "ALWAYS5", standing[0].coupon.code
        for statement in (
            f"UPDATE coupon_redemption SET discount_amount = 0 WHERE id = '{first.id}'",
            f"DELETE FROM coupon_redemption WHERE id = '{first.id}'",
        ):
            try:
                session.execute(text(statement))
                session.commit()
            except DBAPIError as exc:
                assert "append-only" in str(exc), exc
                session.rollback()
            else:
                raise AssertionError(f"the database allowed: {statement}")
        # a code from another company is not this company's coupon
        _refused(
            lambda: coupon_by_code(session, company_id=OTHER, code="LAUNCH50"),
            UnknownCouponError,
        )
        session.rollback()
        print(
            "5. the redemption names its document (quotation/"
            f"{str(DOC_B)[:8]}\u2026), is refused twice on one document, and a hand-written"
            " UPDATE and DELETE are both refused"
        )

        # refusals on filing: a duplicate code, an unknown discount type, a bad allowance
        said = _refused(
            lambda: define_coupon(
                session,
                company_id=COMPANY,
                code="LAUNCH50",
                name="Again",
                campaign="XMAS",
                discount_type="amount",
                discount_value="1",
                stacking_allowance=1,
            ),
            DuplicateCouponError,
        )
        session.rollback()
        said_two = _refused(
            lambda: define_coupon(
                session,
                company_id=COMPANY,
                code="BAD-ALLOWANCE",
                name="Bad",
                campaign="XMAS",
                discount_type="percent",
                discount_value="5",
                stacking_allowance=0,
            ),
            "would forbid the coupon itself",
        )
        session.rollback()
        assert "already has a coupon" in said, said
        print(f"   refusals on filing: {said_two[:64]}\u2026")

        seller = define_role(session, company_id=COMPANY, code="seller", name="Seller")
        grant(session, seller, "pricing.configure", "order.write")
        assign(session, company_id=COMPANY, subject="maria", role=seller)
        session.commit()

    client = TestClient(app, raise_server_exceptions=False)
    headers = {"X-Company-Id": str(COMPANY), "X-Actor": "maria"}

    # 6 — the API files a coupon and spends it
    filed = client.post(
        f"{BASE}/coupons",
        headers=headers,
        json={
            "code": "API-20",
            "name": "20 off, this week only",
            "campaign": "API-CAMPAIGN",
            "discount_type": "amount",
            "discount_value": "20",
            "stacking_allowance": 2,
            "valid_from": "2026-10-01",
            "valid_until": "2026-10-07",
            "max_redemptions": 2,
        },
    )
    assert filed.status_code == 201, filed.text
    assert filed.json()["campaign"] == "API-CAMPAIGN", filed.text

    unknown = client.post(
        f"{BASE}/coupons/NO-SUCH-CODE/redeem",
        headers=headers,
        json={
            "document_type": "quotation",
            "document_id": str(DOC_A),
            "base_price": "200.00",
        },
    )
    assert unknown.status_code == 422, unknown.text
    assert _refusal(unknown, "coupon_error")

    spent = client.post(
        f"{BASE}/coupons/API-20/redeem",
        headers=headers,
        json={
            "document_type": "sales_order",
            "document_id": str(DOC_C),
            "base_price": "200.00",
            "on": "2026-10-07",
        },
    )
    assert spent.status_code == 200, spent.text
    body = spent.json()
    assert body["discount_amount"] == "20.000000", body
    assert body["document_type"] == "sales_order", body
    assert body["document_id"] == str(DOC_C), body

    late = client.post(
        f"{BASE}/coupons/API-20/redeem",
        headers=headers,
        json={
            "document_type": "quotation",
            "document_id": str(DOC_B),
            "base_price": "200.00",
            "on": "2026-11-01",
        },
    )
    assert late.status_code == 422, late.text
    assert "closed on 2026-10-07" in _refusal(late, "coupon_error")
    print(
        "6. the API files a coupon, refuses an unknown code, records the redemption"
        " against its document, and refuses the one used outside its window"
    )

    with Session(engine) as session:
        assert redemption_count(
            session, coupon_by_code(session, company_id=COMPANY, code="API-20")
        ) == 1

    print(
        "\nok — the campaign dimension rides the one engine, coupons hold to their window,"
        " limit and allowance, and every use is written down against its document"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
