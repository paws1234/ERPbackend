"""T-3.AR.05 — payment-gateway webhooks, and the settlement they post.

A gateway calls *in* (§3's Integrations row), so everything here meets
T-0.INT.01's boundary and nothing here talks to a gateway directly:

* **A delivery is processed once.** :func:`record_payment` goes through
  :func:`app.integrations.receive_inbound`, which stores the delivery under the
  sender's own idempotency key and does not run the handler twice for the same one.
  The replayed webhook answers with the first one's outcome instead of settling the
  invoice again.
* **A payment settles once, whatever the gateway sends.** Two events can describe
  one payment (`payment.created`, then `payment.succeeded`), so the payment is keyed
  by the gateway's **payment reference**, unique per company: a second event for a
  reference already seen returns what the first one did rather than appending a
  second settlement. The settlement row is additionally unique on (invoice, source
  type, source id), so no caller can append one twice.
* **Nothing is dropped.** A payment that matches no invoice, arrives in another
  currency, exceeds what is open, or fails at the gateway is **recorded** with its
  reason and — where it is a real payment — **parked** until somebody places it
  (:func:`parked_payments`, :func:`match`). A payment silently discarded is a
  customer who has paid and is still being chased.
* **Whose money it is, is recorded.** The event's optional `customer` code, or the
  invoice it names, attributes the payment to a customer, which is what lets
  T-3.AR.06 count a receipt on account against **that** customer and no other. A
  receipt that names nobody known is parked with the reason, not guessed at.
* **The settlement posts, and the fee with it.** One debit to `bank`, one to the
  configured `payment_fees` account for the gateway's own charge, and one credit to
  `receivables` for the whole payment — so the money received, what it cost to
  receive, and what came off the customer's account are all in the ledger, and the
  entry balances by construction.

A malformed event — no reference, an amount that is not an amount, a fee larger than
the payment, an outcome nobody has classified — is **refused at entry**, before the
boundary records anything: an unclassified status must not be able to settle a
receivable, and refusing it is what stops one from being guessed at.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - the annotation only
    from app.sales.customers import Customer

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.ar.invoices import (
    POSTED,
    RECEIVABLES_KEY,
    CustomerInvoice,
    open_amount,
    settle,
)
from app.db import Base
from app.integrations import InboundEvent, receive_inbound
from app.ledger.mapping import mapped_account
from app.ledger.posting import JournalEntry, post_journal_entry

MONEY = Numeric(20, 6)
MONEY_SCALE = Decimal("0.000001")

# The channel this module is the consumer for: the key the boundary files deliveries
# under, and the source an operator reads the log by.
GATEWAY_SOURCE = "gateway"

# What the platform books the money through. `bank` is AP's own key (T-2.AP.04), so
# the receiving side and the paying side point at the same account.
BANK_KEY = "bank"
FEE_KEY = "payment_fees"

# The states a gateway payment can be in. `parked` is a payment the platform could
# not place: not a failure, and not lost.
SETTLED, PARTIAL, PARKED, FAILED = "settled", "partial", "parked", "failed"
PAYMENT_STATUSES = (SETTLED, PARTIAL, PARKED, FAILED)

# The outcomes this module understands. Anything else is refused rather than assumed
# to be a success.
SUCCEEDED, FAILED_OUTCOME = "succeeded", "failed"
OUTCOMES = (SUCCEEDED, FAILED_OUTCOME)


class GatewayError(ValueError):
    """The gateway event refused what was asked of it."""


class UnknownGatewayOutcome(GatewayError):
    """The event states an outcome this platform has not classified."""


class GatewayPayment(Base):
    """One payment the gateway told us about, and what became of it.

    Keyed by the gateway's own payment reference, which is what makes two events
    describing one payment settle it once.
    """

    __tablename__ = "gateway_payment"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "reference", name="uq_gateway_payment_company_reference"
        ),
        CheckConstraint(
            "status IN ('settled', 'partial', 'parked', 'failed')",
            name="ck_gateway_payment_status",
        ),
        CheckConstraint("amount > 0", name="ck_gateway_payment_amount"),
        CheckConstraint("fee >= 0", name="ck_gateway_payment_fee"),
        # A settlement never costs more than it collects.
        CheckConstraint("fee <= amount", name="ck_gateway_payment_fee_within_amount"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    # The gateway's own id for the payment — the key two events about it share.
    reference: Mapped[str] = mapped_column(String(64), nullable=False)
    # The first delivery that told us about it, so the trail leads back to the raw
    # event rather than to a summary of it.
    event_key: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    fee: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    currency: Mapped[str | None] = mapped_column(String(3))
    paid_on: Mapped[date] = mapped_column(Date, nullable=False)
    # The invoice the event named, matched or not: a parked payment still knows which
    # invoice it is for, which is what lets a later match place it without re-reading
    # the raw event.
    invoice_number: Mapped[str | None] = mapped_column(String(32))
    # The invoice it settled, once it has.
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("customer_invoice.id"), index=True
    )
    # Whose money this is, when the event says or the invoice named tells us. Null is
    # "nobody has said", and an unattributed receipt reduces no customer's exposure
    # (T-3.AR.06): money nobody has placed against a customer cannot be allowed to
    # free up that customer's — or anybody else's — credit.
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("customer.id"), index=True
    )
    # Why it is parked or failed. Null once it has settled.
    reason: Mapped[str | None] = mapped_column(Text)
    journal_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("journal_entry.id"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
        nullable=False,
    )

    invoice: Mapped[CustomerInvoice | None] = relationship()
    customer: Mapped[Customer | None] = relationship()
    entry: Mapped[JournalEntry | None] = relationship()


def _amount(value: Any) -> Decimal:
    if isinstance(value, float):
        raise GatewayError(f"a gateway amount is an exact decimal, not the float {value!r}")
    try:
        return value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except Exception as exc:  # noqa: BLE001 — an inexact amount is refused, not parsed
        raise GatewayError(f"not a gateway amount: {value!r}") from exc


def _event(payload: Any) -> dict:
    """The event as this module reads it, or a refusal naming what is missing.

    Everything a settlement depends on is read here, once, before the boundary
    records the delivery: reference, outcome, amount, fee, the invoice it names, its
    currency and its date.
    """
    if not isinstance(payload, dict):
        raise GatewayError(f"a gateway event is an object, not {type(payload).__name__}")
    reference = str(payload.get("reference") or "").strip()
    if not reference:
        raise GatewayError("a gateway event names the payment it is about (reference)")
    outcome = str(payload.get("status") or "").strip().lower()
    if outcome not in OUTCOMES:
        raise UnknownGatewayOutcome(
            f"gateway outcome {outcome!r} for payment {reference!r} is not one this"
            f" platform has classified ({', '.join(OUTCOMES)})"
        )
    if "amount" not in payload:
        raise GatewayError(f"gateway event for {reference!r} states no amount")
    amount = _amount(payload["amount"])
    fee = _amount(payload.get("fee", 0))
    if not amount.is_finite() or not fee.is_finite():
        raise GatewayError(f"payment {reference!r} states a non-finite amount or fee")
    if amount <= 0 or fee < 0 or fee > amount:
        raise GatewayError(
            f"payment {reference!r} states an amount of {amount} and a fee of {fee};"
            " the fee is a part of the amount, not more than it"
        )
    stated_on = payload.get("on")
    if stated_on is None:
        paid_on = date.today()
    elif isinstance(stated_on, date):
        paid_on = stated_on
    else:
        try:
            paid_on = date.fromisoformat(str(stated_on))
        except ValueError as exc:
            raise GatewayError(
                f"payment {reference!r} states {stated_on!r} as its date"
            ) from exc
    number = payload.get("invoice")
    payer = payload.get("customer")
    return {
        "reference": reference,
        "outcome": outcome,
        "amount": amount,
        "fee": fee,
        "currency": None if payload.get("currency") is None else str(payload["currency"]),
        "paid_on": paid_on,
        "invoice_number": None if number is None else str(number).strip(),
        # Who paid, when the gateway says. Optional: a receipt for an invoice is
        # attributed from the invoice, and one that names neither stays unattributed.
        "customer_code": None if payer is None else str(payer).strip(),
        "reason": None if payload.get("reason") is None else str(payload["reason"]),
    }


def _checked(
    session: Session, payment: GatewayPayment, invoice: CustomerInvoice | None
) -> CustomerInvoice | str:
    """The invoice, if this payment can settle it; otherwise why it cannot."""
    if invoice is None:
        return (
            "no invoice was named for this payment"
            if payment.invoice_number is None
            else f"no customer invoice {payment.invoice_number!r} in this company"
        )
    if invoice.status != POSTED:
        return f"invoice {invoice.number!r} is {invoice.status}; nothing is owed on it yet"
    if payment.customer_id is not None and invoice.customer_id != payment.customer_id:
        # The event says one customer paid and names another's invoice. Settling it
        # would take the money off the attributed customer's account, and T-3.AR.06
        # would then read the exposure of a customer who owes nothing less.
        return (
            f"payment is attributed to another customer than invoice"
            f" {invoice.number!r} belongs to; check which is the typo"
        )
    if payment.currency is not None and invoice.currency != payment.currency:
        return (
            f"payment is in {payment.currency} but invoice {invoice.number!r} is in"
            f" {invoice.currency}"
        )
    outstanding = open_amount(session, invoice)
    if payment.amount > outstanding:
        return (
            f"payment of {payment.amount} exceeds the {outstanding} open on invoice"
            f" {invoice.number!r}"
        )
    return invoice


def _named_invoice(
    session: Session, payment: GatewayPayment
) -> CustomerInvoice | None:
    if payment.invoice_number is None:
        return None
    return session.scalar(
        select(CustomerInvoice).where(
            CustomerInvoice.company_id == payment.company_id,
            CustomerInvoice.number == payment.invoice_number,
        )
    )


def post_settlement(
    session: Session,
    payment: GatewayPayment,
    *,
    invoice: CustomerInvoice,
    posting_date: date,
) -> JournalEntry:
    """Settle the invoice and post the money: bank and fee debited, receivables credited.

    The fee is the gateway's charge for collecting, so it is a debit in its own right
    — booked to whatever account the company maps `payment_fees` to — while
    `receivables` is credited the whole payment, because that is how much came off
    the customer's account.
    """
    settle(
        session,
        invoice,
        amount=payment.amount,
        settled_on=posting_date,
        source_type="gateway_payment",
        source_id=payment.id,
    )
    net = (payment.amount - payment.fee).quantize(MONEY_SCALE)
    lines: list[dict[str, Any]] = [
        {
            "account": mapped_account(
                session, company_id=payment.company_id, key=BANK_KEY
            ).code,
            "debit": net,
        }
    ]
    if payment.fee > 0:
        lines.append(
            {
                "account": mapped_account(
                    session, company_id=payment.company_id, key=FEE_KEY
                ).code,
                "debit": payment.fee,
            }
        )
    lines.append(
        {
            "account": mapped_account(
                session, company_id=payment.company_id, key=RECEIVABLES_KEY
            ).code,
            "credit": payment.amount,
        }
    )
    entry = post_journal_entry(
        session,
        company_id=payment.company_id,
        posting_date=posting_date,
        currency=invoice.currency,
        memo=f"gateway payment {payment.reference} against {invoice.number}",
        source_type="gateway_payment",
        source_id=payment.id,
        lines=lines,
    )
    payment.journal_entry_id = entry.id
    payment.invoice_id = invoice.id
    payment.reason = None
    payment.status = SETTLED if open_amount(session, invoice) == 0 else PARTIAL
    session.flush()
    return entry


def _place(session: Session, payment: GatewayPayment, event: dict) -> GatewayPayment:
    """Settle, park or record a failure — the one payment's worth of work."""
    if event["outcome"] == FAILED_OUTCOME:
        # The gateway says the payment did not happen: the invoice stays open (no
        # settlement is ever written) and the attempt is on the record.
        payment.status = FAILED
        payment.reason = event["reason"] or "the gateway reported a failure"
        session.flush()
        return payment
    named = _named_invoice(session, payment)
    if named is not None and payment.customer_id is None:
        # The invoice says whose money this is even when it cannot settle it yet.
        payment.customer = named.customer
        session.flush()
    invoice = _checked(session, payment, named)
    if isinstance(invoice, str):
        payment.status = PARKED
        payment.reason = invoice
        session.flush()
        return payment
    post_settlement(session, payment, invoice=invoice, posting_date=payment.paid_on)
    return payment


