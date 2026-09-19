"""T-1.INV.06 — physical count and the adjustment it produces.

§2.2's "Reconciliation … Physical count and adjustment workflows" and §1 principle
6 (workflow-driven approvals). A count is a document: it snapshots what the system
believes, records what was found, and the **variance** between the two is what the
ledger has to be told about.

* **The variance is the adjustment.** :func:`post_adjustment` writes one stock
  ledger entry per counted line whose variance is not zero — nothing else, and
  nothing for a line that matched.
* **Above the threshold, approval comes first.** The adjustment's value goes
  through T-0.WF.01's engine as document type `inventory_adjustment`; when the
  configured chain routes it, posting without an approved request is refused —
  the ledger is not touched until somebody decided.
* **It is all attributable.** The count's rows are company-scoped (so
  T-0.AUDIT.02's trigger records the count, its lines and the adjustment), and the
  adjustment entries name the count as their source document.

ponytail: a variance that *adds* stock is valued at the item's current unit cost at
that location, which is zero when the location holds nothing — found stock with no
cost basis. Ceiling: a company that wants such a find valued at a stated cost.
Upgrade path: accept a stated unit cost on the line (Phase 2's purchase-price
document already carries one).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.company import company_base_currency
from app.db import Base
from app.stock.entries import MovementError, StockLedgerEntry, on_hand, record_movement
from app.stock.gl_posting import post_movement_to_gl
from app.stock.items import Item
from app.stock.locations import Location, require_leaf
from app.stock.valuation import valuation, value_issue

MONEY = Numeric(20, 6)

# The document type the approval engine knows this adjustment by.
DOC_TYPE = "inventory_adjustment"

# A count's states.
OPEN, POSTED = "open", "posted"


class CountError(MovementError):
    """The count refused what was asked of it."""


class CountAlreadyPostedError(CountError):
    """The adjustment was already posted — posted twice would double the variance."""


class ApprovalRequiredError(CountError):
    """The adjustment reaches a configured approval level and has none, or is unapproved."""


class PhysicalCount(Base):
    """One count of one location, and the system quantities it started from."""

    __tablename__ = "physical_count"
    __table_args__ = (
        CheckConstraint("state IN ('open', 'posted')", name="ck_physical_count_state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    location_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("location.id"), nullable=False, index=True
    )
    state: Mapped[str] = mapped_column(String(8), nullable=False, default=OPEN)
    counted_by: Mapped[str] = mapped_column(String(64), nullable=False)
    posting_date: Mapped[date] = mapped_column(nullable=False, default=date.today)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    lines: Mapped[list[PhysicalCountLine]] = relationship(
        back_populates="count", order_by="PhysicalCountLine.id"
    )


class PhysicalCountLine(Base):
    """One item's counted quantity at that location, against what the ledger said."""

    __tablename__ = "physical_count_line"
    __table_args__ = (
        UniqueConstraint("count_id", "item_id", name="uq_physical_count_line_item"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    count_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("physical_count.id"), nullable=False, index=True
    )
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("item.id"), nullable=False, index=True)
    # What the ledger said when the count started, what was found, and the difference.
    system_quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    counted_quantity: Mapped[Decimal | None] = mapped_column(MONEY)
    variance: Mapped[Decimal | None] = mapped_column(MONEY)
    counted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    count: Mapped[PhysicalCount] = relationship(back_populates="lines")


def start_count(
    session: Session,
    *,
    company_id: uuid.UUID,
    location: Location,
    actor: str,
    posting_date: date | None = None,
) -> PhysicalCount:
    """Open a count of one location, snapshotting the system quantity of every item there."""
    require_leaf(session, location)
    count = PhysicalCount(
        company_id=company_id,
        location_id=location.id,
        state=OPEN,
        counted_by=str(actor),
        posting_date=posting_date or date.today(),
    )
    session.add(count)
    session.flush()

    held = session.execute(
        select(StockLedgerEntry.item_id, func.sum(StockLedgerEntry.quantity))
        .where(
            StockLedgerEntry.company_id == company_id,
            StockLedgerEntry.location_id == location.id,
        )
        .group_by(StockLedgerEntry.item_id)
    ).all()
    for item_id, quantity in held:
        count.lines.append(
            PhysicalCountLine(item_id=item_id, system_quantity=Decimal(quantity))
        )
    session.flush()
    return count


