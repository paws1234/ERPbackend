"""T-2.PROC.07 — the goods receipt note: receiving against a purchase order.

A GRN is where the purchase order meets the warehouse. Goods arrive at a location,
the stock ledger records them and the order learns how much of itself has arrived —
so "what is still expected" stops being a guess:

* **Receiving posts stock, and the posting is the order's information.** Each
  accepted line calls :func:`app.stock.transactions.receive`, which writes the stock
  ledger entry and its GL posting in the caller's transaction (T-1.INV.07), and the
  order line's `received_quantity` moves **only when the receipt is posted**. A draft
  receipt therefore leaves the quantity outstanding, which is what makes T-2.PROC.06's
  "a PO cannot close with open receipts" true without a second rule.
* **Nothing is received against an unapproved order.** :func:`require_approved`
  (T-2.PROC.06) is called first, so "requires approval before it can be sent or
  received against" is enforced where receiving starts.
* **A rejected quantity is recorded, not discarded.** What the warehouse refuses and
  what it accepts are both on the line; the rejected part never enters stock but does
  stay on the document, so a supplier's quality is measurable later (T-2.PROC.09).
* **Over-receipt is a decision.** Receiving more than the order asked for is refused
  unless a reason is stated, and the reason is recorded — the same shape as the
  over-award rule in T-2.PROC.05.
* **The value flows from the order.** A line's value is its quantity at the price the
  order was raised at, so stock valuation (T-1.INV.04) receives what was actually
  agreed rather than what a receiver typed.

ponytail: no batch/serial identity on a receipt line. Ceiling: a batch- or
serial-tracked item cannot be received through this document yet — the stock
transaction's own refusal says so rather than storing an untracked movement. Upgrade
path: add `batch_id`/`serial_id` to the line and pass them to
:func:`app.stock.transactions.receive` when a phase needs tracked receiving.
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

from app.db import Base
from app.procurement.orders import (
    PurchaseOrder,
    PurchaseOrderLine,
    require_approved,
)
from app.stock.entries import StockLedgerEntry, movements_for_source
from app.stock.locations import Location
from app.stock.transactions import receive

# One money scale for the whole platform.
MONEY = Numeric(20, 6)

# A receipt is a draft until it is posted; posting is what moves stock.
DRAFT, POSTED = "draft", "posted"


class ReceiptError(ValueError):
    """The goods receipt refused what was asked of it."""


class DuplicateReceiptError(ReceiptError):
    """That receipt number is already used in this company."""


class ReceiptStateError(ReceiptError):
    """The asked-for change does not apply to the receipt's state."""


class UnknownOrderLineError(ReceiptError):
    """The receipt names a line the purchase order does not have."""


class OverReceiptError(ReceiptError):
    """Receiving more than the order asked for, with no reason stated."""


class GoodsReceipt(Base):
    """One delivery received against one purchase order."""

    __tablename__ = "goods_receipt"
    __table_args__ = (
        UniqueConstraint("company_id", "number", name="uq_goods_receipt_company_number"),
        CheckConstraint("status IN ('draft', 'posted')", name="ck_goods_receipt_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    number: Mapped[str] = mapped_column(String(32), nullable=False)
    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_order.id"), nullable=False, index=True
    )
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("supplier.id"), nullable=False, index=True
    )
    # Where the goods went: a leaf location, checked by the stock transaction itself.
    location_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("location.id"), nullable=False, index=True
    )
    received_on: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=DRAFT)
    # Why more than the order asked for was accepted. Null means it was not.
    over_receipt_reason: Mapped[str | None] = mapped_column(Text)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    order: Mapped[PurchaseOrder] = relationship()
    location: Mapped[Location] = relationship()
    lines: Mapped[list[GoodsReceiptLine]] = relationship(
        back_populates="receipt", order_by="GoodsReceiptLine.line_no"
    )