def record_payment(
    session: Session,
    *,
    company_id: uuid.UUID,
    event_key: str,
    payload: dict,
) -> tuple[GatewayPayment, bool]:
    """Record one gateway delivery, returning the payment and whether it was a repeat.

    The delivery is filed under `event_key` by T-0.INT.01's boundary, so the same
    webhook replayed is answered from the record rather than processed again; and a
    *different* event key describing a payment already seen is answered from the
    payment row, which is what stops one payment being settled twice.
    """
    event = _event(payload)
    produced: list[GatewayPayment | None] = [None]
    # Whether the *payment* had been seen before — a different event key describing a
    # payment already recorded is a duplicate too, and the caller has to be able to
    # tell, or it cannot know that nothing was posted this time.
    already: list[bool] = [False]

    def handle(_delivery: InboundEvent) -> None:
        payment, seen_before = _record(
            session, company_id=company_id, event_key=event_key, event=event
        )
        produced[0] = payment
        already[0] = seen_before

    _, repeated = receive_inbound(
        session,
        company_id=company_id,
        source=GATEWAY_SOURCE,
        idempotency_key=event_key,
        payload=payload,
        handle=handle,
    )
    if repeated:
        # The boundary recognised the delivery and did not run the handler: answer
        # with what that delivery produced the first time.
        payment = _payment_for_event(session, company_id=company_id, event_key=event_key)
        if payment is None:  # pragma: no cover - a repeated delivery always has one
            raise GatewayError(
                f"delivery {event_key!r} was seen before but no payment records it"
            )
        return payment, True
    if produced[0] is None:  # pragma: no cover - the handler always records one
        raise GatewayError(f"delivery {event_key!r} recorded no payment")
    return produced[0], already[0]


