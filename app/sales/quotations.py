"""Quotations — the document a won opportunity hands over (T-3.SALES.02), and the
priced, valid offer it becomes (T-3.SALES.03).

T-3.SALES.02 created this module with the **header only**, because T-3.SALES.03
depends on it and therefore has to come after: what a won opportunity hands across
is the customer (T-3.SALES.01's row, not a copy of its name), the company and a
document number. T-3.SALES.03 adds the other half — the **priced lines**, the
**validity window**, and the **re-pricing** that an expired offer needs before it can
be converted (`app/sales/orders.py`).

**What a line does and does not record.** A line carries the price it was quoted at
and the date that price was fixed (`priced_on`), and it can carry the `rule_code` of
the pricing rule that produced it. Resolving *which* rule applies — tiers, volume,
campaigns — is the pricing engine's (T-3.SALES.06/07), and that task states that "a
quote records the rule that applied so it can be reproduced later". So this module
provides the place the rule is recorded and the price is fixed; the engine fills both.
A price entered by hand names no rule, which is an honest null rather than an invented
code.

ponytail: prices are **stated**, not resolved, because the engine arrives in
T-3.SALES.06/07 and inventing one here would be a second pricing implementation for
that task to replace. Ceiling: nothing yet computes a price from a rule. Upgrade path:
the engine calls `add_line`/`reprice_quotation` with the price it resolved and the
`rule_code` it used, so neither function changes when it lands.

**Re-pricing is all-or-nothing.** A quotation is priced once at quote time, and
re-priced as a whole via `reprice_quotation`: every line must be restated and the new
validity window must be stated with it (the plan names no default window, so none is
invented). A partial re-price would leave some prices stale while the document claimed
to be current — which is the failure the expiry rule exists to prevent.

The header is a *document*, so it is append-only in the sense that matters: it is
never deleted, and the opportunity that produced it is recorded once — the unique
index on `opportunity_id` is what makes "one win, one quotation" a fact of the
schema rather than a promise of the service layer.
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
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    select,
    text,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.db import Base
from app.sales.customers import Customer

# Exact decimals, like every amount in the platform (DOMAIN-MODELS.md §2).
MONEY = Numeric(20, 6)

# The scale the column stores at — what a *product* has to come back to: quantity and
# unit price are each 6dp, so multiplying them yields 12.
MONEY_SCALE = Decimal("0.000001")


class QuotationError(ValueError):
    """The quotation document refused what was asked of it."""


class InvalidQuotationError(QuotationError):
    """The header or a line failed validation at entry."""


class DuplicateQuotationError(QuotationError):
    """That number is already used in this company."""


class DuplicateLineError(QuotationError):
    """That line number is already used on this quotation."""


class ExpiredQuotationError(QuotationError):
    """A quotation past its validity window cannot be converted as it stands."""


class ConvertedQuotationError(QuotationError):
    """A quotation that became an order is no longer an offer — it cannot be re-priced."""


def _required(value: Any, what: str) -> str:
    text_value = "" if value is None else str(value).strip()
    if not text_value:
        raise InvalidQuotationError(f"{what} is required")
    return text_value


def _amount(value: Any, what: str) -> Decimal:
    """Exact decimal from whatever the caller passed — never through float."""
    if value is None or (isinstance(value, str) and not value.strip()):
        raise InvalidQuotationError(f"{what} is required")
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 — the refusal is the point, not the type
        raise InvalidQuotationError(f"{what} is not a decimal amount: {value!r}") from exc
    if not amount.is_finite():
        raise InvalidQuotationError(f"{what} is not a finite amount: {value!r}")
    return amount


class Quotation(Base):
    """The header of one quotation — who it is for, and how it is identified."""

    __tablename__ = "quotation"
    __table_args__ = (
        UniqueConstraint("company_id", "number", name="uq_quotation_company_number"),
        # One win, one quotation: a second quotation for the same opportunity cannot
        # be written even by a caller that skips the service-layer refusal.
        Index(
            "uq_quotation_opportunity",
            "opportunity_id",
            unique=True,
            postgresql_where=text("opportunity_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customer.id"), nullable=False, index=True
    )
    # What a person says out loud and what a document is filed under; unique per
    # company, and supplied by the caller rather than generated, as Phase 2's
    # requisitions and invoices are.
    number: Mapped[str] = mapped_column(String(32), nullable=False)
    # The currency the quotation is priced in. Null means the company's base
    # currency, the same honest-null convention the customer master uses.
    currency: Mapped[str | None] = mapped_column(String(3))
    # The opportunity that produced it, where there was one. A quotation raised
    # directly for a customer leaves this null.
    opportunity_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("opportunity.id"))
    issued_on: Mapped[date] = mapped_column(Date, nullable=False)
    # The last day the offer holds. Null means no window was stated — the plan names
    # no default, so none is invented; an unstated window never expires.
    valid_until: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    customer: Mapped[Customer] = relationship()
    lines: Mapped[list[QuotationLine]] = relationship(
        back_populates="quotation", order_by="QuotationLine.line_no"
    )


class QuotationLine(Base):
    """One priced thing on a quotation.

    Modelled on Phase 2's requisition line, with the two fields a *quotation* needs
    that a request does not: `rule_code`, the pricing rule that produced the price
    (null when a person stated it), and `priced_on`, the day the price was fixed —
    which is what makes "these prices are stale" a fact rather than an opinion.
    """

    __tablename__ = "quotation_line"
    __table_args__ = (
        UniqueConstraint("quotation_id", "line_no", name="uq_quotation_line_no"),
        CheckConstraint("line_no >= 1", name="ck_quotation_line_starts_at_one"),
        CheckConstraint("quantity > 0", name="ck_quotation_line_quantity"),
        CheckConstraint("unit_price >= 0", name="ck_quotation_line_price"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    quotation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("quotation.id"), nullable=False, index=True
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(String(200), nullable=False)
    # The stock item, where the line is for one. A service has no item, so this stays
    # null rather than forcing a placeholder into the master (the Phase 2 convention).
    item_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("item.id"), index=True)
    quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    uom: Mapped[str] = mapped_column(String(16), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    # Which pricing rule produced `unit_price`, where a rule did (T-3.SALES.06/07 fill
    # it). Null is a price a person stated, which is an honest null, not a missing one.
    rule_code: Mapped[str | None] = mapped_column(String(80))
    priced_on: Mapped[date] = mapped_column(Date, nullable=False)

    quotation: Mapped[Quotation] = relationship(back_populates="lines")


def line_amount(line: QuotationLine) -> Decimal:
    """What one line is worth: quantity × unit price, exact **at the money scale**.

    Both operands carry six decimals, so a raw product carries twelve. An amount that
    left this function at twelve would be a different scale from every other amount in
    the platform, and the difference would surface the first time one was posted.
    """
    return (line.quantity * line.unit_price).quantize(MONEY_SCALE)


def create_quotation(
    session: Session,
    *,
    company_id: uuid.UUID,
    customer_id: uuid.UUID,
    number: str,
    opportunity_id: uuid.UUID | None = None,
    currency: str | None = None,
    issued_on: date | None = None,
    valid_until: date | None = None,
) -> Quotation:
    """Open a quotation header, refusing a number this company already uses.

    The references have to form one *tenant-owned* chain: a quotation owned by one
    company but pointing at another company's customer would be visible while the row
    it names is not, which is exactly the hole the company dimension exists to close.

    ponytail: this is checked in the service, not by the schema. Ceiling: a writer
    that bypasses this function could still store a mismatched pair. Upgrade path: a
    composite foreign key on `(customer_id, company_id)` (and the same for the
    opportunity), which needs a unique index on each parent — worth doing when a
    second writer appears; today this function is the only way a quotation is made.
    """
    wanted = _required(number, "a quotation number")
    customer = session.scalar(
        select(Customer).where(
            Customer.id == customer_id, Customer.company_id == company_id
        )
    )
    if customer is None:
        raise InvalidQuotationError(
            f"customer {customer_id} is not this company's; a quotation is filed under"
            " one company's customer (T-3.SALES.02)"
        )
    if opportunity_id is not None:
        # Imported here rather than at module level: `app.sales.pipeline` imports
        # this module to convert a won opportunity, so that edge can only run one
        # way at import time.
        from app.sales.pipeline import Opportunity

        opportunity = session.scalar(
            select(Opportunity).where(
                Opportunity.id == opportunity_id,
                Opportunity.company_id == company_id,
                Opportunity.customer_id == customer_id,
            )
        )
        if opportunity is None:
            raise InvalidQuotationError(
                f"opportunity {opportunity_id} is not this company's, or belongs to"
                " another customer; one win, one quotation (T-3.SALES.02)"
            )
    already = session.scalar(
        select(Quotation).where(
            Quotation.company_id == company_id, Quotation.number == wanted
        )
    )
    if already is not None:
        raise DuplicateQuotationError(
            f"quotation {wanted!r} already exists in this company"
        )
    quotation = Quotation(
        company_id=company_id,
        customer_id=customer_id,
        number=wanted,
        currency=None if currency is None else str(currency),
        opportunity_id=opportunity_id,
        issued_on=issued_on or datetime.now(timezone.utc).date(),
        valid_until=valid_until,
    )
    session.add(quotation)
    session.flush()
    return quotation


def quotation_by_number(
    session: Session, *, company_id: uuid.UUID, number: str
) -> Quotation:
    """The quotation a caller named, or a refusal."""
    quotation = session.scalar(
        select(Quotation).where(
            Quotation.company_id == company_id, Quotation.number == str(number)
        )
    )
    if quotation is None:
        raise InvalidQuotationError(
            f"no quotation {number!r} in this company (T-3.SALES.03)"
        )
    return quotation


def lines_of(session: Session, quotation: Quotation) -> list[QuotationLine]:
    """A quotation's lines, in their own order — the order a document reads in."""
    return list(
        session.scalars(
            select(QuotationLine)
            .where(QuotationLine.quotation_id == quotation.id)
            .order_by(QuotationLine.line_no)
        )
    )


