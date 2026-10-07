"""T-3.SALES.05 — fulfilment: pick lists, shipping, and the stock issue behind them.

§2.5 asks for "Sales Orders & Fulfillment (credit checks, pick lists, shipping)". The
credit side is T-3.SALES.04's; this module is the physical half — what a warehouse
does, and what it costs when it happens.

* **A pick list is a copy of the order's lines, not a second order.** It is generated
  from a **confirmed** order and carries exactly that order's lines, so the paper the
  picker holds and the document the customer agreed cannot drift apart.
* **Shipping is what moves stock.** Confirming an order promises nothing physically;
  a shipment is the event that issues stock out of a location, and it is issued through
  T-1.INV.05's `issue`, so the valuation walk, the no-negative-stock rule and the GL
  posting are the ones every other module already obeys rather than rules re-stated
  here. `issue` posts by itself, which is why every shipment balances.
* **The remainder lives on the order line.** Each shipment increments
  `SalesOrderLine.shipped_quantity`, so an order fulfilled in two shipments reports
  what is left without re-summing the ledger — and shipping more than the remainder is
  refused, which is the same fact stated as a rule.
* **A line that names no stock item is not shipped.** A freight or service line moves
  no stock, so a caller ships the lines that do; naming a line with no item is refused
  and says why, exactly as T-2.PROC.09's receiving does.

Nothing here raises an invoice: the sale's financial document is T-3.AR.01's, and the
invoice references the shipment rather than issuing the stock a second time.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

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

from app.company import company_base_currency
from app.db import Base
from app.sales.orders import CONFIRMED, SalesOrder, SalesOrderLine
from app.stock.entries import MONEY, StockLedgerEntry
from app.stock.items import Item
from app.stock.locations import Location
from app.stock.transactions import issue

# The document type a shipment's stock movements are filed under. T-1.INV.05 maps it to
# the `stock_issue` account key, which the chart of accounts points at cost of sales —
# so the cost side of the sale lands there without this module naming an account.
DOC_TYPE = "stock_issue"


class FulfilmentError(ValueError):
    """Fulfilment refused what was asked of it."""


class OrderNotConfirmed(FulfilmentError):
    """Only a confirmed order is picked or shipped."""


class DuplicatePickListError(FulfilmentError):
    """That order already has a pick list, or that pick-list number is taken."""


class DuplicateShipmentError(FulfilmentError):
    """That shipment number is taken in this company."""


class UnknownOrderLine(FulfilmentError):
    """The order has no such line."""


class OverShipmentError(FulfilmentError):
    """More was shipped on a line than the order still owes on it."""


class NothingToIssueError(FulfilmentError):
    """The line names no stock item, so there is nothing to issue from a location."""


class EmptyShipmentError(FulfilmentError):
    """A shipment with nothing on it is not a shipment."""


def _quantised(value: Any) -> Decimal:
    """A quantity as an exact decimal, refusing what is not a number."""
    if isinstance(value, float):
        raise FulfilmentError(
            f"a quantity is an exact decimal or a string, not the float {value!r}"
        )
    try:
        return Decimal(str(value).strip())
    except Exception as exc:  # noqa: BLE001 — the refusal is the point, not the type
        raise FulfilmentError(f"not a quantity: {value!r}") from exc


class PickList(Base):
    """What a picker is given: the confirmed order's lines, copied once."""

    __tablename__ = "pick_list"
    __table_args__ = (
        UniqueConstraint("company_id", "number", name="uq_pick_list_company_number"),
        # One pick list per order: a second one would be a second set of instructions
        # for the same goods, and the two would disagree the moment one was picked.
        UniqueConstraint("order_id", name="uq_pick_list_order"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sales_order.id"), nullable=False, index=True
    )
    number: Mapped[str] = mapped_column(String(32), nullable=False)
    created_on: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    order: Mapped[SalesOrder] = relationship()
    lines: Mapped[list["PickListLine"]] = relationship(
        back_populates="pick_list", order_by="PickListLine.line_no"
    )


class PickListLine(Base):
    """One ordered line as the picker sees it: how many to pick, and how many were."""

    __tablename__ = "pick_list_line"
    __table_args__ = (
        UniqueConstraint("pick_list_id", "line_no", name="uq_pick_list_line_no"),
        UniqueConstraint(
            "pick_list_id", "order_line_id", name="uq_pick_list_line_once"
        ),
        CheckConstraint("line_no >= 1", name="ck_pick_list_line_starts_at_one"),
        CheckConstraint("quantity > 0", name="ck_pick_list_line_quantity"),
        CheckConstraint(
            "picked_quantity >= 0", name="ck_pick_list_line_picked_not_negative"
        ),
        CheckConstraint(
            "picked_quantity <= quantity", name="ck_pick_list_line_not_over_picked"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    pick_list_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pick_list.id"), nullable=False, index=True
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    order_line_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sales_order_line.id"), nullable=False, index=True
    )
    item_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("item.id"), index=True)
    quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    uom: Mapped[str] = mapped_column(String(16), nullable=False)
    picked_quantity: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, default=Decimal(0)
    )

    pick_list: Mapped[PickList] = relationship(back_populates="lines")
    order_line: Mapped[SalesOrderLine] = relationship()


