"""T-1.INV.01 — the item master: SKU, variants, barcodes and UOM conversion.

§2.2 asks for an "Item Master (SKU, variants, UOM conversion, barcode/QR)" and
DOMAIN-MODELS.md §5 fixes the four tables and their semantics. Four decisions this
module makes, all of them about *identity*:

* **A variant is not a second item.** An item carries a SKU; its variants carry
  their own SKUs and the attribute combination that tells them apart, unique per
  item — so a wider matrix never means duplicate SKUs (§5.2).
* **A barcode resolves to exactly one thing, system-wide.** The value is unique
  across the installation, not per company, so a scan is unambiguous (§5.3). An
  item may carry several codes — EAN/UPC **and** QR, the decided symbologies.
* **UOM conversion is per item and exact.** A row is `1 from_uom = factor ×
  to_uom`; the base UOM needs no row; a conversion walks the chain and multiplies
  rather than assuming transitivity. The reverse is a **division by the same
  factor**, never a stored rounded reciprocal — that is what makes a round trip
  return the quantity it started with (§5.4), where pre-rounding `1/factor` would
  drift a quantity by a rounding step on every pass (`1/12` cannot be written
  exactly at the quantity scale, but `12 ÷ 12` is exactly `1`).
* **`traceability_mode` has no default.** Plan §8 leaves it "Not stated", so
  creating an item states it rather than inheriting an invented `none`.

Nothing here moves stock: quantities are converted by
:func:`convert_quantity` and the ledger entry that stores them is T-1.INV.03's.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.audit import SoftDeleteMixin, deny_hard_delete, soft_delete
from app.db import Base

# How an item is tracked (§2.2's traceability, moved into Phase 1 on 2026-09-17).
TRACEABILITY_MODES = ("none", "batch_lot", "serial")

# The symbologies §8 decided: EAN/UPC and QR.
SYMBOLOGIES = ("ean", "upc", "qr")

# Quantities are exact at this scale — the same scale the stock ledger stores.
QUANTITY_SCALE = Decimal("0.000001")


class ItemError(ValueError):
    """The item master refused what was asked of it."""


class UnknownItemError(ItemError):
    """No such item (or no such SKU / barcode) in this company."""


class DuplicateItemError(ItemError):
    """A SKU or barcode that is already taken."""


class TraceabilityError(ItemError):
    """A traceability mode or attribute combination outside what the item allows."""


class ConversionError(ItemError):
    """A UOM conversion that cannot be stated exactly."""


class Item(SoftDeleteMixin, Base):
    """One stocked thing: the SKU, its base UOM and how it is tracked."""

    __tablename__ = "item"
    __table_args__ = (
        UniqueConstraint("company_id", "sku", name="uq_item_company_sku"),
        CheckConstraint(
            "traceability_mode IN ("
            + ", ".join(f"'{mode}'" for mode in TRACEABILITY_MODES)
            + ")",
            name="ck_item_traceability_mode",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # Every stored quantity is expressed in this UOM (DOMAIN-MODELS.md §5.4).
    base_uom: Mapped[str] = mapped_column(String(16), nullable=False)
    # `none` | `batch_lot` | `serial` — **no default**: §8 leaves it "Not stated",
    # so an item states it, and T-1.INV.08/.09 enforce what it means per movement.
    traceability_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    # The standard cost, where the company's costing method is Standard Cost
    # (T-1.INV.04 owns the method and refuses to value without this).
    standard_cost: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))

    variants: Mapped[list[ItemVariant]] = relationship(back_populates="item")
    barcodes: Mapped[list[ItemBarcode]] = relationship(back_populates="item")
    conversions: Mapped[list[UomConversion]] = relationship(back_populates="item")

    @property
    def is_tracked(self) -> bool:
        return self.traceability_mode != "none"


class ItemVariant(SoftDeleteMixin, Base):
    """One attribute combination of an item — what is actually stocked and sold."""

    __tablename__ = "item_variant"
    __table_args__ = (
        UniqueConstraint("company_id", "sku", name="uq_item_variant_company_sku"),
        # One row per combination: a wider matrix never means duplicate SKUs.
        UniqueConstraint("item_id", "attributes", name="uq_item_variant_attributes"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("item.id"), nullable=False, index=True)
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    # The attribute combination that distinguishes it (`item_variant_attributes`).
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False)

    item: Mapped[Item] = relationship(back_populates="variants")


class ItemBarcode(Base):
    """One code printed on an item or variant, resolvable back to it."""

    __tablename__ = "item_barcode"
    __table_args__ = (
        # Unique **across the installation**, not per company: a scan resolves to
        # exactly one item or variant, whichever company asks (§5.3).
        UniqueConstraint("value", name="uq_item_barcode_value"),
        CheckConstraint(
            "symbology IN (" + ", ".join(f"'{name}'" for name in SYMBOLOGIES) + ")",
            name="ck_item_barcode_symbology",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("item.id"), nullable=False, index=True)
    # Set when the code identifies a variant rather than the item as a whole.
    variant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("item_variant.id"), index=True
    )
    value: Mapped[str] = mapped_column(String(64), nullable=False)
    symbology: Mapped[str] = mapped_column(String(8), nullable=False)

    item: Mapped[Item] = relationship(back_populates="barcodes")


class UomConversion(Base):
    """One declared conversion for one item: `1 from_uom = factor × to_uom`."""

    __tablename__ = "uom_conversion"
    __table_args__ = (
        UniqueConstraint(
            "item_id", "from_uom", "to_uom", name="uq_uom_conversion_direction"
        ),
        CheckConstraint("factor > 0", name="ck_uom_conversion_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("item.id"), nullable=False, index=True)
    from_uom: Mapped[str] = mapped_column(String(16), nullable=False)
    to_uom: Mapped[str] = mapped_column(String(16), nullable=False)
    factor: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)

    item: Mapped[Item] = relationship(back_populates="conversions")


# Masters retire by marking, never by removing the row (T-0.AUDIT.01). A variant is
# a master in its own right: documents point at it, so its row stays.
for _master in (Item, ItemVariant):
    deny_hard_delete(_master.__table__)


# --- Creating items, variants and codes --------------------------------------


def _checked_mode(mode: str) -> str:
    wanted = str(mode).strip().lower()
    if wanted not in TRACEABILITY_MODES:
        raise TraceabilityError(
            f"unknown traceability mode {mode!r}; the modes are"
            f" {', '.join(TRACEABILITY_MODES)}"
        )
    return wanted


def create_item(
    session: Session,
    *,
    company_id: uuid.UUID,
    sku: str,
    name: str,
    base_uom: str,
    traceability_mode: str,
    standard_cost: Decimal | None = None,
) -> Item:
    """Create one item, stating how it is tracked — the mode has no default."""
    if session.scalar(
        select(Item).where(Item.company_id == company_id, Item.sku == str(sku))
    ) is not None:
        raise DuplicateItemError(f"this company already has an item with SKU {sku!r}")
    item = Item(
        company_id=company_id,
        sku=str(sku),
        name=name,
        base_uom=str(base_uom).strip(),
        traceability_mode=_checked_mode(traceability_mode),
        standard_cost=standard_cost,
    )
    session.add(item)
    session.flush()
    return item


def rename_item(session: Session, item: Item, *, name: str) -> Item:
    """Change an item's name — the only field that may change after creation.

    DOMAIN-MODELS.md §5.1 fixes `sku`, `base_uom` and `traceability_mode` as
    immutable once the item has movements; the honest way to keep that promise is
    not to offer a setter for them at all, rather than one that refuses later.
    """
    item.name = name
    session.flush()
    return item


def item_by_sku(session: Session, *, company_id: uuid.UUID, sku: str) -> Item:
    """The live item with that SKU, or a refusal."""
    item = session.scalar(
        select(Item).where(Item.company_id == company_id, Item.sku == str(sku))
    )
    if item is None:
        raise UnknownItemError(f"no item with SKU {sku!r} in this company")
    return item


def add_variant(
    session: Session, item: Item, *, sku: str, attributes: dict
) -> ItemVariant:
    """Add one attribute combination, refusing a combination that already exists."""
    if not attributes:
        raise TraceabilityError(
            "a variant states the attribute combination that distinguishes it;"
            f" {item.sku!r} has no attributes to vary"
        )
    if session.scalar(
        select(ItemVariant).where(ItemVariant.item_id == item.id, ItemVariant.attributes == attributes)
    ) is not None:
        raise TraceabilityError(
            f"{item.sku!r} already has a variant with {attributes}"
        )
    if session.scalar(
        select(ItemVariant).where(
            ItemVariant.company_id == item.company_id, ItemVariant.sku == str(sku)
        )
    ) is not None:
        raise DuplicateItemError(f"this company already has a variant with SKU {sku!r}")
    variant = ItemVariant(
        company_id=item.company_id, item_id=item.id, sku=str(sku), attributes=dict(attributes)
    )
    session.add(variant)
    session.flush()
    return variant


def add_barcode(
    session: Session,
    item: Item,
    *,
    value: str,
    symbology: str,
    variant: ItemVariant | None = None,
) -> ItemBarcode:
    """Put one code on an item or variant. The value is unique system-wide."""
    wanted = str(symbology).strip().lower()
    if wanted not in SYMBOLOGIES:
        raise TraceabilityError(
            f"unknown symbology {symbology!r}; the decided symbologies are"
            f" {', '.join(SYMBOLOGIES)}"
        )
    if session.scalar(
        select(ItemBarcode).where(ItemBarcode.value == str(value))
    ) is not None:
        raise DuplicateItemError(
            f"barcode {value!r} is already used; a scan must resolve to exactly one item"
        )
    if variant is not None and variant.item_id != item.id:
        raise TraceabilityError(
            f"variant {variant.sku!r} belongs to another item"
        )
    barcode = ItemBarcode(
        company_id=item.company_id,
        item_id=item.id,
        variant_id=variant.id if variant is not None else None,
        value=str(value),
        symbology=wanted,
    )
    session.add(barcode)
    session.flush()
    return barcode


def item_from_barcode(
    session: Session, *, company_id: uuid.UUID, value: str
) -> tuple[Item, ItemVariant | None]:
    """What a scanned code resolves to: the item, and the variant when it names one."""
    barcode = session.scalar(
        select(ItemBarcode).where(
            ItemBarcode.company_id == company_id, ItemBarcode.value == str(value)
        )
    )
    if barcode is None:
        raise UnknownItemError(f"no item carries barcode {value!r} in this company")
    item = session.get(Item, barcode.item_id)
    if item is None:
        raise UnknownItemError(f"barcode {value!r} points at a retired item")
    variant = session.get(ItemVariant, barcode.variant_id) if barcode.variant_id else None
    return item, variant


# --- UOM conversion ----------------------------------------------------------


def add_uom_conversion(
    session: Session, item: Item, *, from_uom: str, to_uom: str, factor
) -> UomConversion:
    """Declare `1 from_uom = factor × to_uom` for one item.

    Refused when the two UOMs are the same, when the direction is already
    declared, or when the factor is not positive (the table's check constraint
    refuses that too). The reverse direction needs no row: it is the division by
    this factor, kept exact rather than rounded into a stored reciprocal.
    """
    source = str(from_uom).strip()
    target = str(to_uom).strip()
    if source == target:
        raise ConversionError(f"{source} → itself is not a conversion")
    exact = factor if isinstance(factor, Decimal) else Decimal(str(factor))
    if exact <= 0:
        raise ConversionError(f"a conversion factor must be positive, got {exact}")
    if session.scalar(
        select(UomConversion).where(
            UomConversion.item_id == item.id,
            UomConversion.from_uom == source,
            UomConversion.to_uom == target,
        )
    ) is not None:
        raise ConversionError(
            f"{item.sku!r} already converts {source} → {target}; change that row instead"
        )
    conversion = UomConversion(
        company_id=item.company_id,
        item_id=item.id,
        from_uom=source,
        to_uom=target,
        factor=exact,
    )
    session.add(conversion)
    session.flush()
    return conversion


def _edges(session: Session, item: Item) -> dict[str, list[tuple[str, str, Decimal]]]:
    """The conversion graph of one item, in both directions.

    Each declared row gives an edge `from → to` that **multiplies** by the factor
    and the reverse edge that **divides** by it — the division is the exact inverse,
    so a quantity converted out and back is the quantity it started as. The base
    UOM is a node like any other: the item's own rows anchor conversions to what
    quantities are stored in.
    """
    graph: dict[str, list[tuple[str, str, Decimal]]] = {}
    for row in session.scalars(
        select(UomConversion).where(UomConversion.item_id == item.id)
    ):
        graph.setdefault(row.from_uom, []).append((row.to_uom, "mul", row.factor))
        graph.setdefault(row.to_uom, []).append((row.from_uom, "div", row.factor))
    graph.setdefault(item.base_uom, [])
    return graph


def convert_quantity(
    session: Session, item: Item, *, quantity, from_uom: str, to_uom: str
) -> Decimal:
    """Restate a quantity from one UOM to another, along the declared chain.

    `1 from_uom = factor × to_uom`, so going *from* the larger unit multiplies and
    coming back divides — exactly, with the result quantized only at the end. A
    pair with no path is refused rather than treated as 1:1.
    """
    value = quantity if isinstance(quantity, Decimal) else Decimal(str(quantity))
    source = str(from_uom).strip()
    target = str(to_uom).strip()
    if source == target:
        return value.quantize(QUANTITY_SCALE)

    graph = _edges(session, item)
    if source not in graph or target not in graph:
        raise ConversionError(
            f"{item.sku!r} declares no conversion involving"
            f" {' or '.join(sorted({source, target}))}"
        )

    # Breadth-first along the graph, so the shortest chain wins and a cycle cannot
    # loop: the first path found is the conversion, by composition rather than by
    # assuming the factors combine.
    seen = {source}
    queue: list[tuple[str, Decimal]] = [(source, value)]
    while queue:
        node, amount = queue.pop(0)
        for nxt, operation, factor in graph.get(node, []):
            walked = amount * factor if operation == "mul" else amount / factor
            if nxt == target:
                return walked.quantize(QUANTITY_SCALE)
            if nxt not in seen:
                seen.add(nxt)
                queue.append((nxt, walked))
    raise ConversionError(
        f"{item.sku!r} has no conversion path from {source} to {target}"
        f" (base UOM {item.base_uom!r}) — declare the chain rather than assuming one"
    )

