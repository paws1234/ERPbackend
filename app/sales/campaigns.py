"""T-3.SALES.07 — campaigns and coupons: scoped discounts on top of the engine.

§2.5 asks for "Pricing Rules & Discounts (… campaigns, coupons)". T-3.SALES.06 built the
engine and the tier and volume dimensions; this module adds the other two dimensions the
ledger names, and the thing that makes a coupon a coupon: a **redemption**.

* **A campaign is a scope.** `PriceRule.campaign` is a fifth dimension beside item, tier
  and volume, so "10 % off everything in the summer campaign" is the same kind of row as
  the rules T-3.SALES.06 already resolves — and it takes part in the same documented
  resolution order rather than in a second engine.
* **A coupon is a code with a life.** It belongs to a campaign, holds the discount the
  engine will apply after the rules have had their say, and states its own **validity
  window**, **usage limit** and **stacking allowance**. The plan states none of those, so
  each coupon states its own and there is no default to inherit.
* **A redemption is history.** It is an append-only row naming the document that used the
  coupon, which is what "redemption is recorded against the document that used it" asks
  for — and why a coupon redeemed to its limit stays refused afterwards even if the
  limit is later raised for the next campaign.

**Stacking, stated once.** The allowance that applies to a document is the **strictest**
(smallest) allowance among the coupons on it, including the one being added. That is
deliberately conservative: two coupons that disagree about how many may stack resolve to
the smaller answer rather than to whichever was redeemed first.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.audit import append_only
from app.db import Base
from app.sales.pricing import DISCOUNT_TYPES, MONEY_SCALE, PricingError
from app.sales.quotations import MONEY


class CouponError(ValueError):
    """A coupon refused what was asked of it."""


class UnknownCouponError(CouponError):
    """No coupon is filed under that code in this company."""


class DuplicateCouponError(CouponError):
    """That coupon code is taken in this company."""


class CouponNotYetValidError(CouponError):
    """The coupon's window has not opened."""


class CouponExpiredError(CouponError):
    """The coupon's window has closed."""


class CouponExhaustedError(CouponError):
    """The coupon has been redeemed as many times as it allows."""


class CouponStackingRefused(CouponError):
    """This document already carries as many coupons as the allowance permits."""


def _decimal(value: Any, what: str) -> Decimal:
    if isinstance(value, float):
        raise CouponError(
            f"{what} is an exact decimal or a string, not the float {value!r}"
        )
    try:
        return Decimal(str(value).strip())
    except Exception as exc:  # noqa: BLE001 — the refusal is the point, not the type
        raise CouponError(f"not {what}: {value!r}") from exc


