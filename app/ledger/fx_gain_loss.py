"""T-1.ACCT.06 — realized and unrealized FX gain/loss, posted through the ledger.

§2.1 asks for "unrealized/realized gain/loss" as part of the multi-currency
engine. Both are the same piece of arithmetic — the difference between what a
foreign balance was carried at and what it is worth now — and both are **posted**,
because a difference that is only reported is a difference nobody's books show:

* **Realized** (:func:`settle_document`): a foreign document is settled at a rate
  other than the one it was booked at. The difference between its booked base
  value (the sum of `amount × exchange_rate` over its own postings) and its value
  at the settlement rate is posted against the document's control account, offset
  to the gain or the loss account.
* **Unrealized** (:func:`revalue_open_balance`): the same difference for a balance
  that is still open, at a chosen date's rate. It is **repeatable without
  double-counting** — what is already posted for that account and currency is
  subtracted before anything new is posted — so the run can happen as often as
  someone wants, and the second run of the same date posts nothing.
  :func:`reverse_revaluation` posts the negation, because the ledger is
  append-only: a revaluation is undone by an entry, never by deleting one.

Where the difference goes is configuration, not code: `fx_gain` and `fx_loss` are
account-mapping keys (T-1.ACCT.03), so a company points them at its own accounts —
the Philippines pack's 4910/5990 are only what the pack suggests.

**The sign carries the meaning.** A positive difference is a gain — the balance is
worth more than it was carried at — so the gain account is credited; a negative
one is a loss and the loss account is debited. Which side of the balance it lands
on follows from the difference itself, because a credit balance times a rate
change produces a negative difference, so no caller has to say whether its account
is an asset or a liability.

ponytail: a revaluation is recognised again by the memo its own run wrote, because
one run is "this account, this currency, this date" and this module is the only
writer of that text. Ceiling: a caller inventing a memo in the same shape. Upgrade
path: give a revaluation its own document row when it needs finding other than by
account and date.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.company import company_base_currency
from app.ledger.currency import rate_for
from app.ledger.gl import entries_for_source
from app.ledger.mapping import mapped_account
from app.ledger.posting import JournalEntry, JournalLine, post_journal_entry

# The source types this module writes. A revaluation is a document in its own
# right: it is found by these, and an open balance excludes them so a revaluation
# never revalues its own prior posting.
REVALUATION = "fx_revaluation"
REVALUATION_REVERSAL = "fx_revaluation_reversal"
REVALUATION_TYPES = (REVALUATION, REVALUATION_REVERSAL)

# The memo a revaluation run writes, and how it is recognised again.
MEMO_PREFIX = "unrealized FX on"

MONEY_SCALE = Decimal("0.000001")


class FxGainLossError(ValueError):
    """The gain/loss calculation could not be made as asked."""


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_SCALE)


def _revaluation_memo(account_code: str, currency: str, rate: Any, as_of: date) -> str:
    return f"{MEMO_PREFIX} {account_code} {str(currency).upper()} at {rate} on {as_of}"


def _memo_pattern(account_code: str, currency: str) -> str:
    """How a balance's revaluations are recognised: this account, this currency.

    A reversal carries the same account and currency in its memo ("reversal of
    unrealized FX on 1100 USD on …"), so it nets against the run it undid instead
    of being counted as one more revaluation.
    """
    return f"%{MEMO_PREFIX} {account_code} {str(currency).upper()} %"


def _revaluation_id(prefix: str, **parts: Any) -> uuid.UUID:
    """A stable id for one balance and date — one document per run."""
    tail = "/".join(str(parts[key]) for key in sorted(parts))
    return uuid.uuid5(uuid.NAMESPACE_URL, f"{prefix}/{tail}")


def open_foreign_balance(
    session: Session, *, company_id: uuid.UUID, account_code: str, currency: str, as_of: date
) -> tuple[Decimal, Decimal]:
    """`(foreign amount, booked base value)` for one account in one currency.

    The foreign amount is the account's signed net in `currency`; the booked base
    value is the same lines at the rate each was posted at. Revaluation entries are
    excluded: they restate the carrying value, they do not change the balance.
    """
    statement = (
        select(
            func.coalesce(
                func.sum(JournalLine.debit - JournalLine.credit), 0
            ).label("foreign"),
            func.coalesce(
                func.sum((JournalLine.debit - JournalLine.credit) * JournalEntry.exchange_rate),
                0,
            ).label("booked"),
        )
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .where(
            JournalEntry.company_id == company_id,
            JournalEntry.currency == str(currency).strip().upper(),
            JournalEntry.posting_date <= as_of,
            JournalLine.account == str(account_code),
            JournalEntry.source_type.notin_(REVALUATION_TYPES),
        )
    )
    foreign, booked = session.execute(statement).one()
    return Decimal(foreign), Decimal(booked)


def already_revalued(
    session: Session, *, company_id: uuid.UUID, account_code: str, currency: str, as_of: date
) -> Decimal:
    """What revaluation has already been posted on this balance up to `as_of`."""
    statement = (
        select(func.coalesce(func.sum(JournalLine.debit - JournalLine.credit), 0))
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .where(
            JournalEntry.company_id == company_id,
            JournalEntry.posting_date <= as_of,
            JournalLine.account == str(account_code),
            JournalEntry.source_type.in_(REVALUATION_TYPES),
            JournalEntry.memo.like(_memo_pattern(account_code, currency)),
        )
    )
    return Decimal(session.scalar(statement))


def _post_difference(
    session: Session,
    *,
    company_id: uuid.UUID,
    account_code: str,
    difference: Decimal,
    posting_date: date,
    source_type: str,
    source_id: uuid.UUID,
    memo: str,
) -> JournalEntry:
    """Post `difference` on the account, offset to the gain or loss account.

    Positive is a gain (credit the gain account), negative is a loss (debit the
    loss account) — see the module docstring for why the sign is enough.
    """
    amount = _money(abs(difference))
    offset = mapped_account(
        session, company_id=company_id, key="fx_gain" if difference > 0 else "fx_loss"
    ).code
    return post_journal_entry(
        session,
        company_id=company_id,
        posting_date=posting_date,
        currency=company_base_currency(session, company_id=company_id),
        source_type=source_type,
        source_id=source_id,
        memo=memo,
        lines=[
            {
                "account": str(account_code),
                "debit": amount if difference > 0 else Decimal(0),
                "credit": Decimal(0) if difference > 0 else amount,
            },
            {
                "account": offset,
                "credit": amount if difference > 0 else Decimal(0),
                "debit": Decimal(0) if difference > 0 else amount,
            },
        ],
    )


def settle_document(
    session: Session,
    *,
    company_id: uuid.UUID,
    document_type: str,
    document_id: uuid.UUID,
    account_code: str,
    settlement_rate: Any,
    settlement_date: date,
) -> JournalEntry | None:
    """Post the realized difference on a foreign-currency document being settled.

    `account_code` is the document's control account (the payable or receivable
    the postings used). The difference is the value at `settlement_rate` minus the
    value the postings carried, both exact; zero means no entry, because there is
    nothing to post.
    """
    entries = entries_for_source(
        session, company_id=company_id, source_type=document_type, source_id=document_id
    )
    if not entries:
        raise FxGainLossError(
            f"no posting found for {document_type} {document_id}; nothing to settle"
        )
    lines = [
        line for entry in entries for line in entry.lines if line.account == str(account_code)
    ]
    if not lines:
        raise FxGainLossError(
            f"{document_type} {document_id} has no posting on account {account_code!r};"
            " state the account its postings used"
        )
    foreign = sum((line.debit - line.credit for line in lines), Decimal(0))
    booked = sum(
        (
            (line.debit - line.credit) * entry.exchange_rate
            for entry in entries
            for line in entry.lines
            if line.account == str(account_code)
        ),
        Decimal(0),
    )
    rate = settlement_rate if isinstance(settlement_rate, Decimal) else Decimal(str(settlement_rate))
    difference = _money(foreign * rate - booked)
    if difference == 0:
        return None
    return _post_difference(
        session,
        company_id=company_id,
        account_code=account_code,
        difference=difference,
        posting_date=settlement_date,
        source_type=f"{document_type}_fx_settlement",
        source_id=document_id,
        memo=f"realized FX on {document_type} {document_id} at {rate}",
    )


def revalue_open_balance(
    session: Session,
    *,
    company_id: uuid.UUID,
    account_code: str,
    currency: str,
    as_of: date,
    rate: Any = None,
) -> JournalEntry | None:
    """Post the unrealized difference on an open foreign balance at `as_of`.

    Repeatable: the difference is measured against what has already been posted
    for the same balance, so running it twice for the same date posts once. Zero
    difference (or nothing to revalue) means no entry.
    """
    base = company_base_currency(session, company_id=company_id)
    if str(currency).strip().upper() == base:
        raise FxGainLossError(f"{currency} is the base currency; it does not revalue")
    wanted = (
        rate
        if rate is not None
        else rate_for(
            session,
            company_id=company_id,
            base_currency=base,
            currency=currency,
            on=as_of,
        )
    )
    wanted = wanted if isinstance(wanted, Decimal) else Decimal(str(wanted))
    foreign, booked = open_foreign_balance(
        session, company_id=company_id, account_code=account_code, currency=currency, as_of=as_of
    )
    if foreign == 0:
        return None
    posted = already_revalued(
        session, company_id=company_id, account_code=account_code, currency=currency, as_of=as_of
    )
    difference = _money(foreign * wanted - booked - posted)
    if difference == 0:
        return None
    return _post_difference(
        session,
        company_id=company_id,
        account_code=account_code,
        difference=difference,
        posting_date=as_of,
        source_type=REVALUATION,
        source_id=_revaluation_id(
            "fx_revaluation",
            company_id=company_id,
            account_code=str(account_code),
            currency=str(currency).upper(),
            as_of=as_of,
        ),
        memo=_revaluation_memo(account_code, currency, wanted, as_of),
    )


def reverse_revaluation(
    session: Session,
    *,
    company_id: uuid.UUID,
    account_code: str,
    currency: str,
    as_of: date,
    posting_date: date | None = None,
) -> JournalEntry | None:
    """Undo the revaluation posted for one balance and date, by posting its negative.

    Reversing a revaluation is a new entry — the original stays where it is, which
    is the whole point of an append-only ledger. Returns ``None`` when there is
    nothing posted to reverse.
    """
    posted = already_revalued(
        session, company_id=company_id, account_code=account_code, currency=currency, as_of=as_of
    )
    if posted == 0:
        return None
    return _post_difference(
        session,
        company_id=company_id,
        account_code=account_code,
        difference=-_money(posted),
        posting_date=posting_date or as_of,
        source_type=REVALUATION_REVERSAL,
        source_id=_revaluation_id(
            "fx_revaluation_reversal",
            company_id=company_id,
            account_code=str(account_code),
            currency=str(currency).upper(),
            as_of=as_of,
        ),
        memo=f"reversal of {MEMO_PREFIX} {account_code} {str(currency).upper()} on {as_of}",
    )
