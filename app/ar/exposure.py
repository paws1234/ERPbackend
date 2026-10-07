"""T-3.AR.06 — a customer's live exposure, and the one figure the order check uses.

A credit limit is only a control if the number it is compared against is the whole
picture, so this module computes one figure and everything that judges a limit reads
**it** rather than a number of its own:

* **Itemised, because a total nobody can open up is a total nobody trusts.**
  :class:`Exposure` carries its components — what has been invoiced, what has been
  received against it, what is committed on orders not yet billed, and any money
  received with no invoice to apply it to — and the total is their sum. A customer
  disputing a figure can be shown the documents that made it.
* **One implementation.** :func:`app.sales.orders.confirm_order` calls this to get the
  exposure when the caller does not state one, so the order-time check and any
  statement a credit controller reads are the same arithmetic over the same rows:
  T-3.SALES.04's recorded stop left the exposure to be *stated*, and this is what
  replaced it.
* **Per currency.** A limit is agreed in one currency (the customer's own,
  T-3.SALES.01), and two currencies cannot be added without a rate neither side
  stored — so the figure for the agreed currency is the one compared against the
  limit and the others are reported beside it rather than summed into it.
* **A credit is a credit, not a negative exposure.** Money the customer holds is
  subtracted, and where it exceeds what is owed the total is stated at zero with the
  credit named separately: `CreditDecision`'s own rule is that an exposure is not
  negative, and a negative number would make the breach test mean nothing.

**On-account receipts** are the payments received that name no invoice at all — money
in the bank with nothing to apply it to (T-3.AR.05's parked payments carry the reason
they could not be placed). They are money the customer holds against future invoices,
so they reduce the exposure — **its own customer's**, and only when the payment is
attributed to one: the gateway's event names the payer, or the invoice's own customer
does. A receipt nobody has attributed reduces **no** customer's exposure, because
crediting it to each of them in turn would let one customer's order pass on another
customer's prepayment; it stays on the parked list until somebody places it. A payment
parked for a *named* invoice that could not be placed yet is deliberately **not**
counted here either: it belongs to that invoice, and treating it as a credit would
understate what the customer owes.

**Changing a limit is audited** without anything new here: `set_credit_limit` writes
the `customer` row, and T-0.AUDIT.02's trail records every change to an audited table
with its before and after values, so who moved a ceiling and when is on the record.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ar.gateway import PARKED, GatewayPayment
from app.ar.invoices import open_amount, open_invoices, settled_amount
from app.company import company_base_currency
from app.sales.customers import Customer, credit_limit_of

MONEY_SCALE = Decimal("0.000001")

# The components, in the order a statement reads them.
INVOICED, RECEIVED, UNBILLED, ON_ACCOUNT = (
    "invoiced",
    "received",
    "unbilled_orders",
    "on_account_receipts",
)


class ExposureError(ValueError):
    """The exposure refused what was asked of it."""


class Exposure:
    """One customer's exposure in one currency, with the documents behind it."""

    def __init__(
        self,
        *,
        customer_code: str,
        currency: str,
        as_of: date,
        invoiced: Decimal,
        received: Decimal,
        unbilled: Decimal,
        on_account: Decimal,
        other_currencies: dict[str, Decimal],
    ):
        self.customer_code = customer_code
        self.currency = currency
        self.as_of = as_of
        self.invoiced = invoiced.quantize(MONEY_SCALE)
        self.received = received.quantize(MONEY_SCALE)
        self.unbilled_orders = unbilled.quantize(MONEY_SCALE)
        self.on_account_receipts = on_account.quantize(MONEY_SCALE)
        self.other_currencies = {
            code: value.quantize(MONEY_SCALE) for code, value in other_currencies.items()
        }
        # What is owed on invoices already raised: what was billed less what came in
        # against it. Derived from the documents, never a stored balance.
        self.open_invoices = (self.invoiced - self.received).quantize(MONEY_SCALE)
        raw = (self.open_invoices + self.unbilled_orders - self.on_account_receipts).quantize(
            MONEY_SCALE
        )
        # A credit larger than what is owed leaves a credit, not a negative exposure.
        self.credit = (-raw).quantize(MONEY_SCALE) if raw < 0 else Decimal(0).quantize(
            MONEY_SCALE
        )
        # Stated at the money scale either way, so a statement never shows a bare "0"
        # beside figures that carry six decimals.
        self.total = raw if raw > 0 else Decimal(0).quantize(MONEY_SCALE)

    @property
    def components(self) -> dict[str, Decimal]:
        """The itemised figures the total is made of, in statement order."""
        return {
            INVOICED: self.invoiced,
            RECEIVED: self.received,
            UNBILLED: self.unbilled_orders,
            ON_ACCOUNT: self.on_account_receipts,
        }

    def statement(self) -> dict:
        """The exposure as a credit controller reads it, documents included."""
        return {
            "customer": self.customer_code,
            "currency": self.currency,
            "as_of": self.as_of,
            "components": {name: str(value) for name, value in self.components.items()},
            "open_invoices": str(self.open_invoices),
            "credit": str(self.credit),
            "total": str(self.total),
            "other_currencies": {
                code: str(value) for code, value in sorted(self.other_currencies.items())
            },
        }

    def __repr__(self) -> str:  # pragma: no cover - a convenience for a caller's log
        return (
            f"Exposure({self.customer_code}, {self.currency} {self.total}"
            f" = {self.open_invoices} open + {self.unbilled_orders} unbilled"
            f" - {self.on_account_receipts} on account)"
        )


