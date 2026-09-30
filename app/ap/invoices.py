"""T-2.AP.01 — the supplier invoice, its posting, and what is still owed on it.

The invoice is where procurement becomes a liability. Three decisions shape this
module:

* **An invoice posts through the one interface.** :func:`post_invoice` calls
  :func:`app.ledger.posting.post_journal_entry` with the payables control account on
  the credit side and the cost on the debit side, both resolved through
  T-1.ACCT.03's **account mapping** — `payables`, `input_tax` and, per line,
  `inventory` for an item or `expense` for anything else. No account code is written
  into this module, so which account is the control account stays a configuration
  question, and the entry balances by construction.
* **A duplicate is refused, not discovered later.** The same supplier, the same
  invoice number and the same amount is a re-keyed invoice, and paying it twice is
  the classic AP loss — so the pair is unique in the database *and* checked here, where
  a useful message can be given.
* **What is owed is derived from settlements, never stored as a balance.**
  :class:`SupplierInvoiceSettlement` is append-only and T-2.AP.03 and T-2.AP.04 write
  it, so :func:`open_amount` is the invoice total less what has been settled and the
  later aging, reconciliation and payment run all read the same figure instead of
  three copies of it.

A foreign-currency invoice keeps its currency, and the entry it posts carries the rate
*for its own posting date* (T-1.ACCT.05), so its base amount is derivable exactly and
never re-read at today's rate.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
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

from app.audit import append_only
from app.company import company_base_currency
from app.db import Base
from app.ledger.currency import currency_by_code
from app.ledger.mapping import mapped_account
from app.ledger.posting import JournalEntry, post_journal_entry
from app.procurement.suppliers import Supplier

# One money scale for the whole platform.
MONEY = Numeric(20, 6)

# A document is a draft until it is posted; posting is what writes the ledger.
DRAFT, POSTED = "draft", "posted"

# The mapping keys this module books through. `inventory` is T-1.INV.07's own key, so
# a stock invoice debits the same account a stock receipt credits.
PAYABLES_KEY = "payables"
INPUT_TAX_KEY = "input_tax"
EXPENSE_KEY = "expense"
INVENTORY_KEY = "inventory"
GRNI_KEY = "stock_receipt"


class InvoiceError(ValueError):
    """The supplier invoice refused what was asked of it."""


class DuplicateInvoiceError(InvoiceError):
    """That supplier has already had this invoice entered."""


class InvoiceStateError(InvoiceError):
    """The asked-for change does not apply to the invoice's state."""


class OverSettlementError(InvoiceError):
    """More is being settled against the invoice than it is owed."""


class SupplierInvoice(Base):
    """One supplier invoice: what is owed, to whom, and by when."""

    __tablename__ = "supplier_invoice"
    __table_args__ = (
        UniqueConstraint("company_id", "number", name="uq_supplier_invoice_company_number"),
        # The same invoice from the same supplier is a duplicate, whatever number this
        # platform happens to give it — this is the loss the rule exists to stop.
        UniqueConstraint(
            "company_id",
            "supplier_id",
            "supplier_reference",
            "gross_amount",
            name="uq_supplier_invoice_duplicate",
        ),
        CheckConstraint("status IN ('draft', 'posted')", name="ck_supplier_invoice_status"),
        CheckConstraint("net_amount >= 0", name="ck_supplier_invoice_net"),
        CheckConstraint("tax_amount >= 0", name="ck_supplier_invoice_tax"),
        CheckConstraint("gross_amount >= 0", name="ck_supplier_invoice_gross"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    # This platform's own number for the document.
    number: Mapped[str] = mapped_column(String(32), nullable=False)
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("supplier.id"), nullable=False, index=True
    )
    # The supplier's own invoice number — what a human sees on the paper.
    supplier_reference: Mapped[str] = mapped_column(String(64), nullable=False)
    # The documents that produced it, where there are any: the order it was raised
    # against and the receipt the goods arrived on (T-2.MATCH.01 compares all three).
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("purchase_order.id"), index=True
    )
    receipt_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("goods_receipt.id"), index=True
    )
    invoice_date: Mapped[date] = mapped_column(Date, nullable=False)
    # When it falls due: the invoice date plus the supplier's own terms (T-2.PROC.01).
    due_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    net_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    gross_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=DRAFT)
    journal_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("journal_entry.id"), index=True
    )
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    supplier: Mapped[Supplier] = relationship()
    lines: Mapped[list[SupplierInvoiceLine]] = relationship(
        back_populates="invoice", order_by="SupplierInvoiceLine.line_no"
    )
    settlements: Mapped[list[SupplierInvoiceSettlement]] = relationship(
        back_populates="invoice", order_by="SupplierInvoiceSettlement.settled_on"
    )


