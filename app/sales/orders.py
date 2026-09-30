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

**What this module does not do.** Confirmation, amendment, closing and the credit
decision at order time are T-3.SALES.04's — it owns the order's *lifecycle*, and it
depends on this task, so the document has to exist first. Fulfilment (pick, ship, issue)
is T-3.SALES.05's. Nothing here posts to the ledger: an order is not a financial
document until it is fulfilled and invoiced (T-3.AR.01).
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
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    select,
    text,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.db import Base
from app.sales.customers import Customer
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

    order: Mapped[SalesOrder] = relationship(back_populates="lines")


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