def _payment_for_event(
    session: Session, *, company_id: uuid.UUID, event_key: str
) -> GatewayPayment | None:
    return session.scalar(
        select(GatewayPayment).where(
            GatewayPayment.company_id == company_id,
            GatewayPayment.event_key == event_key,
        )
    )


def _record(
    session: Session, *, company_id: uuid.UUID, event_key: str, event: dict
) -> tuple[GatewayPayment, bool]:
    """One payment's row, existing or new, and whether it already existed."""
    existing = session.scalar(
        select(GatewayPayment).where(
            GatewayPayment.company_id == company_id,
            GatewayPayment.reference == event["reference"],
        )
    )
    if existing is not None:
        # One payment, however many events describe it: nothing is posted again, and
        # the delivery itself is still on the record (the boundary wrote it), so the
        # event is not lost either.
        return existing, True
    payment = GatewayPayment(
        company_id=company_id,
        reference=event["reference"],
        event_key=event_key,
        status=PARKED,
        amount=event["amount"],
        fee=event["fee"],
        currency=event["currency"],
        paid_on=event["paid_on"],
        invoice_number=event["invoice_number"],
    )
    session.add(payment)
    session.flush()
    if _attribute(session, payment, event["customer_code"]):
        return payment, False
    return _place(session, payment, event), False


