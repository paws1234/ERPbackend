"""T-2.AP.04 — selecting invoices into a payment batch, and paying them.

A payment run is the moment money leaves. Four decisions shape this module:

* **A batch selects invoices, it does not decide which may be paid.** Every invoice in
  it is checked: it must be posted, owe something, be the batch's currency, and
  **not be held** — :func:`app.matching.require_not_held` is called here, so "a batch
  cannot include an invoice that has failed 3-way matching or is otherwise on hold" is
  enforced where the batch is built rather than trusted to the screen.
* **One balanced entry per settled invoice.** Executing debits the payables control
  account and credits the bank, per invoice, through T-1.ACCT.03's mapping — so the
  ledger shows which liability each payment cleared rather than one lump nobody can
  unpick. Partial settlement is the same arithmetic with a smaller amount.
* **A batch is executed once.** Execution settles each invoice with a settlement row
  (T-2.AP.01) and posts; running it twice would pay twice, so the second attempt is
  refused, and a line cannot be removed once the batch has run.
* **The file the bank reads comes from the pack.** `bank_file` writes the columns
  `bank_file_format` (T-0.LOC.01) names, in its order, and **refuses** a required
  column it has no value for — a payment file with a blank account number is worse than
  no file, because it is the one somebody uploads.
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
    PAYABLES_KEY,
    SupplierInvoice,
    invoice_by_number,
    open_amount,
    settle,
)
from app.company import company_base_currency
from app.db import Base
from app.ledger.currency import currency_by_code
from app.ledger.mapping import mapped_account
from app.ledger.posting import JournalEntry, post_journal_entry
from app.localization import bank_file_format, packs
from app.matching import require_not_held
from app.procurement.suppliers import Supplier, primary_bank_account
from app.workflow import (
    APPROVED as WF_APPROVED,
    PENDING as WF_PENDING,
    REJECTED as WF_REJECTED,
    RETURNED as WF_RETURNED,
    ApprovalRequest,
)
from app.workflow import decide as workflow_decide
from app.workflow import start_approval

# One money scale for the whole platform.
MONEY = Numeric(20, 6)

# The account a payment is made from (T-1.ACCT.03's mapping key).
BANK_KEY = "bank"

# The document type the approval engine knows payment batches by.
DOC_TYPE = "payment_batch"

DRAFT, PENDING, APPROVED, REJECTED, RETURNED, EXECUTED = (
    "draft",
    "pending",
    "approved",
    "rejected",
    "returned",
    "executed",
)

_FROM_ENGINE = {
    WF_PENDING: PENDING,
    WF_APPROVED: APPROVED,
    WF_REJECTED: REJECTED,
    WF_RETURNED: RETURNED,
}


class PaymentError(ValueError):
    """The payment batch refused what was asked of it."""


class DuplicateBatchError(PaymentError):
    """That batch number is already used in this company."""


class BatchStateError(PaymentError):
    """The asked-for change does not apply to the batch's state."""


class UnpayableInvoiceError(PaymentError):
    """The invoice cannot go into a payment batch, and the reason is stated."""


class EmptyBatchError(PaymentError):
    """A batch with nothing in it."""