def add_line(
    session: Session,
    quotation: Quotation,
    *,
    line_no: int,
    description: str,
    quantity: Any,
    unit_price: Any,
    uom: str = "unit",
    item_id: uuid.UUID | None = None,
    rule_code: str | None = None,
    priced_on: date | None = None,
) -> QuotationLine:
    """Price one line onto a quotation, recording the day the price was fixed.

    Prices are stated here rather than resolved: *which* rule applies is the pricing
    engine's (T-3.SALES.06/07), and what this records is the outcome — the price, and
    the rule's name when there was one. A converted quotation cannot be re-priced, so
    a line cannot be added to one either: the document the customer holds is the
    document that was ordered.

    ponytail: the conversion check is one indexed query per line. Ceiling: a quotation
    with very many lines written in one request. Upgrade path: check once in
    `create_quotation`'s caller, where "brand new, so unconvertible" is already known —
    or compute it from a single `order_for_quotation` lookup passed down.
    """
    _refuse_if_converted(session, quotation)
    wanted = int(line_no)
    if wanted < 1:
        raise InvalidQuotationError(
            f"a line number starts at 1 on quotation {quotation.number!r}; got {wanted}"
        )
    clash = session.scalar(
        select(QuotationLine).where(
            QuotationLine.quotation_id == quotation.id, QuotationLine.line_no == wanted
        )
    )
    if clash is not None:
        raise DuplicateLineError(
            f"quotation {quotation.number!r} already has a line {wanted}"
        )
    amount = _amount(quantity, "a line quantity")
    if amount <= 0:
        raise InvalidQuotationError(
            f"line {wanted} asks for {amount}; a quantity is greater than zero"
        )
    price = _amount(unit_price, f"line {wanted}'s unit price")
    if price < 0:
        raise InvalidQuotationError(
            f"line {wanted} is priced at {price}; a unit price is not negative"
        )
    line = QuotationLine(
        company_id=quotation.company_id,
        quotation_id=quotation.id,
        line_no=wanted,
        description=_required(description, f"line {wanted}'s description"),
        item_id=item_id,
        quantity=amount,
        uom=_required(uom, f"line {wanted}'s unit of measure"),
        unit_price=price,
        rule_code=None if rule_code is None else str(rule_code).strip() or None,
        priced_on=priced_on or datetime.now(timezone.utc).date(),
    )
    session.add(line)
    session.flush()
    return line


