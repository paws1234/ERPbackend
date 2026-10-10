"""T-6.PORTAL.01 — the supplier's own door: what it sees, and the three things it may do.

A supplier is an outside party, so everything here starts from one question: **whose account
is this?** A portal account is a `subject` (the actor identity a request arrives under) linked
to one supplier (T-2.PROC.01's master), and every read and every write is scoped by that link.
Nothing in this module trusts a supplier code, an id or a number the caller states to decide
what the caller may see: the account decides, and a document that is not the account's
supplier's is refused by name rather than filtered out quietly.

The three acts are the plan's, and each one is **the domain's own call**, not a second path:

* **Respond to an RFQ** — T-2.PROC.03's `record_response`, so the answer is *one record*: the
  portal's submission and an internally captured answer are the same row, and the comparison
  matrix (T-2.PROC.03's own read of the responses) shows both without either being copied.
* **Acknowledge a purchase order** — T-2.PROC.06's `acknowledge_order`, the explicit act that
  moves an approved order to `acknowledged`. An order that is not this supplier's, or that has
  not been approved, is refused.
* **Submit an invoice** — T-2.AP.01's `create_invoice`, recorded as a **draft**. The portal
  deliberately does not post it: posting is the buyer's step, and the match (T-2.MATCH.01's
  `match_invoice`) only reads a posted invoice — so a submitted invoice enters the normal
  review path and cannot be approved by the act of submitting it.

What is deliberately absent: no marketplace or discovery (the plan names none), no price list,
and no ability to see another supplier's documents through any argument.

**Two scopes, and they are not the same scope.** What this supplier may *do* is the
`portal.supplier` capability on its role (T-0.SEC.01), and *which* supplier it is is the
account. The **field** restrictions T-0.SEC.01 states are stated against company roles and the
entities they read — a buyer's view of an item or an employee — and they are not applied here:
a supplier is not a company role reading the company's records, it is a party reading its own
documents, and filtering its payload through restrictions aimed at the company's staff would
be answering a question the plan did not ask. What the payload contains is instead fixed by
this module, field by field, and that is the whole of what a supplier is shown.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    Uuid,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

if TYPE_CHECKING:  # pragma: no cover — an annotation, never an import at run time
    from app.ap.invoices import SupplierInvoice

from app.db import Base
from app.procurement.orders import (
    ACKNOWLEDGED,
    APPROVED,
    CLOSED,
    PurchaseOrder,
    acknowledge_order,
    order_total,
)
from app.procurement.rfq import (
    ISSUED,
    NotInvitedError,
    Rfq,
    RfqResponse,
    RfqSupplier,
    record_response,
    rfq_by_number,
)
from app.procurement.suppliers import Supplier, supplier_by_code

# The capability a portal account's role must hold. Named here once so the API and anything
# that seeds roles cannot disagree about it.
CAPABILITY = "portal.supplier"

# The order states a supplier is shown: what the buyer has released to it. A draft or a
# pending order is the buyer's business until it is approved — the supplier seeing one would
# be seeing a plan, not an order.
VISIBLE_ORDER_STATES = (APPROVED, ACKNOWLEDGED, CLOSED)


class PortalError(ValueError):
    """The portal refused what was asked of it."""


class NoPortalAccountError(PortalError):
    """The subject is not linked to a supplier: it has no door to be at."""


class AlreadyLinkedError(PortalError):
    """That subject already acts for a supplier; one subject, one supplier."""


class NotYoursError(PortalError):
    """The document named belongs to another supplier."""


class SupplierAccount(Base):
    """One login, one supplier: the whole of the portal's scope."""

    __tablename__ = "supplier_portal_account"
    __table_args__ = (
        UniqueConstraint("company_id", "subject", name="uq_supplier_portal_account_subject"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("supplier.id"), nullable=False, index=True
    )
    subject: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    linked_by: Mapped[str] = mapped_column(String(64), nullable=False)

    supplier: Mapped[Supplier] = relationship()


def link_account(
    session: Session,
    *,
    company_id: uuid.UUID,
    supplier: Supplier,
    subject: str,
    actor: str,
    at: datetime | None = None,
) -> SupplierAccount:
    """Link one subject to one supplier — the row that makes a portal exist for somebody.

    Administrative on purpose, like every other role assignment (T-0.SEC.01): what a supplier
    may *do* is a capability on its role, and which supplier it is is this link.
    """
    if supplier.company_id != company_id:
        raise PortalError("that supplier belongs to another company")
    who = str(subject or "").strip()
    if not who:
        raise PortalError("a portal account names the subject it is for")
    existing = session.scalar(
        select(SupplierAccount).where(
            SupplierAccount.company_id == company_id, SupplierAccount.subject == who
        )
    )
    if existing is not None:
        if existing.supplier_id == supplier.id:
            return existing
        other = session.get(Supplier, existing.supplier_id)
        raise AlreadyLinkedError(
            f"{who!r} already acts for supplier {other.party.code!r}; one login is one supplier,"
            " so a second relationship needs its own subject"
        )
    account = SupplierAccount(
        company_id=company_id,
        supplier_id=supplier.id,
        subject=who,
        linked_at=at or datetime.now(timezone.utc),
        linked_by=str(actor or "").strip() or "unknown",
    )
    session.add(account)
    session.flush()
    return account


def account_for(
    session: Session, *, company_id: uuid.UUID, subject: str
) -> SupplierAccount:
    """The supplier a subject acts for, or a refusal naming what is missing."""
    account = session.scalar(
        select(SupplierAccount).where(
            SupplierAccount.company_id == company_id,
            SupplierAccount.subject == str(subject).strip(),
        )
    )
    if account is None:
        raise NoPortalAccountError(
            f"{subject!r} is not linked to a supplier, so it has no portal to read"
        )
    return account


def _invited_rfqs(session: Session, *, account: SupplierAccount) -> list[Rfq]:
    """The RFQs this supplier was issued, newest first — and nobody else's."""
    return list(
        session.scalars(
            select(Rfq)
            .join(RfqSupplier, RfqSupplier.rfq_id == Rfq.id)
            .where(
                Rfq.company_id == account.company_id,
                RfqSupplier.supplier_id == account.supplier_id,
                Rfq.status == ISSUED,
            )
            .order_by(Rfq.number)
        )
    )


def _orders(session: Session, *, account: SupplierAccount) -> list[PurchaseOrder]:
    """The orders released to this supplier."""
    return list(
        session.scalars(
            select(PurchaseOrder)
            .where(
                PurchaseOrder.company_id == account.company_id,
                PurchaseOrder.supplier_id == account.supplier_id,
                PurchaseOrder.status.in_(VISIBLE_ORDER_STATES),
            )
            .order_by(PurchaseOrder.number)
        )
    )


def _invoices(session: Session, *, account: SupplierAccount) -> list["SupplierInvoice"]:
    """The invoices this supplier submitted — its own, never another's."""
    from app.ap.invoices import SupplierInvoice

    return list(
        session.scalars(
            select(SupplierInvoice)
            .where(
                SupplierInvoice.company_id == account.company_id,
                SupplierInvoice.supplier_id == account.supplier_id,
            )
            .order_by(SupplierInvoice.number)
        )
    )


def _answered(session: Session, *, rfq: Rfq, supplier_id: uuid.UUID) -> RfqResponse | None:
    return session.scalar(
        select(RfqResponse).where(
            RfqResponse.rfq_id == rfq.id, RfqResponse.supplier_id == supplier_id
        )
    )


def documents_for(session: Session, *, account: SupplierAccount) -> dict:
    """Everything this supplier may see, and nothing else.

    Each list is scoped by the account's supplier: an RFQ it was invited to, an order released
    to it, an invoice it submitted. What the payload carries is the supplier's own view — the
    buyer's internal state (who else was invited, what they quoted, the match findings) is
    absent rather than blank, because a field that is not there cannot be read by accident.
    """
    supplier = session.get(Supplier, account.supplier_id)
    rfqs = []
    for rfq in _invited_rfqs(session, account=account):
        answered = _answered(session, rfq=rfq, supplier_id=account.supplier_id)
        rfqs.append(
            {
                "number": rfq.number,
                "issued_on": rfq.issued_on,
                "response_deadline": rfq.response_deadline,
                "currency": rfq.currency,
                "status": rfq.status,
                "lines": [
                    {
                        "line_no": line.line_no,
                        "description": line.description,
                        "quantity": Decimal(line.quantity),
                        "uom": line.uom,
                    }
                    for line in rfq.lines
                ],
                "answered": answered is not None,
                "answered_on": None if answered is None else answered.received_on,
            }
        )
    orders = []
    for order in _orders(session, account=account):
        orders.append(
            {
                "number": order.number,
                "status": order.status,
                "raised_on": order.created_at.date(),
                "required_date": order.required_date,
                "currency": order.currency,
                "total": Decimal(order_total(order)),
                "revision_no": order.revision_no,
                "lines": [
                    {
                        "line_no": line.line_no,
                        "item": line.item_id,
                        "quantity": Decimal(line.quantity),
                        "unit_price": Decimal(line.unit_price),
                    }
                    for line in order.lines
                ],
            }
        )
    invoices = [
        {
            "number": invoice.number,
            "status": invoice.status,
            "invoice_date": invoice.invoice_date,
            "due_date": invoice.due_date,
            "currency": invoice.currency,
            "gross_amount": Decimal(invoice.gross_amount),
            "order": None if invoice.order_id is None else str(invoice.order_id),
        }
        for invoice in _invoices(session, account=account)
    ]
    return {
        "supplier": {"code": supplier.party.code, "name": supplier.party.name},
        "subject": account.subject,
        "rfqs": rfqs,
        "orders": orders,
        "invoices": invoices,
    }


def respond_to_rfq(
    session: Session,
    *,
    account: SupplierAccount,
    number: str,
    lines: Any,
    received_on: date,
    currency: str | None = None,
    lead_time_days: int | None = None,
    valid_until: date | None = None,
    note: str | None = None,
) -> RfqResponse:
    """Record this supplier's answer through T-2.PROC.03's own call.

    The RFQ is looked up and then checked against the account: a number that is not this
    supplier's is refused *here*, before the domain call, so the two refusals a supplier could
    provoke — somebody else's RFQ, and one it was not invited to — read differently.
    """
    supplier = session.get(Supplier, account.supplier_id)
    rfq = rfq_by_number(session, company_id=account.company_id, number=str(number).strip())
    if supplier.id not in {row.supplier_id for row in rfq.invitations}:
        raise NotInvitedError(
            f"RFQ {rfq.number!r} was not issued to supplier {supplier.party.code!r}"
        )
    return record_response(
        session,
        rfq,
        supplier_code=supplier.party.code,
        received_on=received_on,
        lines=lines,
        currency=currency,
        lead_time_days=lead_time_days,
        valid_until=valid_until,
        note=note,
    )


def acknowledge_order_for(
    session: Session, *, account: SupplierAccount, number: str
) -> PurchaseOrder:
    """Acknowledge one of this supplier's orders — the domain's own transition.

    The order is resolved **by supplier**: a number belonging to another supplier is not found
    rather than found and refused, so the portal has no way to say whether somebody else's
    order exists.
    """
    order = session.scalar(
        select(PurchaseOrder).where(
            PurchaseOrder.company_id == account.company_id,
            PurchaseOrder.supplier_id == account.supplier_id,
            PurchaseOrder.number == str(number).strip(),
        )
    )
    if order is None:
        raise NotYoursError(
            f"no purchase order {number!r} has been released to this supplier"
        )
    return acknowledge_order(session, order)


def submit_invoice(
    session: Session,
    *,
    account: SupplierAccount,
    number: str,
    invoice_date: date,
    supplier_reference: str,
    lines: Any,
    order_number: str | None = None,
) -> "SupplierInvoice":
    """Submit an invoice as a **draft**, through T-2.AP.01's own record.

    The invoice is recorded for the account's own supplier — never one the caller states —
    and it is left a **draft**: posting it is the buyer's step and the match reads only a
    posted invoice, so submitting one cannot approve it, and the review path is the one every
    invoice takes.

    An order number, when the supplier states one, is checked against the orders released to
    it: naming somebody else's order is refused rather than stored. The invoice is not linked
    to the order by it, because T-2.AP.01 links a claim to a **whole** chain — an order *and*
    its posted receipt — and the receipt is the platform's own record of what arrived, not
    something a supplier can state. The buyer links the chain during the review, which is the
    same step that posts the invoice.
    """
    # Imported here, not at module scope: this module is reachable from the API, and T-2.AP.01's
    # tables are not the API's business until somebody submits an invoice — the same reason
    # `app.ap.invoices` imports the receipt it links to inside the function that links it.
    from app.ap.invoices import DRAFT as INVOICE_DRAFT
    from app.ap.invoices import create_invoice

    supplier = session.get(Supplier, account.supplier_id)
    if order_number is not None:
        order = session.scalar(
            select(PurchaseOrder).where(
                PurchaseOrder.company_id == account.company_id,
                PurchaseOrder.supplier_id == account.supplier_id,
                PurchaseOrder.number == str(order_number).strip(),
            )
        )
        if order is None:
            raise NotYoursError(
                f"no purchase order {order_number!r} has been released to this supplier"
            )
    invoice = create_invoice(
        session,
        company_id=account.company_id,
        number=number,
        supplier=supplier,
        supplier_reference=supplier_reference,
        invoice_date=invoice_date,
        lines=lines,
    )
    if invoice.status != INVOICE_DRAFT:  # pragma: no cover — `create_invoice` states it
        raise PortalError(
            f"invoice {invoice.number!r} was recorded as {invoice.status}; the portal only"
            " submits drafts"
        )
    return invoice


def accounts_of(session: Session, *, company_id: uuid.UUID) -> list[SupplierAccount]:
    """Every portal account this company has linked, oldest first."""
    return list(
        session.scalars(
            select(SupplierAccount)
            .where(SupplierAccount.company_id == company_id)
            .order_by(SupplierAccount.linked_at, SupplierAccount.subject)
        )
    )


__all__ = [
    "AlreadyLinkedError",
    "CAPABILITY",
    "NoPortalAccountError",
    "NotYoursError",
    "PortalError",
    "SupplierAccount",
    "account_for",
    "accounts_of",
    "acknowledge_order_for",
    "documents_for",
    "link_account",
    "respond_to_rfq",
    "submit_invoice",
]
