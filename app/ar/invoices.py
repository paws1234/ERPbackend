"""T-3.AR.01 — the customer invoice, its posting, and what is still owed on it.

The invoice is where an order becomes a receivable. Four decisions shape this
module:

* **An invoice posts through the one interface.** :func:`post_invoice` calls
  :func:`app.ledger.posting.post_journal_entry` with the receivables control
  account on the debit side and the revenue and the tax on the credit side, all
  resolved through T-1.ACCT.03's **account mapping** — `receivables`, `revenue` and
  `output_tax`. No account code is written into this module, so which account is
  the control account stays a configuration question, and the entry balances by
  construction.
* **An invoice moves no stock.** Goods that have shipped already left the shelf
  through T-3.SALES.05's `issue`, so invoicing them again would double the issue
  and the cost of sales with it. An invoice raised for goods that shipped therefore
  **references the shipment** it bills, the posting touches no stock account, and
  T-1.INV.07's stock-to-GL reconciliation stays true. (It *credits* revenue; the
  stock side was booked when the goods left.)
* **A duplicate is refused, not discovered later.** The same customer, the same
  order and the same amount is a re-keyed invoice, and billing twice is the classic
  AR loss — so the triple is unique in the database *and* checked here, where a
  useful message can be given.
* **What is owed is derived from settlements, never stored as a balance.**
  :class:`CustomerInvoiceSettlement` is append-only and T-3.AR.05 writes it, so
  :func:`open_amount` is the invoice total less what has been settled, and the
  later aging, dunning, exposure and reconciliation all read the same figure
  instead of four copies of it.

Tax is applied per the **active pack** (T-0.LOC.01) through
:mod:`app.sales.tax`, which resolves the one rule a sales order, its invoice and a
POS sale share — so the Revenue account and the amount the pack's VAT-OUT-12
produces cannot disagree between the document and the till.

A foreign-currency invoice keeps its currency, and the entry it posts carries the
rate *for its own posting date* (T-1.ACCT.05), so its base amount is derivable
exactly and never re-read at today's rate.
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
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    func,
    select,
    text,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.audit import append_only
from app.company import company_base_currency
from app.db import Base
from app.ledger.currency import currency_by_code
from app.ledger.mapping import mapped_account
from app.ledger.posting import JournalEntry, post_journal_entry
from app.sales.customers import Customer
from app.sales.tax import tax_on

# One money scale for the whole platform.
MONEY = Numeric(20, 6)

# A document is a draft until it is posted; posting is what writes the ledger.
DRAFT, POSTED = "draft", "posted"

# The mapping keys this module books through. `receivables` is the control account
# T-3.AR.07 reconciles the subledger against and `output_tax` is the liability the
# pack's selling rule produces; neither is an account code fixed here.
RECEIVABLES_KEY = "receivables"
REVENUE_KEY = "revenue"
OUTPUT_TAX_KEY = "output_tax"


class InvoiceError(ValueError):
    """The customer invoice refused what was asked of it."""


class DuplicateInvoiceError(InvoiceError):
    """That customer has already had this invoice entered."""


class InvoiceStateError(InvoiceError):
    """The asked-for change does not apply to the invoice's state."""


class OverSettlementError(InvoiceError):
    """More is being settled against the invoice than it is owed."""


