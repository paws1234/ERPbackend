"""T-1.INV.09 — serial identity: one row per unit, and where it is.

§2.2's serial tracking, moved into Phase 1 with batch/lot on 2026-09-17. DOMAIN-MODELS.md
§7 fixed the `serial_id` column; this module owns the table and the lifecycle:

* **One row per unit, and the unit is in one place.** A serial-tracked item's
  quantity therefore equals its count of serials in stock, which is the property
  the Phase 1 exit criteria name — and the reason a movement of a serial-tracked
  item is exactly one unit long (T-1.INV.03's recorder refuses anything else).
* **Receiving, transferring and issuing transition the serial's status**, so "where
  is unit 0007 and what happened to it" is a row rather than a reconstruction.
* **A code repeats only across items.** A duplicate within one item is refused
  (unique per item); the same value on another item is a different thing, and
  allowed.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    Uuid,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.audit import SoftDeleteMixin, deny_hard_delete
from app.db import Base
from app.stock.items import Item, ItemError

# What a serial can be: registered but not yet received, in the warehouse, or gone.
UNRECEIVED, IN_STOCK, ISSUED = "unreceived", "in_stock", "issued"


class SerialError(ItemError):
    """The serial could not be used as asked."""


class UnknownSerialError(SerialError):
    """No such serial (or no such code) for this item."""


class DuplicateSerialError(SerialError):
    """That serial already exists for this item."""


class Serial(SoftDeleteMixin, Base):
    """One identifiable unit of one item, and where it is now."""

    __tablename__ = "serial"
    __table_args__ = (
        # Unique per item: the same value on another item is a different unit.
        UniqueConstraint("item_id", "code", name="uq_serial_item_code"),
        CheckConstraint("status IN ('unreceived', 'in_stock', 'issued')", name="ck_serial_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("item.id"), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=UNRECEIVED)
    # Where it is, while it is in stock. Null once it has been issued.
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("location.id"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    item: Mapped[Item] = relationship()


# A master: a unit is retired by marking it, never by removing the row (a warranty
# claim or a return needs the row it points at).
deny_hard_delete(Serial.__table__)


def add_serial(session: Session, *, item: Item, code: str) -> Serial:
    """Register one unit of one item — refused if that unit already exists."""
    if str(item.traceability_mode) != "serial":
        raise SerialError(
            f"{item.sku!r} is tracked as {item.traceability_mode!r}, so it has no serials;"
            " set traceability_mode to 'serial' when the item is created (T-1.INV.01)"
        )
    existing = session.scalar(
        select(Serial).where(Serial.item_id == item.id, Serial.code == str(code))
    )
    if existing is not None:
        raise DuplicateSerialError(
            f"{item.sku!r} already has serial {code!r}; a unit is one row"
        )
    serial = Serial(company_id=item.company_id, item_id=item.id, code=str(code))
    session.add(serial)
    session.flush()
    return serial


def serial_by_code(session: Session, *, item: Item, code: str) -> Serial:
    """The live serial with that code for that item, or a refusal."""
    serial = session.scalar(
        select(Serial).where(Serial.item_id == item.id, Serial.code == str(code))
    )
    if serial is None:
        raise UnknownSerialError(f"{item.sku!r} has no serial {code!r}")
    return serial


def place_serial(
    session: Session, *, serial: Serial, location_id: uuid.UUID | None, quantity_sign: int
) -> Serial:
    """Move a serial with the movement that carried it.

    Receiving puts it in stock at the named location and issuing takes it out. A
    transfer keeps the unit in stock by skipping the outbound state change and
    placing it on the inbound half. A unit that is already issued cannot be issued
    again, which is what keeps a serial in one place at one time.
    """
    if quantity_sign > 0:
        serial.location_id = location_id
        serial.status = IN_STOCK
    else:
        serial.location_id = None
        serial.status = ISSUED
    session.flush()
    return serial


def serials_in_stock(
    session: Session, *, item: Item, location_id: uuid.UUID | None = None
) -> list[Serial]:
    """The units of this item currently in stock, optionally at one location."""
    statement = select(Serial).where(
        Serial.item_id == item.id, Serial.status == IN_STOCK
    ).order_by(Serial.code)
    if location_id is not None:
        statement = statement.where(Serial.location_id == location_id)
    return list(session.scalars(statement))