def _attribute(session: Session, payment: GatewayPayment, code: str | None) -> bool:
    """Who paid, when the event says — parking the payment when it names nobody known.

    Returns whether the payment is already parked because of who it names, which stops
    a receipt attributed to nobody from being placed against somebody.
    """
    if code is None:
        return False
    # Imported here, not at module scope: the customer's own module reads the ledger,
    # and this module is read by the exposure, which the customer's documents read.
    from app.party import UnknownPartyError
    from app.sales.customers import CustomerError, customer_by_code

    try:
        payment.customer = customer_by_code(
            session, company_id=payment.company_id, code=code
        )
    except (CustomerError, UnknownPartyError) as exc:
        payment.status = PARKED
        payment.reason = str(exc)
        session.flush()
        return True
    session.flush()
    return False


def match(session: Session, payment: GatewayPayment) -> GatewayPayment:
    """Place a parked or failed payment now that it can be placed.

    The same checks :func:`record_payment` applies on arrival, applied again — this
    is the path a late invoice, a corrected currency or a mistyped invoice number is
    worked through. A payment that is already settled is refused: settling it again
    would take the same money off the customer's account twice.
    """
    if payment.status in (SETTLED, PARTIAL):
        raise GatewayError(
            f"gateway payment {payment.reference!r} is already {payment.status}"
        )
    named = _named_invoice(session, payment)
    if named is not None and payment.customer_id is None:
        # The invoice says whose money this is, exactly as it does on arrival.
        payment.customer = named.customer
        session.flush()
    invoice = _checked(session, payment, named)
    if isinstance(invoice, str):
        payment.status = PARKED
        payment.reason = invoice
        session.flush()
        return payment
    post_settlement(session, payment, invoice=invoice, posting_date=payment.paid_on)
    return payment


