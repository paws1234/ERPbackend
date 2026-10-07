"""Sales orders — what an accepted quotation becomes (T-3.SALES.03).

§2.5 asks for "Quotation → Order conversion ... without re-keying", and this module is
that conversion: one accepted quotation becomes one order whose lines are **identical**
to the quotation's, linked back to it.

**The arrow runs one way, and once.** `sales_order.quotation_id` carries a partial
unique index, so "a quotation cannot be converted twice" is a fact of the schema
rather than a promise of this service — the same shape T-3.SALES.02 used to make "one
win, one quotation" hold. Nothing here converts an order back into a quotation, and
nothing edits a quotation to match an order: the document the customer accepted is the
document that was ordered.

**An expired offer is not accepted as it stands.** The conversion refuses a quotation
past its `valid_until` and says what fixes it (`reprice_quotation`), because converting
stale prices would put a price on an order that nobody offered.

**What this module does not do.** Fulfilment (pick, ship, issue) is T-3.SALES.05's.
Nothing here posts to the ledger: an order is not a financial document until it is
fulfilled and invoiced (T-3.AR.01).

**T-3.SALES.04 owns the order's lifecycle**: confirmation, and the credit decision taken
at that moment. The decision is a row of its own (`credit_decision`), append-only like
every other decision in this codebase, because "the decision shows the limit, the
exposure and the order value that produced it" has to keep meaning the same thing after
somebody changes the customer's limit or the company's mode.

**Where the exposure comes from.** T-3.AR.06 owns the *live* exposure across open AR.
Until it exists, the caller **states** the exposure (`exposure=`), and the decision
records the number it was given rather than one this module guessed: a `warn` or `block`
outcome that understated what a customer already owes would look exactly like a pass.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    select,
    text,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.audit import append_only
from app.company import credit_check_mode_of
from app.db import Base
from app.sales.customers import Customer, credit_limit_of
from app.sales.quotations import (
    MONEY,
    MONEY_SCALE,
    Quotation,
    line_amount,
    lines_of,
    refuse_if_expired,
)


class OrderError(ValueError):
    """The sales order refused what was asked of it."""


class DuplicateOrderError(OrderError):
    """That order number is taken, or that quotation already became an order."""


class IncompleteOrderError(OrderError):
    """A quotation with nothing on it is not an order."""


class OrderAlreadyConfirmed(OrderError):
    """An order is confirmed once; confirming it again would restate a decision."""


class CreditError(OrderError):
    """The order-time credit check refused the confirmation (T-3.SALES.04)."""


class NoCreditCheckMode(CreditError):
    """The company has stated no credit-check mode, so there is no policy to apply."""


class CreditLimitExceeded(CreditError):
    """Mode `block`: the order would take the customer over the agreed limit."""


class CreditAcknowledgementRequired(CreditError):
    """Mode `warn`: the breach may proceed, but only once somebody owns it."""


# The two outcomes a recorded decision can have. "Breached" is recorded even in `off`
# mode, where nothing is enforced: the decision is history, not a verdict.
WITHIN_LIMIT = "within_limit"
BREACHED = "breached"
CREDIT_OUTCOMES = (WITHIN_LIMIT, BREACHED)

DRAFT = "draft"
CONFIRMED = "confirmed"
ORDER_STATUSES = (DRAFT, CONFIRMED)


def _required(value: Any, what: str) -> str:
    stated = "" if value is None else str(value).strip()
    if not stated:
        raise IncompleteOrderError(f"{what} is required")
    return stated


class SalesOrder(Base):
    """What one accepted quotation became — the header, and the quotation it came from."""

    __tablename__ = "sales_order"
    __table_args__ = (
        UniqueConstraint("company_id", "number", name="uq_sales_order_company_number"),
        # One quotation, one order. Nullable because an order may later be raised for a
        # customer directly (T-3.SALES.04), which is why the index is partial rather
        # than a plain unique over a column that would then collide on every such row.
        Index(
            "uq_sales_order_quotation",
            "quotation_id",
            unique=True,
            postgresql_where=text("quotation_id IS NOT NULL"),
        ),
        CheckConstraint(
            "status IN ('draft', 'confirmed')", name="ck_sales_order_status"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    quotation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("quotation.id"), index=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customer.id"), nullable=False, index=True
    )
    number: Mapped[str] = mapped_column(String(32), nullable=False)
    # Carried across from the quotation, including its honest null: null means the
    # company's base currency, exactly as the customer master and the quotation state it.
    currency: Mapped[str | None] = mapped_column(String(3))
    ordered_on: Mapped[date] = mapped_column(Date, nullable=False)
    # `draft` until T-3.SALES.04 confirms it. The credit decision is taken once, at
    # confirmation, and recorded against the order (see `CreditDecision`).
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=DRAFT)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    customer: Mapped[Customer] = relationship()
    quotation: Mapped[Quotation | None] = relationship()
    lines: Mapped[list[SalesOrderLine]] = relationship(
        back_populates="order", order_by="SalesOrderLine.line_no"
    )


class SalesOrderLine(Base):
    """One ordered thing — the quotation's line, carried across without re-keying.

    Every field the quotation priced is repeated here rather than read through the
    quotation: an order has to be readable as itself, and T-3.SALES.04 will amend and
    confirm it, which must never rewrite the quotation the customer accepted.
    """

    __tablename__ = "sales_order_line"
    __table_args__ = (
        UniqueConstraint("order_id", "line_no", name="uq_sales_order_line_no"),
        CheckConstraint("line_no >= 1", name="ck_sales_order_line_starts_at_one"),
        CheckConstraint("quantity > 0", name="ck_sales_order_line_quantity"),
        CheckConstraint("unit_price >= 0", name="ck_sales_order_line_price"),
        CheckConstraint(
            "shipped_quantity >= 0", name="ck_sales_order_line_shipped"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sales_order.id"), nullable=False, index=True
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(String(200), nullable=False)
    item_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("item.id"), index=True)
    quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    uom: Mapped[str] = mapped_column(String(16), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    # The rule the price came from, exactly as the quotation recorded it, so the order
    # and the offer agree about why the price is what it is (T-3.SALES.06/07).
    rule_code: Mapped[str | None] = mapped_column(String(80))
    priced_on: Mapped[date] = mapped_column(Date, nullable=False)
    # Fulfilled so far (T-3.SALES.05): incremented by each shipment, so what an order
    # still owes is readable from the line alone — the same shape a purchase order
    # line's `received_quantity` uses for the buying side.
    shipped_quantity: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, default=Decimal(0)
    )

    order: Mapped[SalesOrder] = relationship(back_populates="lines")


class CreditDecision(Base):
    """One order-time credit decision — appended, never changed (T-3.SALES.04).

    Its own row rather than fields on the order, because the decision has to survive
    everything that produced it: `mode`, `limit_amount` and `exposure` are recorded as
    they stood when the order was confirmed, so a later change to the company's mode or
    the customer's limit cannot restate what was decided.
    """

    __tablename__ = "credit_decision"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('off', 'warn', 'block')", name="ck_credit_decision_mode"
        ),
        CheckConstraint(
            "outcome IN ('within_limit', 'breached')",
            name="ck_credit_decision_outcome",
        ),
        CheckConstraint("exposure >= 0", name="ck_credit_decision_exposure"),
        CheckConstraint("order_value >= 0", name="ck_credit_decision_order_value"),
        # An acknowledgement belongs to a breach and nothing else: it is the record of
        # somebody accepting a limit that was already exceeded.
        CheckConstraint(
            "acknowledged_by IS NULL OR outcome = 'breached'",
            name="ck_credit_decision_acknowledgement",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sales_order.id"), nullable=False, index=True
    )
    # The company's mode at the moment of the decision — not a pointer to it, so a
    # later `set_credit_check_mode` cannot rewrite history.
    mode: Mapped[str] = mapped_column(String(8), nullable=False)
    # Null means *no limit has been agreed* for this customer, the same three-state
    # column T-3.SALES.01 established; it is not zero.
    limit_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    exposure: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    order_value: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    # Who accepted the breach in `warn` mode. Null wherever nothing was accepted.
    acknowledged_by: Mapped[str | None] = mapped_column(String(64))
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    order: Mapped[SalesOrder] = relationship()


# A decision is history: it cannot be edited or removed (T-0.AUDIT.01).
append_only(CreditDecision.__table__)


def order_total(order: SalesOrder) -> Decimal:
    """What the order is worth: the sum of its lines, exact **at the money scale**."""
    return sum((line_amount(line) for line in order.lines), Decimal(0)).quantize(
        MONEY_SCALE
    )


def order_for_quotation(session: Session, quotation: Quotation) -> SalesOrder | None:
    """The order a quotation became, or ``None`` — the conversion's own uniqueness test.

    Used by `app.sales.quotations` to refuse re-pricing a converted offer, so it answers
    with ``None`` rather than refusing: both callers want to *ask*.
    """
    return session.scalar(
        select(SalesOrder).where(SalesOrder.quotation_id == quotation.id)
    )


def order_by_number(session: Session, *, company_id: uuid.UUID, number: str) -> SalesOrder:
    """The order a caller named, or a refusal."""
    order = session.scalar(
        select(SalesOrder).where(
            SalesOrder.company_id == company_id, SalesOrder.number == str(number)
        )
    )
    if order is None:
        raise OrderError(f"no sales order {number!r} in this company (T-3.SALES.03)")
    return order


def convert_quotation_to_order(
    session: Session,
    quotation: Quotation,
    *,
    number: str,
    on: date | None = None,
) -> SalesOrder:
    """Turn an accepted quotation into an order — once, with identical lines.

    Refused when the quotation already became an order (the schema holds this too,
    through `uq_sales_order_quotation`), when it is past its validity window (the price
    is stale, so re-price it first), when it carries nothing, or when the order number
    is taken in this company.
    """
    wanted = _required(number, "an order number")
    today = on or datetime.now(timezone.utc).date()

    existing = order_for_quotation(session, quotation)
    if existing is not None:
        raise DuplicateOrderError(
            f"quotation {quotation.number!r} already became order {existing.number!r};"
            " a quotation is converted once (T-3.SALES.03)"
        )
    refuse_if_expired(quotation, on=today)
    lines = lines_of(session, quotation)
    if not lines:
        raise IncompleteOrderError(
            f"quotation {quotation.number!r} has no lines, so there is nothing to order"
            " (T-3.SALES.03)"
        )
    clash = session.scalar(
        select(SalesOrder).where(
            SalesOrder.company_id == quotation.company_id, SalesOrder.number == wanted
        )
    )
    if clash is not None:
        raise DuplicateOrderError(
            f"sales order {wanted!r} already exists in this company"
        )

    order = SalesOrder(
        company_id=quotation.company_id,
        quotation_id=quotation.id,
        customer_id=quotation.customer_id,
        number=wanted,
        currency=quotation.currency,
        ordered_on=today,
    )
    session.add(order)
    session.flush()
    for line in lines:
        session.add(
            SalesOrderLine(
                company_id=quotation.company_id,
                order_id=order.id,
                line_no=line.line_no,
                description=line.description,
                item_id=line.item_id,
                quantity=line.quantity,
                uom=line.uom,
                unit_price=line.unit_price,
                rule_code=line.rule_code,
                priced_on=line.priced_on,
            )
        )
    session.flush()
    return order


def _exposure_value(exposure: Any) -> Decimal:
    """The caller's stated exposure as an exact, non-negative decimal.

    Until T-3.AR.06 computes the live exposure across open AR, the caller states it —
    the path T-3.SALES.04's own recorded stop chose. A value that is not an amount is
    refused here rather than compared: a float, an infinity or a negative would make
    the breach test mean something nobody agreed to.
    """
    if isinstance(exposure, float):
        raise CreditError(
            f"an exposure is an exact decimal or a string, not the float {exposure!r}"
        )
    try:
        value = (
            exposure if isinstance(exposure, Decimal) else Decimal(str(exposure).strip())
        )
    except (InvalidOperation, AttributeError, TypeError) as exc:
        raise CreditError(
            f"not an exposure: {exposure!r}; state the customer's current exposure as"
            " an exact decimal"
        ) from exc
    if not value.is_finite():
        raise CreditError(f"an exposure must be a finite amount, not {value}")
    if value < 0:
        raise CreditError(
            f"an exposure is not negative: {value}; a credit the customer holds is a"
            " reduction in the exposure, not a negative one"
        )
    return value


def credit_decision_for(session: Session, order: SalesOrder) -> CreditDecision | None:
    """The decision taken when this order was confirmed, or ``None`` while it is a draft."""
    return session.scalar(
        select(CreditDecision)
        .where(CreditDecision.order_id == order.id)
        .order_by(CreditDecision.decided_at.desc())
    )


def confirm_order(
    session: Session,
    order: SalesOrder,
    *,
    exposure: Any,
    actor: str,
    acknowledge_breach: bool = False,
    on: datetime | None = None,
) -> CreditDecision:
    """Confirm a sales order, deciding the customer's credit as the order is placed.

    The mode decides what a breach means: `block` refuses the confirmation, `warn`
    lets it through only once somebody has **acknowledged** the breach, and `off`
    records the decision without acting on it. A company that has stated **no** mode
    is refused rather than defaulted — plan §8 leaves the mode undecided, so there is
    no honest policy to apply.

    The decision is written down either way, with the limit, the exposure and the order
    value that produced it, so it can be shown afterwards and cannot be restated by a
    later change to the mode or the limit.
    """
    if order.status == CONFIRMED:
        raise OrderAlreadyConfirmed(
            f"sales order {order.number!r} is already confirmed (T-3.SALES.04)"
        )
    mode = credit_check_mode_of(session, company_id=order.company_id)
    if mode is None:
        raise NoCreditCheckMode(
            f"no credit-check mode is stated for this company, so sales order"
            f" {order.number!r} cannot be confirmed: state one first (T-3.SALES.04)"
        )
    who = _required(actor, "the actor confirming the order")
    limit = credit_limit_of(order.customer)
    value = order_total(order)
    stated = _exposure_value(exposure)

    # The order is not on the account yet, so the breach is judged on what confirming
    # it would leave the customer owing, measured against a limit that was actually
    # agreed (null means none was).
    after = stated + value
    breached = limit is not None and after > limit

    acknowledged = None
    if breached:
        if mode == "block":
            raise CreditLimitExceeded(
                f"confirming {order.number!r} would take the customer to {after}, over the"
                f" agreed limit {limit}: refused while the mode is 'block' (T-3.SALES.04)"
            )
        if mode == "warn":
            if not acknowledge_breach:
                raise CreditAcknowledgementRequired(
                    f"confirming {order.number!r} would take the customer to {after}, over"
                    f" the agreed limit {limit}: the mode is 'warn', so the breach is"
                    " acknowledged by the confirmation (T-3.SALES.04)"
                )
            acknowledged = who

    now = on or datetime.now(timezone.utc)
    decision = CreditDecision(
        company_id=order.company_id,
        order_id=order.id,
        mode=mode,
        limit_amount=limit,
        exposure=stated,
        order_value=value,
        outcome=BREACHED if breached else WITHIN_LIMIT,
        acknowledged_by=acknowledged,
        decided_at=now,
    )
    session.add(decision)
    order.status = CONFIRMED
    order.confirmed_at = now
    order.confirmed_by = who
    session.flush()
    return decision
