"""T-1.INV.03 — the stock ledger: one append-only row per movement.

§2.2's "Stock Ledger & Valuation" and §1 principle 5 ("real-time valuation and
stock ledgers"), with DOMAIN-MODELS.md §7 fixing the shape. Four decisions:

* **One row per movement, and never edited.** The table is append-only
  (T-0.AUDIT.01), so a correction is another movement, and an item's on-hand
  quantity and value are the **sum** of its rows — Phase 1 keeps no balance table,
  because a stored balance is a second truth that can disagree with the ledger.
* **Signed, in the item's base UOM.** Positive means received into the location,
  negative issued out of it (via UOM conversion, T-1.INV.01). The *sum* may not go
  below zero, which T-1.INV.05's issue refuses — this table records what happened.
* **The location is a leaf.** A movement against a warehouse or a zone is refused
  here, using T-1.INV.02's level rule, so stock always sits somewhere a picker can
  walk to.
* **A movement names its document.** `(source_type, source_id)` are both required
  (NOT NULL in the table *and* refused by the recorder), because a stocked quantity
  nobody can trace to a document is a quantity nobody can explain.

`batch_id` and `serial_id` are the two columns §7 fixes here — their presence in
the entry's shape is why traceability moved into Phase 1 — and T-1.INV.08 /
T-1.INV.09 own what fills them.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DDL,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Uuid,
    event,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.audit import append_only
from app.db import Base
from app.stock.items import Item, ItemError, ItemVariant, TraceabilityError
from app.stock.locations import Location, require_leaf

# One scale for quantities and values, the same one money uses.
MONEY = Numeric(20, 6)


class MovementError(ItemError):
    """The movement cannot be written as stated."""


class MissingSourceError(MovementError):
    """A movement without the document that caused it — refused, never guessed."""


class StockLedgerEntry(Base):
    """One movement of one item at one location: quantity and value, signed."""

    __tablename__ = "stock_ledger_entry"
    __table_args__ = (
        # A movement moved something.
        CheckConstraint("quantity <> 0", name="ck_stock_entry_moves_something"),
        # ...and its value does not fight the direction it moved in.
        CheckConstraint(
            "(quantity > 0 AND value >= 0) OR (quantity < 0 AND value <= 0)",
            name="ck_stock_entry_value_direction",
        ),
        # A movement names the document that caused it (§7).
        CheckConstraint(
            "source_type IS NOT NULL AND source_type <> '' AND source_id IS NOT NULL",
            name="ck_stock_entry_source",
        ),
        CheckConstraint("char_length(currency) = 3", name="ck_stock_entry_currency"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("item.id"), nullable=False, index=True)
    # Required when the item has variants (§7): which one moved.
    variant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("item_variant.id"), index=True
    )
    # A leaf (§6): where it moved.
    location_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("location.id"), nullable=False, index=True
    )
    # Signed, in the item's base UOM.
    quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    # Signed the same way, stated in `currency` (the transaction currency).
    value: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    # Traceability (T-1.INV.08 / T-1.INV.09 own the tables): which lot, which unit.
    # Required exactly when the item's `traceability_mode` says so, which is what
    # `record_movement` enforces below.
    batch_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("batch.id"), index=True
    )
    serial_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("serial.id"), index=True
    )
    # The document that caused the movement, and the date it counts from.
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    posting_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    item: Mapped[Item] = relationship()
    location: Mapped[Location] = relationship()


# A ledger: never updated, never deleted (T-0.AUDIT.01).
append_only(StockLedgerEntry.__table__)


# --- The leaf rule, at the storage boundary ----------------------------------
# T-1.INV.02's level rule says stock sits in a bin. The recorder asks for the leaf
# before it writes; this trigger is the rule, so a writer that bypasses the
# recorder — raw SQL, a restored dump — cannot put stock in a warehouse.
# '%%' because SQLAlchemy's DDL wrapper interpolates the statement.
_LEAF_FUNCTION = DDL(
    """
CREATE OR REPLACE FUNCTION stock_entry_leaf_only() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    held_type text;
    held_code text;
BEGIN
    SELECT type, code INTO held_type, held_code FROM location WHERE id = NEW.location_id;
    IF held_type IS NULL THEN
        RAISE EXCEPTION 'stock movement names location %% which does not exist',
            NEW.location_id;
    END IF;
    IF held_type <> 'bin' THEN
        RAISE EXCEPTION
            'stock movement names %% which is a %%: stock is held in a bin',
            held_code, held_type;
    END IF;
    -- BEFORE INSERT: returning NEW lets the row through; returning NULL would
    -- silently discard it, which is not what this trigger is for.
    RETURN NEW;
