"""T-2.PROC.03 — issuing an RFQ and capturing what suppliers answer.

An approved requisition says *what* is needed; an RFQ asks several suppliers what
they would charge for it. This module owns both halves — the question and the
answers — and three decisions shape it:

* **An RFQ asks for the requisition's own lines.** The lines are copied from an
  approved requisition (T-2.PROC.02) when the RFQ is issued, and each RFQ line keeps
  the requisition line it stands for. So "which requisition lines does this response
  cover" is answerable exactly, and an RFQ cannot quietly ask for something nobody
  approved. Issuing is refused for a requisition that has not finished approving.
* **A late answer is recorded, not silently accepted or silently dropped.** A
  response that arrives after the deadline is stored with `late = true`, because the
  decision to still use it is a buyer's decision — the record has to be able to say
  that it was late. Silence about it would make a late quote look like a punctual one.
* **One response per invited supplier.** A second one for the same supplier on the
  same RFQ is refused rather than appended, so "what did ACME quote" has one answer;
  a supplier that quotes nothing for a line is *distinguishable* from a supplier that
  did not answer at all — the first is a line the response does not carry, the second
  is no response row ([`non_responders`][app.procurement.rfq.non_responders]).

What is deliberately not here: comparing the answers (T-2.PROC.04), choosing a winner
(T-2.PROC.05) and the supplier-facing portal (T-6.PORTAL.01). This module only asks
and records.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.db import Base
from app.ledger.currency import currency_by_code
from app.procurement.requisitions import Requisition, RequisitionLine, require_sourceable
from app.procurement.suppliers import Supplier, supplier_by_code

# One money scale for the whole platform.
MONEY = Numeric(20, 6)

# An RFQ is open until it is closed; awarding is T-2.PROC.05.
ISSUED, CLOSED = "issued", "closed"


class RfqError(ValueError):
    """The RFQ refused what was asked of it."""


class DuplicateRfqError(RfqError):
    """That RFQ number is already used in this company."""


class RfqStateError(RfqError):
    """The asked-for change does not apply to the RFQ's state."""


class UnknownRfqLineError(RfqError):
    """A response quoted a line this RFQ never asked about."""


class NotInvitedError(RfqError):
    """A response arrived from a supplier the RFQ was never issued to."""


class DuplicateResponseError(RfqError):
    """That supplier already answered this RFQ."""


class IncompleteResponseError(RfqError):
    """A response with nothing in it, or a line that says nothing."""