class SupplierInvoiceLine(Base):
    """One charged line: what was bought, at what price, and its share of the tax."""

    __tablename__ = "supplier_invoice_line"
    __table_args__ = (
        UniqueConstraint("invoice_id", "line_no", name="uq_supplier_invoice_line_no"),
        CheckConstraint("line_no >= 1", name="ck_supplier_invoice_line_starts_at_one"),
        CheckConstraint("quantity > 0", name="ck_supplier_invoice_line_quantity"),
        CheckConstraint("unit_price >= 0", name="ck_supplier_invoice_line_price"),
        CheckConstraint("tax_amount >= 0", name="ck_supplier_invoice_line_tax"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("supplier_invoice.id"), nullable=False, index=True
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(String(200), nullable=False)
    item_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("item.id"), index=True)
    quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    # The ordered and received lines this invoice is for, where it is for one — what
    # the three-way match compares (T-2.MATCH.01).
    order_line_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("purchase_order_line.id"), index=True
    )
    receipt_line_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("goods_receipt_line.id"), index=True
    )

    invoice: Mapped[SupplierInvoice] = relationship(back_populates="lines")


class SupplierInvoiceSettlement(Base):
    """One reduction of what an invoice is owed — a payment, or a debit note.

    Append-only, and written by whatever settles the invoice (T-2.AP.03's debit note,
    T-2.AP.04's payment run). A balance stored beside the invoice could drift from the
    documents that moved it; this table is the documents.
    """

    __tablename__ = "supplier_invoice_settlement"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_settlement_amount"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("supplier_invoice.id"), nullable=False, index=True
    )
    settled_on: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    # What settled it: "payment_batch" or "debit_note", with that document's id.
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    invoice: Mapped[SupplierInvoice] = relationship(back_populates="settlements")


# A settlement is history: it is written once and never edited or removed.
append_only(SupplierInvoiceSettlement.__table__)