class PaymentBatch(Base):
    """One run: a set of invoices to be paid together, and what happened to it."""

    __tablename__ = "payment_batch"
    __table_args__ = (
        UniqueConstraint("company_id", "number", name="uq_payment_batch_company_number"),
        CheckConstraint(
            "status IN ('draft', 'pending', 'approved', 'rejected', 'returned', 'executed')",
            name="ck_payment_batch_status",
        ),
        CheckConstraint("total_amount >= 0", name="ck_payment_batch_total"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    number: Mapped[str] = mapped_column(String(32), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    scheduled_on: Mapped[date] = mapped_column(Date, nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=DRAFT)
    approval_request_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("approval_request.id"), index=True
    )
    approved_by: Mapped[str | None] = mapped_column(String(64))
    executed_on: Mapped[date | None] = mapped_column(Date)
    executed_by: Mapped[str | None] = mapped_column(String(64))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    lines: Mapped[list[PaymentBatchLine]] = relationship(
        back_populates="batch", order_by="PaymentBatchLine.line_no"
    )


class PaymentBatchLine(Base):
    """One invoice in the run, for what it owed when the batch was built."""

    __tablename__ = "payment_batch_line"
    __table_args__ = (
        UniqueConstraint("batch_id", "line_no", name="uq_payment_batch_line_no"),
        UniqueConstraint("batch_id", "invoice_id", name="uq_payment_batch_line_once"),
        CheckConstraint("line_no >= 1", name="ck_payment_batch_line_starts_at_one"),
        CheckConstraint("amount > 0", name="ck_payment_batch_line_amount"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    batch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("payment_batch.id"), nullable=False, index=True
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("supplier_invoice.id"), nullable=False, index=True
    )
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    # The entry this payment posted, where it has run.
    journal_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("journal_entry.id"), index=True
    )

    batch: Mapped[PaymentBatch] = relationship(back_populates="lines")
    invoice: Mapped[SupplierInvoice] = relationship()


def _amount(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def create_batch(
    session: Session,
    *,
    company_id: uuid.UUID,
    number: str,
    scheduled_on: date,
    invoice_numbers: Any,
) -> PaymentBatch:
    """Build a draft batch from the invoices named, refusing any that cannot be paid.

    One currency per batch, because a bank file and a bank account are per currency. An
    invoice that is held, unposted, already settled or in another currency is refused
    with the reason, so a batch cannot quietly leave one out.
    """
    wanted = str(number).strip()
    if not wanted:
        raise PaymentError("a batch number is required")
    if session.scalar(
        select(PaymentBatch).where(
            PaymentBatch.company_id == company_id, PaymentBatch.number == wanted
        )
    ) is not None:
        raise DuplicateBatchError(f"payment batch {wanted!r} already exists in this company")
    numbers = [str(one).strip() for one in invoice_numbers]
    if not numbers:
        raise EmptyBatchError(
            "a payment batch selects at least one invoice; an empty run pays nothing"
        )

    invoices = [
        invoice_by_number(session, company_id=company_id, number=one)
        for one in dict.fromkeys(numbers)
    ]
    currency = None
    batch = PaymentBatch(
        company_id=company_id,
        number=wanted,
        currency=company_base_currency(session, company_id=company_id),
        scheduled_on=scheduled_on,
        status=DRAFT,
    )
    checked = []
    for invoice in invoices:
        if invoice.status != "posted":
            raise UnpayableInvoiceError(
                f"invoice {invoice.number!r} is {invoice.status}; only a posted invoice is paid"
            )
        outstanding = open_amount(session, invoice)
        if outstanding <= 0:
            raise UnpayableInvoiceError(
                f"invoice {invoice.number!r} owes nothing; there is nothing to pay"
            )
        # Held invoices are refused **here**, where the batch is built.
        require_not_held(session, invoice)
        if currency is None:
            currency = invoice.currency
        elif invoice.currency != currency:
            raise UnpayableInvoiceError(
                f"invoice {invoice.number!r} is in {invoice.currency} but the batch is in"
                f" {currency}; one run pays in one currency"
            )
        checked.append((invoice, outstanding))

    batch.currency = currency_by_code(
        session, company_id=company_id, code=currency
    ).code
    session.add(batch)
    session.flush()
    for line_no, (invoice, outstanding) in enumerate(checked, start=1):
        batch.lines.append(
            PaymentBatchLine(
                company_id=company_id,
                batch_id=batch.id,
                line_no=line_no,
                invoice_id=invoice.id,
                amount=outstanding,
            )
        )
    session.flush()
    batch.total_amount = sum(
        (line.amount for line in batch.lines), Decimal(0)
    ).quantize(Decimal("0.000001"))
    session.flush()
    return batch


def remove_line(session: Session, batch: PaymentBatch, invoice: SupplierInvoice) -> None:
    """Take an invoice out of a batch that has not run yet.

    Refused once the batch is executed: the payment is already in the ledger and the
    money is already gone, so "remove it" would be a lie about what happened.
    """
    if batch.status == EXECUTED:
        raise BatchStateError(
            f"batch {batch.number!r} has been executed; an invoice it paid cannot be"
            " taken out of it"
        )
    line = next((row for row in batch.lines if row.invoice_id == invoice.id), None)
    if line is None:
        raise PaymentError(
            f"invoice {invoice.number!r} is not in batch {batch.number!r}"
        )
    session.delete(line)
    # Taken out of the collection too, so the batch in hand reads the way the database
    # will: a total computed over a collection still holding the deleted line would be
    # the old one.
    batch.lines.remove(line)
    session.flush()
    batch.total_amount = sum(
        (row.amount for row in batch.lines), Decimal(0)
    ).quantize(Decimal("0.000001"))
    session.flush()


def submit_batch(session: Session, batch: PaymentBatch, *, actor: str) -> PaymentBatch:
    """Send the batch for approval, through the configured chain."""
    if batch.status != DRAFT:
        raise BatchStateError(
            f"batch {batch.number!r} is {batch.status}; only a draft is submitted"
        )
    if not batch.lines:
        raise EmptyBatchError(f"batch {batch.number!r} has no lines")
    request = start_approval(
        session,
        company_id=batch.company_id,
        doc_type=DOC_TYPE,
        document_id=batch.id,
        amount=batch.total_amount,
    )
    if request is None:
        batch.status = APPROVED
        batch.approved_by = str(actor)
    else:
        batch.approval_request_id = request.id
        batch.status = PENDING
    session.flush()
    return batch


def decide_batch(
    session: Session,
    batch: PaymentBatch,
    *,
    actor: str,
    action: str,
    role: str,
    reason: str | None = None,
) -> PaymentBatch:
    """Take one decision on a pending batch, through the engine."""
    if batch.status != PENDING or batch.approval_request_id is None:
        raise BatchStateError(
            f"batch {batch.number!r} is {batch.status}; nothing is waiting to be decided"
        )
    request = session.get(ApprovalRequest, batch.approval_request_id)
    if request is None:  # pragma: no cover — the foreign key forbids it
        raise BatchStateError(
            f"batch {batch.number!r} names an approval request that is gone"
        )
    workflow_decide(session, request, actor=actor, action=action, role=role, reason=reason)
    batch.status = _FROM_ENGINE[request.state]
    if batch.status == APPROVED:
        batch.approved_by = str(actor)
    session.flush()
    return batch


def execute_batch(
    session: Session, batch: PaymentBatch, *, actor: str, executed_on: date | None = None
) -> PaymentBatch:
    """Run the batch: settle each invoice and post one balanced entry per invoice.

    Refused for a batch that has not been approved or has already run. Everything
    happens in the caller's transaction, so a failure half-way leaves neither a
    settlement nor a posting behind — a half-paid run is not a state anybody can
    reconcile.
    """
    if batch.status == EXECUTED:
        raise BatchStateError(
            f"batch {batch.number!r} has already been executed; running it again would"
            " pay the same invoices twice"
        )
    if batch.status != APPROVED:
        raise BatchStateError(
            f"batch {batch.number!r} is {batch.status}; only an approved batch is executed"
        )
    # Generate and validate the exact bank file before the first journal entry or
    # settlement is flushed.  A missing account must not leave a half-paid batch.
    bank_file(session, batch)
    day = executed_on or batch.scheduled_on
    payables = mapped_account(session, company_id=batch.company_id, key=PAYABLES_KEY).code
    bank = mapped_account(session, company_id=batch.company_id, key=BANK_KEY).code

    for line in batch.lines:
        invoice = line.invoice
        # Re-checked at payment time, not only when the batch was built: the invoice may
        # have been put on hold since, and paying it then would be the failure this whole
        # chain exists to prevent.
        require_not_held(session, invoice)
        entry = post_journal_entry(
            session,
            company_id=batch.company_id,
            posting_date=day,
            currency=batch.currency,
            memo=f"payment batch {batch.number} for invoice {invoice.number}",
            source_type="payment_batch",
            source_id=batch.id,
            lines=[
                {"account": payables, "debit": line.amount, "party": invoice.supplier.party.code},
                {"account": bank, "credit": line.amount},
            ],
        )
        line.journal_entry_id = entry.id
        settle(
            session,
            invoice,
            amount=line.amount,
            settled_on=day,
            source_type="payment_batch",
            source_id=batch.id,
        )
    batch.status = EXECUTED
    batch.executed_on = day
    batch.executed_by = str(actor)
    batch.executed_at = datetime.now(timezone.utc)
    session.flush()
    return batch


def bank_file(session: Session, batch: PaymentBatch) -> str:
    """The batch as the bank's own file layout, from the pack (T-0.LOC.01).

    The columns are the pack's, in the pack's order. A **required** column with no value
    stops the file: a payment file with a blank account number is the one somebody
    uploads, so it is better refused here than discovered by the bank.
    """
    markets = packs()
    if len(markets) != 1:
        raise PaymentError(
            f"this deployment ships {len(markets)} packs; the bank file layout is a"
            " market's, so there is no single layout to write"
        )
    layout = bank_file_format(markets[0])
    columns = [column["name"] for column in layout["columns"]]
    required = {column["name"] for column in layout["columns"] if column.get("required")}
    delimiter = layout.get("delimiter", ",")

    rows = [delimiter.join(columns)]
    for line in batch.lines:
        invoice = line.invoice
        supplier: Supplier = invoice.supplier
        account = primary_bank_account(supplier)
        if account is not None and account.currency is not None and account.currency != invoice.currency:
            raise PaymentError(
                f"bank account for invoice {invoice.number!r} is in {account.currency},"
                f" not the invoice currency {invoice.currency}"
            )
        values = {
            "payee_name": supplier.party.name,
            "payee_account": None if account is None else account.account_number,
            # The pack asks for the receiving bank's own code; the stored BIC is what
            # this installation has, and a missing one stops the file rather than
            # guessing a code.
            "bank_code": None if account is None else account.swift,
            "amount": format(line.amount, "f"),
            "reference": invoice.number,
            "purpose": f"{invoice.number} {invoice.supplier_reference}",
        }
        missing = [name for name in columns if name in required and not values.get(name)]
        if missing:
            raise PaymentError(
                f"the bank file needs {', '.join(missing)} for invoice"
                f" {invoice.number!r} and this supplier has none recorded; record them"
                " before the payment run (T-2.PROC.01)"
            )
        rows.append(
            delimiter.join("" if values[name] is None else str(values[name]) for name in columns)
        )
    return "\n".join(rows) + "\n"


def batches(session: Session, *, company_id: uuid.UUID) -> list[PaymentBatch]:
    """Every batch of this company, oldest first."""
    return list(
        session.scalars(
            select(PaymentBatch)
            .where(PaymentBatch.company_id == company_id)
            .order_by(PaymentBatch.created_at, PaymentBatch.number)
        )
    )


def batch_by_number(
    session: Session, *, company_id: uuid.UUID, number: str
) -> PaymentBatch:
    """The batch a bank file or a payment quotes, or a refusal naming what is missing."""
    found = session.scalar(
        select(PaymentBatch).where(
            PaymentBatch.company_id == company_id,
            PaymentBatch.number == str(number).strip(),
        )
    )
    if found is None:
        raise PaymentError(f"no payment batch {number!r} in this company")
    return found


def paid_by_batch(session: Session, batch: PaymentBatch) -> Decimal:
    """What the batch actually paid, from the settlements it wrote."""
    from app.ap.invoices import SupplierInvoiceSettlement

    total = session.scalar(
        select(func.coalesce(func.sum(SupplierInvoiceSettlement.amount), 0)).where(
            SupplierInvoiceSettlement.source_type == "payment_batch",
            SupplierInvoiceSettlement.source_id == batch.id,
        )
    )
    return _amount(total or 0).quantize(Decimal("0.000001"))