class Coupon(Base):
    """A code, the campaign it belongs to, and the lifetime it is allowed."""

    __tablename__ = "coupon"
    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_coupon_company_code"),
        CheckConstraint(
            "discount_type IN ('percent', 'amount')", name="ck_coupon_discount_type"
        ),
        CheckConstraint("discount_value >= 0", name="ck_coupon_value_not_negative"),
        CheckConstraint(
            "discount_type <> 'percent' OR discount_value <= 100",
            name="ck_coupon_percent_within_a_hundred",
        ),
        CheckConstraint("stacking_allowance >= 1", name="ck_coupon_allowance"),
        CheckConstraint(
            "max_redemptions IS NULL OR max_redemptions >= 1",
            name="ck_coupon_redemption_limit",
        ),
        CheckConstraint(
            "valid_until IS NULL OR valid_from IS NULL OR valid_until >= valid_from",
            name="ck_coupon_window_the_right_way",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # The campaign a coupon is scoped to. Required: a coupon is a *campaign* code, and
    # a coupon belonging to no campaign could not be reported on by one.
    campaign: Mapped[str] = mapped_column(String(40), nullable=False)
    discount_type: Mapped[str] = mapped_column(String(16), nullable=False)
    discount_value: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    # The window, both ends optional and both inclusive. Null means "always open" —
    # the plan states no validity, so none is invented.
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_until: Mapped[date | None] = mapped_column(Date)
    # Null is *no usage limit*, which is not zero: a coupon nobody capped is not a
    # coupon nobody may use.
    max_redemptions: Mapped[int | None] = mapped_column(Integer)
    # How many coupons may stand on one document. Stated per coupon, because the plan
    # states no allowance; the strictest one on a document governs it.
    stacking_allowance: Mapped[int] = mapped_column(Integer, nullable=False)

    redemptions: Mapped[list["CouponRedemption"]] = relationship(
        back_populates="coupon"
    )


class CouponRedemption(Base):
    """One use of one coupon, against the document that used it — appended, never changed."""

    __tablename__ = "coupon_redemption"
    __table_args__ = (
        # One redemption of one coupon per document: redeeming the same code twice on
        # one document would discount it twice for one decision.
        UniqueConstraint(
            "coupon_id", "document_type", "document_id", name="uq_coupon_once_per_document"
        ),
        CheckConstraint("discount_amount >= 0", name="ck_redemption_not_negative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    coupon_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("coupon.id"), nullable=False, index=True
    )
    # The document that used it — a quotation or a sales order today. Free of a foreign
    # key because the redemption must be recordable against whichever document type the
    # caller is holding, and the pair is meaningful together rather than either half.
    document_type: Mapped[str] = mapped_column(String(32), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    discount_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    redeemed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    coupon: Mapped[Coupon] = relationship(back_populates="redemptions")


# A redemption is history: it cannot be edited or removed (T-0.AUDIT.01).
append_only(CouponRedemption.__table__)


def define_coupon(
    session: Session,
    *,
    company_id: uuid.UUID,
    code: str,
    name: str,
    campaign: str,
    discount_type: str,
    discount_value: Any,
    stacking_allowance: int,
    valid_from: date | None = None,
    valid_until: date | None = None,
    max_redemptions: int | None = None,
) -> Coupon:
    """File one coupon, refusing a code this company already uses.

    Every limit is stated as it is meant: an omitted window is *always open*, an omitted
    usage limit is *no limit*, and `stacking_allowance` is required because there is no
    allowance in the plan to fall back on.
    """
    wanted = str(code or "").strip()
    if not wanted:
        raise CouponError("a coupon code is required")
    kind = str(discount_type or "").strip().lower()
    if kind not in DISCOUNT_TYPES:
        raise PricingError(
            f"{discount_type!r} is not a discount type; state one of"
            f" {', '.join(DISCOUNT_TYPES)}"
        )
    if session.scalar(
        select(Coupon).where(Coupon.company_id == company_id, Coupon.code == wanted)
    ) is not None:
        raise DuplicateCouponError(f"this company already has a coupon {wanted!r}")
    allowance = int(stacking_allowance)
    if allowance < 1:
        raise CouponError(
            f"a stacking allowance of {allowance} would forbid the coupon itself;"
            " state one or more"
        )
    limit = None if max_redemptions is None else int(max_redemptions)
    if limit is not None and limit < 1:
        raise CouponError(
            f"a usage limit of {limit} would forbid the campaign it belongs to;"
            " use null for no limit"
        )
    if valid_from is not None and valid_until is not None and valid_until < valid_from:
        raise CouponError(
            f"coupon {wanted!r} closes on {valid_until} before it opens on {valid_from}"
        )
    coupon = Coupon(
        company_id=company_id,
        code=wanted,
        name=str(name),
        campaign=str(campaign or "").strip(),
        discount_type=kind,
        discount_value=_decimal(discount_value, "a discount value"),
        valid_from=valid_from,
        valid_until=valid_until,
        max_redemptions=limit,
        stacking_allowance=allowance,
    )
    if not coupon.campaign:
        raise CouponError("a coupon belongs to a campaign, and none was stated")
    session.add(coupon)
    session.flush()
    return coupon


def coupon_by_code(session: Session, *, company_id: uuid.UUID, code: str) -> Coupon:
    """The coupon a caller named, or a refusal that names the code it could not find."""
    coupon = session.scalar(
        select(Coupon).where(
            Coupon.company_id == company_id, Coupon.code == str(code).strip()
        )
    )
    if coupon is None:
        raise UnknownCouponError(
            f"no coupon {code!r} in this company; check the code or file it first"
        )
    return coupon


def coupon_discount(coupon: Coupon, base_price: Any) -> Decimal:
    """What one coupon takes off a price, exact at the money scale.

    The same two ways of expressing a discount the pricing engine uses, so a coupon and
    a rule cannot mean different things by "10 %".
    """
    base = _decimal(base_price, "a base price")
    if coupon.discount_type == "percent":
        cut = (base * coupon.discount_value / Decimal(100)).quantize(MONEY_SCALE)
    else:
        cut = coupon.discount_value
    if cut > base:
        raise CouponError(
            f"coupon {coupon.code!r} takes {cut} off a price of {base}, which is more"
            " than the price: an `amount` discount is a reduction, not a refund"
        )
    return cut


def redemptions_for(
    session: Session, *, document_type: str, document_id: uuid.UUID
) -> list[CouponRedemption]:
    """Every coupon already standing on one document, oldest first."""
    return list(
        session.scalars(
            select(CouponRedemption)
            .where(
                CouponRedemption.document_type == str(document_type),
                CouponRedemption.document_id == document_id,
            )
            .order_by(CouponRedemption.redeemed_at, CouponRedemption.id)
        )
    )


def redemption_count(session: Session, coupon: Coupon) -> int:
    """How many times a coupon has been used — what its usage limit is measured against."""
    return len(
        list(
            session.scalars(
                select(CouponRedemption.id).where(
                    CouponRedemption.coupon_id == coupon.id
                )
            )
        )
    )


def redeem_coupon(
    session: Session,
    coupon: Coupon,
    *,
    document_type: str,
    document_id: uuid.UUID,
    base_price: Any,
    on: date | None = None,
) -> CouponRedemption:
    """Apply a coupon to a document, or refuse it with the reason it cannot apply.

    Four things are checked, in the order a person would ask them: is the code real, is
    the window open, has it been used up, and does the document have room for it — and
    then the use is **written down** against the document, because a coupon whose
    redemption was not recorded could be spent forever.
    """
    today = on or datetime.now(timezone.utc).date()
    if coupon.valid_from is not None and today < coupon.valid_from:
        raise CouponNotYetValidError(
            f"coupon {coupon.code!r} opens on {coupon.valid_from}, and {today} is"
            " before it"
        )
    if coupon.valid_until is not None and today > coupon.valid_until:
        raise CouponExpiredError(
            f"coupon {coupon.code!r} closed on {coupon.valid_until}, and {today} is"
            " after it (T-3.SALES.07)"
        )
    used = redemption_count(session, coupon)
    if coupon.max_redemptions is not None and used >= coupon.max_redemptions:
        raise CouponExhaustedError(
            f"coupon {coupon.code!r} allows {coupon.max_redemptions} redemption(s) and"
            f" has used {used} (T-3.SALES.07)"
        )
    standing = redemptions_for(
        session, document_type=document_type, document_id=document_id
    )
    already = [row.coupon for row in standing]
    if any(row.code == coupon.code for row in already):
        raise CouponStackingRefused(
            f"coupon {coupon.code!r} is already on this document"
        )
    # The strictest allowance among the coupons on the document governs how many may
    # stand on it, the new one included.
    allowances = [row.stacking_allowance for row in already] + [coupon.stacking_allowance]
    if len(standing) + 1 > min(allowances):
        raise CouponStackingRefused(
            f"this document already carries {len(standing)} coupon(s) and the strictest"
            f" allowance is {min(allowances)}; {coupon.code!r} cannot be stacked on it"
            " (T-3.SALES.07)"
        )
    amount = coupon_discount(coupon, base_price)
    redemption = CouponRedemption(
        company_id=coupon.company_id,
        coupon_id=coupon.id,
        document_type=str(document_type),
        document_id=document_id,
        discount_amount=amount,
    )
    session.add(redemption)
    session.flush()
    return redemption