def _amount(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def line_net(line: SupplierInvoiceLine) -> Decimal:
    """What one line costs before its tax."""
    return (line.quantity * line.unit_price).quantize(Decimal("0.000001"))


def create_invoice(
    session: Session,
    *,
    company_id: uuid.UUID,
    number: str,
    supplier: Supplier,
    supplier_reference: str,
    invoice_date: date,
    lines: Any,
    currency: str | None = None,
    order_id: uuid.UUID | None = None,
    receipt_id: uuid.UUID | None = None,
    terms_days: int | None = None,
) -> SupplierInvoice:
    """Record a supplier invoice, with its net, tax and gross computed from its lines.

    The currency defaults to the supplier's own (`transaction_currency`, T-2.PROC.01)
    and then to nothing stated, which is the company's base currency — never a guess at
    a third one. The due date is the invoice date plus the supplier's payment terms.
    """
    if supplier.company_id != company_id:
        raise InvoiceError("that supplier belongs to another company")
    wanted = str(number).strip()
    if not wanted:
        raise InvoiceError("an invoice number is required")
    if session.scalar(
        select(SupplierInvoice).where(
            SupplierInvoice.company_id == company_id, SupplierInvoice.number == wanted
        )
    ) is not None:
        raise DuplicateInvoiceError(f"invoice {wanted!r} already exists in this company")
    reference = str(supplier_reference).strip()
    if not reference:
        raise InvoiceError("the supplier's own invoice number is required")
    entries = list(lines)
    if not entries:
        raise InvoiceError(f"invoice {wanted!r} has no lines")

    # A linked invoice is a claim on one coherent procurement chain.  Resolve the
    # headers before creating the invoice so a bad link cannot leave a half-document.
    order = receipt = None
    if order_id is not None or receipt_id is not None:
        if order_id is None or receipt_id is None:
            raise InvoiceError("an invoice chain names both an order and a receipt")
        from app.procurement.orders import PurchaseOrder
        from app.procurement.receipts import GoodsReceipt

        order = session.get(PurchaseOrder, order_id)
        receipt = session.get(GoodsReceipt, receipt_id)
        if order is None or order.company_id != company_id:
            raise InvoiceError("the invoice names no purchase order in this company")
        if receipt is None or receipt.company_id != company_id:
            raise InvoiceError("the invoice names no goods receipt in this company")
        if order.supplier_id != supplier.id or receipt.order_id != order.id:
            raise InvoiceError("the invoice order, receipt and supplier are not one chain")
        if receipt.status != POSTED:
            raise InvoiceError("the invoice receipt must be posted")

    terms = supplier.payment_terms_days if terms_days is None else int(terms_days)

    # The totals are computed from the stated lines **before** the row exists, so a
    # duplicate is refused by name here rather than by an index name at flush.
    checked = []
    for raw in entries:
        quantity = _amount(raw["quantity"])
        price = _amount(raw["unit_price"])
        tax = _amount(raw.get("tax_amount", 0))
        if quantity <= 0 or price < 0 or tax < 0:
            raise InvoiceError(
                f"a line states a positive quantity, a non-negative price and tax; got"
                f" {quantity}, {price}, {tax}"
            )
        order_line_id = raw.get("order_line_id")
        receipt_line_id = raw.get("receipt_line_id")
        if order_line_id is not None or receipt_line_id is not None:
            if order is None or receipt is None or order_line_id is None or receipt_line_id is None:
                raise InvoiceError("a linked invoice line names both an order and receipt line")
            from app.procurement.orders import PurchaseOrderLine
            from app.procurement.receipts import GoodsReceiptLine

            order_line = session.get(PurchaseOrderLine, order_line_id)
            receipt_line = session.get(GoodsReceiptLine, receipt_line_id)
            if (
                order_line is None
                or receipt_line is None
                or order_line.order_id != order.id
                or receipt_line.receipt_id != receipt.id
                or receipt_line.order_line_id != order_line.id
                or raw.get("item_id") != order_line.item_id
            ):
                raise InvoiceError(
                    "the invoice line's order and receipt links do not form its document chain"
                )
        checked.append((raw, quantity, price, tax))
    net = sum(
        ((quantity * price).quantize(Decimal("0.000001")) for _, quantity, price, _ in checked),
        Decimal(0),
    ).quantize(Decimal("0.000001"))
    tax_total = sum((tax for _, _, _, tax in checked), Decimal(0)).quantize(Decimal("0.000001"))
    gross = (net + tax_total).quantize(Decimal("0.000001"))

    duplicate = session.scalar(
        select(SupplierInvoice).where(
            SupplierInvoice.company_id == company_id,
            SupplierInvoice.supplier_id == supplier.id,
            SupplierInvoice.supplier_reference == reference,
            SupplierInvoice.gross_amount == gross,
        )
    )
    if duplicate is not None:
        raise DuplicateInvoiceError(
            f"{reference!r} for {gross} has already been entered as"
            f" {duplicate.number!r}; the same invoice is not entered twice"
        )

    invoice = SupplierInvoice(
        company_id=company_id,
        number=wanted,
        supplier_id=supplier.id,
        supplier_reference=reference,
        order_id=order_id,
        receipt_id=receipt_id,
        invoice_date=invoice_date,
        due_date=invoice_date + timedelta(days=terms),
        currency=currency_by_code(
            session,
            company_id=company_id,
            # An unstated currency is the company's base currency — the supplier's own
            # terms if it has any, and never a third guess.
            code=currency
            or supplier.transaction_currency
            or company_base_currency(session, company_id=company_id),
        ).code,
        net_amount=net,
        tax_amount=tax_total,
        gross_amount=gross,
        status=DRAFT,
    )
    session.add(invoice)
    session.flush()
    for raw, quantity, price, tax in checked:
        invoice.lines.append(
            SupplierInvoiceLine(
                company_id=company_id,
                invoice_id=invoice.id,
                line_no=len(invoice.lines) + 1,
                description=str(raw.get("description", "")).strip() or "—",
                item_id=raw.get("item_id"),
                quantity=quantity,
                unit_price=price,
                tax_amount=tax,
                order_line_id=raw.get("order_line_id"),
                receipt_line_id=raw.get("receipt_line_id"),
            )
        )
    session.flush()
    return invoice


def post_invoice(
    session: Session, invoice: SupplierInvoice, *, posting_date: date | None = None
) -> JournalEntry:
    """Post the invoice: payables credited, the cost debited, the tax apart.

    The entry is one debit per line (to `inventory` when the line is for a stock item,
    `expense` otherwise), one debit for the tax as a whole to `input_tax`, and one
    credit for the gross to `payables` — all of them mapping keys, so no account code
    is fixed here. Balanced by construction; the primitive refuses it otherwise.
    """
    if invoice.status == POSTED:
        raise InvoiceStateError(
            f"invoice {invoice.number!r} is already posted; posting it again would"
            " double the liability"
        )
    from app.procurement.tax import require_supplier_tax

    require_supplier_tax(session, invoice.supplier, document_type="supplier_invoice")
    lines: list[dict[str, Any]] = []
    for line in invoice.lines:
        # A received stock line reverses GRNI; inventory was already debited by the
        # receipt.  Only an unreceived stock purchase is booked to inventory here.
        key = (
            GRNI_KEY
            if line.receipt_line_id is not None
            else (INVENTORY_KEY if line.item_id is not None else EXPENSE_KEY)
        )
        lines.append(
            {"account": mapped_account(session, company_id=invoice.company_id, key=key).code,
             "debit": line_net(line)}
        )
    if invoice.tax_amount > 0:
        lines.append(
            {
                "account": mapped_account(
                    session, company_id=invoice.company_id, key=INPUT_TAX_KEY
                ).code,
                "debit": invoice.tax_amount,
            }
        )
    lines.append(
        {
            "account": mapped_account(
                session, company_id=invoice.company_id, key=PAYABLES_KEY
            ).code,
            "credit": invoice.gross_amount,
        }
    )
    entry = post_journal_entry(
        session,
        company_id=invoice.company_id,
        posting_date=posting_date or invoice.invoice_date,
        currency=invoice.currency,
        memo=f"supplier invoice {invoice.number} ({invoice.supplier_reference})",
        source_type="supplier_invoice",
        source_id=invoice.id,
        lines=lines,
    )
    invoice.journal_entry_id = entry.id
    invoice.status = POSTED
    invoice.posted_at = datetime.now(timezone.utc)
    session.flush()
    return entry


def settled_amount(
    session: Session, invoice: SupplierInvoice, *, as_of: date | None = None
) -> Decimal:
    """How much of the invoice has been settled, from the settlement rows themselves."""
    statement = select(func.coalesce(func.sum(SupplierInvoiceSettlement.amount), 0)).where(
        SupplierInvoiceSettlement.invoice_id == invoice.id
    )
    if as_of is not None:
        statement = statement.where(SupplierInvoiceSettlement.settled_on <= as_of)
    total = session.scalar(statement)
    return _amount(total or 0).quantize(Decimal("0.000001"))


def open_amount(
    session: Session, invoice: SupplierInvoice, *, as_of: date | None = None
) -> Decimal:
    """What is still owed on the invoice — the total less what settled it.

    The one figure T-2.AP.02 ages, T-2.AP.04 selects for payment and T-2.AP.05
    reconciles against the control account: derived, so the three cannot disagree.
    """
    return (invoice.gross_amount - settled_amount(session, invoice, as_of=as_of)).quantize(
        Decimal("0.000001")
    )


def settle(
    session: Session,
    invoice: SupplierInvoice,
    *,
    amount: Any,
    settled_on: date,
    source_type: str,
    source_id: uuid.UUID,
) -> SupplierInvoiceSettlement:
    """Reduce what the invoice is owed, recording what did it.

    Refuses to settle more than is open: an over-settlement is a payment or a debit
    note that does not belong to this invoice, and absorbing it here would hide the
    mistake in a balance instead of stopping it.
    """
    if invoice.status != POSTED:
        raise InvoiceStateError(
            f"invoice {invoice.number!r} is {invoice.status}; nothing is owed on it yet"
        )
    value = _amount(amount)
    if value <= 0:
        raise InvoiceError(f"a settlement is above zero, got {value}")
    # Lock the invoice row before deriving the ceiling.  Two payment workers must not
    # both observe the same open amount and append settlements over it.
    locked = session.scalar(
        select(SupplierInvoice)
        .where(SupplierInvoice.id == invoice.id)
        .with_for_update()
    )
    if locked is None:  # pragma: no cover - the caller supplied a persistent invoice
        raise InvoiceError(f"invoice {invoice.number!r} no longer exists")
    invoice = locked
    outstanding = open_amount(session, invoice)
    if value > outstanding:
        raise OverSettlementError(
            f"invoice {invoice.number!r} is owed {outstanding}; settling {value} would"
            " over-settle it"
        )
    settlement = SupplierInvoiceSettlement(
        company_id=invoice.company_id,
        invoice_id=invoice.id,
        settled_on=settled_on,
        amount=value,
        source_type=str(source_type),
        source_id=source_id,
    )
    session.add(settlement)
    session.flush()
    return settlement


def open_invoices(
    session: Session,
    *,
    company_id: uuid.UUID,
    supplier: Supplier | None = None,
    as_of: date | None = None,
) -> list[SupplierInvoice]:
    """Every posted invoice of this company, oldest first — the aging population.

    A settled invoice stays in the list: its `open_amount` is zero, and whether to
    show it is the report's decision, not this lookup's.
    """
    statement = select(SupplierInvoice).where(
        SupplierInvoice.company_id == company_id,
        SupplierInvoice.status == POSTED,
    )
    if supplier is not None:
        statement = statement.where(SupplierInvoice.supplier_id == supplier.id)
    if as_of is not None:
        statement = statement.where(SupplierInvoice.invoice_date <= as_of)
    return list(
        session.scalars(statement.order_by(SupplierInvoice.due_date, SupplierInvoice.number))
    )


def invoice_by_number(
    session: Session, *, company_id: uuid.UUID, number: str
) -> SupplierInvoice:
    """The invoice a later document quotes, or a refusal naming what is missing."""
    found = session.scalar(
        select(SupplierInvoice).where(
            SupplierInvoice.company_id == company_id,
            SupplierInvoice.number == str(number).strip(),
        )
    )
    if found is None:
        raise InvoiceError(f"no supplier invoice {number!r} in this company")
    return found
