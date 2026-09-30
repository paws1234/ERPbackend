"""Quotations — the document a won opportunity hands over (T-3.SALES.02).

T-3.SALES.02 creates this module with the **header only**, because T-3.SALES.03
depends on it and therefore has to come after: what a won opportunity hands across
is the customer (T-3.SALES.01's row, not a copy of its name), the company and a
document number. Pricing, validity and the conversion into a sales order are
T-3.SALES.03's, and they extend this module rather than replace it.

The header is a *document*, so it is append-only in the sense that matters: it is
never deleted, and the opportunity that produced it is recorded once — the unique
index on `opportunity_id` is what makes "one win, one quotation" a fact of the
schema rather than a promise of the service layer.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    select,
    text,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.db import Base
from app.sales.customers import Customer


class QuotationError(ValueError):
    """The quotation document refused what was asked of it."""


class InvalidQuotationError(QuotationError):
    """The header failed validation at entry."""


class DuplicateQuotationError(QuotationError):
    """That number is already used in this company."""


def _required(value: Any, what: str) -> str:
    text_value = "" if value is None else str(value).strip()
    if not text_value:
        raise InvalidQuotationError(f"{what} is required")
    return text_value


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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    customer: Mapped[Customer] = relationship()


def create_quotation(
    session: Session,
    *,
    company_id: uuid.UUID,
    customer_id: uuid.UUID,
    number: str,
    opportunity_id: uuid.UUID | None = None,
    currency: str | None = None,
    issued_on: date | None = None,
) -> Quotation:
    """Open a quotation header, refusing a number this company already uses."""
    wanted = _required(number, "a quotation number")
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
