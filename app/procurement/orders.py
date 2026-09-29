"""T-2.PROC.05 — awarding RFQ lines and generating the purchase order from them.

An award is the decision: *this* supplier, *this* quantity, at the price they quoted.
The purchase order is the same decision written as a document something can be
received against, and this module writes both in one step so the two cannot disagree
— the awarded price, the supplier, the requisition and the required date are carried
across with **no re-keying** and no second chance to mistype a number.

Four rules the module is built around:

* **Only what was quoted may be awarded.** A supplier that never answered cannot win
  a line, and a line the supplier was silent about cannot be awarded at all — the
  price would have to be invented.
* **The requisitioned quantity is the ceiling.** Awarding more than was approved
  needs an **explicit override**, and the override carries a reason on the order, so
  "we bought more than we asked for" is a recorded decision rather than a silent
  one.
* **A partial award stays partial.** :func:`remaining_awardable` reports what is
  still open per RFQ line, derived from the orders already raised — so the rest of a
  line is genuinely available to award to somebody else, or later.
* **The order starts as a draft.** What may be done with it (approval, amendment,
  closure) is T-2.PROC.06's; generating it does not decide that.

The lines keep the RFQ line and the requisition line they came from, which is what
makes T-2.MATCH.01's three-way comparison answerable at all: an invoice's quantity
can only be compared with what was ordered *and* what was asked for if the order
still knows both.
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
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.audit import read_trail
from app.db import Base
from app.ledger.currency import currency_by_code
from app.stock.items import Item
from app.procurement.requisitions import Requisition, require_sourceable
from app.procurement.rfq import Rfq, RfqResponse, RfqResponseLine, responses as rfq_responses
from app.procurement.suppliers import Supplier, supplier_by_code
from app.workflow import (
    APPROVED as WF_APPROVED,
    PENDING as WF_PENDING,
    REJECTED as WF_REJECTED,
    RETURNED as WF_RETURNED,
    ApprovalRequest,
)
from app.workflow import decide as workflow_decide
from app.workflow import start_approval

# The document type the approval engine knows purchase orders by.
DOC_TYPE = "purchase_order"

# One money scale for the whole platform.
MONEY = Numeric(20, 6)

# A generated order is a draft: T-2.PROC.06 owns what happens to it next.
DRAFT, PENDING, APPROVED, CLOSED = "draft", "pending", "approved", "closed"

# A returned order goes back to draft with a reason (T-0.WF.01's `return`).
RETURNED = "returned"
REJECTED = "rejected"

# How the engine's request state maps onto the order's.
_FROM_ENGINE = {
    WF_PENDING: PENDING,
    WF_APPROVED: APPROVED,
    WF_REJECTED: REJECTED,
    WF_RETURNED: RETURNED,
}


class OrderError(ValueError):
    """The purchase order refused what was asked of it."""


class DuplicateOrderError(OrderError):
    """That order number is already used in this company."""


class NotAwardableError(OrderError):
    """That supplier, or that line, was never quoted — nothing to award."""


class OverAwardError(OrderError):
    """The award would pass the requisitioned quantity without an explicit override."""


class OrderStateError(OrderError):
    """The asked-for change does not apply to the order's state."""


class PurchaseOrder(Base):
    """What was ordered: one supplier, the requisition behind it, a required date."""

    __tablename__ = "purchase_order"
    __table_args__ = (
        UniqueConstraint("company_id", "number", name="uq_purchase_order_company_number"),
        CheckConstraint(
            "status IN ('draft', 'pending', 'approved', 'returned', 'rejected', 'closed')",
            name="ck_purchase_order_status",
        ),
        CheckConstraint("revision_no >= 1", name="ck_purchase_order_revision"),
        CheckConstraint(
            "(revision_no = 1) = (revision_of_id IS NULL)",
            name="ck_purchase_order_revision_pair",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    number: Mapped[str] = mapped_column(String(32), nullable=False)
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("supplier.id"), nullable=False, index=True
    )
    # The chain back to why this order exists: the requisition, and the RFQ whose
    # answer it was awarded from. Both are required — an order nobody asked for is
    # exactly what §2.3's procure-to-pay cycle is meant to make impossible.
    requisition_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_requisition.id"), nullable=False, index=True
    )
    rfq_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("rfq.id"), nullable=False, index=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    required_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=DRAFT)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    revision_of_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("purchase_order.id"), index=True
    )
    # Why this order exceeds what was requisitioned. Null means it did not.
    over_award_reason: Mapped[str | None] = mapped_column(Text)
    # Why this order was closed while something was still outstanding. Null means
    # it was closed with everything received.
    close_reason: Mapped[str | None] = mapped_column(Text)
    approved_by: Mapped[str | None] = mapped_column(String(64))
    approval_request_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("approval_request.id"), index=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    supplier: Mapped[Supplier] = relationship()
    requisition: Mapped[Requisition] = relationship()
    rfq: Mapped[Rfq] = relationship()
    lines: Mapped[list[PurchaseOrderLine]] = relationship(
        back_populates="order", order_by="PurchaseOrderLine.line_no"
    )


