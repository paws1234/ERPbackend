"""T-4.BOM.01 — the multi-level bill of materials, and what one order of it consumes.

A bill of materials is the answer to "what is this made of, and how much of it", so
this module keeps exactly that and derives everything else from it:

* **A BOM is one item's make-up at one version.** :class:`Bom` names the item it
  produces and the version of that item's make-up; :class:`BomLine` names one
  component and how much of it one unit of the parent takes. A BOM is **released**
  once something is built from it, and a released BOM's lines do not move again: the
  sanctioned way to change what a product is made of is :func:`revise`, which earns a
  new version, so a work order created yesterday still means what it meant yesterday
  (T-4.WO.01 snapshots the version it used).
* **Scrap rides on the line, and "unset" is not "zero".** A line's `scrap_percent` is
  the extra it takes to end up with the quantity asked for — 5 % on a line means 105
  of it are consumed per 100 required. A **null** is not a zero: it is a line nobody
  has stated a scrap figure for, and the explosion and the reports keep the two apart
  so a figure nobody filled in is never read as a decision that there is none.
* **Multi-level, to any depth, without a cycle.** :func:`explode` walks the tree and
  multiplies the requirements down it — a component's own requirement is exploded
  through its own BOM, scrap included at every level, so the number a work order is
  planned against is the same number the lowest level is bought against.
* **A component cannot be its own ancestor.** :func:`add_line` refuses a component the
  parent is already reachable through, which is what keeps the explosion finite; the
  refusal names the path rather than reporting a stack overflow later.

Costing is deliberately not here: what a BOM *costs* is T-4.WO.05's question, and the
routing is T-4.BOM.02's.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.db import Base
from app.stock.items import Item, ItemError

# One scale for quantities and money, as everywhere else in the repository.
MONEY = Numeric(20, 6)
SCALE = Decimal("0.000001")
HUNDRED = Decimal(100)

DRAFT, RELEASED = "draft", "released"
BOM_STATUSES = (DRAFT, RELEASED)


class BomError(ItemError):
    """The bill of materials refused what was asked of it."""


class DuplicateBomError(BomError):
    """That item already has a BOM at that version."""


class BomLockedError(BomError):
    """The BOM is released: a change to it is a new version, never an edit."""


class CircularBomError(BomError):
    """The component is the BOM's own item, or an ancestor of it."""


class BomLineError(BomError):
    """The line as stated cannot be a line of this BOM."""