class GoodsReceiptLine(Base):
    """What arrived on one ordered line: accepted, and what was turned away."""

    __tablename__ = "goods_receipt_line"
    __table_args__ = (
        UniqueConstraint("receipt_id", "line_no", name="uq_goods_receipt_line_no"),
        UniqueConstraint("receipt_id", "order_line_id", name="uq_goods_receipt_line_once"),
        CheckConstraint("line_no >= 1", name="ck_goods_receipt_line_starts_at_one"),
        CheckConstraint("quantity > 0", name="ck_goods_receipt_line_quantity"),
        CheckConstraint("rejected_quantity >= 0", name="ck_goods_receipt_line_rejected"),
        CheckConstraint("unit_price >= 0", name="ck_goods_receipt_line_price"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    receipt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("goods_receipt.id"), nullable=False, index=True
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    order_line_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_order_line.id"), nullable=False, index=True
    )
    # Accepted: this is what enters stock.
    quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    # Turned away: never enters stock, but stays on the document so what a supplier
    # actually delivered is on the record (T-2.PROC.09 scores it).
    rejected_quantity: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, default=Decimal(0)
    )
    uom: Mapped[str] = mapped_column(String(16), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    # The stock ledger entry posting produced, so a receipt line points at its own
    # movement rather than leaving the two to be matched up by hand.
    movement_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("stock_ledger_entry.id"), index=True
    )

    receipt: Mapped[GoodsReceipt] = relationship(back_populates="lines")
    order_line: Mapped[PurchaseOrderLine] = relationship()


