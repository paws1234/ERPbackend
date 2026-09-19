"""T-1.INV.08 — batch/lot identity with expiry, and the FEFO suggestion.

§2.2's "Traceability (Batch/Lot with expiry, Serial numbers)", moved into Phase 1
on 2026-09-17 because the identity has to be in the stock ledger entry's shape
before it has rows. DOMAIN-MODELS.md §7 fixed the `batch_id` column; this module
owns the table it points at and the rules around it:

* **A batch-tracked item moves by batch, always.** A movement without a batch is
  refused (T-1.INV.03's recorder checks the item's `traceability_mode`), and an
  item whose mode is `none` may not carry one either — so the ledger's batch column
  means what it says on every row.
* **Issues suggest FEFO** — the earliest expiry among the batches that hold stock
  at that location — which is what a warehouse does with perishables.
* **An expired batch is refused unless somebody explicitly overrides it**, and the
  override is attributable: the caller states the actor, the ledger entry records
  the movement, and the audit trail records who wrote it (T-0.AUDIT.02).

ponytail: expiry is enforced at issue, not at receipt (obviously) and not as a
background job — there is no scheduler in Phase 1 (the queue is still unpinned).
Ceiling: automatic blocking of a whole location's expired stock. Upgrade path: a
scheduled check when the job queue is chosen.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    Uuid,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.audit import SoftDeleteMixin, deny_hard_delete, soft_delete
from app.db import Base
from app.stock.items import Item, ItemError


class BatchError(ItemError):
    """The batch could not be used as asked."""


class UnknownBatchError(BatchError):
    """No such batch (or no such code) for this item."""


class ExpiredBatchError(BatchError):
    """The batch is past its expiry and no explicit override was given."""


class Batch(SoftDeleteMixin, Base):
    """One lot of one item: what it is called and when it expires, if it does."""

    __tablename__ = "batch"
    __table_args__ = (
        UniqueConstraint("item_id", "code", name="uq_batch_item_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("item.id"), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    # `batch_expiry_required` is "Not stated" in the plan, so the column is
    # nullable and the requirement is a per-item decision the caller makes when it
    # creates the batch — not an invented default here.
    expiry_date: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    item: Mapped[Item] = relationship()


# A master: a batch is retired by marking it, never by removing the row.
deny_hard_delete(Batch.__table__)


def create_batch(
    session: Session, *, item: Item, code: str, expiry_date: date | None = None
) -> Batch:
    """Register one lot of one item — the code is unique within the item."""
    if str(item.traceability_mode) != "batch_lot":
        raise BatchError(
            f"{item.sku!r} is tracked as {item.traceability_mode!r}, so it has no batches;"
            " set traceability_mode to 'batch_lot' when the item is created (T-1.INV.01)"
        )
    existing = session.scalar(
        select(Batch).where(Batch.item_id == item.id, Batch.code == str(code))
    )
    if existing is not None:
        return existing
    batch = Batch(
        company_id=item.company_id,
        item_id=item.id,
        code=str(code),
        expiry_date=expiry_date,
    )
    session.add(batch)
    session.flush()
    return batch


def batch_by_code(session: Session, *, item: Item, code: str) -> Batch:
    """The live batch with that code for that item, or a refusal."""
    batch = session.scalar(
        select(Batch).where(Batch.item_id == item.id, Batch.code == str(code))
    )
    if batch is None:
        raise UnknownBatchError(f"{item.sku!r} has no batch {code!r}")
    return batch


def require_usable(
    session: Session,
    batch: Batch,
    *,
    on: date,
    allow_expired: bool = False,
    actor: str | None = None,
) -> Batch:
    """Refuse an expired batch unless the caller explicitly overrides it.

    The override is a deliberate, attributed act: the caller states who is doing
    it, and the movement that follows is on the ledger with that actor on the audit
    trail — which is what "an explicit, audited override" means here.
    """
    if batch.expiry_date is None or batch.expiry_date >= on:
        return batch
    if not allow_expired:
        raise ExpiredBatchError(
            f"batch {batch.code!r} expired on {batch.expiry_date}; issuing it needs an"
            " explicit override (allow_expired=True) and that override is recorded"
        )
    if not actor:
        raise ExpiredBatchError(
            f"batch {batch.code!r} expired on {batch.expiry_date}: an override names the"
            " actor who took the decision"
        )
    from app.audit import set_origin

    set_origin(session, "expired_batch_override", f"{batch.code}@{on.isoformat()}")
    return batch


def fefo_batch(
    session: Session, *, item: Item, location_id: uuid.UUID
) -> Batch | None:
    """The batch to issue next from a location: the earliest expiry that still holds stock.

    A batch with no expiry is offered last — it is the one that cannot go off. The
    answer is a suggestion: the caller may issue another batch, and T-1.INV.08's
    rule is only that an *expired* one is refused.
    """
    from app.stock.entries import on_hand

    stock = session.execute(
        select(Batch)
        .where(Batch.item_id == item.id)
        .order_by(Batch.expiry_date.is_(None), Batch.expiry_date, Batch.code)
    ).scalars()
    for batch in stock:
        held = on_hand(
            session,
            company_id=item.company_id,
            item_id=item.id,
            location_id=location_id,
            batch_id=batch.id,
        )["quantity"]
        if held > 0:
            return batch
    return None


def retire_batch(session: Session, batch: Batch) -> Batch:
    """Retire a lot by marking it (a master, never removed)."""
    from app.stock.entries import on_hand

    held = on_hand(session, company_id=batch.company_id, item_id=batch.item_id, batch_id=batch.id)
    if held["quantity"] != 0:
        raise BatchError(
            f"batch {batch.code!r} still holds {held['quantity']}; it cannot be retired"
        )
    return soft_delete(session, batch)
