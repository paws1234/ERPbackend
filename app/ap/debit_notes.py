"""T-2.AP.03 — debit notes: giving an invoice back, in money and in stock.

A debit note is the supplier-side mirror of an invoice. Two shapes of it, and the
difference matters:

* an **adjustment** — the price was wrong, nothing moves physically. The note reverses
  the invoice's cost and tax and reduces what is owed.
* a **return** — the goods go back. The note does the same money reversal **and** takes
  the stock out of the location it went into.

So the note posts **one** entry: payables debited (what the supplier now owes back),
the cost accounts and input tax credited, mirroring T-2.AP.01's posting exactly. A
return's stock movement is written with :func:`app.stock.entries.record_movement`
**without** its own GL posting — `app.stock.transactions.issue` would post a second
entry crediting inventory for the same goods, and one economic event must not be
booked twice. The value it moves comes from the same costing walk an issue uses
(:func:`app.stock.valuation.value_issue`), so the return leaves stock at what it cost.

Two more rules, both from the task's own criteria:

* **The open invoice is the ceiling.** A note for more than the invoice's remaining
  open amount is refused unless an explicit override is given, which is recorded on
  the note — the same shape as the over-receipt and over-award rules.
* **A return cannot conjure stock.** The location must actually hold what is going
  back; a return from an empty bin is a data error, not a movement.
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

from app.ap.invoices import (
    EXPENSE_KEY,
    INVENTORY_KEY,
    INPUT_TAX_KEY,
    PAYABLES_KEY,
    SupplierInvoice,
    open_amount,
    settle,
)
from app.db import Base
from app.ledger.currency import currency_by_code
from app.ledger.mapping import mapped_account
from app.ledger.posting import JournalEntry, post_journal_entry
from app.procurement.suppliers import Supplier
from app.stock.entries import on_hand, record_movement
from app.stock.items import Item
from app.stock.locations import Location
from app.stock.valuation import value_issue

# One money scale for the whole platform.
MONEY = Numeric(20, 6)

# A document is a draft until it is posted.
DRAFT, POSTED = "draft", "posted"

# The two shapes a debit note takes: nothing moves physically, or goods go back.
RETURN, ADJUSTMENT = "return", "adjustment"
KINDS = (RETURN, ADJUSTMENT)


class DebitNoteError(ValueError):
    """The debit note refused what was asked of it."""


class DuplicateDebitNoteError(DebitNoteError):
    """That debit note number is already used in this company."""


class DebitNoteStateError(DebitNoteError):
    """The asked-for change does not apply to the note's state."""


class OverNoteError(DebitNoteError):
    """The note is for more than the invoice still owes."""