class Shipment(Base):
    """What left the warehouse: one warehouse, one day, and the lines it carried."""

    __tablename__ = "shipment"
    __table_args__ = (
        UniqueConstraint("company_id", "number", name="uq_shipment_company_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sales_order.id"), nullable=False, index=True
    )
    number: Mapped[str] = mapped_column(String(32), nullable=False)
    # Where the goods were issued from: a leaf location, checked by the stock
    # transaction itself rather than trusted here.
    location_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("location.id"), nullable=False, index=True
    )
    shipped_on: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    order: Mapped[SalesOrder] = relationship()
    location: Mapped[Location] = relationship()
    lines: Mapped[list["ShipmentLine"]] = relationship(
        back_populates="shipment", order_by="ShipmentLine.line_no"
    )


class ShipmentLine(Base):
    """One ordered line as it was shipped, and the movement that issued it."""

    __tablename__ = "shipment_line"
    __table_args__ = (
        UniqueConstraint("shipment_id", "line_no", name="uq_shipment_line_no"),
        UniqueConstraint(
            "shipment_id", "order_line_id", name="uq_shipment_line_once"
        ),
        CheckConstraint("line_no >= 1", name="ck_shipment_line_starts_at_one"),
        CheckConstraint("quantity > 0", name="ck_shipment_line_quantity"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    shipment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shipment.id"), nullable=False, index=True
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    order_line_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sales_order_line.id"), nullable=False, index=True
    )
    quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    uom: Mapped[str] = mapped_column(String(16), nullable=False)
    # The issue that carried this line out, so the document and its stock effect are
    # findable from each other rather than matched up by hand.
    movement_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("stock_ledger_entry.id"), index=True
    )

    shipment: Mapped[Shipment] = relationship(back_populates="lines")
    order_line: Mapped[SalesOrderLine] = relationship()
    movement: Mapped[StockLedgerEntry | None] = relationship()


def remaining_quantity(line: SalesOrderLine) -> Decimal:
    """How much of one ordered line is still to ship — what a second shipment may take."""
    return (line.quantity - line.shipped_quantity).quantize(Decimal("0.000001"))


def pick_list_for(session: Session, order: SalesOrder) -> PickList | None:
    """The order's pick list, or ``None`` — asked, not refused."""
    return session.scalar(select(PickList).where(PickList.order_id == order.id))


def pick_lines(session: Session, pick_list: PickList) -> list[PickListLine]:
    """The pick list's lines, in the order's own order."""
    return list(
        session.scalars(
            select(PickListLine)
            .where(PickListLine.pick_list_id == pick_list.id)
            .order_by(PickListLine.line_no)
        )
    )


def shipments_for(session: Session, order: SalesOrder) -> list[Shipment]:
    """Every shipment raised against an order, oldest first."""
    return list(
        session.scalars(
            select(Shipment)
            .where(Shipment.order_id == order.id)
            .order_by(Shipment.created_at, Shipment.number)
        )
    )


def generate_pick_list(
    session: Session,
    order: SalesOrder,
    *,
    number: str,
    on: date | None = None,
) -> PickList:
    """Give a confirmed order its pick list — exactly its lines, once.

    A draft is refused: nothing has been agreed to pick yet, and a pick list drawn from
    an order that may still change is instructions to fetch the wrong goods. The lines
    are **copied** rather than referenced so the picker's paper, the order and the
    eventual shipment can be compared line by line.
    """
    if order.status != CONFIRMED:
        raise OrderNotConfirmed(
            f"sales order {order.number!r} is {order.status!r}; only a confirmed order"
            f" is picked (T-3.SALES.05)"
        )
    wanted = str(number or "").strip()
    if not wanted:
        raise FulfilmentError("a pick list number is required")
    if pick_list_for(session, order) is not None:
        raise DuplicatePickListError(
            f"sales order {order.number!r} already has a pick list (T-3.SALES.05)"
        )
    clash = session.scalar(
        select(PickList).where(
            PickList.company_id == order.company_id, PickList.number == wanted
        )
    )
    if clash is not None:
        raise DuplicatePickListError(
            f"pick list {wanted!r} already exists in this company"
        )
    listed = PickList(
        company_id=order.company_id,
        order_id=order.id,
        number=wanted,
        created_on=on or datetime.now(timezone.utc).date(),
    )
    session.add(listed)
    session.flush()
    for line in order.lines:
        session.add(
            PickListLine(
                company_id=order.company_id,
                pick_list_id=listed.id,
                line_no=line.line_no,
                order_line_id=line.id,
                item_id=line.item_id,
                quantity=line.quantity,
                uom=line.uom,
            )
        )
    session.flush()
    return listed


