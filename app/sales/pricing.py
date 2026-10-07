"""T-3.SALES.06 — the pricing engine: ordered rules over customer tier and volume.

§2.5 asks for "Pricing Rules & Discounts (customer tiers, volume…)". T-3.SALES.03 could
already put a price on a line and record the rule that produced it; **which** rule
applies is this module's, and T-3.SALES.07 adds the campaign and coupon dimensions
beside these two.

**A rule is a scope plus a discount.** Its scope is the dimensions the plan names —
the item, the customer's tier, and the quantity band — and each dimension may be left
open, so "5 % off everything" and "10 % off this item, in this tier, from 100 up" are
the same kind of row. `discount_type` is `percent` or `amount`, the two the ledger
names, and **each rule states its own**: the ledger records no default, so there is
none to inherit.

**Where the price comes from.** The plan names no price list and the item master has no
list price, so the engine does not invent one: the **caller states the base price** and
a rule discounts it. That keeps the engine honest about what it knows — it decides
*which rule applies and what it does*, never *what the goods list at*.

**The resolution order is fixed, documented and recorded.** Overlapping rules resolve
by, in order:

1. `priority` ascending — the operator's own ordering, and the only field a person sets
   to say "this rule beats that one";
2. the more specific rule first — an item-scoped rule beats an any-item rule, and a
   tier-scoped rule beats an any-tier rule;
3. the higher `min_quantity` first — the tighter volume band beats the wider one;
4. `code` ascending — so a tie is broken by the document, never by row order.

The winner's `code` **and** the `priority` that decided it are written onto the priced
line, which is what "the resolution order is stated on the priced line" asks for: a
line read a year later still says which rule won and where it sat.

**One implementation, used by both documents.** A quotation is priced through
:func:`price_quote_line`; an order carries the quotation's lines *without re-keying*
(T-3.SALES.03), so the price and the rule on an order are the engine's own answer rather
than a second calculation that could disagree with it.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db import Base
from app.sales.customers import Customer
from app.sales.quotations import (
    MONEY,
    MONEY_SCALE,
    InvalidQuotationError,
    add_line,
)

# The two ways a discount is expressed, as the ledger's `discount_type` names them.
DISCOUNT_TYPES = ("percent", "amount")


class PricingError(ValueError):
    """The pricing engine refused what was asked of it."""


class UnknownDiscountType(PricingError):
    """A discount type that is not one of the two the ledger names."""


class UnknownRuleError(PricingError):
    """No rule is filed under that code in this company."""


class DuplicateRuleError(PricingError):
    """That rule code is taken in this company."""


def _decimal(value: Any, what: str) -> Decimal:
    if isinstance(value, float):
        raise PricingError(
            f"{what} is an exact decimal or a string, not the float {value!r}"
        )
    try:
        return Decimal(str(value).strip())
    except Exception as exc:  # noqa: BLE001 — the refusal is the point, not the type
        raise PricingError(f"not {what}: {value!r}") from exc


class PriceRule(Base):
    """One discount and the scope it applies to.

    A null dimension is **no constraint**, not a wildcard value: a rule with `tier`
    null applies whatever the customer's tier, while a rule naming a tier does not
    apply to a customer that sits in none. That reading is what stops a rule written
    for a tier from silently pricing everybody.
    """

    __tablename__ = "price_rule"
    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_price_rule_company_code"),
        CheckConstraint(
            "discount_type IN ('percent', 'amount')", name="ck_price_rule_discount_type"
        ),
        CheckConstraint("discount_value >= 0", name="ck_price_rule_value_not_negative"),
        CheckConstraint(
            "discount_type <> 'percent' OR discount_value <= 100",
            name="ck_price_rule_percent_within_a_hundred",
        ),
        CheckConstraint("min_quantity > 0", name="ck_price_rule_min_quantity"),
        CheckConstraint(
            "max_quantity IS NULL OR max_quantity >= min_quantity",
            name="ck_price_rule_band_downside_up",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # Null: the rule applies to every item. Otherwise it applies to this one only.
    item_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("item.id"), index=True)
    # Null: every tier. Otherwise only a customer whose tier matches by name.
    tier: Mapped[str | None] = mapped_column(String(32))
    # Null: every campaign. Otherwise only a line priced under that campaign — the
    # fourth dimension the ledger names, added by T-3.SALES.07 beside tier and volume.
    campaign: Mapped[str | None] = mapped_column(String(40))
    # The volume band. `min_quantity` defaults to 1 — every quantity is at least one —
    # and a null ceiling is an open band rather than an invented one.
    min_quantity: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, default=Decimal(1)
    )
    max_quantity: Mapped[Decimal | None] = mapped_column(MONEY)
    # The operator's ordering. Lower wins; it is the only field a person sets to say
    # "this rule beats that one", and it is recorded on the line that used it.
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    discount_type: Mapped[str] = mapped_column(String(16), nullable=False)
    discount_value: Mapped[Decimal] = mapped_column(MONEY, nullable=False)


def define_rule(
    session: Session,
    *,
    company_id: uuid.UUID,
    code: str,
    name: str,
    discount_type: str,
    discount_value: Any,
    item_id: uuid.UUID | None = None,
    tier: str | None = None,
    campaign: str | None = None,
    min_quantity: Any = 1,
    max_quantity: Any | None = None,
    priority: int = 100,
) -> PriceRule:
    """File one pricing rule, refusing a code this company already uses.

    Every dimension is stated as it is meant: an omitted tier is *any* tier, an omitted
    ceiling is an *open* band, and both are stored as null rather than as a sentinel
    value that would later have to be explained.
    """
    wanted = str(code or "").strip()
    if not wanted:
        raise PricingError("a rule code is required")
    kind = str(discount_type or "").strip().lower()
    if kind not in DISCOUNT_TYPES:
        raise UnknownDiscountType(
            f"{discount_type!r} is not a discount type; state one of"
            f" {', '.join(DISCOUNT_TYPES)}"
        )
    if session.scalar(
        select(PriceRule).where(
            PriceRule.company_id == company_id, PriceRule.code == wanted
        )
    ) is not None:
        raise DuplicateRuleError(f"this company already has a rule {wanted!r}")
    floor = _decimal(min_quantity, "a minimum quantity")
    if floor <= 0:
        raise PricingError(f"a volume band starts at one or more, not {floor}")
    ceiling = None if max_quantity is None else _decimal(max_quantity, "a maximum quantity")
    if ceiling is not None and ceiling < floor:
        raise PricingError(
            f"a band from {floor} to {ceiling} is upside down"
        )
    rule = PriceRule(
        company_id=company_id,
        code=wanted,
        name=str(name),
        item_id=item_id,
        tier=None if tier is None else str(tier).strip() or None,
        campaign=None if campaign is None else str(campaign).strip() or None,
        min_quantity=floor,
        max_quantity=ceiling,
        priority=int(priority),
        discount_type=kind,
        discount_value=_decimal(discount_value, "a discount value"),
    )
    session.add(rule)
    session.flush()
    return rule


def rule_by_code(session: Session, *, company_id: uuid.UUID, code: str) -> PriceRule:
    """The rule a caller named, or a refusal naming it."""
    rule = session.scalar(
        select(PriceRule).where(
            PriceRule.company_id == company_id, PriceRule.code == str(code)
        )
    )
    if rule is None:
        raise UnknownRuleError(f"no pricing rule {code!r} in this company")
    return rule


class PriceDecision:
    """What the engine decided, and the whole ordering it decided in.

    `considered` is every rule that matched, **in resolution order** — so the winner is
    simply the first of them, and a caller that wants to explain a price can say which
    rules lost and why, not only which one won.
    """

    __slots__ = ("base_price", "considered", "price", "rule")

    def __init__(
        self,
        *,
        base_price: Decimal,
        price: Decimal,
        rule: PriceRule | None,
        considered: list[PriceRule],
    ) -> None:
        self.base_price = base_price
        self.price = price
        self.rule = rule
        self.considered = considered

    @property
    def rule_code(self) -> str | None:
        """The winning rule's code, or nothing when no rule applied."""
        return None if self.rule is None else self.rule.code

    @property
    def rule_priority(self) -> int | None:
        """Where the winning rule sat in the order — the number that decided it."""
        return None if self.rule is None else self.rule.priority