class Rfq(Base):
    """One request for quotation, raised against an approved requisition."""

    __tablename__ = "rfq"
    __table_args__ = (
        UniqueConstraint("company_id", "number", name="uq_rfq_company_number"),
        CheckConstraint("status IN ('issued', 'closed')", name="ck_rfq_status"),
        CheckConstraint(
            "response_deadline >= issued_on", name="ck_rfq_deadline_after_issue"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    number: Mapped[str] = mapped_column(String(32), nullable=False)
    # The approved requisition this asks about — the document an award points back at.
    requisition_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_requisition.id"), nullable=False, index=True
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    issued_on: Mapped[date] = mapped_column(Date, nullable=False)
    response_deadline: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=ISSUED)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    requisition: Mapped[Requisition] = relationship()
    lines: Mapped[list[RfqLine]] = relationship(
        back_populates="rfq", order_by="RfqLine.line_no"
    )
    invitations: Mapped[list[RfqSupplier]] = relationship(back_populates="rfq")
    responses: Mapped[list[RfqResponse]] = relationship(back_populates="rfq")


class RfqLine(Base):
    """One line of the requisition the RFQ asks about, at its own line number."""

    __tablename__ = "rfq_line"
    __table_args__ = (
        UniqueConstraint("rfq_id", "line_no", name="uq_rfq_line_no"),
        CheckConstraint("line_no >= 1", name="ck_rfq_line_starts_at_one"),
        CheckConstraint("quantity > 0", name="ck_rfq_line_quantity"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    rfq_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rfq.id"), nullable=False, index=True
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    # The requisition line this stands for — how "which requisition lines does this
    # response cover" is answered exactly rather than by matching descriptions.
    requisition_line_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_requisition_line.id"), nullable=False, index=True
    )
    description: Mapped[str] = mapped_column(String(200), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    uom: Mapped[str] = mapped_column(String(16), nullable=False)

    rfq: Mapped[Rfq] = relationship(back_populates="lines")
    # The requisition line this asks about — where the item behind the request is
    # found when the line is awarded (T-2.PROC.05).
    requisition_line: Mapped[RequisitionLine] = relationship()


class RfqSupplier(Base):
    """A supplier the RFQ was issued to — invited whether or not they answer."""

    __tablename__ = "rfq_supplier"
    __table_args__ = (
        UniqueConstraint("rfq_id", "supplier_id", name="uq_rfq_supplier_once"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    rfq_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rfq.id"), nullable=False, index=True
    )
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("supplier.id"), nullable=False, index=True
    )

    rfq: Mapped[Rfq] = relationship(back_populates="invitations")
    supplier: Mapped[Supplier] = relationship()


class RfqResponse(Base):
    """One supplier's answer to one RFQ, with its lead time and validity."""

    __tablename__ = "rfq_response"
    __table_args__ = (
        UniqueConstraint("rfq_id", "supplier_id", name="uq_rfq_response_once"),
        CheckConstraint("lead_time_days IS NULL OR lead_time_days >= 0",
                        name="ck_rfq_response_lead_time"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    rfq_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rfq.id"), nullable=False, index=True
    )
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("supplier.id"), nullable=False, index=True
    )
    received_on: Mapped[date] = mapped_column(Date, nullable=False)
    # Arrived after the deadline: recorded, so the record can say so. Deciding
    # whether to still use it is the buyer's, not this module's.
    late: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    # How long the supplier says delivery takes after the order, and how long the
    # quoted price stands — both optional, because a quote may not state them.
    lead_time_days: Mapped[int | None] = mapped_column(Integer)
    valid_until: Mapped[date | None] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    rfq: Mapped[Rfq] = relationship(back_populates="responses")
    supplier: Mapped[Supplier] = relationship()
    lines: Mapped[list[RfqResponseLine]] = relationship(
        back_populates="response", order_by="RfqResponseLine.line_no"
    )


class RfqResponseLine(Base):
    """What a supplier quoted for one RFQ line — absent means they quoted nothing."""

    __tablename__ = "rfq_response_line"
    __table_args__ = (
        UniqueConstraint("response_id", "rfq_line_id", name="uq_rfq_response_line_once"),
        # The RFQ line's own number, kept here so a response reads in the order the
        # RFQ asked its questions — ordering by the line's id would order by a UUID.
        UniqueConstraint("response_id", "line_no", name="uq_rfq_response_line_no"),
        CheckConstraint("line_no >= 1", name="ck_rfq_response_line_starts_at_one"),
        # Zero is a real quote (a free sample, a bundled item), so it is allowed;
        # a negative price is not a price.
        CheckConstraint("unit_price >= 0", name="ck_rfq_response_line_price"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    response_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rfq_response.id"), nullable=False, index=True
    )
    rfq_line_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rfq_line.id"), nullable=False, index=True
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)

    response: Mapped[RfqResponse] = relationship(back_populates="lines")
    rfq_line: Mapped[RfqLine] = relationship()


def _amount(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def issue_rfq(
    session: Session,
    *,
    requisition: Requisition,
    number: str,
    supplier_codes: Any,
    response_deadline: date,
    issued_on: date | None = None,
) -> Rfq:
    """Issue an RFQ against an **approved** requisition to one or more suppliers.

    The requisition's lines are copied as the RFQ's own lines, so the question asked
    is the question that was approved. An unapproved requisition is refused by
    :func:`require_sourceable` — nothing may be sourced, including by asking prices.
    """
    require_sourceable(session, requisition)
    if not requisition.lines:
        raise RfqError(
            f"requisition {requisition.number!r} has no lines; there is nothing to ask about"
        )
    wanted = str(number).strip()
    if not wanted:
        raise RfqError("a number is required")
    if session.scalar(
        select(Rfq).where(Rfq.company_id == requisition.company_id, Rfq.number == wanted)
    ) is not None:
        raise DuplicateRfqError(f"RFQ {wanted!r} already exists in this company")

    codes = [str(code).strip() for code in supplier_codes]
    if not codes:
        raise RfqError(
            "an RFQ is issued to at least one supplier; asking nobody asks nothing"
        )
    issued = issued_on or date.today()
    if response_deadline < issued:
        raise RfqError(
            f"the deadline {response_deadline} is before the issue date {issued}"
        )

    rfq = Rfq(
        company_id=requisition.company_id,
        number=wanted,
        requisition_id=requisition.id,
        currency=requisition.currency,
        issued_on=issued,
        response_deadline=response_deadline,
        status=ISSUED,
    )
    session.add(rfq)
    session.flush()
    for line in requisition.lines:
        rfq.lines.append(
            RfqLine(
                company_id=requisition.company_id,
                rfq_id=rfq.id,
                line_no=line.line_no,
                requisition_line_id=line.id,
                description=line.description,
                quantity=line.quantity,
                uom=line.uom,
            )
        )
    # The suppliers are resolved through T-2.PROC.01, so an RFQ cannot be issued to
    # a party that is not a supplier of this company.
    for code in dict.fromkeys(codes):
        supplier = supplier_by_code(session, company_id=requisition.company_id, code=code)
        rfq.invitations.append(
            RfqSupplier(
                company_id=requisition.company_id, rfq_id=rfq.id, supplier_id=supplier.id
            )
        )
    session.flush()
    return rfq


def record_response(
    session: Session,
    rfq: Rfq,
    *,
    supplier_code: str,
    received_on: date,
    lines: Any,
    currency: str | None = None,
    lead_time_days: int | None = None,
    valid_until: date | None = None,
    note: str | None = None,
) -> RfqResponse:
    """Record one invited supplier's answer, line by line.

    `lines` is an iterable of ``{"line_no": …, "unit_price": …}`` naming the RFQ's own
    line numbers — a line the RFQ never asked about is refused rather than stored
    against nothing. A response that arrives after the deadline is stored with
    ``late = true`` instead of being refused: the buyer still gets to decide, and the
    record still says what happened.
    """
    if rfq.status != ISSUED:
        raise RfqStateError(
            f"RFQ {rfq.number!r} is {rfq.status}; it takes no further responses"
        )
    supplier = supplier_by_code(session, company_id=rfq.company_id, code=supplier_code)
    invited = {row.supplier_id for row in rfq.invitations}
    if supplier.id not in invited:
        raise NotInvitedError(
            f"supplier {supplier_code!r} was not issued RFQ {rfq.number!r}; invite it"
            " on the RFQ before recording its answer"
        )
    if session.scalar(
        select(RfqResponse).where(
            RfqResponse.rfq_id == rfq.id, RfqResponse.supplier_id == supplier.id
        )
    ) is not None:
        raise DuplicateResponseError(
            f"supplier {supplier_code!r} already answered RFQ {rfq.number!r}; one response"
            " per supplier, so its quote has one answer"
        )
    quoted = list(lines)
    if not quoted:
        raise IncompleteResponseError(
            f"the response from {supplier_code!r} quotes nothing; a response carries at"
            " least one line"
        )
    by_number = {line.line_no: line for line in rfq.lines}
    response = RfqResponse(
        company_id=rfq.company_id,
        rfq_id=rfq.id,
        supplier_id=supplier.id,
        received_on=received_on,
        late=received_on > rfq.response_deadline,
        currency=currency_by_code(
            session, company_id=rfq.company_id, code=currency or rfq.currency
        ).code,
        lead_time_days=None if lead_time_days is None else int(lead_time_days),
        valid_until=valid_until,
        note=note,
    )
    if response.lead_time_days is not None and response.lead_time_days < 0:
        raise IncompleteResponseError(
            f"a lead time is a number of days, not {lead_time_days!r}"
        )
    session.add(response)
    session.flush()
    for raw in quoted:
        line_no = int(raw["line_no"])
        asked = by_number.get(line_no)
        if asked is None:
            raise UnknownRfqLineError(
                f"RFQ {rfq.number!r} has no line {line_no}; it asks about"
                f" {sorted(by_number)}"
            )
        price = _amount(raw["unit_price"])
        if price < 0:
            raise IncompleteResponseError(f"a quoted price is not negative, got {price}")
        response.lines.append(
            RfqResponseLine(
                company_id=rfq.company_id,
                response_id=response.id,
                rfq_line_id=asked.id,
                line_no=line_no,
                unit_price=price,
            )
        )
    session.flush()
    return response


def close_rfq(session: Session, rfq: Rfq) -> Rfq:
    """Stop taking responses — the RFQ is done asking."""
    if rfq.status == CLOSED:
        raise RfqStateError(f"RFQ {rfq.number!r} is already closed")
    rfq.status = CLOSED
    session.flush()
    return rfq


def responses(session: Session, rfq: Rfq) -> list[RfqResponse]:
    """Every answer received, oldest first."""
    return list(
        session.scalars(
            select(RfqResponse)
            .where(RfqResponse.rfq_id == rfq.id)
            .order_by(RfqResponse.received_on, RfqResponse.created_at)
        )
    )


def invited(session: Session, rfq: Rfq) -> list[Supplier]:
    """Every supplier the RFQ was issued to, whether or not they answered.

    Ordered by the party code — the order a person reads them in, and a stable one.
    Ordering by the supplier's id would order by a UUID.
    """
    rows = list(
        session.scalars(
            select(Supplier)
            .join(RfqSupplier, RfqSupplier.supplier_id == Supplier.id)
            .where(RfqSupplier.rfq_id == rfq.id)
        )
    )
    return sorted(rows, key=lambda supplier: supplier.party.code)


def non_responders(session: Session, rfq: Rfq) -> list[Supplier]:
    """The suppliers that were asked and did not answer at all.

    Distinct from a supplier that answered and quoted nothing for a line: that one
    has a response, this one has none, and T-2.PROC.04's comparison has to be able to
    tell them apart.
    """
    answered = {row.supplier_id for row in responses(session, rfq)}
    return [
        supplier
        for supplier in invited(session, rfq)
        if supplier.id not in answered
    ]


def late_responses(session: Session, rfq: Rfq) -> list[RfqResponse]:
    """The answers that arrived after the deadline — recorded as late, not dropped."""
    return [row for row in responses(session, rfq) if row.late]


def quoted_line_numbers(response: RfqResponse) -> list[int]:
    """The RFQ line numbers one response actually quotes, in order."""
    return sorted(line.line_no for line in response.lines)


def requisition_lines_covered(response: RfqResponse) -> list[uuid.UUID]:
    """The requisition lines a response covers — what an award may draw on."""
    return sorted({line.rfq_line.requisition_line_id for line in response.lines}, key=str)


def rfq_by_number(session: Session, *, company_id: uuid.UUID, number: str) -> Rfq:
    """The RFQ a later document quotes, or a refusal naming what is missing."""
    found = session.scalar(
        select(Rfq).where(Rfq.company_id == company_id, Rfq.number == str(number).strip())
    )
    if found is None:
        raise RfqError(f"no RFQ {number!r} in this company")
    return found