def record_picked(
    session: Session,
    pick_list: PickList,
    *,
    line_no: int,
    quantity: Any,
) -> PickListLine:
    """Record how much of one line was actually picked off the shelf.

    A whole number of the line's own quantity at most: over-picking is refused rather
    than capped, because silently reducing it would put a figure on the paper that the
    picker did not report.
    """
    wanted = _quantised(quantity)
    if wanted < 0:
        raise FulfilmentError(f"a picked quantity is not negative: {wanted}")
    line = _pick_line(session, pick_list, line_no)
    if wanted > line.quantity:
        raise FulfilmentError(
            f"line {line_no} asks for {line.quantity}; {wanted} was picked, which is"
            " more than the order says (T-3.SALES.05)"
        )
    line.picked_quantity = wanted.quantize(Decimal("0.000001"))
    session.flush()
    return line


def _pick_line(session: Session, pick_list: PickList, line_no: int) -> PickListLine:
    line = session.scalar(
        select(PickListLine).where(
            PickListLine.pick_list_id == pick_list.id,
            PickListLine.line_no == line_no,
        )
    )
    if line is None:
        raise UnknownOrderLine(
            f"pick list {pick_list.number!r} has no line {line_no}"
        )
    return line


def _order_line(order: SalesOrder, line_no: int) -> SalesOrderLine:
    for line in order.lines:
        if line.line_no == line_no:
            return line
    raise UnknownOrderLine(
        f"sales order {order.number!r} has no line {line_no}"
    )


def ship_order(
    session: Session,
    order: SalesOrder,
    *,
    number: str,
    warehouse: Location,
    lines: Iterable[tuple[int, Any]],
    on: date | None = None,
) -> Shipment:
    """Ship what is named of a confirmed order, issuing it out of `warehouse`.

    Each `(line_no, quantity)` is checked against what the order still owes **on that
    line**, so an order fulfilled in two shipments cannot hand out the same quantity
    twice. The stock issue is T-1.INV.05's `issue`: it values the goods by the company's
    costing method, refuses to take the location negative, writes the stock ledger entry
    and posts the cost side to the ledger — so this module states *what* left and never
    *what it is worth*.
    """
    if order.status != CONFIRMED:
        raise OrderNotConfirmed(
            f"sales order {order.number!r} is {order.status!r}; only a confirmed order"
            f" is shipped (T-3.SALES.05)"
        )
    wanted = str(number or "").strip()
    if not wanted:
        raise FulfilmentError("a shipment number is required")
    asked = [(no, _quantised(quantity)) for no, quantity in lines]
    if not asked:
        raise EmptyShipmentError(
            f"shipment {wanted!r} names no lines, so nothing would leave the warehouse"
        )
    clash = session.scalar(
        select(Shipment).where(
            Shipment.company_id == order.company_id, Shipment.number == wanted
        )
    )
    if clash is not None:
        raise DuplicateShipmentError(
            f"shipment {wanted!r} already exists in this company"
        )

    currency = company_base_currency(session, company_id=order.company_id)
    shipment = Shipment(
        company_id=order.company_id,
        order_id=order.id,
        number=wanted,
        location_id=warehouse.id,
        shipped_on=on or datetime.now(timezone.utc).date(),
    )
    session.add(shipment)
    session.flush()

    for line_no, quantity in asked:
        line = _order_line(order, line_no)
        if line.item_id is None:
            raise NothingToIssueError(
                f"line {line_no} of order {order.number!r} names no stock item — a"
                " service is not issued out of a location (T-3.SALES.05)"
            )
        if quantity <= 0:
            raise FulfilmentError(
                f"line {line_no} is shipped as {quantity}; a shipment carries a"
                " positive quantity"
            )
        left = remaining_quantity(line)
        if quantity > left:
            raise OverShipmentError(
                f"line {line_no} still owes {left} of {line.quantity}; shipping"
                f" {quantity} would ship more than the order says (T-3.SALES.05)"
            )
        entry = issue(
            session,
            item=session.get(Item, line.item_id),
            location=warehouse,
            uom=line.uom,
            quantity=quantity,
            currency=currency,
            source_type=DOC_TYPE,
            source_id=shipment.id,
            posting_date=shipment.shipped_on,
        )
        shipped = ShipmentLine(
            company_id=order.company_id,
            shipment_id=shipment.id,
            line_no=line.line_no,
            order_line_id=line.id,
            quantity=quantity,
            uom=line.uom,
            movement_id=entry.id,
        )
        session.add(shipped)
        line.shipped_quantity = (
            line.shipped_quantity + quantity
        ).quantize(Decimal("0.000001"))
    session.flush()
    return shipment
