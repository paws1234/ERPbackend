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

from app.ar.invoices import RECEIVABLES_KEY
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
