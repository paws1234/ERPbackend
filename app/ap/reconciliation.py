"""T-2.AP.05 — proving the payables subledger and the control account agree.

§3's "Double-Entry Integrity" row and §6's metrics are the reason this exists, and the
task states the rule exactly: **open supplier balances equal the payables control
account, per company and period, and any difference is reported rather than
absorbed.**

Three things about how it does that:

* **It compares, it does not correct.** :func:`reconcile` returns the two figures, the
  difference and a verdict. Nothing here posts an adjusting entry — a difference is a
  finding for somebody to investigate, and a reconciliation that quietly fixed itself
  would hide the very thing it is for.
* **It compares per currency.** The control account is read from the journal lines as
  they were **stated**, and the subledger from the invoices as they were **stated**, so
  both sides are in the document's own currency. Comparing in the base currency would
  mean choosing a rate for a comparison neither side stored — and a reconciliation that
  depends on today's rate is a reconciliation that changes when the rate does.
* **The subledger is built from the settlements, not from a balance.** What an invoice
  is owed is T-2.AP.01's derived `open_amount`, so a payment (T-2.AP.04) or a debit
  note (T-2.AP.03) moves both sides of this comparison by construction.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ap.invoices import PAYABLES_KEY, SupplierInvoice, open_amount
from app.ledger.mapping import mapped_account
from app.ledger.posting import JournalEntry, JournalLine

# One money scale for the whole platform.
MONEY_SCALE = Decimal("0.000001")


class ReconciliationError(ValueError):
    """The reconciliation could not be run as asked."""


def subledger_balance(
    session: Session,
    *,
    company_id: uuid.UUID,
    currency: str,
    as_of: date | None = None,
) -> Decimal:
    """What the invoices in one currency are still owed, as at a date.

    Built from each invoice's derived `open_amount`, so it is the settlements and the
    postings that decide it rather than a figure kept beside them.
    """
    statement = select(SupplierInvoice).where(
        SupplierInvoice.company_id == company_id,
        SupplierInvoice.currency == str(currency),
        SupplierInvoice.status == "posted",
    )
    if as_of is not None:
        statement = statement.where(SupplierInvoice.invoice_date <= as_of)
    total = Decimal(0)
    for invoice in session.scalars(statement):
        total += open_amount(session, invoice, as_of=as_of)
    return total.quantize(MONEY_SCALE)


def control_balance(
    session: Session,
    *,
    company_id: uuid.UUID,
    currency: str,
    as_of: date | None = None,
    start: date | None = None,
) -> Decimal:
    """What the payables control account holds in one currency, from the ledger itself.

    Read from `journal_line` joined to its entry, as the amounts were **stated** — the
    account the mapping points at, restricted to entries in that currency, so a
    foreign-currency document is compared with the entry it actually produced.
    """
    account = mapped_account(session, company_id=company_id, key=PAYABLES_KEY)
    statement = (
        select(
            func.coalesce(func.sum(JournalLine.credit - JournalLine.debit), 0)
        )
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


def currencies_in_use(session: Session, *, company_id: uuid.UUID) -> list[str]:
    """Every currency present in either the invoice subledger or control account."""
    invoice_rows = session.scalars(
        select(SupplierInvoice.currency)
        .where(
            SupplierInvoice.company_id == company_id,
            SupplierInvoice.status == "posted",
        )
        .distinct()
    )
    try:
        account = mapped_account(session, company_id=company_id, key=PAYABLES_KEY)
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
    currencies: Any = None,
) -> dict:
    """Compare the payables subledger with the control account, per currency.

    Returns one row per currency with both figures, the difference and whether it is
    nil, plus an overall verdict. `difference` is what it is — a mismatch is a number to
    investigate, never a correction made here.
    """
    wanted = list(currencies or currencies_in_use(session, company_id=company_id))
    rows = []
    for currency in wanted:
        subledger = subledger_balance(
            session, company_id=company_id, currency=currency, as_of=as_of
        )
        control = control_balance(
            session, company_id=company_id, currency=currency, as_of=as_of, start=start
        )
        difference = (subledger - control).quantize(MONEY_SCALE)
        rows.append(
            {
                "currency": str(currency),
                "subledger": subledger,
                "control": control,
                "difference": difference,
                "balanced": difference == 0,
            }
        )
    return {
        "as_of": as_of,
        "start": start,
        "currencies": rows,
        "balanced": all(row["balanced"] for row in rows) if rows else True,
        "difference_total": sum(
            (row["difference"].copy_abs() for row in rows), Decimal(0)
        ).quantize(MONEY_SCALE),
        "reason": None
        if rows
        else "no posted invoice is stated in any currency, so there is nothing to compare",
    }


def explain(report: dict) -> str:
    """One line naming the currency that does not agree, or that they all do."""
    for row in report["currencies"]:
        if not row["balanced"]:
            return (
                f"{row['currency']}: the subledger says {row['subledger']} is owed but"
                f" the control account holds {row['control']} — a difference of"
                f" {row['difference']}"
            )
    if not report["currencies"]:
        return report["reason"] or "nothing to compare"
    names = ", ".join(row["currency"] for row in report["currencies"])
    return f"the subledger and the payables control account agree in {names}"