def _matches(
    rule: PriceRule,
    *,
    item_id,
    tier: str | None,
    quantity: Decimal,
    campaign: str | None = None,
) -> bool:
    """Whether one rule's scope covers what is being priced."""
    if rule.item_id is not None and rule.item_id != item_id:
        return False
    if rule.tier is not None and rule.tier != tier:
        return False
    if rule.campaign is not None and rule.campaign != campaign:
        return False
    if quantity < rule.min_quantity:
        return False
    return rule.max_quantity is None or quantity <= rule.max_quantity


def _order_key(rule: PriceRule) -> tuple:
    """The documented resolution order, as a sort key — one definition, used everywhere.

    Priority first (the operator's own ordering), then specificity (item before
    any-item, tier before any-tier), then the tighter band, then the code, so that two
    rules that agree on everything else are still ordered by something a person can read
    rather than by whichever row the database happened to return first.
    """
    return (
        rule.priority,
        0 if rule.item_id is not None else 1,
        0 if rule.tier is not None else 1,
        0 if rule.campaign is not None else 1,
        -rule.min_quantity,
        rule.code,
    )


def price_for(rule: PriceRule, base_price: Decimal) -> Decimal:
    """What one rule makes of a base price, exact at the money scale."""
    if rule.discount_type == "percent":
        cut = (base_price * rule.discount_value / Decimal(100)).quantize(MONEY_SCALE)
    else:
        cut = rule.discount_value
    price = (base_price - cut).quantize(MONEY_SCALE)
    if price < 0:
        # An amount discount larger than the price would otherwise invent money the
        # customer is owed on a line they are buying.
        raise PricingError(
            f"rule {rule.code!r} takes {cut} off a price of {base_price}, which is less"
            " than nothing: an `amount` discount is a reduction, not a credit"
        )
    return price