def record_count(
    session: Session, count: PhysicalCount, *, item: Item, counted_quantity
) -> PhysicalCountLine:
    """Record what was found for one item; the variance is against the snapshot."""
    if count.state != OPEN:
        raise CountAlreadyPostedError(
            f"count {count.id} is {count.state}; start a new count to count again"
        )
    found = (
        counted_quantity
        if isinstance(counted_quantity, Decimal)
        else Decimal(str(counted_quantity))
    )
    if found < 0:
        raise CountError(f"a counted quantity cannot be negative, got {found}")
    line = session.scalar(
        select(PhysicalCountLine).where(
            PhysicalCountLine.count_id == count.id, PhysicalCountLine.item_id == item.id
        )
    )
    if line is None:
        line = PhysicalCountLine(count_id=count.id, item_id=item.id, system_quantity=Decimal(0))
        session.add(line)
    line.counted_quantity = found
    line.variance = found - line.system_quantity
    line.counted_at = datetime.now().astimezone()
    session.flush()
    return line


def counted(session: Session, count: PhysicalCount) -> list[PhysicalCountLine]:
    """The lines that were actually counted, with their variances — what posting reads."""
    return [
        line
        for line in session.scalars(
            select(PhysicalCountLine)
            .where(PhysicalCountLine.count_id == count.id)
            .order_by(PhysicalCountLine.item_id)
        )
        if line.counted_quantity is not None
    ]


def adjustment_value(session: Session, count: PhysicalCount) -> Decimal:
    """The absolute value of the adjustment this count would post, at today's costs."""
    total = Decimal(0)
    for line in counted(session, count):
        if line.variance == 0:
            continue
        item = session.get(Item, line.item_id)
        if line.variance < 0:
            total += value_issue(
                session,
                company_id=count.company_id,
                item=item,
                quantity=-line.variance,
                location_id=count.location_id,
            )
        else:
            unit = Decimal(
                valuation(
                    session,
                    company_id=count.company_id,
                    item=item,
                    location_id=count.location_id,
                )["unit_cost"]
            )
            total += line.variance * unit
    return total.quantize(Decimal("0.000001"))


def post_adjustment(
    session: Session,
    count: PhysicalCount,
    *,
    approval=None,
) -> list[StockLedgerEntry]:
    """Post the count's variance to the ledger, exactly and only once.

    `approval` is the approval request (T-0.WF.01) this adjustment is being posted
    under, when the configured chain routes one. A routed adjustment with no
    approved request is refused before anything is written — "nobody approved it"
    is not "approved" — and a posting that is already done refuses to post again.
    """
    if count.state != OPEN:
        raise CountAlreadyPostedError(
            f"count {count.id} is {count.state}; its variance is already in the ledger"
        )
    from app.workflow import APPROVED, chain_for, start_approval

    amount = adjustment_value(session, count)
    request = approval
    if request is None:
        request = start_approval(
            session,
            company_id=count.company_id,
            doc_type=DOC_TYPE,
            document_id=count.id,
            amount=amount,
        )
    if request is not None and request.state != APPROVED:
        raise ApprovalRequiredError(
            f"the adjustment of {amount} needs approval ({DOC_TYPE}); it is"
            f" {request.state}, so the ledger has not been touched"
        )

    written: list[StockLedgerEntry] = []
    currency = company_base_currency(session, company_id=count.company_id)
    for line in counted(session, count):
        if line.variance == 0:
            continue
        item = session.get(Item, line.item_id)
        location = session.get(Location, count.location_id)
        if line.variance < 0:
            value = -value_issue(
                session,
                company_id=count.company_id,
                item=item,
                quantity=-line.variance,
                location_id=count.location_id,
            )
        else:
            unit = Decimal(
                valuation(
                    session,
                    company_id=count.company_id,
                    item=item,
                    location_id=count.location_id,
                )["unit_cost"]
            )
            value = line.variance * unit
        movement = record_movement(
            session,
            item=item,
            location=location,
            quantity=line.variance,
            value=value,
            currency=currency,
            source_type=DOC_TYPE,
            source_id=count.id,
            posting_date=count.posting_date,
        )
        # The GL half of the same document (T-1.INV.07).
        post_movement_to_gl(session, entry=movement)
        written.append(movement)
    count.state = POSTED
    session.flush()
    return written
