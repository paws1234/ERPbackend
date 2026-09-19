"""T-1.INV.02 — the four-level location hierarchy, and what may be posted where.

§2.2 names the levels: **Warehouse → Zone → Aisle → Bin**. DOMAIN-MODELS.md §6
fixes what that means, and three of its rules are enforced here:

* **A level cannot be skipped.** A zone's parent is a warehouse, an aisle's is a
  zone, a bin's is an aisle, and a warehouse has no parent — checked in the helper
  *and* by a deferred constraint trigger, so a writer that bypasses the helper is
  refused at COMMIT, like the ledger's balance rule.
* **Nothing may be moved into its own subtree**, and a parent with live children
  cannot be retired while they are live.
* **A location holding stock cannot be removed.** The stock ledger is T-1.INV.03's;
  this module asks it at the moment of retirement (imported there and then), which
  is what keeps the two modules from having to know each other's tables.

**A stock movement references a leaf** (`type = 'bin'`) and referencing a non-leaf
is refused — the refusal lives with the movement that makes it (T-1.INV.03), so a
movement cannot be recorded against a warehouse by any route.
"""

from __future__ import annotations

import uuid

from sqlalchemy import (
    CheckConstraint,
    DDL,
    ForeignKey,
    String,
    UniqueConstraint,
    Uuid,
    event,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.audit import SoftDeleteMixin, deny_hard_delete, soft_delete
from app.db import Base

# The four levels §2.2 names, from the top down. The order is the rule.
LEVELS = ("warehouse", "zone", "aisle", "bin")

# Which type may stand under which.
PARENT_TYPE = {"warehouse": None, "zone": "warehouse", "aisle": "zone", "bin": "aisle"}

# The level a stock movement may reference: the leaf.
LEAF = "bin"


class LocationError(ValueError):
    """The location hierarchy refused what was asked of it."""


class UnknownLocationError(LocationError):
    """No such location (or no such code) in this company."""


class LocationLevelError(LocationError):
    """A level was skipped, or a type was given the wrong parent."""


class LocationInUseError(LocationError):
    """The location still has live children, or stock."""


class NotALeafError(LocationError):
    """A stock movement named a location that is not a bin."""


class Location(SoftDeleteMixin, Base):
    """One node of the warehouse tree: a warehouse, zone, aisle or bin."""

    __tablename__ = "location"
    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_location_company_code"),
        CheckConstraint(
            "type IN (" + ", ".join(f"'{level}'" for level in LEVELS) + ")",
            name="ck_location_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # `type` is a builtin's name, so the attribute carries the suffix and the column
    # keeps the name DOMAIN-MODELS.md §6 fixes.
    location_type: Mapped[str] = mapped_column("type", String(16), nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("location.id"), index=True)

    parent: Mapped[Location | None] = relationship(
        back_populates="children", remote_side="Location.id"
    )
    children: Mapped[list[Location]] = relationship(
        back_populates="parent", order_by="Location.code"
    )

    @property
    def is_leaf(self) -> bool:
        return self.location_type == LEAF


# --- The level rules, at the storage boundary ---------------------------------
# The checks in the helpers below are the caller's copy; these triggers are the
# rules. Deferred, so a whole tree built in one transaction is judged as a set at
# COMMIT and a writer that bypasses the helpers — raw SQL, a restored dump — is
# refused there. Postgres-only by design, like the ledger's balance rule.
# '%%' because SQLAlchemy's DDL wrapper interpolates the statement.
_LEVELS_FUNCTION = DDL(
    """
CREATE OR REPLACE FUNCTION location_tree_rules() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    parent_type    text;
    parent_company uuid;
    cyclic         boolean;
BEGIN
    IF NEW.parent_id IS NOT NULL THEN
        SELECT type, company_id INTO parent_type, parent_company
          FROM location WHERE id = NEW.parent_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'location %% names a parent %% that does not exist',
                NEW.code, NEW.parent_id;
        END IF;
        IF parent_company <> NEW.company_id THEN
            RAISE EXCEPTION 'location %% and its parent %% belong to different companies',
                NEW.code, NEW.parent_id;
        END IF;
        IF parent_type <> (CASE NEW.type
                             WHEN 'zone' THEN 'warehouse'
                             WHEN 'aisle' THEN 'zone'
                             WHEN 'bin' THEN 'aisle'
                           END) THEN
            RAISE EXCEPTION
                'a %% cannot stand under a %%: the levels are Warehouse → Zone → Aisle → Bin',
                NEW.type, parent_type;
        END IF;

        WITH RECURSIVE up(id, parent_id, depth) AS (
            SELECT id, parent_id, 1 FROM location WHERE id = NEW.parent_id
            UNION ALL
            SELECT l.id, l.parent_id, up.depth + 1
              FROM location l JOIN up ON l.id = up.parent_id
             WHERE up.depth < 100
        )
        SELECT EXISTS (SELECT 1 FROM up WHERE id = NEW.id) INTO cyclic;
        IF cyclic THEN
            RAISE EXCEPTION
                'location %% cannot be moved under %%: that parent is inside its own subtree',
                NEW.code, NEW.parent_id;
        END IF;
    ELSIF NEW.type <> 'warehouse' THEN
        RAISE EXCEPTION 'a %% needs a parent: only a warehouse stands at the top',
            NEW.type;
    END IF;

    IF NEW.deleted_at IS NOT NULL
       AND EXISTS (SELECT 1 FROM location c
                    WHERE c.parent_id = NEW.id AND c.deleted_at IS NULL) THEN
        RAISE EXCEPTION 'location %% still has live children; retire them first', NEW.code;
    END IF;
    RETURN NULL;
END;
$$;
"""
)

_LEVELS_TRIGGER = DDL(
    """
CREATE CONSTRAINT TRIGGER location_tree_rules
    AFTER INSERT OR UPDATE ON location
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION location_tree_rules();
"""
)

for _ddl in (_LEVELS_FUNCTION, _LEVELS_TRIGGER):
    event.listen(Location.__table__, "after_create", _ddl)

# A master: retired by marking, never removed (T-0.AUDIT.01).
deny_hard_delete(Location.__table__)


def _checked_type(location_type: str) -> str:
    wanted = str(location_type).strip().lower()
    if wanted not in LEVELS:
        raise LocationLevelError(
            f"unknown location type {location_type!r}; the levels are"
            f" {' → '.join(LEVELS)}"
        )
    return wanted


def _parent(
    session: Session, *, company_id: uuid.UUID, parent_id: uuid.UUID | None
) -> Location | None:
    if parent_id is None:
        return None
    parent = session.scalar(
        select(Location).where(Location.id == parent_id, Location.company_id == company_id)
    )
    if parent is None:
        raise UnknownLocationError(
            f"no location {parent_id} in this company to stand under"
        )
    return parent


def create_location(
    session: Session,
    *,
    company_id: uuid.UUID,
    code: str,
    name: str,
    location_type: str,
    parent_id: uuid.UUID | None = None,
) -> Location:
    """Create one node, keeping the level rule (and saying why when it is broken)."""
    wanted = _checked_type(location_type)
    expected = PARENT_TYPE[wanted]
    parent = _parent(session, company_id=company_id, parent_id=parent_id)
    if expected is None and parent is not None:
        raise LocationLevelError(
            f"a {wanted} stands at the top of the tree and takes no parent"
        )
    if expected is not None and parent is None:
        raise LocationLevelError(
            f"a {wanted} needs a parent: its parent is a {expected}"
        )
    if parent is not None and parent.location_type != expected:
        raise LocationLevelError(
            f"a {wanted} cannot stand under a {parent.location_type}:"
            f" the levels are {' → '.join(LEVELS)}"
        )
    if session.scalar(
        select(Location).where(Location.company_id == company_id, Location.code == str(code))
    ) is not None:
        raise LocationError(f"this company already has a location {code!r}")

    location = Location(
        company_id=company_id,
        code=str(code),
        name=name,
        location_type=wanted,
        parent_id=parent.id if parent is not None else None,
    )
    session.add(location)
    session.flush()
    return location


def location_by_code(session: Session, *, company_id: uuid.UUID, code: str) -> Location:
    """The live location with that code, or a refusal."""
    location = session.scalar(
        select(Location).where(Location.company_id == company_id, Location.code == str(code))
    )
    if location is None:
        raise UnknownLocationError(f"no location {code!r} in this company")
    return location


def move_location(
    session: Session, location: Location, *, parent_id: uuid.UUID | None
) -> Location:
    """Move a node to another parent, keeping the level rule."""
    parent = _parent(session, company_id=location.company_id, parent_id=parent_id)
    expected = PARENT_TYPE[location.location_type]
    if expected is None and parent is not None:
        raise LocationLevelError(f"a {location.location_type} takes no parent")
    if expected is not None and parent is None:
        raise LocationLevelError(f"a {location.location_type} needs a {expected} as its parent")
    if parent is not None and parent.location_type != expected:
        raise LocationLevelError(
            f"a {location.location_type} cannot stand under a {parent.location_type}"
        )
    location.parent_id = parent.id if parent is not None else None
    session.flush()
    return location


def require_leaf(session: Session, location: Location) -> Location:
    """The location a movement may reference, or a refusal naming what it is instead."""
    if not location.is_leaf:
        raise NotALeafError(
            f"{location.code} is a {location.location_type}; a stock movement references a"
            f" {LEAF} — post to the bin that holds the goods"
        )
    return location


def retire_location(session: Session, location: Location) -> Location:
    """Retire a node, refusing while it still has live children or holds stock."""
    live_children = session.scalars(
        select(Location).where(Location.parent_id == location.id)
    ).all()
    if live_children:
        raise LocationInUseError(
            f"{location.code} still has live children"
            f" ({', '.join(child.code for child in live_children)}); retire them first"
        )
    # Asked of the stock ledger (T-1.INV.03) rather than duplicated here: what
    # "holding stock" means is that ledger's business.
    from app.stock.entries import on_hand

    held = on_hand(session, company_id=location.company_id, location_id=location.id)
    if held["quantity"] != 0:
        raise LocationInUseError(
            f"{location.code} still holds {held['quantity']} — move or issue the stock"
            " before retiring the location"
        )
    return soft_delete(session, location)


def location_tree(session: Session, *, company_id: uuid.UUID) -> list[dict]:
    """The whole hierarchy as a nested tree, parents before their children."""
    locations = list(
        session.scalars(
            select(Location).where(Location.company_id == company_id).order_by(Location.code)
        )
    )
    nodes = {
        location.id: {
            "id": str(location.id),
            "code": location.code,
            "name": location.name,
            "type": location.location_type,
            "parent_id": str(location.parent_id) if location.parent_id else None,
            "children": [],
        }
        for location in locations
    }
    roots: list[dict] = []
    for location in locations:
        node = nodes[location.id]
        parent = nodes.get(location.parent_id) if location.parent_id else None
        if parent is None:
            roots.append(node)
        else:
            parent["children"].append(node)
    return roots