class PurchaseOrderLine(Base):
    """One ordered line, carrying the price it was awarded at."""

    __tablename__ = "purchase_order_line"
    __table_args__ = (
        UniqueConstraint("order_id", "line_no", name="uq_purchase_order_line_no"),
        CheckConstraint("line_no >= 1", name="ck_purchase_order_line_starts_at_one"),
        CheckConstraint("quantity > 0", name="ck_purchase_order_line_quantity"),
        CheckConstraint("unit_price >= 0", name="ck_purchase_order_line_price"),
        CheckConstraint("received_quantity >= 0", name="ck_purchase_order_line_received"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_order.id"), nullable=False, index=True
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(String(200), nullable=False)
    item_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("item.id"), index=True)
    quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    uom: Mapped[str] = mapped_column(String(16), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    # How much of this line has actually arrived, maintained by the receipt
    # (T-2.PROC.07). Kept on the line so "can this order close" is answered from
    # the order itself, and so a receipt can never be counted twice.
    received_quantity: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, default=Decimal(0)
    )
    # The RFQ line this was awarded from and the requisition line behind it: what
    # T-2.MATCH.01 compares an invoice against, and what makes a partial award
    # measurable.
    rfq_line_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rfq_line.id"), nullable=False, index=True
    )
    requisition_line_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_requisition_line.id"), nullable=False, index=True
    )

    order: Mapped[PurchaseOrder] = relationship(back_populates="lines")
    # The stock item this line is for, where it is one — what a receipt moves.
    item: Mapped[Item | None] = relationship()