def parked_payments(session: Session, *, company_id: uuid.UUID) -> list[GatewayPayment]:
    """The payments the platform could not place — what somebody has to work through.

    Oldest first, each carrying its reason: a parked payment is money a customer has
    paid that the books have not seen.
    """
    return list(
        session.scalars(
            select(GatewayPayment)
            .where(
                GatewayPayment.company_id == company_id,
                GatewayPayment.status == PARKED,
            )
            .order_by(GatewayPayment.paid_on, GatewayPayment.reference)
        )
    )


def payment_report(session: Session, *, company_id: uuid.UUID) -> dict:
    """Every gateway payment by state, with the unplaced ones spelled out.

    What makes "parked and reported" different from "dropped": a run reads this and
    knows exactly which payments need a person.
    """
    rows = list(
        session.scalars(
            select(GatewayPayment)
            .where(GatewayPayment.company_id == company_id)
            .order_by(GatewayPayment.paid_on, GatewayPayment.reference)
        )
    )
    counts = {state: 0 for state in PAYMENT_STATUSES}
    for row in rows:
        counts[row.status] += 1
    return {
        "counts": counts,
        "collected": sum(
            (row.amount for row in rows if row.status in (SETTLED, PARTIAL)), Decimal(0)
        ).quantize(MONEY_SCALE),
        "unplaced": [
            {
                "reference": row.reference,
                "amount": row.amount,
                "status": row.status,
                "reason": row.reason,
            }
            for row in rows
            if row.status in (PARKED, FAILED)
        ],
    }


def payments_for(session: Session, invoice: CustomerInvoice) -> list[GatewayPayment]:
    """The gateway payments matched to one invoice, in the order they were paid."""
    return list(
        session.scalars(
            select(GatewayPayment)
            .where(GatewayPayment.invoice_id == invoice.id)
            .order_by(GatewayPayment.paid_on, GatewayPayment.reference)
        )
    )