def resolve_price(
    session: Session,
    *,
    company_id: uuid.UUID,
    base_price: Any,
    quantity: Any = 1,
    item_id: uuid.UUID | None = None,
    tier: str | None = None,
    campaign: str | None = None,
) -> PriceDecision:
    """The price one line comes to, and the ordering that produced it.

    `base_price` is **stated by the caller**: the plan names no price list and the item
    master holds none, so inventing a list price here would be inventing the number the
    whole discount is measured against.
    """
    base = _decimal(base_price, "a base price")
    if base < 0:
        raise PricingError(f"a base price is not negative: {base}")
    wanted = _decimal(quantity, "a quantity")
    matched = [
        rule
        for rule in session.scalars(
            select(PriceRule).where(PriceRule.company_id == company_id)
        )
        if _matches(
            rule, item_id=item_id, tier=tier, quantity=wanted, campaign=campaign
        )
    ]
    matched.sort(key=_order_key)
    if not matched:
        return PriceDecision(
            base_price=base, price=base.quantize(MONEY_SCALE), rule=None, considered=[]
        )
    winner = matched[0]
    return PriceDecision(
        base_price=base,
        price=price_for(winner, base),
        rule=winner,
        considered=matched,
    )


def tier_of(customer: Customer) -> str | None:
    """The tier a customer sits in, or ``None`` — the dimension the rules key on."""
    return customer.tier


def price_quote_line(
    session: Session,
    quotation,
    *,
    line_no: int,
    description: str,
    quantity: Any,
    base_price: Any,
    item_id: uuid.UUID | None = None,
    uom: str = "unit",
    campaign: str | None = None,
    on: date | None = None,
):
    """Price one line of a quotation through the engine and write down what decided it.

    This is the **one** place a document is priced: the price, the winning rule's code
    and its priority all land on the line together, so the offer can be reproduced later
    even after the rules behind it have changed. An order carries these same lines
    across without re-keying, which is why the two documents cannot disagree.
    """
    decision = resolve_price(
        session,
        company_id=quotation.company_id,
        base_price=base_price,
        quantity=quantity,
        item_id=item_id,
        tier=tier_of(quotation.customer),
        campaign=campaign,
    )
    if decision.price <= 0:
        raise InvalidQuotationError(
            f"line {line_no} prices at {decision.price} after rule"
            f" {decision.rule_code!r}; a free or negative price is not an offer"
        )
    return add_line(
        session,
        quotation,
        line_no=line_no,
        description=description,
        quantity=quantity,
        unit_price=decision.price,
        uom=uom,
        item_id=item_id,
        rule_code=decision.rule_code,
        rule_priority=decision.rule_priority,
        priced_on=on or datetime.now(timezone.utc).date(),
    )
