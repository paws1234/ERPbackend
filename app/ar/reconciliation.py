"""T-3.AR.07 — proving the receivables subledger and the control account agree.

§3's "Double-Entry Integrity" row and §6's metrics are the reason this module
exists, and the task states the rule exactly: **open customer balances equal the
receivables control account, per company and period, and any difference is
reported rather than absorbed.**

Three things about how it does that:

* **It compares, it does not correct.** :func:`reconcile` returns the two figures,
  the difference and a verdict. Nothing here writes an adjustment, because an
  adjustment would hide exactly the discrepancy this exists to surface.
* **Both sides come from the documents, not from a balance.** The subledger side
  walks T-3.AR.01's posted invoices and adds up what each is still owed, so a
  partial receipt (T-3.AR.05) moves it by the settlement it appended; the control
  side reads the ledger lines the postings themselves wrote. The only input the two
  share is those postings, so a drift between them is a real finding rather than an
  artefact of two caches.
* **Per currency.** A foreign-currency invoice is compared with the entry it
  actually produced, in its own currency: mixing currencies would need a rate
  neither side stored (T-1.ACCT.05).

:func:`control_balance` is also what T-3.AR.02's aging report shows beside its own
total — the same figure, read through one function, so the two cannot drift apart.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ar.invoices import (
    RECEIVABLES_KEY,
    CustomerInvoice,
    CustomerInvoiceSettlement,
    open_amount,
)
from app.ledger.mapping import mapped_account
from app.ledger.posting import JournalEntry, JournalLine

MONEY_SCALE = Decimal("0.000001")


class ReconciliationError(ValueError):
    """The reconciliation refused what was asked of it."""


def control_balance(
    session: Session,
    *,
    company_id: uuid.UUID,
    currency: str,
    as_of: date | None = None,
    start: date | None = None,
) -> Decimal:
    """What the receivables control account holds in one currency, from the ledger.

    Read from `journal_line` joined to its entry, as the amounts were **stated** —
    the account the mapping points at, restricted to entries in that currency. A
    receivable is a debit, so the balance is debits less credits: the same sign as
    the open amounts the aging report adds up, which is what makes the two sides
    comparable at all.
    """
    account = mapped_account(session, company_id=company_id, key=RECEIVABLES_KEY)
    statement = (
        select(func.coalesce(func.sum(JournalLine.debit - JournalLine.credit), 0))
        .select_from(JournalLine)
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .where(
            JournalEntry.company_id == company_id,
            JournalEntry.currency == str(currency),
            JournalLine.account_id == account.id,
        )
    )
    if as_of is not None:
        statement = statement.where(JournalEntry.posting_date <= as_of)
    if start is not None:
        statement = statement.where(JournalEntry.posting_date >= start)
    total = session.scalar(statement)
    return Decimal(total or 0).quantize(MONEY_SCALE)


def subledger_balance(
    session: Session,
    *,
    company_id: uuid.UUID,
    currency: str,
    as_of: date | None = None,
) -> Decimal:
    """What the invoices in one currency are still owed, as at a date.

    Built from each invoice's derived `open_amount`, so it is the settlements and the
    postings that decide it rather than a figure kept beside them — which is what lets
    a partial receipt (T-3.AR.05) move both sides of this comparison by the same
    amount rather than by two amounts that have to be kept in step.
    """
    statement = select(CustomerInvoice).where(
        CustomerInvoice.company_id == company_id,
        CustomerInvoice.currency == str(currency),
        CustomerInvoice.status == "posted",
    )
    if as_of is not None:
        statement = statement.where(CustomerInvoice.invoice_date <= as_of)
    total = Decimal(0)
    for invoice in session.scalars(statement):
        total += open_amount(session, invoice, as_of=as_of)
    return total.quantize(MONEY_SCALE)


def subledger_movement(
    session: Session,
    *,
    company_id: uuid.UUID,
    currency: str,
    start: date,
    as_of: date | None = None,
) -> Decimal:
    """What the subledger moved by between two dates — the window's own figure.

    The counterpart of :func:`control_balance`'s window: invoices raised inside it,
    less the settlements posted inside it. It is stated this way, and not as
    `subledger_balance(to) - subledger_balance(from)`, so that a settlement is counted
    in the window it was *posted* in — the same window the control account counts it
    in. Comparing a position to a movement would report every correct period as a
    difference, which is the one thing a reconciliation must never do.
    """
    raised = (
        select(func.coalesce(func.sum(CustomerInvoice.gross_amount), 0))
        .where(
            CustomerInvoice.company_id == company_id,
            CustomerInvoice.currency == str(currency),
            CustomerInvoice.status == "posted",
            CustomerInvoice.invoice_date >= start,
        )
    )
    if as_of is not None:
        raised = raised.where(CustomerInvoice.invoice_date <= as_of)
    settled = (
        select(func.coalesce(func.sum(CustomerInvoiceSettlement.amount), 0))
        .join(CustomerInvoice, CustomerInvoice.id == CustomerInvoiceSettlement.invoice_id)
        .where(
            CustomerInvoice.company_id == company_id,
            CustomerInvoice.currency == str(currency),
            CustomerInvoiceSettlement.settled_on >= start,
        )
    )
    if as_of is not None:
        settled = settled.where(CustomerInvoiceSettlement.settled_on <= as_of)
    total = Decimal(session.scalar(raised) or 0) - Decimal(session.scalar(settled) or 0)
    return total.quantize(MONEY_SCALE)


def currencies_in_use(session: Session, *, company_id: uuid.UUID) -> list[str]:
    """Every currency present in either the invoice subledger or the control account.

    Both sides, not just the invoices: a posting that reached the control account with
    nothing behind it in the subledger is exactly the difference this module exists to
    report, and reading only the invoices would hide it.
    """
    invoice_rows = session.scalars(
        select(CustomerInvoice.currency)
        .where(
            CustomerInvoice.company_id == company_id,
            CustomerInvoice.status == "posted",
        )
        .distinct()
    )
    try:
        account = mapped_account(session, company_id=company_id, key=RECEIVABLES_KEY)
    except ValueError:
        control_rows = ()
    else:
        control_rows = session.scalars(
            select(JournalEntry.currency)
            .join(JournalLine, JournalLine.entry_id == JournalEntry.id)
            .where(
                JournalEntry.company_id == company_id,
                JournalLine.account_id == account.id,
            )
            .distinct()
        )
    return sorted({str(row) for row in (*invoice_rows, *control_rows)})


def reconcile(
    session: Session,
    *,
    company_id: uuid.UUID,
    as_of: date | None = None,
    start: date | None = None,
) -> dict:
    """Compare the AR subledger with the control account, per currency.

    Returns the two sides, the difference and the verdict for each currency, plus the
    overall verdict. Both sides always measure the **same thing**: as at `as_of` they
    are positions (what the invoices are still owed, what the account holds), and from
    `start` they are that window's movements (invoices raised and settlements posted,
    against the account's own movement). A position compared with a movement would
    report a correct period as a difference.

    A difference is **reported**, never absorbed: repairing it here would destroy the
    evidence the reconciliation exists to carry.
    """
    moment = as_of or date.today()
    if start is not None and start > moment:
        raise ReconciliationError(
            f"a window of {start}..{moment} ends before it starts; there is nothing to"
            " reconcile in it"
        )
    per_currency = []
    for currency in currencies_in_use(session, company_id=company_id):
        if start is None:
            subledger = subledger_balance(
                session, company_id=company_id, currency=currency, as_of=moment
            )
        else:
            subledger = subledger_movement(
                session,
                company_id=company_id,
                currency=currency,
                start=start,
                as_of=moment,
            )
        control = control_balance(
            session, company_id=company_id, currency=currency, as_of=moment, start=start
        )
        difference = (subledger - control).quantize(MONEY_SCALE)
        per_currency.append(
            {
                "currency": currency,
                "subledger": subledger,
                "control": control,
                "difference": difference,
                "balanced": difference == 0,
            }
        )
    return {
        "as_of": moment,
        "start": start,
        # Which of the two comparable measurements this report is: the position to
        # date, or the movement inside the window.
        "measure": "position" if start is None else "period",
        "currencies": per_currency,
        "balanced": all(row["balanced"] for row in per_currency),
    }


def explain(report: dict) -> str:
    """The reconciliation as a sentence a person can act on."""
    lines = []
    for row in report["currencies"]:
        verdict = "agrees" if row["balanced"] else f"differs by {row['difference']}"
        lines.append(
            f"{row['currency']}: the AR subledger holds {row['subledger']} and the"
            f" receivables control account holds {row['control']} — {verdict}"
        )
    head = "balanced" if report["balanced"] else "NOT balanced"
    measure = (
        ""
        if report.get("measure") != "period"
        else f" over {report['start']}..{report['as_of']}"
    )
    return f"AR to GL as at {report['as_of']}{measure} ({head}): " + "; ".join(lines)