class CustomerInvoice(Base):
    """One customer invoice: what is owed, by whom, and by when."""

    __tablename__ = "customer_invoice"
    __table_args__ = (
        UniqueConstraint("company_id", "number", name="uq_customer_invoice_company_number"),
        # The same order billed the same amount twice is the same invoice re-keyed —
        # this is the loss the rule exists to stop. Partial rather than plain: a
        # standalone invoice has no order, and "no order" is not a value two of them
        # can collide on, so `order_id IS NULL` rows are left out of the index
        # entirely rather than being made to look alike.
        Index(
            "uq_customer_invoice_duplicate",
            "company_id",
            "customer_id",
            "order_id",
            "gross_amount",
            unique=True,
            postgresql_where=text("order_id IS NOT NULL"),
        ),
        CheckConstraint("status IN ('draft', 'posted')", name="ck_customer_invoice_status"),
        CheckConstraint("net_amount >= 0", name="ck_customer_invoice_net"),
        CheckConstraint("tax_amount >= 0", name="ck_customer_invoice_tax"),
        CheckConstraint("gross_amount >= 0", name="ck_customer_invoice_gross"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    # This platform's own number for the document.
    number: Mapped[str] = mapped_column(String(32), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customer.id"), nullable=False, index=True
    )
    # The documents that produced it, where there are any: the order it bills and
    # the shipment the goods left on. An invoice for goods already shipped names
    # both, so nothing has to be re-keyed and no stock is issued a second time.
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sales_order.id"), index=True
    )
    shipment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("shipment.id"), index=True
    )
    invoice_date: Mapped[date] = mapped_column(Date, nullable=False)
    # When it falls due: the invoice date plus the customer's own terms (T-3.SALES.01).
    due_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    net_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    gross_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    # The pack's tax rule the whole document was charged under (T-3.AR.01's
    # "tax is applied per the active pack"), recorded so a later reader knows which
    # classification produced the figure rather than re-deriving it from today's pack.
    tax_rule_code: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=DRAFT)
    journal_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("journal_entry.id"), index=True
    )
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    customer: Mapped[Customer] = relationship()
    lines: Mapped[list[CustomerInvoiceLine]] = relationship(
        back_populates="invoice", order_by="CustomerInvoiceLine.line_no"
    )
    settlements: Mapped[list[CustomerInvoiceSettlement]] = relationship(
        back_populates="invoice", order_by="CustomerInvoiceSettlement.settled_on"
    )


class CustomerInvoiceLine(Base):
    """One billed line: what was sold, at what price, and its share of the tax."""

    __tablename__ = "customer_invoice_line"
    __table_args__ = (
        UniqueConstraint("invoice_id", "line_no", name="uq_customer_invoice_line_no"),
        CheckConstraint("line_no >= 1", name="ck_customer_invoice_line_starts_at_one"),
        CheckConstraint("quantity > 0", name="ck_customer_invoice_line_quantity"),
        CheckConstraint("unit_price >= 0", name="ck_customer_invoice_line_price"),
        CheckConstraint("tax_amount >= 0", name="ck_customer_invoice_line_tax"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customer_invoice.id"), nullable=False, index=True
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(String(200), nullable=False)
    item_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("item.id"), index=True)
    quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    uom: Mapped[str | None] = mapped_column(String(16))
    unit_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    # The classification this line was charged under, where it differs from the
    # document's — a zero-rated export among standard-rated lines (T-0.LOC.01's pack
    # states the codes; the document states which one applies).
    tax_rule_code: Mapped[str | None] = mapped_column(String(32))
    # The ordered and shipped lines this invoice is for, where it is for them — what
    # ties the bill back to the goods that left without re-keying either.
    order_line_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sales_order_line.id"), index=True
    )
    shipment_line_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("shipment_line.id"), index=True
    )

    invoice: Mapped[CustomerInvoice] = relationship(back_populates="lines")


class CustomerInvoiceSettlement(Base):
    """One reduction of what an invoice is owed — a receipt.

    Append-only, and written by whatever settles the invoice (T-3.AR.05's gateway
    settlement, a counter payment). A balance stored beside the invoice could drift
    from the documents that moved it; this table is the documents.
    """

    __tablename__ = "customer_invoice_settlement"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_customer_settlement_amount"),
# One document settles one invoice once: the same gateway payment (or receipt)
        # arriving twice must not take the amount off the customer's account twice.
        # T-3.AR.05's boundary already refuses a replayed delivery; this is the guard
        # that holds even for a caller that skips it.
        UniqueConstraint(
            "invoice_id",
            "source_type",
            "source_id",
            name="uq_customer_settlement_once_per_source",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customer_invoice.id"), nullable=False, index=True
    )
    settled_on: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    # What settled it: "gateway_payment" or "receipt", with that document's id.
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    invoice: Mapped[CustomerInvoice] = relationship(back_populates="settlements")