def _amount(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def line_total(line: PurchaseOrderLine) -> Decimal:
    """What one ordered line is worth: quantity × the awarded unit price."""
    return (line.quantity * line.unit_price).quantize(Decimal("0.000001"))


def order_total(order: PurchaseOrder) -> Decimal:
    """What the whole order is worth — derived from its lines, never stored beside them."""
    return sum((line_total(line) for line in order.lines), Decimal(0)).quantize(
        Decimal("0.000001")
    )


def awarded_quantity(session: Session, rfq_line_id: uuid.UUID) -> Decimal:
    """How much of one RFQ line has already been ordered, across every order.

    Derived from the orders themselves rather than from a counter kept beside them:
    a counter can drift, and this figure decides whether an award may proceed.
    """
    awarded = session.scalar(
        select(func.coalesce(func.sum(PurchaseOrderLine.quantity), 0)).where(
            PurchaseOrderLine.rfq_line_id == rfq_line_id
        )
    )
    return _amount(awarded or 0)


def remaining_awardable(session: Session, rfq: Rfq) -> dict[int, Decimal]:
    """What is still open per RFQ line, after the orders already raised.

    This is what makes a **partial** award genuinely partial: the rest of the line
    stays available, so it can be awarded to another supplier or later, and nothing
    silently re-awards what was already bought.
    """
    return {
        line.line_no: (line.quantity - awarded_quantity(session, line.id)).quantize(
            Decimal("0.000001")
        )
        for line in rfq.lines
    }


def _quoted(response: RfqResponse, rfq_line_id: uuid.UUID) -> RfqResponseLine | None:
    return next((line for line in response.lines if line.rfq_line_id == rfq_line_id), None)


def award(
    session: Session,
    *,
    rfq: Rfq,
    awards: Any,
    actor: str,
    override_reason: str | None = None,
) -> list[PurchaseOrder]:
    """Award RFQ lines to suppliers and generate the purchase order(s).

    `awards` is an iterable of
    ``{"supplier_code": …, "number": …, "required_date": …,
       "lines": [{"line_no": …, "quantity": …}]}``
    — one entry per order to generate, so one award call can produce several orders
    (one per winning supplier) in the same transaction.

    Each line's price is taken from that supplier's own response, so nothing is
    re-keyed. A supplier that did not answer, or was silent about a line, is refused
    rather than awarded at a guessed price. Ordering more than the requisition asked
    for needs `override_reason`, which is recorded on the order.
    """
    require_sourceable(session, rfq.requisition)
    if not str(actor or "").strip():
        raise OrderError("an award states who made it; the audit trail records the actor")

    answered = {response.supplier_id: response for response in rfq_responses(session, rfq)}
    by_number = {line.line_no: line for line in rfq.lines}
    orders: list[PurchaseOrder] = []

    for entry in awards:
        raw = dict(entry)
        supplier = supplier_by_code(
            session, company_id=rfq.company_id, code=raw["supplier_code"]
        )
        response = answered.get(supplier.id)
        if response is None:
            raise NotAwardableError(
                f"supplier {raw['supplier_code']!r} did not answer RFQ {rfq.number!r};"
                " there is no quote to award"
            )
        number = str(raw["number"]).strip()
        if not number:
            raise OrderError("a purchase order number is required")
        if session.scalar(
            select(PurchaseOrder).where(
                PurchaseOrder.company_id == rfq.company_id, PurchaseOrder.number == number
            )
        ) is not None:
            raise DuplicateOrderError(
                f"purchase order {number!r} already exists in this company"
            )

        wanted = list(raw["lines"])
        if not wanted:
            raise OrderError(f"an award to {raw['supplier_code']!r} names no lines")

        # Everything is checked **before** the order exists, so a refused award
        # leaves no half-written document behind for the caller to notice.
        resolved = []
        for raw_line in wanted:
            line_no = int(raw_line["line_no"])
            asked = by_number.get(line_no)
            if asked is None:
                raise NotAwardableError(
                    f"RFQ {rfq.number!r} has no line {line_no}; it asks about"
                    f" {sorted(by_number)}"
                )
            quote = _quoted(response, asked.id)
            if quote is None:
                raise NotAwardableError(
                    f"{raw['supplier_code']!r} quoted nothing for line {line_no} of"
                    f" RFQ {rfq.number!r}; there is no price to award at"
                )
            quantity = _amount(raw_line["quantity"])
            if quantity <= 0:
                raise OrderError(f"an awarded quantity is above zero, got {quantity}")
            already = awarded_quantity(session, asked.id)
            if already + quantity > asked.quantity and not override_reason:
                raise OverAwardError(
                    f"line {line_no} of RFQ {rfq.number!r} asked for {asked.quantity}"
                    f" and {already} is already awarded; awarding {quantity} more needs"
                    " an explicit override with a reason"
                )
            resolved.append((asked, quote, quantity))

        order = PurchaseOrder(
            company_id=rfq.company_id,
            number=number,
            supplier_id=supplier.id,
            requisition_id=rfq.requisition_id,
            rfq_id=rfq.id,
            currency=currency_by_code(
                session, company_id=rfq.company_id, code=rfq.currency
            ).code,
            required_date=raw["required_date"],
            status=DRAFT,
            revision_no=1,
            over_award_reason=override_reason,
        )
        session.add(order)
        session.flush()

        for line_no, (asked, quote, quantity) in enumerate(resolved, start=1):
            order.lines.append(
                PurchaseOrderLine(
                    company_id=rfq.company_id,
                    order_id=order.id,
                    line_no=line_no,
                    description=asked.description,
                    item_id=asked.requisition_line.item_id,
                    quantity=quantity,
                    uom=asked.uom,
                    unit_price=quote.unit_price,
                    rfq_line_id=asked.id,
                    requisition_line_id=asked.requisition_line_id,
                )
            )
        session.flush()
        orders.append(order)
    return orders


def orders_for_rfq(session: Session, rfq: Rfq) -> list[PurchaseOrder]:
    """Every order raised from one RFQ, oldest first."""
    return list(
        session.scalars(
            select(PurchaseOrder)
            .where(PurchaseOrder.rfq_id == rfq.id)
            .order_by(PurchaseOrder.created_at, PurchaseOrder.number)
        )
    )


def order_by_number(
    session: Session, *, company_id: uuid.UUID, number: str
) -> PurchaseOrder:
    """The order a receipt or an invoice quotes, or a refusal naming what is missing."""
    found = session.scalar(
        select(PurchaseOrder).where(
            PurchaseOrder.company_id == company_id,
            PurchaseOrder.number == str(number).strip(),
        )
    )
    if found is None:
        raise OrderError(f"no purchase order {number!r} in this company")
    return found


# --- T-2.PROC.06 — the order's lifecycle: approve, amend, close --------------
# The approval half is T-0.WF.01's again, under its own document type: the levels,
# thresholds and roles are rows in that engine, and this module only mirrors the
# engine's resulting state so the two cannot disagree. Who did what, and when, is not
# copied here either — every transition is a row change on this table, and
# T-0.AUDIT.02 records actor and time for each one (:func:`status_trail` reads it back).


def require_approved(session: Session, order: PurchaseOrder) -> PurchaseOrder:
    """Refuse anything that must not happen to an unapproved order.

    The gate T-2.PROC.07's receipt calls before goods move: "a PO above threshold
    requires approval before it can be sent or received against" is enforced where
    receiving starts, not assumed by each caller.
    """
    if order.status != APPROVED:
        raise OrderStateError(
            f"purchase order {order.number!r} is {order.status}; it cannot be sent or"
            " received against until its approval chain has approved it"
        )
    return order


def submit_order(session: Session, order: PurchaseOrder, *, actor: str) -> PurchaseOrder:
    """Send the order through its configured approval chain.

    An order below every configured level is approved on the spot — the engine's own
    answer — and one whose document type has no chain at all is refused by the engine
    rather than waved through.
    """
    if order.status != DRAFT:
        raise OrderStateError(
            f"purchase order {order.number!r} is {order.status}; only a draft is submitted"
        )
    if not order.lines:
        raise OrderError(f"purchase order {order.number!r} has no lines")
    request = start_approval(
        session,
        company_id=order.company_id,
        doc_type=DOC_TYPE,
        document_id=order.id,
        amount=order_total(order),
    )
    if request is None:
        order.status = APPROVED
        order.approved_by = str(actor)
    else:
        order.approval_request_id = request.id
        order.status = PENDING
    session.flush()
    return order


def decide_order(
    session: Session,
    order: PurchaseOrder,
    *,
    actor: str,
    action: str,
    role: str,
    reason: str | None = None,
) -> PurchaseOrder:
    """Take one decision on a pending order, through the engine."""
    if order.status != PENDING or order.approval_request_id is None:
        raise OrderStateError(
            f"purchase order {order.number!r} is {order.status}; nothing is waiting to"
            " be decided"
        )
    request = session.get(ApprovalRequest, order.approval_request_id)
    if request is None:  # pragma: no cover — the foreign key forbids it
        raise OrderStateError(
            f"purchase order {order.number!r} names an approval request that is gone"
        )
    workflow_decide(session, request, actor=actor, action=action, role=role, reason=reason)
    order.status = _FROM_ENGINE[request.state]
    if order.status == APPROVED:
        order.approved_by = str(actor)
    session.flush()
    return order


def amend(
    session: Session,
    order: PurchaseOrder,
    *,
    number: str,
    actor: str,
    lines: Any = None,
    required_date: date | None = None,
) -> PurchaseOrder:
    """Raise a **new draft revision** of an order that is no longer a draft.

    The approved order is not edited: it keeps its lines, its approval and its
    history, and the revision carries them forward with whatever changed. That is the
    only way an approved order changes — otherwise the approval on record was given
    for a document that no longer exists. `lines` is an optional iterable of
    ``{"line_no": …, "quantity": …, "unit_price": …}`` overriding individual lines.
    """
    if order.status == DRAFT:
        raise OrderStateError(
            f"purchase order {order.number!r} is still a draft; edit it rather than"
            " raising a revision"
        )
    existing = session.scalar(
        select(PurchaseOrder).where(PurchaseOrder.revision_of_id == order.id)
    )
    if existing is not None:
        raise OrderStateError(
            f"purchase order {order.number!r} already has revision {existing.number!r};"
            " revise that one"
        )
    wanted = str(number).strip()
    if not wanted:
        raise OrderError("a revision number is required")
    if session.scalar(
        select(PurchaseOrder).where(
            PurchaseOrder.company_id == order.company_id, PurchaseOrder.number == wanted
        )
    ) is not None:
        raise DuplicateOrderError(f"purchase order {wanted!r} already exists in this company")

    override = {
        int(raw["line_no"]): raw for raw in (lines or [])
    }
    revision = PurchaseOrder(
        company_id=order.company_id,
        number=wanted,
        supplier_id=order.supplier_id,
        requisition_id=order.requisition_id,
        rfq_id=order.rfq_id,
        currency=order.currency,
        required_date=required_date or order.required_date,
        status=DRAFT,
        revision_no=order.revision_no + 1,
        revision_of_id=order.id,
    )
    session.add(revision)
    session.flush()
    for line in order.lines:
        change = override.get(line.line_no, {})
        quantity = line.quantity if "quantity" not in change else Decimal(str(change["quantity"]))
        price = line.unit_price if "unit_price" not in change else Decimal(str(change["unit_price"]))
        if quantity <= 0:
            raise OrderError(f"line {line.line_no} would be amended to {quantity}")
        if price < 0:
            raise OrderError(f"line {line.line_no} would be amended to a negative price")
        if quantity < line.received_quantity:
            raise OrderError(
                f"line {line.line_no} already has {line.received_quantity} received; it"
                f" cannot be amended down to {quantity}"
            )
        revision.lines.append(
            PurchaseOrderLine(
                company_id=order.company_id,
                order_id=revision.id,
                line_no=line.line_no,
                description=line.description,
                item_id=line.item_id,
                quantity=quantity,
                uom=line.uom,
                unit_price=price,
                rfq_line_id=line.rfq_line_id,
                requisition_line_id=line.requisition_line_id,
            )
        )
    session.flush()
    return revision


def revisions(session: Session, order: PurchaseOrder) -> list[PurchaseOrder]:
    """The chain of revisions that starts at `order`, oldest first — including it."""
    first = order
    while first.revision_of_id is not None:
        parent = session.get(PurchaseOrder, first.revision_of_id)
        if parent is None:  # pragma: no cover — the foreign key forbids it
            break
        first = parent
    chain = [first]
    while True:
        following = session.scalar(
            select(PurchaseOrder).where(PurchaseOrder.revision_of_id == chain[-1].id)
        )
        if following is None:
            return chain
        chain.append(following)


def receipt_progress(session: Session, order: PurchaseOrder) -> dict:
    """Ordered against received, per line, and what is still outstanding.

    The figure the closure rule reads, and the one a receipt (T-2.PROC.07) moves.
    Derived from the lines themselves so it cannot disagree with them.
    """
    lines = []
    for line in order.lines:
        outstanding = (line.quantity - line.received_quantity).quantize(
            Decimal("0.000001")
        )
        lines.append(
            {
                "line_no": line.line_no,
                "ordered": line.quantity,
                "received": line.received_quantity,
                "outstanding": outstanding if outstanding > 0 else Decimal(0),
            }
        )
    return {
        "lines": lines,
        "outstanding": sum((row["outstanding"] for row in lines), Decimal(0)).quantize(
            Decimal("0.000001")
        ),
    }


def close_order(
    session: Session,
    order: PurchaseOrder,
    *,
    actor: str,
    short_close_reason: str | None = None,
) -> PurchaseOrder:
    """Close an approved order — refused while it still expects goods.

    "A PO cannot close with open receipts": an order with outstanding quantity is
    still expecting something, so closing it has to be a decision somebody makes and
    states (`short_close_reason`), not something that happens by accident.
    """
    if order.status != APPROVED:
        raise OrderStateError(
            f"purchase order {order.number!r} is {order.status}; only an approved order"
            " is closed"
        )
    progress = receipt_progress(session, order)
    if progress["outstanding"] > 0 and not (short_close_reason or "").strip():
        raise OrderStateError(
            f"purchase order {order.number!r} still expects"
            f" {progress['outstanding']}; closing it short needs a stated reason"
        )
    order.status = CLOSED
    order.closed_at = datetime.now(timezone.utc)
    order.close_reason = (
        str(short_close_reason).strip() if short_close_reason else None
    )
    if str(actor or "").strip() == "":
        raise OrderError("closing an order states who did it")
    session.flush()
    return order


def status_trail(session: Session, order: PurchaseOrder) -> list:
    """Every recorded change to this order — actor, action and time.

    Read from T-0.AUDIT.02's trail rather than from a table of its own: the trail
    already records who changed the row and when, so a second copy here would be a
    second answer to the same question.
    """
    return read_trail(session, entity="purchase_order", entity_id=order.id)