def _order_currency(session: Session, order, customer: Customer) -> str:
    """The currency an order is stated in, resolved the way its own documents do."""
    return str(
        order.currency
        or customer.transaction_currency
        or company_base_currency(session, company_id=customer.company_id)
    )


def _unbilled_by_currency(
    session: Session, customer: Customer, *, as_of: date
) -> dict[str, Decimal]:
    """What confirmed orders still commit the customer to, per currency.

    The order's lines less what has shipped: the goods promised and not yet billed.
    The arithmetic is `order_total`'s (T-3.SALES.04) — quantity times unit price —
    applied to the remaining quantity, so an order and the exposure it causes cannot
    disagree about what a line is worth.
    """
    # Imported here rather than at the top: `app.sales.orders` reads this module (that
    # is the point of T-3.AR.06 — one implementation of the exposure), so importing it
    # at module scope would be a cycle.
    from app.sales.orders import CONFIRMED, SalesOrder

    statement = select(SalesOrder).where(
        SalesOrder.company_id == customer.company_id,
        SalesOrder.customer_id == customer.id,
        SalesOrder.status == CONFIRMED,
        SalesOrder.ordered_on <= as_of,
    )
    totals: dict[str, Decimal] = {}
    for order in session.scalars(statement):
        currency = _order_currency(session, order, customer)
        committed = Decimal(0)
        for line in order.lines:
            remaining = (line.quantity - line.shipped_quantity).quantize(MONEY_SCALE)
            if remaining > 0:
                committed += (remaining * line.unit_price).quantize(MONEY_SCALE)
        totals[currency] = totals.get(currency, Decimal(0)) + committed
    return totals