class DebitNote(Base):
    """One debit note against one invoice: a return or an adjustment."""

    __tablename__ = "debit_note"
    __table_args__ = (
        UniqueConstraint("company_id", "number", name="uq_debit_note_company_number"),
        CheckConstraint("kind IN ('return', 'adjustment')", name="ck_debit_note_kind"),
        CheckConstraint("status IN ('draft', 'posted')", name="ck_debit_note_status"),
        CheckConstraint("net_amount >= 0", name="ck_debit_note_net"),
        CheckConstraint("tax_amount >= 0", name="ck_debit_note_tax"),
        CheckConstraint("gross_amount >= 0", name="ck_debit_note_gross"),
        CheckConstraint(
            "(kind = 'adjustment') OR (location_id IS NOT NULL)",
            name="ck_debit_note_return_has_location",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    number: Mapped[str] = mapped_column(String(32), nullable=False)
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("supplier.id"), nullable=False, index=True
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("supplier_invoice.id"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # Where goods go back to. Required for a return, absent for an adjustment.
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("location.id"), index=True
    )
    note_date: Mapped[date] = mapped_column(Date, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    net_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    tax_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    gross_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=DRAFT)
    # Why the note goes beyond the invoice's open amount. Null means it did not.
    over_note_reason: Mapped[str | None] = mapped_column(Text)
    # Credit on the supplier account not applied to this invoice when the override
    # exceeds its open amount.
    unapplied_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    journal_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("journal_entry.id"), index=True
    )
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    supplier: Mapped[Supplier] = relationship()
    invoice: Mapped[SupplierInvoice] = relationship()
    location: Mapped[Location | None] = relationship()
    lines: Mapped[list[DebitNoteLine]] = relationship(
        back_populates="note", order_by="DebitNoteLine.line_no"
    )


class DebitNoteLine(Base):
    """One line given back: what, how much, and what it was invoiced at."""

    __tablename__ = "debit_note_line"
    __table_args__ = (
        UniqueConstraint("note_id", "line_no", name="uq_debit_note_line_no"),
        CheckConstraint("line_no >= 1", name="ck_debit_note_line_starts_at_one"),
        CheckConstraint("quantity > 0", name="ck_debit_note_line_quantity"),
        CheckConstraint("unit_price >= 0", name="ck_debit_note_line_price"),
        CheckConstraint("tax_amount >= 0", name="ck_debit_note_line_tax"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    note_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("debit_note.id"), nullable=False, index=True
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(String(200), nullable=False)
    item_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("item.id"), index=True)
    quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    uom: Mapped[str] = mapped_column(String(16), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    # The stock ledger entry a return produced for this line, where it produced one.
    movement_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("stock_ledger_entry.id"), index=True
    )
    # The invoice line this return gives back.  Keeping the link makes the quantity
    # ceiling derivable across multiple notes instead of trusting the note header.
    invoice_line_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("supplier_invoice_line.id"), index=True
    )

    note: Mapped[DebitNote] = relationship(back_populates="lines")


def _amount(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def note_net(line: DebitNoteLine) -> Decimal:
    """What one line is worth before its tax."""
    return (line.quantity * line.unit_price).quantize(Decimal("0.000001"))


def create_debit_note(
    session: Session,
    *,
    number: str,
    invoice: SupplierInvoice,
    note_date: date,
    kind: str,
    lines: Any,
    location: Location | None = None,
) -> DebitNote:
    """Raise a **draft** debit note against a posted invoice.

    `lines` is an iterable of ``{"description": …, "quantity": …, "uom": …,
    "unit_price": …, "tax_amount": …, "item_id": …}`` — the price is usually the
    invoice's own, but a price adjustment is exactly the case where it is not, so it is
    stated per line rather than copied.
    """
    if invoice.status != POSTED:
        raise DebitNoteStateError(
            f"invoice {invoice.number!r} is {invoice.status}; there is nothing to give back"
        )
    wanted = str(kind).strip().lower()
    if wanted not in KINDS:
        raise DebitNoteError(f"unknown kind {kind!r}; a debit note is {', '.join(KINDS)}")
    if wanted == RETURN and location is None:
        raise DebitNoteError(
            "a return states where the goods go back to; there is no default location"
        )
    if wanted == ADJUSTMENT and location is not None:
        raise DebitNoteError(
            "an adjustment moves no stock, so it has no location; a return does"
        )
    stated = str(number).strip()
    if not stated:
        raise DebitNoteError("a debit note number is required")
    if session.scalar(
        select(DebitNote).where(
            DebitNote.company_id == invoice.company_id, DebitNote.number == stated
        )
    ) is not None:
        raise DuplicateDebitNoteError(
            f"debit note {stated!r} already exists in this company"
        )
    entries = list(lines)
    if not entries:
        raise DebitNoteError(f"debit note {stated!r} names no lines")

    note = DebitNote(
        company_id=invoice.company_id,
        number=stated,
        supplier_id=invoice.supplier_id,
        invoice_id=invoice.id,
        kind=wanted,
        location_id=location.id if location is not None else None,
        note_date=note_date,
        currency=currency_by_code(
            session, company_id=invoice.company_id, code=invoice.currency
        ).code,
        unapplied_amount=Decimal(0),
        status=DRAFT,
    )
    session.add(note)
    session.flush()
    invoice_lines = list(invoice.lines)
    for raw in entries:
        quantity = _amount(raw["quantity"])
        price = _amount(raw["unit_price"])
        tax = _amount(raw.get("tax_amount", 0))
        if quantity <= 0 or price < 0 or tax < 0:
            raise DebitNoteError(
                f"a line states a positive quantity, a non-negative price and tax; got"
                f" {quantity}, {price}, {tax}"
            )
        invoice_line_id = raw.get("invoice_line_id")
        if wanted == RETURN:
            if invoice_line_id is None:
                candidates = [
                    candidate
                    for candidate in invoice_lines
                    if candidate.item_id == raw.get("item_id")
                ]
                if len(candidates) == 1:
                    invoice_line_id = candidates[0].id
                else:
                    raise DebitNoteError(
                        f"return line for {stated!r} must name exactly one invoice line"
                    )
            invoice_line = next(
                (candidate for candidate in invoice_lines if candidate.id == invoice_line_id),
                None,
            )
            if (
                invoice_line is None
                or invoice_line.invoice_id != invoice.id
                or invoice_line.item_id != raw.get("item_id")
            ):
                raise DebitNoteError(
                    f"return line for {stated!r} does not belong to invoice {invoice.number!r}"
                )
            already = session.scalar(
                select(func.coalesce(func.sum(DebitNoteLine.quantity), 0))
                .join(DebitNote, DebitNote.id == DebitNoteLine.note_id)
                .where(
                    DebitNoteLine.invoice_line_id == invoice_line.id,
                    DebitNote.status == POSTED,
                )
            )
            if _amount(already or 0) + quantity > invoice_line.quantity:
                raise DebitNoteError(
                    f"return quantity for invoice line {invoice_line.line_no} exceeds"
                    f" its invoiced quantity of {invoice_line.quantity}"
                )
        note.lines.append(
            DebitNoteLine(
                company_id=invoice.company_id,
                note_id=note.id,
                line_no=len(note.lines) + 1,
                description=str(raw.get("description", "")).strip() or "—",
                item_id=raw.get("item_id"),
                quantity=quantity,
                uom=str(raw.get("uom", "each")),
                unit_price=price,
                tax_amount=tax,
                invoice_line_id=invoice_line_id,
            )
        )
    session.flush()
    note.net_amount = sum((note_net(line) for line in note.lines), Decimal(0)).quantize(
        Decimal("0.000001")
    )
    note.tax_amount = sum((line.tax_amount for line in note.lines), Decimal(0)).quantize(
        Decimal("0.000001")
    )
    note.gross_amount = (note.net_amount + note.tax_amount).quantize(Decimal("0.000001"))
    session.flush()
    return note


def post_debit_note(
    session: Session, note: DebitNote, *, over_note_reason: str | None = None
) -> JournalEntry:
    """Post the note: reverse the invoice in the ledger, and return the goods.

    The entry is the invoice's posting with the sides swapped — payables debited for
    the gross, the cost accounts and input tax credited — so the two are the same
    arithmetic and can never drift apart. A return additionally writes one stock
    movement per line, without a GL posting of its own (see the module docstring).
    """
    if note.status == POSTED:
        raise DebitNoteStateError(
            f"debit note {note.number!r} is already posted; posting it twice would"
            " give the same goods back twice"
        )
    invoice = note.invoice
    outstanding = open_amount(session, invoice)
    if note.gross_amount > outstanding and not (over_note_reason or "").strip():
        raise OverNoteError(
            f"invoice {invoice.number!r} is owed {outstanding}; a debit note for"
            f" {note.gross_amount} needs an explicit override with a reason"
        )
    if note.gross_amount > outstanding:
        note.over_note_reason = str(over_note_reason).strip()
        note.unapplied_amount = (note.gross_amount - outstanding).quantize(
            Decimal("0.000001")
        )

    if note.kind == RETURN:
        current_by_invoice_line: dict[uuid.UUID, Decimal] = {}
        for line in note.lines:
            if line.invoice_line_id is None:
                raise DebitNoteError(
                    f"return line {line.line_no} of {note.number!r} has no invoice line"
                )
            invoice_line = next(
                (candidate for candidate in invoice.lines if candidate.id == line.invoice_line_id),
                None,
            )
            if invoice_line is None:
                raise DebitNoteError(
                    f"return line {line.line_no} of {note.number!r} names another invoice"
                )
            already = session.scalar(
                select(func.coalesce(func.sum(DebitNoteLine.quantity), 0))
                .join(DebitNote, DebitNote.id == DebitNoteLine.note_id)
                .where(
                    DebitNoteLine.invoice_line_id == invoice_line.id,
                    DebitNote.status == POSTED,
                    DebitNote.id != note.id,
                )
            )
            current_by_invoice_line[invoice_line.id] = (
                current_by_invoice_line.get(invoice_line.id, Decimal(0)) + line.quantity
            )
            if _amount(already or 0) + current_by_invoice_line[invoice_line.id] > invoice_line.quantity:
                raise DebitNoteError(
                    f"return quantity for invoice line {invoice_line.line_no} exceeds"
                    f" its invoiced quantity of {invoice_line.quantity}"
                )

    return_costs: dict[uuid.UUID, Decimal] = {}
    # A return takes the stock out first, so a location that does not hold it refuses
    # the whole note before anything is posted.
    if note.kind == RETURN:
        for line in note.lines:
            if line.item_id is None:
                raise DebitNoteError(
                    f"line {line.line_no} of {note.number!r} has no stock item; a return"
                    " gives goods back, so a service is an adjustment instead"
                )
            held = on_hand(
                session,
                company_id=note.company_id,
                item_id=line.item_id,
                location_id=note.location_id,
            )["quantity"]
            if line.quantity > held:
                raise DebitNoteError(
                    f"{note.location.code} holds {held} of the item on line"
                    f" {line.line_no}; returning {line.quantity} would take it negative"
                )
            return_costs[line.id] = value_issue(
                session,
                company_id=note.company_id,
                item=session.get(Item, line.item_id),
                quantity=line.quantity,
                location_id=note.location_id,
            )

    lines: list[dict[str, Any]] = []
    for line in note.lines:
        key = INVENTORY_KEY if line.item_id is not None else EXPENSE_KEY
        amount = return_costs.get(line.id, note_net(line))
        lines.append(
            {
                "account": mapped_account(session, company_id=note.company_id, key=key).code,
                "credit": amount,
            }
        )
        if note.kind == RETURN:
            variance = (note_net(line) - amount).quantize(Decimal("0.000001"))
            if variance != 0:
                variance_account = mapped_account(
                    session, company_id=note.company_id, key="stock_issue"
                ).code
                lines.append(
                    {
                        "account": variance_account,
                        ("credit" if variance > 0 else "debit"): abs(variance),
                    }
                )
    if note.tax_amount > 0:
        lines.append(
            {
                "account": mapped_account(
                    session, company_id=note.company_id, key=INPUT_TAX_KEY
                ).code,
                "credit": note.tax_amount,
            }
        )
    lines.append(
        {
            "account": mapped_account(
                session, company_id=note.company_id, key=PAYABLES_KEY
            ).code,
            "debit": note.gross_amount,
        }
    )
    entry = post_journal_entry(
        session,
        company_id=note.company_id,
        posting_date=note.note_date,
        currency=note.currency,
        memo=f"debit note {note.number} ({note.kind}) against {invoice.number}",
        source_type="debit_note",
        source_id=note.id,
        lines=lines,
    )

    if note.kind == RETURN:
        for line in note.lines:
            item = session.get(Item, line.item_id)
            cost = return_costs[line.id]
            movement = record_movement(
                session,
                item=item,
                location=note.location,
                quantity=-line.quantity,
                value=-cost,
                currency=note.currency,
                source_type="debit_note",
                source_id=note.id,
                posting_date=note.note_date,
            )
            line.movement_id = movement.id

    # What the supplier now owes back is settled against the invoice, so the aging and
    # the control-account reconciliation see it without knowing about debit notes.
    settle(
        session,
        invoice,
        amount=note.gross_amount if note.gross_amount <= outstanding else outstanding,
        settled_on=note.note_date,
        source_type="debit_note",
        source_id=note.id,
    )
    note.journal_entry_id = entry.id
    note.status = POSTED
    note.posted_at = datetime.now(timezone.utc)
    session.flush()
    return entry


def debit_notes_for_invoice(session: Session, invoice: SupplierInvoice) -> list[DebitNote]:
    """Every note raised against one invoice, oldest first."""
    return list(
        session.scalars(
            select(DebitNote)
            .where(DebitNote.invoice_id == invoice.id)
            .order_by(DebitNote.created_at, DebitNote.number)
        )
    )


def noted_amount(session: Session, invoice: SupplierInvoice) -> Decimal:
    """How much of the invoice has been given back by debit notes."""
    total = session.scalar(
        select(func.coalesce(func.sum(DebitNote.gross_amount), 0)).where(
            DebitNote.invoice_id == invoice.id, DebitNote.status == POSTED
        )
    )
    return _amount(total or 0).quantize(Decimal("0.000001"))


def debit_note_by_number(
    session: Session, *, company_id: uuid.UUID, number: str
) -> DebitNote:
    """The note a later document quotes, or a refusal naming what is missing."""
    found = session.scalar(
        select(DebitNote).where(
            DebitNote.company_id == company_id, DebitNote.number == str(number).strip()
        )
    )
    if found is None:
        raise DebitNoteError(f"no debit note {number!r} in this company")
    return found