def _amount(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def line_value(line: GoodsReceiptLine) -> Decimal:
    """What one accepted line is worth: quantity × the ordered unit price."""
    return (line.quantity * line.unit_price).quantize(Decimal("0.000001"))


def receipt_value(receipt: GoodsReceipt) -> Decimal:
    """What the whole receipt is worth — derived from its lines, never stored."""
    return sum((line_value(line) for line in receipt.lines), Decimal(0)).quantize(
        Decimal("0.000001")
    )


def create_receipt(
    session: Session,
    *,
    order: PurchaseOrder,
    number: str,
    location: Location,
    received_on: date,
    lines: Any,
) -> GoodsReceipt:
    """Raise a **draft** receipt against an approved order.

    `lines` is an iterable of
    ``{"line_no": …, "quantity": …, "rejected_quantity": …}`` naming the **order's**
    line numbers. The price is taken from the order line, so nothing is re-keyed and
    the value that reaches stock valuation is the value that was agreed.
    """
    require_approved(session, order)
    wanted = str(number).strip()
    if not wanted:
        raise ReceiptError("a receipt number is required")
    if session.scalar(
        select(GoodsReceipt).where(
            GoodsReceipt.company_id == order.company_id, GoodsReceipt.number == wanted
        )
    ) is not None:
        raise DuplicateReceiptError(
            f"goods receipt {wanted!r} already exists in this company"
        )
    if location.company_id != order.company_id:
        raise ReceiptError(
            f"location {location.code!r} belongs to another company"
        )
    entries = list(lines)
    if not entries:
        raise ReceiptError(f"receipt {wanted!r} names no lines; nothing arrived")

    by_line_no = {line.line_no: line for line in order.lines}
    receipt = GoodsReceipt(
        company_id=order.company_id,
        number=wanted,
        order_id=order.id,
        supplier_id=order.supplier_id,
        location_id=location.id,
        received_on=received_on,
        status=DRAFT,
    )
    session.add(receipt)
    session.flush()
    for raw in entries:
        line_no = int(raw["line_no"])
        order_line = by_line_no.get(line_no)
        if order_line is None:
            raise UnknownOrderLineError(
                f"purchase order {order.number!r} has no line {line_no}; it has"
                f" {sorted(by_line_no)}"
            )
        quantity = _amount(raw["quantity"])
        if quantity <= 0:
            raise ReceiptError(f"a received quantity is above zero, got {quantity}")
        rejected = _amount(raw.get("rejected_quantity", 0))
        if rejected < 0:
            raise ReceiptError(f"a rejected quantity is not negative, got {rejected}")
        receipt.lines.append(
            GoodsReceiptLine(
                company_id=order.company_id,
                receipt_id=receipt.id,
                line_no=len(receipt.lines) + 1,
                order_line_id=order_line.id,
                quantity=quantity,
                rejected_quantity=rejected,
                uom=order_line.uom,
                unit_price=order_line.unit_price,
            )
        )
    session.flush()
    return receipt


def post_receipt(
    session: Session, receipt: GoodsReceipt, *, over_receipt_reason: str | None = None
) -> GoodsReceipt:
    """Post the receipt: move stock, post the GL and tell the order what arrived.

    All of it in the caller's transaction, so a receipt that fails half-way leaves
    neither a movement nor a changed order behind. Over-receipt is refused unless
    `over_receipt_reason` is stated, and the reason is recorded on the receipt.
    """
    if receipt.status == POSTED:
        raise ReceiptStateError(
            f"goods receipt {receipt.number!r} is already posted; a posted receipt is"
            " history and is never posted twice"
        )
    order = receipt.order
    require_approved(session, order)
    over = False
    for line in receipt.lines:
        order_line = line.order_line
        arriving = order_line.received_quantity + line.quantity
        if arriving > order_line.quantity:
            over = True
            if not (over_receipt_reason or "").strip():
                raise OverReceiptError(
                    f"line {order_line.line_no} of order {order.number!r} asked for"
                    f" {order_line.quantity} and {order_line.received_quantity} has"
                    f" arrived; receiving {line.quantity} more needs a stated reason"
                )
    if over:
        receipt.over_receipt_reason = str(over_receipt_reason).strip()

    for line in receipt.lines:
        order_line = line.order_line
        if order_line.item_id is None:
            raise ReceiptError(
                f"line {order_line.line_no} of order {order.number!r} has no stock item"
                " — a service is not received into a location"
            )
        entry = receive(
            session,
            item=order_line.item,
            location=receipt.location,
            uom=line.uom,
            quantity=line.quantity,
            value=line_value(line),
            currency=order.currency,
            source_type="goods_receipt",
            source_id=receipt.id,
            posting_date=receipt.received_on,
        )
        line.movement_id = entry.id
        order_line.received_quantity = (
            order_line.received_quantity + line.quantity
        ).quantize(Decimal("0.000001"))
    receipt.status = POSTED
    receipt.posted_at = datetime.now(timezone.utc)
    session.flush()
    return receipt


def receipts_for_order(session: Session, order: PurchaseOrder) -> list[GoodsReceipt]:
    """Every receipt raised against one order, oldest first."""
    return list(
        session.scalars(
            select(GoodsReceipt)
            .where(GoodsReceipt.order_id == order.id)
            .order_by(GoodsReceipt.created_at, GoodsReceipt.number)
        )
    )


def open_receipts(session: Session, order: PurchaseOrder) -> list[GoodsReceipt]:
    """The receipts raised against an order that have **not** been posted.

    An open receipt is why an order cannot be closed: the quantity it names is still
    outstanding, because `received_quantity` only moves when the receipt is posted.
    """
    return [row for row in receipts_for_order(session, order) if row.status == DRAFT]


def rejected_quantity(session: Session, order: PurchaseOrder) -> Decimal:
    """How much a supplier delivered and the warehouse turned away, order-wide."""
    total = sum(
        (
            line.rejected_quantity
            for receipt in receipts_for_order(session, order)
            for line in receipt.lines
        ),
        Decimal(0),
    )
    return total.quantize(Decimal("0.000001"))


def movement_for_line(session: Session, receipt: GoodsReceipt, line: GoodsReceiptLine):
    """The stock ledger entry one posted receipt line produced, if it is posted."""
    if line.movement_id is None:
        return None
    return session.get(StockLedgerEntry, line.movement_id)


def receipt_movements(session: Session, receipt: GoodsReceipt) -> list[StockLedgerEntry]:
    """Every stock movement this receipt caused — the drill-down from document to ledger."""
    return movements_for_source(
        session,
        company_id=receipt.company_id,
        source_type="goods_receipt",
        source_id=receipt.id,
    )


def receipt_by_number(
    session: Session, *, company_id: uuid.UUID, number: str
) -> GoodsReceipt:
    """The receipt a later document quotes, or a refusal naming what is missing."""
    found = session.scalar(
        select(GoodsReceipt).where(
            GoodsReceipt.company_id == company_id,
            GoodsReceipt.number == str(number).strip(),
        )
    )
    if found is None:
        raise ReceiptError(f"no goods receipt {number!r} in this company")
    return found