class Bom(Base):
    """One item's make-up, at one version, for one company."""

    __tablename__ = "bom"
    __table_args__ = (
        UniqueConstraint("company_id", "item_id", "version", name="uq_bom_item_version"),
        CheckConstraint("version >= 1", name="ck_bom_version_starts_at_one"),
        CheckConstraint("status IN ('draft', 'released')", name="ck_bom_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    # The item this BOM produces.
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("item.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=DRAFT)
    memo: Mapped[str | None] = mapped_column(String(200))

    item: Mapped[Item] = relationship()
    lines: Mapped[list[BomLine]] = relationship(
        back_populates="bom", order_by="BomLine.line_no", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover - a convenience for a caller's log
        return f"Bom({self.item_id} v{self.version} {self.status})"


class BomLine(Base):
    """One component of a BOM: how much of it one unit of the parent takes."""

    __tablename__ = "bom_line"
    __table_args__ = (
        UniqueConstraint("bom_id", "line_no", name="uq_bom_line_no"),
        CheckConstraint("line_no >= 1", name="ck_bom_line_starts_at_one"),
        CheckConstraint("quantity > 0", name="ck_bom_line_quantity"),
        # An unset scrap figure is null, never a negative or an oversized one. The
        # ceiling is 100 %: a line that scrapes more than all of itself is a typo the
        # explosion would quietly multiply through every level.
        CheckConstraint(
            "scrap_percent IS NULL OR (scrap_percent >= 0 AND scrap_percent <= 100)",
            name="ck_bom_line_scrap",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    bom_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bom.id"), nullable=False, index=True)
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("item.id"), nullable=False, index=True)
    # How much of the component one unit of the parent takes, before scrap.
    quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    uom: Mapped[str] = mapped_column(String(16), nullable=False)
    # Null means nobody has stated a scrap figure; 0 means somebody stated none is taken.
    scrap_percent: Mapped[Decimal | None] = mapped_column(Numeric(9, 4))
    # The operation that consumes this component, or null for a component the job
    # consumes as a whole (T-4.BOM.02). Stated without a database-level foreign key to
    # the routing: the column is added by a task whose BOM must be writable on its own,
    # and `app.manufacturing.routing` is the only writer.
    operation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)

    bom: Mapped[Bom] = relationship(back_populates="lines")
    item: Mapped[Item] = relationship()


def _amount(value: Any, what: str) -> Decimal:
    amount = value if isinstance(value, Decimal) else Decimal(str(value))
    if amount <= 0:
        raise BomLineError(f"{what} is above zero, got {amount}")
    return amount


def _scrap(value: Any) -> Decimal | None:
    """The scrap figure as stated: null stays null, everything else is checked."""
    if value is None or str(value).strip() == "":
        return None
    percent = value if isinstance(value, Decimal) else Decimal(str(value))
    if percent < 0 or percent > HUNDRED:
        raise BomLineError(f"a scrap figure is between 0 and 100 percent, got {percent}")
    return percent.quantize(Decimal("0.0001"))


def create_bom(
    session: Session,
    *,
    company_id: uuid.UUID,
    item: Item,
    version: int | None = None,
    memo: str | None = None,
) -> Bom:
    """Raise a **draft** BOM for an item, at the next free version unless one is named."""
    if item.company_id != company_id:
        raise BomError(f"item {item.sku!r} belongs to another company")
    wanted = int(version) if version is not None else next_version(session, item)
    if wanted < 1:
        raise BomError(f"a BOM version starts at 1, got {wanted}")
    if session.scalar(
        select(Bom).where(
            Bom.company_id == company_id, Bom.item_id == item.id, Bom.version == wanted
        )
    ) is not None:
        raise DuplicateBomError(
            f"{item.sku!r} already has a BOM at version {wanted}; revising it is a new"
            " version, not a second BOM at the same number"
        )
    bom = Bom(
        company_id=company_id,
        item_id=item.id,
        version=wanted,
        status=DRAFT,
        memo=memo,
    )
    session.add(bom)
    session.flush()
    return bom


def next_version(session: Session, item: Item) -> int:
    """The version a new BOM for this item would take: one past the highest."""
    highest = session.scalar(
        select(Bom.version).where(Bom.item_id == item.id).order_by(Bom.version.desc()).limit(1)
    )
    return int(highest or 0) + 1


def bom_by_version(
    session: Session, *, company_id: uuid.UUID, item: Item, version: int
) -> Bom:
    """One named version of an item's BOM, or a refusal."""
    bom = session.scalar(
        select(Bom).where(
            Bom.company_id == company_id,
            Bom.item_id == item.id,
            Bom.version == int(version),
        )
    )
    if bom is None:
        raise BomError(f"{item.sku!r} has no BOM at version {version} in this company")
    return bom


def released_bom(session: Session, item: Item) -> Bom | None:
    """The version of the item's BOM that is in use, or ``None`` where none is.

    A work order is raised against **this** version (T-4.WO.01), so a draft the
    planner is still writing cannot become a shop-floor commitment by accident.
    """
    return session.scalar(
        select(Bom)
        .where(Bom.company_id == item.company_id, Bom.item_id == item.id, Bom.status == RELEASED)
        .order_by(Bom.version.desc())
        .limit(1)
    )


def lines_of(session: Session, bom: Bom) -> list[BomLine]:
    """A BOM's lines, in order — read from the rows, never from a loaded collection.

    The relationship is there for a caller's convenience, but a line written in this
    session with `bom_id` stated is not appended to it, so anything that must see what
    is *stored* — the next line number, an explosion — reads the table.
    """
    return list(
        session.scalars(
            select(BomLine).where(BomLine.bom_id == bom.id).order_by(BomLine.line_no)
        )
    )


def add_line(
    session: Session,
    bom: Bom,
    *,
    item: Item,
    quantity: Any,
    uom: str | None = None,
    scrap_percent: Any = None,
) -> BomLine:
    """Add one component to a **draft** BOM, refusing a cycle as it is written."""
    if bom.status != DRAFT:
        raise BomLockedError(
            f"BOM v{bom.version} of this item is {bom.status}: a BOM in use does not"
            " change — revise it into a new version instead (T-4.BOM.01)"
        )
    if item.company_id != bom.company_id:
        raise BomError(f"item {item.sku!r} belongs to another company")
    parent = session.get(Item, bom.item_id)
    _refuse_cycle(session, bom=bom, parent=parent, component=item)
    highest = session.scalar(
        select(func.max(BomLine.line_no)).where(BomLine.bom_id == bom.id)
    )
    line = BomLine(
        company_id=bom.company_id,
        bom_id=bom.id,
        line_no=int(highest or 0) + 1,
        item_id=item.id,
        quantity=_amount(quantity, "a component quantity"),
        uom=str(uom or item.base_uom).strip(),
        scrap_percent=_scrap(scrap_percent),
    )
    session.add(line)
    session.flush()
    return line


def _refuse_cycle(session: Session, *, bom: Bom, parent: Item, component: Item) -> None:
    """Refuse a component the parent is already reachable through.

    The check is on **items**, not on this BOM: what must not exist is a chain of
    BOMs from the component back to the parent, because that is what would make the
    explosion endless. The refusal names the path, so the planner sees which BOM to
    correct rather than learning only that something is wrong.
    """
    if component.id == parent.id:
        raise CircularBomError(
            f"{parent.sku!r} cannot be a component of its own BOM: an item is not made"
            " of itself (T-4.BOM.01)"
        )
    reached = _descendants(session, component)
    if parent.id in reached:
        raise CircularBomError(
            f"adding {component.sku!r} to {parent.sku!r}'s BOM would close a loop:"
            f" {component.sku} is already made from {parent.sku} (T-4.BOM.01)"
        )


def _descendants(session: Session, item: Item) -> set[uuid.UUID]:
    """Every item reachable *down* from an item, following the newest BOM at each step."""
    seen: set[uuid.UUID] = set()
    queue = [item.id]
    while queue:
        current = queue.pop()
        for line in session.scalars(
            select(BomLine)
            .join(Bom, Bom.id == BomLine.bom_id)
            .where(Bom.item_id == current)
        ):
            if line.item_id in seen or line.item_id == item.id:
                continue
            seen.add(line.item_id)
            queue.append(line.item_id)
    return seen


def release(session: Session, bom: Bom) -> Bom:
    """Freeze a BOM as the version in use: its lines stop moving from here on."""
    if bom.status == RELEASED:
        raise BomLockedError(f"BOM v{bom.version} is already released")
    bom.status = RELEASED
    session.flush()
    return bom


def revise(session: Session, bom: Bom) -> Bom:
    """Start the **next** version from a released BOM's lines — the sanctioned change.

    A work order that snapshotted version *n* keeps its own copy of the requirements,
    so editing what the product is made of has to be a new version rather than a
    rewrite of the one the shop floor is already building against.
    """
    if bom.status != RELEASED:
        raise BomError(
            f"BOM v{bom.version} is {bom.status}: revise a released BOM, or edit this"
            " draft — there is nothing frozen to copy past"
        )
    item = session.get(Item, bom.item_id)
    fresh = create_bom(
        session,
        company_id=bom.company_id,
        item=item,
        version=next_version(session, item),
        memo=f"revision of v{bom.version}",
    )
    for line in lines_of(session, bom):
        add_line(
            session,
            fresh,
            item=session.get(Item, line.item_id),
            quantity=line.quantity,
            uom=line.uom,
            scrap_percent=line.scrap_percent,
        )
    session.flush()
    return fresh


def uplift(scrap_percent: Decimal | None) -> Decimal:
    """What a scrap figure multiplies a requirement by: 1.05 for 5 %, 1 for null."""
    if scrap_percent is None:
        return Decimal(1)
    return (Decimal(1) + (Decimal(scrap_percent) / HUNDRED)).quantize(
        Decimal("0.000000000001")
    )


def explode(session: Session, bom: Bom, *, quantity: Any = Decimal(1)) -> dict:
    """Everything one order of `quantity` of the BOM's item consumes, level by level.

    The requirement for a component is the parent's own requirement times what one
    unit of the parent takes, **uplifted by that line's scrap** — and a component that
    is itself built is then exploded through *its* BOM at that already-scrapped
    quantity, which is why scrap at every level multiplies through instead of being
    added up at the end. A line with no scrap figure is uplifted by nothing and says
    so (`scrap_percent: None`), so a figure nobody stated is never read as a zero.

    Returns the walk *and* the total per item, because the two answer different
    questions: the level list says where a component is consumed, the total says how
    much to buy.
    """
    ordered = _amount(quantity, "the quantity to explode")
    levels: list[dict] = []
    totals: dict[uuid.UUID, Decimal] = {}
    _explode_into(
        session,
        bom=bom,
        parent_quantity=ordered,
        level=1,
        path=[session.get(Item, bom.item_id).sku],
        levels=levels,
        totals=totals,
    )
    by_sku = {}
    for item_id, amount in totals.items():
        item = session.get(Item, item_id)
        by_sku[item.sku] = amount.quantize(SCALE)
    return {
        "item": session.get(Item, bom.item_id).sku,
        "bom_version": bom.version,
        "quantity": ordered.quantize(SCALE),
        "levels": levels,
        "required": dict(sorted(by_sku.items())),
    }


def _explode_into(
    session: Session,
    *,
    bom: Bom,
    parent_quantity: Decimal,
    level: int,
    path: list[str],
    levels: list[dict],
    totals: dict[uuid.UUID, Decimal],
) -> None:
    """Walk one level down, then recurse into each component's own BOM."""
    for line in lines_of(session, bom):
        component = session.get(Item, line.item_id)
        required = (
            parent_quantity * Decimal(line.quantity) * uplift(line.scrap_percent)
        ).quantize(SCALE)
        if component.sku in path:
            raise CircularBomError(
                "the BOM loops: " + " → ".join([*path, component.sku]) + " (T-4.BOM.01)"
            )
        levels.append(
            {
                "level": level,
                "item": component.sku,
                "uom": component.base_uom,
                "per_parent": Decimal(line.quantity).quantize(SCALE),
                "scrap_percent": line.scrap_percent,
                "quantity": required,
                "path": " → ".join([*path, component.sku]),
            }
        )
        totals[component.id] = totals.get(component.id, Decimal(0)) + required
        built = released_bom(session, component)
        if built is not None:
            _explode_into(
                session,
                bom=built,
                parent_quantity=required,
                level=level + 1,
                path=[*path, component.sku],
                levels=levels,
                totals=totals,
            )