# A settlement is history: it is written once and never edited or removed.
append_only(CustomerInvoiceSettlement.__table__)


def _amount(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def line_net(line: CustomerInvoiceLine) -> Decimal:
    """What one line is billed for before its tax."""
    return (line.quantity * line.unit_price).quantize(Decimal("0.000001"))


def create_invoice(
    session: Session,
    *,
    company_id: uuid.UUID,
    number: str,
    customer: Customer,
    invoice_date: date,
    lines: Any,
    currency: str | None = None,
    order_id: uuid.UUID | None = None,
    shipment_id: uuid.UUID | None = None,
    terms_days: int | None = None,
    tax_rule_code: str | None = None,
) -> CustomerInvoice:
    """Record a customer invoice, with its net, tax and gross computed from its lines.

    The currency defaults to the customer's own (`transaction_currency`, T-3.SALES.01)
    and then to the company's base currency — never a guess at a third one. The due
    date is the invoice date plus the customer's payment terms, which is the figure
    AR aging, dunning and the credit exposure all read.

    Tax is the active pack's rule (:func:`app.sales.tax.tax_on`), applied per line —
    a line may name its own classification (a zero-rated export beside standard-rated
    goods) and everything unnamed takes the document's rule.
    """
    if customer.company_id != company_id:
        raise InvoiceError("that customer belongs to another company")
    wanted = str(number).strip()
    if not wanted:
        raise InvoiceError("an invoice number is required")
    if session.scalar(
        select(CustomerInvoice).where(
            CustomerInvoice.company_id == company_id, CustomerInvoice.number == wanted
        )
    ) is not None:
        raise DuplicateInvoiceError(f"invoice {wanted!r} already exists in this company")
    entries = list(lines)
    if not entries:
        raise InvoiceError(f"invoice {wanted!r} has no lines")

    # A linked invoice is a claim on one coherent selling chain.  Resolve the
    # headers before creating the invoice so a bad link cannot leave a half-document.
    order = shipment = None
    if order_id is not None or shipment_id is not None:
        if order_id is None or shipment_id is None:
            raise InvoiceError("an invoice chain names both an order and a shipment")
        from app.sales.fulfilment import Shipment
        from app.sales.orders import SalesOrder

        order = session.get(SalesOrder, order_id)
        shipment = session.get(Shipment, shipment_id)
        if order is None or order.company_id != company_id:
            raise InvoiceError("the invoice names no sales order in this company")
        if shipment is None or shipment.company_id != company_id:
            raise InvoiceError("the invoice names no shipment in this company")
        if order.customer_id != customer.id or shipment.order_id != order.id:
            raise InvoiceError("the invoice order, shipment and customer are not one chain")

    terms = customer.payment_terms_days if terms_days is None else int(terms_days)

    # The totals are computed from the stated lines **before** the row exists, so a
    # duplicate is refused by name here rather than by an index name at flush.
    checked = []
    for raw in entries:
        quantity = _amount(raw["quantity"])
        price = _amount(raw["unit_price"])
        if quantity <= 0 or price < 0:
            raise InvoiceError(
                f"a line states a positive quantity and a non-negative price; got"
                f" {quantity}, {price}"
            )
        order_line_id = raw.get("order_line_id")
        shipment_line_id = raw.get("shipment_line_id")
        if order_line_id is not None or shipment_line_id is not None:
            if order is None or shipment is None or order_line_id is None or shipment_line_id is None:
                raise InvoiceError("a linked invoice line names both an order and shipment line")
            from app.sales.fulfilment import ShipmentLine
            from app.sales.orders import SalesOrderLine

            order_line = session.get(SalesOrderLine, order_line_id)
            shipment_line = session.get(ShipmentLine, shipment_line_id)
            if (
                order_line is None
                or shipment_line is None
                or order_line.order_id != order.id
                or shipment_line.shipment_id != shipment.id
                or shipment_line.order_line_id != order_line.id
                or raw.get("item_id") != order_line.item_id
            ):
                raise InvoiceError(
                    "the invoice line's order and shipment links do not form its document chain"
                )
        # The line's classification: its own, else the document's, else the pack's
        # default selling rule.  `tax_on` refuses a code the pack does not apply to a
        # selling document, so a buying-side rule cannot be charged on a sale.
        rule_code = raw.get("tax_rule_code", tax_rule_code)
        tax = tax_on(quantity * price, rule_code=rule_code)
        checked.append((raw, quantity, price, tax))
    net = sum(
        ((quantity * price).quantize(Decimal("0.000001")) for _, quantity, price, _ in checked),
        Decimal(0),
    ).quantize(Decimal("0.000001"))
    tax_total = sum((tax["tax"] for _, _, _, tax in checked), Decimal(0)).quantize(
        Decimal("0.000001")
    )
    gross = (net + tax_total).quantize(Decimal("0.000001"))

    if order is not None:
        duplicate = session.scalar(
            select(CustomerInvoice).where(
                CustomerInvoice.company_id == company_id,
                CustomerInvoice.customer_id == customer.id,
                CustomerInvoice.order_id == order.id,
                CustomerInvoice.gross_amount == gross,
            )
        )
        if duplicate is not None:
            raise DuplicateInvoiceError(
                f"order {order.number!r} has already been invoiced for {gross} as"
                f" {duplicate.number!r}; the same invoice is not raised twice"
            )

    invoice = CustomerInvoice(
        company_id=company_id,
        number=wanted,
        customer_id=customer.id,
        order_id=order_id,
        shipment_id=shipment_id,
        invoice_date=invoice_date,
        due_date=invoice_date + timedelta(days=terms),
        currency=currency_by_code(
            session,
            company_id=company_id,
            # An unstated currency is the company's base currency — the customer's own
            # terms if it has any, and never a third guess.
            code=currency
            or customer.transaction_currency
            or company_base_currency(session, company_id=company_id),
        ).code,
        net_amount=net,
        tax_amount=tax_total,
        gross_amount=gross,
        tax_rule_code=tax_rule_code,
        status=DRAFT,
    )
    session.add(invoice)
    session.flush()
    for raw, quantity, price, tax in checked:
        invoice.lines.append(
            CustomerInvoiceLine(
                company_id=company_id,
                invoice_id=invoice.id,
                line_no=len(invoice.lines) + 1,
                description=str(raw.get("description", "")).strip() or "—",
                item_id=raw.get("item_id"),
                quantity=quantity,
                uom=raw.get("uom"),
                unit_price=price,
                tax_amount=tax["tax"],
                tax_rule_code=raw.get("tax_rule_code", tax_rule_code),
                order_line_id=raw.get("order_line_id"),
                shipment_line_id=raw.get("shipment_line_id"),
            )
        )
    session.flush()
    return invoice


def post_invoice(
    session: Session, invoice: CustomerInvoice, *, posting_date: date | None = None
) -> JournalEntry:
    """Post the invoice: receivables debited, revenue and the tax credited.

    The entry is one credit per line to `revenue`, one credit for the tax as a whole
    to `output_tax`, and one debit for the gross to `receivables` — all of them
    mapping keys, so no account code is fixed here. Balanced by construction; the
    primitive refuses it otherwise.

    No stock account is touched: the goods left the shelf when T-3.SALES.05 shipped
    them, and the shipment this invoice references is the proof that they did.
    """
    if invoice.status == POSTED:
        raise InvoiceStateError(
            f"invoice {invoice.number!r} is already posted; posting it again would"
            " double the receivable"
        )
    lines: list[dict[str, Any]] = []
    for line in invoice.lines:
        lines.append(
            {
                "account": mapped_account(
                    session, company_id=invoice.company_id, key=REVENUE_KEY
                ).code,
                "credit": line_net(line),
            }
        )
    if invoice.tax_amount > 0:
        lines.append(
            {
                "account": mapped_account(
                    session, company_id=invoice.company_id, key=OUTPUT_TAX_KEY
                ).code,
                "credit": invoice.tax_amount,
            }
        )
    lines.append(
        {
            "account": mapped_account(
                session, company_id=invoice.company_id, key=RECEIVABLES_KEY
            ).code,
            "debit": invoice.gross_amount,
        }
    )
    entry = post_journal_entry(
        session,
        company_id=invoice.company_id,
        posting_date=posting_date or invoice.invoice_date,
        currency=invoice.currency,
        memo=f"customer invoice {invoice.number}",
        source_type="customer_invoice",
        source_id=invoice.id,
        lines=lines,
    )
    invoice.journal_entry_id = entry.id
    invoice.status = POSTED
    invoice.posted_at = datetime.now(timezone.utc)
    session.flush()
    return entry


def settled_amount(
    session: Session, invoice: CustomerInvoice, *, as_of: date | None = None
) -> Decimal:
    """How much of the invoice has been settled, from the settlement rows themselves."""
    statement = select(func.coalesce(func.sum(CustomerInvoiceSettlement.amount), 0)).where(
        CustomerInvoiceSettlement.invoice_id == invoice.id
    )
    if as_of is not None:
        statement = statement.where(CustomerInvoiceSettlement.settled_on <= as_of)
    total = session.scalar(statement)
    return _amount(total or 0).quantize(Decimal("0.000001"))


def open_amount(
    session: Session, invoice: CustomerInvoice, *, as_of: date | None = None
) -> Decimal:
    """What is still owed on the invoice — the total less what settled it.

    The one figure T-3.AR.02 ages, T-3.AR.04 dunnes, T-3.AR.06 includes in the
    customer's exposure and T-3.AR.07 reconciles against the control account:
    derived, so the four cannot disagree.
    """
    return (invoice.gross_amount - settled_amount(session, invoice, as_of=as_of)).quantize(
        Decimal("0.000001")
    )


def settle(
    session: Session,
    invoice: CustomerInvoice,
    *,
    amount: Any,
    settled_on: date,
    source_type: str,
    source_id: uuid.UUID,
) -> CustomerInvoiceSettlement:
    """Reduce what the invoice is owed, recording what did it.

    Refuses to settle more than is open: an over-settlement is a receipt that does
    not belong to this invoice, and absorbing it here would hide the mistake in a
    balance instead of stopping it.
    """
    if invoice.status != POSTED:
        raise InvoiceStateError(
            f"invoice {invoice.number!r} is {invoice.status}; nothing is owed on it yet"
        )
    value = _amount(amount)
    if value <= 0:
        raise InvoiceError(f"a settlement is above zero, got {value}")
    # Lock the invoice row before deriving the ceiling.  Two settlement writers — the
    # gateway's webhook and a counter receipt — must not both observe the same open
    # amount and append settlements over it.
    locked = session.scalar(
        select(CustomerInvoice)
        .where(CustomerInvoice.id == invoice.id)
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
    settlement = CustomerInvoiceSettlement(
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
    customer: Customer | None = None,
    as_of: date | None = None,
) -> list[CustomerInvoice]:
    """Every posted invoice of this company, oldest due first — the aging population.

    A settled invoice stays in the list: its `open_amount` is zero, and whether to
    show it is the report's decision, not this lookup's.
    """
    statement = select(CustomerInvoice).where(
        CustomerInvoice.company_id == company_id,
        CustomerInvoice.status == POSTED,
    )
    if customer is not None:
        statement = statement.where(CustomerInvoice.customer_id == customer.id)
    if as_of is not None:
        statement = statement.where(CustomerInvoice.invoice_date <= as_of)
    return list(
        session.scalars(
            statement.order_by(CustomerInvoice.due_date, CustomerInvoice.number)
        )
    )


def invoice_by_number(
    session: Session, *, company_id: uuid.UUID, number: str
) -> CustomerInvoice:
    """The invoice a later document quotes, or a refusal naming what is missing."""
    found = session.scalar(
        select(CustomerInvoice).where(
            CustomerInvoice.company_id == company_id,
            CustomerInvoice.number == str(number).strip(),
        )
    )
    if found is None:
        raise InvoiceError(f"no customer invoice {number!r} in this company")
    return found
