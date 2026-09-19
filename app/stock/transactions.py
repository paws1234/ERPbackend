"""T-1.INV.05 — the Phase 1 stock transactions: receipt, issue and transfer.

§2.2's "Stock Transactions (Receipt, Issue, Transfer, Reconciliation)" and §4's
Phase 1 bullet 4. Each one writes stock ledger entries (T-1.INV.03) and nothing
else — the GL side is T-1.INV.07's, deliberately, so this module can be read
without holding the whole posting story in mind at once.

What each transaction guarantees:

* **The quantity arrives in the caller's UOM and is stored in the item's base
  UOM** (T-1.INV.01's `convert_quantity`), so every stored quantity is comparable
  without re-reading the factors (§5.4).
* **A receipt carries its cost, an issue is priced by the valuation engine, and a
  transfer carries the value across** — the three cases the second half of §2.2
  describes. The engine prices an issue because a caller dividing a total by a
  quantity is exactly how a value drifts.
* **Stock never goes negative** (`allow_negative_stock` is `false`, decided
  2026-09-17): an issue or a transfer out of a location that does not hold the
  quantity is refused before anything is written.
* **Each transaction is atomic with the caller's document.** Nothing here commits:
  the entries flush into the caller's transaction, so a document that fails after
  moving stock leaves no movement behind.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.stock.entries import MovementError, StockLedgerEntry, on_hand, record_movement
from app.stock.gl_posting import post_movement_to_gl
from app.stock.items import Item, ItemVariant
from app.stock.locations import Location, require_leaf
from app.stock.valuation import value_issue


class TransactionError(MovementError):
    """The stock transaction refused what was asked of it."""


class InsufficientStockError(TransactionError):
    """The location does not hold the quantity (negative stock is never allowed)."""


def _value(value: Any) -> Decimal:
    stated = value if isinstance(value, Decimal) else Decimal(str(value))
    if stated < 0:
        raise TransactionError(f"a receipt's value cannot be negative, got {stated}")
    return stated


def _stock_lock_key(
    *,
    item: Item,
    location: Location,
    variant: ItemVariant | None = None,
    batch=None,
    serial=None,
) -> int:
    digest = hashlib.blake2b(
        "|".join(
            (
                str(item.company_id),
                str(item.id),
                str(location.id),
                str(variant.id if variant is not None else ""),
                str(batch.id if batch is not None else ""),
                str(serial.id if serial is not None else ""),
            )
        ).encode(),
        digest_size=8,
    ).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


def _lock_stock(
    session: Session,
    *,
    item: Item,
    location: Location,
    variant: ItemVariant | None = None,
    batch=None,
    serial=None,
) -> None:
    if session.get_bind().dialect.name != "postgresql":
        return
    session.execute(
        select(
            func.pg_advisory_xact_lock(
                _stock_lock_key(
                    item=item, location=location, variant=variant, batch=batch, serial=serial
                )
            )
        )
    ).one()


def _lock_transfer(
    session: Session,
    *,
    item: Item,
    from_location: Location,
    to_location: Location,
    variant: ItemVariant | None = None,
    batch=None,
    serial=None,
) -> None:
    if session.get_bind().dialect.name != "postgresql":
        return
    keys = sorted(
        {
            _stock_lock_key(
                item=item,
                location=from_location,
                variant=variant,
                batch=batch,
                serial=serial,
            ),
            _stock_lock_key(
                item=item,
                location=to_location,
                variant=variant,
                batch=batch,
                serial=serial,
            ),
        }
    )
    for key in keys:
        session.execute(select(func.pg_advisory_xact_lock(key))).one()


def _held_quantity(
    session: Session,
    *,
    item: Item,
    location: Location,
    variant: ItemVariant | None = None,
    batch=None,
    serial=None,
) -> Decimal:
    if serial is not None and (
        getattr(serial, "location_id", None) != location.id or getattr(serial, "status", None) != "in_stock"
    ):
        raise InsufficientStockError(
            f"serial {serial.code!r} is not in stock at {location.code!r}"
        )
    return on_hand(
        session,
        company_id=item.company_id,
        item_id=item.id,
        location_id=location.id,
        variant_id=variant.id if variant is not None else None,
        batch_id=batch.id if batch is not None else None,
        serial_id=serial.id if serial is not None else None,
    )["quantity"]


def receive(
    session: Session,
    *,
    item: Item,
    location: Location,
    uom: str,
    quantity: Any,
    value: Any,
    currency: str,
    source_type: str,
    source_id: uuid.UUID,
    posting_date: date,
    variant: ItemVariant | None = None,
    batch=None,
    serial=None,
) -> StockLedgerEntry:
    """Receive stock into a location, with the value it cost.

    `quantity` is in `uom` — as many `uom` as arrived — and `value` is what they
    cost in total, stated in `currency` (DOMAIN-MODELS.md §7's input). The ledger
    stores the quantity in the item's base UOM and the value as stated. `batch` and
    `serial` carry the identity a tracked item needs (T-1.INV.08 / T-1.INV.09).
    """
    from app.stock.items import convert_quantity

    given = quantity if isinstance(quantity, Decimal) else Decimal(str(quantity))
    if given <= 0:
        raise TransactionError(f"a receipt takes a positive quantity, got {given}")
    _lock_stock(session, item=item, location=location, variant=variant, batch=batch, serial=serial)
    moved = convert_quantity(session, item, quantity=given, from_uom=uom, to_uom=item.base_uom)
    entry = record_movement(
        session,
        item=item,
        location=location,
        quantity=moved,
        value=_value(value),
        currency=currency,
        source_type=source_type,
        source_id=source_id,
        posting_date=posting_date,
        variant_id=variant.id if variant is not None else None,
        batch_id=batch.id if batch is not None else None,
        serial_id=serial.id if serial is not None else None,
    )
    # The GL half of the same document (T-1.INV.07), in the caller's transaction.
    post_movement_to_gl(session, entry=entry)
    return entry


def issue(
    session: Session,
    *,
    item: Item,
    location: Location,
    uom: str,
    quantity: Any,
    currency: str,
    source_type: str,
    source_id: uuid.UUID,
    posting_date: date,
    variant: ItemVariant | None = None,
    batch=None,
    serial=None,
    allow_expired: bool = False,
    actor: str | None = None,
) -> StockLedgerEntry:
    """Issue stock out of a location, valued by the costing method in force.

    Refused when the location does not hold the quantity: `allow_negative_stock` is
    `false`, so an issue that would take a location below zero is an error to fix,
    not a state to record. An **expired batch** is refused unless the caller
    explicitly overrides it, naming the actor who took that decision
    (T-1.INV.08).
    """
    from app.stock.batches import require_usable
    from app.stock.items import convert_quantity

    given = quantity if isinstance(quantity, Decimal) else Decimal(str(quantity))
    if given <= 0:
        raise TransactionError(f"an issue takes a positive quantity, got {given}")
    if batch is not None:
        require_usable(
            session, batch, on=posting_date, allow_expired=allow_expired, actor=actor
        )
    _lock_stock(session, item=item, location=location, variant=variant, batch=batch, serial=serial)
    moved = convert_quantity(session, item, quantity=given, from_uom=uom, to_uom=item.base_uom)
    held = _held_quantity(
        session,
        item=item,
        location=location,
        variant=variant,
        batch=batch,
        serial=serial,
    )
    if moved > held:
        raise InsufficientStockError(
            f"{location.code} holds {held} of {item.sku!r}; issuing {moved} would take it"
            " negative, which this platform never allows"
        )
    cost = value_issue(
        session,
        company_id=item.company_id,
        item=item,
        quantity=moved,
        location_id=location.id,
        variant_id=variant.id if variant is not None else None,
        batch_id=batch.id if batch is not None else None,
        serial_id=serial.id if serial is not None else None,
    )
    entry = record_movement(
        session,
        item=item,
        location=location,
        quantity=-moved,
        value=-cost,
        currency=currency,
        source_type=source_type,
        source_id=source_id,
        posting_date=posting_date,
        variant_id=variant.id if variant is not None else None,
        batch_id=batch.id if batch is not None else None,
        serial_id=serial.id if serial is not None else None,
    )
    post_movement_to_gl(session, entry=entry)
    return entry


def transfer(
    session: Session,
    *,
    item: Item,
    from_location: Location,
    to_location: Location,
    uom: str,
    quantity: Any,
    currency: str,
    source_type: str,
    source_id: uuid.UUID,
    posting_date: date,
    variant: ItemVariant | None = None,
    batch=None,
    serial=None,
) -> tuple[StockLedgerEntry, StockLedgerEntry]:
    """Move stock between two bins, with the value going along.

    Two entries, one document: the quantity and the value leave one location and
    arrive at the other, so the item's total quantity and total value are
    unchanged — which is what makes a transfer a move rather than a revaluation.
    """
    require_leaf(session, from_location)
    require_leaf(session, to_location)
    if from_location.id == to_location.id:
        raise TransactionError(
            f"a transfer moves stock between two locations; both are {from_location.code}"
        )
    from app.stock.items import convert_quantity

    given = quantity if isinstance(quantity, Decimal) else Decimal(str(quantity))
    if given <= 0:
        raise TransactionError(f"a transfer takes a positive quantity, got {given}")
    _lock_transfer(
        session,
        item=item,
        from_location=from_location,
        to_location=to_location,
        variant=variant,
        batch=batch,
        serial=serial,
    )
    moved = convert_quantity(session, item, quantity=given, from_uom=uom, to_uom=item.base_uom)
    held = _held_quantity(
        session,
        item=item,
        location=from_location,
        variant=variant,
        batch=batch,
        serial=serial,
    )
    if moved > held:
        raise InsufficientStockError(
            f"{from_location.code} holds {held} of {item.sku!r}; transferring {moved}"
            " would take it negative"
        )
    cost = value_issue(
        session,
        company_id=item.company_id,
        item=item,
        quantity=moved,
        location_id=from_location.id,
        variant_id=variant.id if variant is not None else None,
        batch_id=batch.id if batch is not None else None,
        serial_id=serial.id if serial is not None else None,
    )
    out = record_movement(
        session,
        item=item,
        location=from_location,
        quantity=-moved,
        value=-cost,
        currency=currency,
        source_type=source_type,
        source_id=source_id,
        posting_date=posting_date,
        variant_id=variant.id if variant is not None else None,
        batch_id=batch.id if batch is not None else None,
        serial_id=serial.id if serial is not None else None,
        move_serial=False,
    )
    into = record_movement(
        session,
        item=item,
        location=to_location,
        quantity=moved,
        value=cost,
        currency=currency,
        source_type=source_type,
        source_id=source_id,
        posting_date=posting_date,
        variant_id=variant.id if variant is not None else None,
        batch_id=batch.id if batch is not None else None,
        serial_id=serial.id if serial is not None else None,
    )
    # Both halves post: the value leaves the inventory account and arrives back in
    # it, so the transfer is traceable in the GL and the inventory total is unmoved.
    post_movement_to_gl(session, entry=out)
    post_movement_to_gl(session, entry=into)
    return out, into