def _refuse_if_converted(session: Session, quotation: Quotation) -> None:
    """Refuse to change a quotation that already became an order.

    Imported inside the function rather than at module level because
    `app.sales.orders` imports this module to convert a quotation, so that edge can
    only run one way at import time (the same shape `create_quotation` already uses
    for the opportunity).
    """
    from app.sales.orders import order_for_quotation

    if order_for_quotation(session, quotation) is not None:
        raise ConvertedQuotationError(
            f"quotation {quotation.number!r} was converted to an order, so it is no"
            " longer an offer; raise a new quotation instead of re-pricing this one"
            " (T-3.SALES.03)"
        )


def reprice_quotation(
    session: Session,
    quotation: Quotation,
    *,
    prices: dict[int, Any],
    valid_until: date,
    rules: dict[int, str | None] | None = None,
    on: date | None = None,
) -> Quotation:
    """Re-price a quotation as a whole, with its new validity window.

    This is what an expired quotation needs before it can be converted: **every** line
    must be restated — a partial re-price would leave some prices stale while the
    document claimed to be current — and the window must be stated with it, because
    the plan names no default and none is invented here.

    The rule a line was priced by is restated alongside its price, so a re-priced line
    cannot silently keep the rule that produced its *old* price.
    """
    _refuse_if_converted(session, quotation)
    stated = {int(line_no): line_no for line_no in prices}
    lines = lines_of(session, quotation)
    if not lines:
        raise InvalidQuotationError(
            f"quotation {quotation.number!r} has no lines to re-price"
        )
    missing = [line.line_no for line in lines if line.line_no not in stated]
    unknown = sorted(line_no for line_no in stated if line_no not in {l.line_no for l in lines})
    if missing:
        raise InvalidQuotationError(
            f"a re-price restates every line; {quotation.number!r} line(s)"
            f" {missing} were not stated"
        )
    if unknown:
        raise InvalidQuotationError(
            f"quotation {quotation.number!r} has no line(s) {unknown}"
        )
    today = on or datetime.now(timezone.utc).date()
    for line in lines:
        price = _amount(prices[line.line_no], f"line {line.line_no}'s unit price")
        if price < 0:
            raise InvalidQuotationError(
                f"line {line.line_no} is re-priced at {price}; a unit price is not negative"
            )
        line.unit_price = price
        line.priced_on = today
        if rules is not None and line.line_no in rules:
            line.rule_code = None if rules[line.line_no] is None else str(rules[line.line_no])
    if valid_until <= today:
        raise InvalidQuotationError(
            f"a re-priced quotation needs a window that has not closed; {valid_until} is"
            f" not after {today}"
        )
    quotation.valid_until = valid_until
    session.flush()
    return quotation


def expired(quotation: Quotation, *, on: date) -> bool:
    """Whether the offer has closed: a stated window is the only thing that expires."""
    return quotation.valid_until is not None and on > quotation.valid_until


def refuse_if_expired(quotation: Quotation, *, on: date) -> None:
    """Refuse a conversion of an expired quotation, naming what fixes it."""
    if expired(quotation, on=on):
        raise ExpiredQuotationError(
            f"quotation {quotation.number!r} expired on {quotation.valid_until} and is"
            f" being converted on {on}; re-price it with a new window before"
            " converting (T-3.SALES.03)"
        )