END;
$$;
"""
)

_LEAF_TRIGGER = DDL(
    """
CREATE TRIGGER stock_entry_leaf_only
    BEFORE INSERT ON stock_ledger_entry
    FOR EACH ROW EXECUTE FUNCTION stock_entry_leaf_only();
"""
)

for _ddl in (_LEAF_FUNCTION, _LEAF_TRIGGER):
    event.listen(StockLedgerEntry.__table__, "after_create", _ddl)


# The two tables the traceability columns point at (T-1.INV.08 / T-1.INV.09) are
# imported here on purpose: the columns are part of the entry's shape (§7), so a
# caller that builds a schema from this module alone must still resolve the foreign
# keys rather than meet "no such table: batch".
from app.stock import batches as _batches  # noqa: E402,F401
from app.stock import serials as _serials  # noqa: E402,F401


def record_movement(
    session: Session,
    *,
    item: Item,
    location: Location,
    quantity: Any,
    value: Any,
    currency: str,
    source_type: str,
    source_id: uuid.UUID,
    posting_date: date,
    variant_id: uuid.UUID | None = None,
    batch_id: uuid.UUID | None = None,
    serial_id: uuid.UUID | None = None,
    move_serial: bool = True,
) -> StockLedgerEntry:
    """Write one movement — the only way into the stock ledger.

    The quantity is already in the item's base UOM (the transactions of T-1.INV.05
    convert it at their own edge) and is **signed by the transaction**, not by this
    function: a receipt passes a positive quantity, an issue a negative one.
    """
    require_leaf(session, location)
    if item.company_id != location.company_id:
        raise MovementError(f"item {item.sku!r} and location {location.code!r} differ in company")
    if not str(source_type or "").strip():
        raise MissingSourceError(
            "a stock movement names the document that caused it (source_type);"
            " an unexplained movement cannot be reconciled later"
        )
    if source_id is None:
        raise MissingSourceError(
            "a stock movement names its document's id (source_id) as well as its type"
        )
    moved = quantity if isinstance(quantity, Decimal) else Decimal(str(quantity))
    valued = value if isinstance(value, Decimal) else Decimal(str(value))
    if moved == 0:
        raise MovementError("a movement of zero is not a movement")
    if (moved > 0) != (valued >= 0):
        raise MovementError(
            f"a movement of {moved} cannot be valued {valued}: the value keeps the"
            " quantity's direction"
        )

    # The item's traceability mode decides what identity the movement must carry
    # (T-1.INV.08 / T-1.INV.09): a batch-tracked item moves by lot, a serial-tracked
    # item one unit at a time, and an untracked item carries neither.
    mode = str(item.traceability_mode)
    if mode == "batch_lot" and batch_id is None:
        raise TraceabilityError(
            f"{item.sku!r} is tracked by batch/lot, so every movement names its batch"
        )
    if mode == "batch_lot" and serial_id is not None:
        raise TraceabilityError(
            f"{item.sku!r} is tracked by batch/lot, so a movement carries no serial"
        )
    if mode == "serial" and serial_id is None:
        raise TraceabilityError(
            f"{item.sku!r} is tracked by serial, so every movement names its unit"
        )
    if mode == "serial" and batch_id is not None:
        raise TraceabilityError(
            f"{item.sku!r} is tracked by serial, so a movement carries no batch"
        )
    if mode == "none" and (batch_id is not None or serial_id is not None):
        raise TraceabilityError(
            f"{item.sku!r} is tracked as 'none', so a movement carries no batch or serial"
        )
    if mode == "serial" and abs(moved) != 1:
        raise TraceabilityError(
            f"a serial-tracked item moves one unit at a time, got {moved}"
        )
    if variant_id is not None:
        variant = session.get(ItemVariant, variant_id)
        if variant is None or variant.company_id != item.company_id or variant.item_id != item.id:
            raise TraceabilityError(f"variant {variant_id} belongs to another item")
    serial = None
    if batch_id is not None:
        from app.stock.batches import Batch

        batch = session.get(Batch, batch_id)
        if batch is None or batch.company_id != item.company_id or batch.item_id != item.id:
            raise TraceabilityError(f"batch {batch_id} belongs to another item")
    if serial_id is not None:
        from app.stock.serials import Serial

        serial = session.get(Serial, serial_id)
        if serial is None or serial.company_id != item.company_id or serial.item_id != item.id:
            raise TraceabilityError(f"serial {serial_id} belongs to another item")

    entry = StockLedgerEntry(
        company_id=item.company_id,
        item_id=item.id,
        variant_id=variant_id,
        location_id=location.id,
        quantity=moved,
        value=valued,
        currency=str(currency).strip().upper(),
        batch_id=batch_id,
        serial_id=serial_id,
        source_type=str(source_type),
        source_id=source_id,
        posting_date=posting_date,
    )
    session.add(entry)
    session.flush()
    # A serial moves with the unit it identifies (T-1.INV.09): the ledger says the
    # unit moved, the serial row says where to.
    if serial is not None and move_serial:
        from app.stock.serials import place_serial

        place_serial(
            session,
            serial=serial,
            location_id=location.id,
            quantity_sign=1 if moved > 0 else -1,
        )
    return entry


def on_hand(
    session: Session,
    *,
    company_id: uuid.UUID,
    item_id: uuid.UUID | None = None,
    location_id: uuid.UUID | None = None,
    variant_id: uuid.UUID | None = None,
    batch_id: uuid.UUID | None = None,
    serial_id: uuid.UUID | None = None,
    as_of: date | None = None,
) -> dict[str, Decimal]:
    """The sum of the ledger for what was asked: quantity and value on hand.

    Every argument is an optional narrowing — an item, a location, a variant, a
    batch, a date — and the answer is the ledger's own sum, which is the only
    source of truth Phase 1 keeps.
    """
    statement = select(
        func.coalesce(func.sum(StockLedgerEntry.quantity), 0),
        func.coalesce(func.sum(StockLedgerEntry.value), 0),
    ).where(StockLedgerEntry.company_id == company_id)
    if item_id is not None:
        statement = statement.where(StockLedgerEntry.item_id == item_id)
    if location_id is not None:
        statement = statement.where(StockLedgerEntry.location_id == location_id)
    if variant_id is not None:
        statement = statement.where(StockLedgerEntry.variant_id == variant_id)
    if batch_id is not None:
        statement = statement.where(StockLedgerEntry.batch_id == batch_id)
    if serial_id is not None:
        statement = statement.where(StockLedgerEntry.serial_id == serial_id)
    if as_of is not None:
        statement = statement.where(StockLedgerEntry.posting_date <= as_of)
    quantity, value = session.execute(statement).one()
    return {"quantity": Decimal(quantity), "value": Decimal(value)}


def movements_for_source(
    session: Session, *, company_id: uuid.UUID, source_type: str, source_id: uuid.UUID
) -> list[StockLedgerEntry]:
    """Every movement one document caused, oldest first — the drill-down."""
    return list(
        session.scalars(
            select(StockLedgerEntry)
            .where(
                StockLedgerEntry.company_id == company_id,
                StockLedgerEntry.source_type == str(source_type),
                StockLedgerEntry.source_id == source_id,
            )
            .order_by(StockLedgerEntry.created_at)
        )
    )


def movements(
    session: Session,
    *,
    company_id: uuid.UUID,
    item_id: uuid.UUID | None = None,
    location_id: uuid.UUID | None = None,
    variant_id: uuid.UUID | None = None,
    batch_id: uuid.UUID | None = None,
    serial_id: uuid.UUID | None = None,
    as_of: date | None = None,
) -> list[StockLedgerEntry]:
    """The ledger rows for an item and/or location, oldest first — what valuation reads."""
    statement = (
        select(StockLedgerEntry)
        .where(StockLedgerEntry.company_id == company_id)
        .order_by(StockLedgerEntry.posting_date, StockLedgerEntry.created_at)
    )
    if item_id is not None:
        statement = statement.where(StockLedgerEntry.item_id == item_id)
    if location_id is not None:
        statement = statement.where(StockLedgerEntry.location_id == location_id)
    if variant_id is not None:
        statement = statement.where(StockLedgerEntry.variant_id == variant_id)
    if batch_id is not None:
        statement = statement.where(StockLedgerEntry.batch_id == batch_id)
    if serial_id is not None:
        statement = statement.where(StockLedgerEntry.serial_id == serial_id)
    if as_of is not None:
        statement = statement.where(StockLedgerEntry.posting_date <= as_of)
    return list(session.scalars(statement))