def customer_exposure(
    session: Session,
    customer: Customer,
    *,
    as_of: Any = None,
    currency: str | None = None,
) -> Exposure:
    """What this customer owes and has committed, itemised, in one currency.

    The currency is the customer's own (T-3.SALES.01's `transaction_currency`, or the
    company's base where it has none) unless the caller names another — the currency a
    limit is agreed in. Documents in other currencies are reported in
    :attr:`Exposure.other_currencies` rather than added to a figure they cannot be
    added to without a rate.
    """
    moment = as_of or date.today()
    chosen = str(
        currency
        or customer.transaction_currency
        or company_base_currency(session, company_id=customer.company_id)
    )

    invoiced: dict[str, Decimal] = {}
    received: dict[str, Decimal] = {}
    for invoice in open_invoices(
        session, company_id=customer.company_id, customer=customer, as_of=moment
    ):
        code = invoice.currency
        invoiced[code] = invoiced.get(code, Decimal(0)) + Decimal(invoice.gross_amount)
        received[code] = received.get(code, Decimal(0)) + settled_amount(
            session, invoice, as_of=moment
        )

    unbilled = _unbilled_by_currency(session, customer, as_of=moment)

    on_account: dict[str, Decimal] = {}
    for payment in session.scalars(
        select(GatewayPayment).where(
            GatewayPayment.company_id == customer.company_id,
            # Whose money it is. A receipt nobody has attributed to a customer
            # (T-3.AR.05 leaves it parked and listed) reduces *no* customer's
            # exposure: crediting it to every customer would let one customer's order
            # pass on another customer's prepayment, which is what this figure exists
            # to prevent.
            GatewayPayment.customer_id == customer.id,
            GatewayPayment.status == PARKED,
            GatewayPayment.invoice_number.is_(None),
            GatewayPayment.paid_on <= moment,
        )
    ):
        # An unstated currency on the payment is the customer's own, exactly as an
        # invoice's unstated currency is.
        code = payment.currency or chosen
        on_account[code] = on_account.get(code, Decimal(0)) + Decimal(payment.amount)

    others = {
        code: (invoiced.get(code, Decimal(0)) - received.get(code, Decimal(0)))
        + unbilled.get(code, Decimal(0))
        - on_account.get(code, Decimal(0))
        for code in set(invoiced) | set(unbilled) | set(on_account)
        if code != chosen
    }
    if currency is None and chosen not in invoiced:
        # A customer whose only documents are abroad still exists; the figure for a
        # currency it has nothing in is simply zero, and the others are reported.
        invoiced.setdefault(chosen, Decimal(0))
    return Exposure(
        customer_code=customer.party.code,
        currency=chosen,
        as_of=moment,
        invoiced=invoiced.get(chosen, Decimal(0)),
        received=received.get(chosen, Decimal(0)),
        unbilled=unbilled.get(chosen, Decimal(0)),
        on_account=on_account.get(chosen, Decimal(0)),
        other_currencies=others,
    )


def exposure_against_limit(
    session: Session, customer: Customer, *, as_of: Any = None
) -> dict:
    """The exposure beside the limit it is judged against — what a credit review reads.

    Null stays null: a customer with no agreed ceiling is reported as having none
    rather than as a zero one (T-3.SALES.01's three states).
    """
    exposure = customer_exposure(session, customer, as_of=as_of)
    limit = credit_limit_of(customer)
    used = None if limit is None else Decimal(limit)
    return {
        "statement": exposure.statement(),
        "limit": None if used is None else str(used),
        "headroom": None if used is None else str((used - exposure.total).quantize(MONEY_SCALE)),
        "breached": False if used is None else exposure.total > used,
    }


def open_items(
    session: Session,
    customer: Customer,
    *,
    as_of: Any = None,
    currency: str | None = None,
) -> list[dict]:
    """The open invoices behind the exposure, one row each — the itemisation.

    The document-level backing for the figure above, so a customer asking "what is
    this made of" can be answered with the invoices rather than with the total. Every
    open invoice is listed with its own currency; naming a `currency` narrows it to the
    ones that make up that currency's figure.
    """
    moment = as_of or date.today()
    rows = []
    for invoice in open_invoices(
        session, company_id=customer.company_id, customer=customer, as_of=moment
    ):
        if currency is not None and invoice.currency != str(currency):
            continue
        outstanding = open_amount(session, invoice, as_of=moment)
        if outstanding <= 0:
            continue
        rows.append(
            {
                "invoice": invoice.number,
                "invoice_date": invoice.invoice_date,
                "due_date": invoice.due_date,
                "currency": invoice.currency,
                "gross_amount": invoice.gross_amount,
                "open_amount": outstanding,
            }
        )
    return rows
