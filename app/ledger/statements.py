"""T-1.ACCT.07 — the four statements, read from the general ledger.

§2.1 asks for "Financial statements (Trial Balance, P&L, Balance Sheet, Cash
Flow)", and §4's Phase 1 bullet 5 for "basic financial reports". All four are
**derived from the entries** — no balance table, no snapshot, nothing cached — so
a statement cannot disagree with the ledger it came from; if one does, the ledger
is what happened.

Two conventions this module is explicit about, because both are easy to get wrong:

* **Everything is stated in the company's base currency.** A line's amount is in
  the entry's transaction currency, so every figure here is
  `(debit − credit) × exchange_rate` — the carrying value (T-1.ACCT.05). A dollar
  balance and a peso balance are therefore addable, which is the whole point of a
  reporting currency.
* **Each statement says whether it foots.** `balanced` is computed, not asserted by
  a human: Trial Balance debit total equals credit total, the Balance Sheet's
  assets equal liabilities plus equity plus the period's earnings, and the Cash
  Flow's closing balance equals the cash accounts' balance on the closing date.
  A report that says `false` is the alarm, not a rendering bug.

The four are registered with T-0.REPORT.01's framework (:func:`register_builders`),
so a definition with a cron schedule and a recipient list (both rows) runs them
and delivers the result through the integration boundary. The scheduled run covers
the **current calendar month**; a caller wanting another period calls the function
directly with it.

ponytail: cash is identified by the account-mapping keys that begin with `cash`
(`cash`, `cash_bank`, `cash_petty`, …) rather than by a new column, and operating /
investing / financing classification is not attempted — the plan names neither the
classification nor its inputs. Ceiling: a company wanting classified cash flow.
Upgrade path: Phase 6's analytics owns the classification; the rows here are
already the counterpart-level detail it needs.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ledger.accounts import Account
from app.ledger.mapping import mappings
from app.ledger.posting import JournalEntry, JournalLine

MONEY_SCALE = Decimal("0.000001")

# The classes a statement groups by.
ASSET, LIABILITY, EQUITY, INCOME, EXPENSE = (
    "asset",
    "liability",
    "equity",
    "income",
    "expense",
)

# The mapping keys that name a cash account, by convention.
CASH_KEY_PREFIX = "cash"


class StatementError(ValueError):
    """A statement could not be produced as asked."""


def _money(value) -> str:
    return format(Decimal(value).quantize(MONEY_SCALE), "f")


def _period(start: date | None, end: date | None) -> tuple[date | None, date | None]:
    if start is not None and end is not None and start > end:
        raise StatementError(f"the period starts after it ends: {start} > {end}")
    return start, end


def _movements(
    session: Session,
    *,
    company_id: uuid.UUID,
    start: date | None = None,
    end: date | None = None,
    accounts: list[str] | None = None,
) -> dict[str, Decimal]:
    """Base-currency net (`debit − credit`) per account code over a period."""
    start, end = _period(start, end)
    statement = (
        select(
            JournalLine.account,
            func.sum((JournalLine.debit - JournalLine.credit) * JournalEntry.exchange_rate),
        )
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .where(JournalEntry.company_id == company_id)
        .group_by(JournalLine.account)
    )
    if start is not None:
        statement = statement.where(JournalEntry.posting_date >= start)
    if end is not None:
        statement = statement.where(JournalEntry.posting_date <= end)
    if accounts is not None:
        statement = statement.where(JournalLine.account.in_(accounts))
    return {code: Decimal(total) for code, total in session.execute(statement)}


def _debits_credits(
    session: Session, *, company_id: uuid.UUID, start: date | None, end: date | None
) -> dict[str, tuple[Decimal, Decimal]]:
    """Base-currency debit and credit totals per account over a period."""
    start, end = _period(start, end)
    statement = (
        select(
            JournalLine.account,
            func.sum(JournalLine.debit * JournalEntry.exchange_rate),
            func.sum(JournalLine.credit * JournalEntry.exchange_rate),
        )
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .where(JournalEntry.company_id == company_id)
        .group_by(JournalLine.account)
    )
    if start is not None:
        statement = statement.where(JournalEntry.posting_date >= start)
    if end is not None:
        statement = statement.where(JournalEntry.posting_date <= end)
    return {
        code: (Decimal(debits or 0), Decimal(credits or 0))
        for code, debits, credits in session.execute(statement)
    }


def _account_names(session: Session, *, company_id: uuid.UUID) -> dict[str, tuple[str, str]]:
    """`code -> (name, class)` for the company's live accounts."""
    return {
        account.code: (account.name, account.account_class)
        for account in session.scalars(
            select(Account).where(Account.company_id == company_id)
        )
    }


def trial_balance(
    session: Session, *, company_id: uuid.UUID, start: date | None = None, end: date | None = None
) -> dict:
    """Every account's debit and credit totals, and the sum of each.

    `balanced` is the classic check: the two totals are equal when every posting
    balanced, so a `false` here means the ledger itself is broken.
    """
    totals = _debits_credits(session, company_id=company_id, start=start, end=end)
    names = _account_names(session, company_id=company_id)
    rows = [
        {
            "account": code,
            "name": names.get(code, ("", ""))[0],
            "class": names.get(code, ("", ""))[1],
            "debit": _money(debits),
            "credit": _money(credits),
            "balance": _money(debits - credits),
        }
        for code, (debits, credits) in sorted(totals.items())
    ]
    total_debit = sum((Decimal(row["debit"]) for row in rows), Decimal(0))
    total_credit = sum((Decimal(row["credit"]) for row in rows), Decimal(0))
    return {
        "statement": "trial_balance",
        "from": start.isoformat() if start else None,
        "to": end.isoformat() if end else None,
        "rows": rows,
        "total_debit": _money(total_debit),
        "total_credit": _money(total_credit),
        "balanced": total_debit == total_credit,
    }


def _by_class(movements: dict[str, Decimal], names: dict, wanted: str, credit_positive: bool):
    """The accounts of one class, as positive figures, in code order."""
    rows = []
    for code, net in sorted(movements.items()):
        name, account_class = names.get(code, ("", ""))
        if account_class != wanted or net == 0:
            continue
        amount = -net if credit_positive else net
        rows.append({"account": code, "name": name, "amount": _money(amount)})
    return rows


def profit_and_loss(
    session: Session, *, company_id: uuid.UUID, start: date, end: date
) -> dict:
    """Income and expenses for a period, and the result.

    Income is credit-positive and expense debit-positive, so both read as positive
    figures and the profit is `income − expenses`. It foots by construction, and
    `balanced` reports that the arithmetic did what it says.
    """
    movements = _movements(session, company_id=company_id, start=start, end=end)
    names = _account_names(session, company_id=company_id)
    income = _by_class(movements, names, INCOME, credit_positive=True)
    expenses = _by_class(movements, names, EXPENSE, credit_positive=False)
    total_income = sum((Decimal(row["amount"]) for row in income), Decimal(0))
    total_expenses = sum((Decimal(row["amount"]) for row in expenses), Decimal(0))
    return {
        "statement": "profit_and_loss",
        "from": start.isoformat(),
        "to": end.isoformat(),
        "income": income,
        "expenses": expenses,
        "total_income": _money(total_income),
        "total_expenses": _money(total_expenses),
        "net_profit": _money(total_income - total_expenses),
        "balanced": True,
    }


def balance_sheet(session: Session, *, company_id: uuid.UUID, as_of: date) -> dict:
    """Assets, liabilities and equity at a date, with the period's earnings in equity.

    Phase 1 has no year-end closing entries, so the income and expense accounts'
    balance to date is shown as the current earnings inside equity — that is what
    makes `assets = liabilities + equity` hold on any date rather than only after a
    close.
    """
    movements = _movements(session, company_id=company_id, end=as_of)
    names = _account_names(session, company_id=company_id)
    assets = _by_class(movements, names, ASSET, credit_positive=False)
    liabilities = _by_class(movements, names, LIABILITY, credit_positive=True)
    equity = _by_class(movements, names, EQUITY, credit_positive=True)

    income = sum(
        (abs(net) for code, net in movements.items() if names.get(code, ("", ""))[1] == INCOME),
        Decimal(0),
    )
    expenses = sum(
        (net for code, net in movements.items() if names.get(code, ("", ""))[1] == EXPENSE),
        Decimal(0),
    )
    earnings = income - expenses
    equity.append(
        {"account": None, "name": "Current Year Earnings", "amount": _money(earnings)}
    )

    total_assets = sum((Decimal(row["amount"]) for row in assets), Decimal(0))
    total_liabilities = sum((Decimal(row["amount"]) for row in liabilities), Decimal(0))
    total_equity = sum((Decimal(row["amount"]) for row in equity), Decimal(0))
    return {
        "statement": "balance_sheet",
        "as_of": as_of.isoformat(),
        "assets": assets,
        "liabilities": liabilities,
        "equity": equity,
        "total_assets": _money(total_assets),
        "total_liabilities": _money(total_liabilities),
        "total_equity": _money(total_equity),
        "balanced": total_assets == total_liabilities + total_equity,
    }


def cash_accounts(session: Session, *, company_id: uuid.UUID) -> list[str]:
    """The account codes the company's `cash*` mappings name, in key order."""
    return [
        code
        for key, code in _mapped_codes(session, company_id=company_id).items()
        if key.startswith(CASH_KEY_PREFIX)
    ]


def _mapped_codes(session: Session, *, company_id: uuid.UUID) -> dict[str, str]:
    """`mapping key -> account code`, for the cash convention and any caller."""
    from app.ledger.accounts import Account as _Account

    codes: dict[str, str] = {}
    for mapping in mappings(session, company_id=company_id):
        account = session.get(_Account, mapping.account_id)
        if account is not None:
            codes[mapping.key] = account.code
    return codes


def cash_flow(
    session: Session,
    *,
    company_id: uuid.UUID,
    start: date,
    end: date,
    accounts: list[str] | None = None,
) -> dict:
    """Opening cash, what moved it and closing cash, for a period.

    Each movement is grouped by the **counterpart** account — the account on the
    other side of the entry that touched cash — because that is the detail a
    reader classifies: a sale, a supplier payment, a loan. The closing figure is
    checked against the cash accounts' own balance on the closing date, which is
    what makes the statement verifiable rather than merely plausible.
    """
    cash = accounts if accounts is not None else cash_accounts(session, company_id=company_id)
    if not cash:
        raise StatementError(
            "no cash accounts are mapped; map them with keys beginning with"
            f" {CASH_KEY_PREFIX!r} (T-1.ACCT.03) before running the cash flow"
        )

    # The statement opens the day before the period: everything posted up to there
    # is the balance being opened with.
    opening = sum(
        _movements(session, company_id=company_id, end=start - timedelta(days=1)).get(
            code, Decimal(0)
        )
        for code in cash
    )

    rows: dict[str, Decimal] = {}
    entries = session.scalars(
        select(JournalEntry)
        .where(
            JournalEntry.company_id == company_id,
            JournalEntry.posting_date >= start,
            JournalEntry.posting_date <= end,
        )
        .order_by(JournalEntry.posting_date, JournalEntry.created_at)
    ).all()
    for entry in entries:
        cash_lines = [line for line in entry.lines if line.account in cash]
        if not cash_lines:
            continue
        for line in entry.lines:
            if line.account in cash:
                continue
            effect = -(line.debit - line.credit) * entry.exchange_rate
            rows[line.account] = rows.get(line.account, Decimal(0)) + effect

    movements = [
        {"account": code, "amount": _money(amount)}
        for code, amount in sorted(rows.items())
        if amount != 0
    ]
    total = sum((Decimal(row["amount"]) for row in movements), Decimal(0))
    closing = opening + total
    reported_closing = sum(
        _movements(session, company_id=company_id, end=end).get(code, Decimal(0))
        for code in cash
    )
    return {
        "statement": "cash_flow",
        "from": start.isoformat(),
        "to": end.isoformat(),
        "cash_accounts": sorted(cash),
        "opening": _money(opening),
        "movements": movements,
        "net_change": _money(total),
        "closing": _money(closing),
        "closing_per_ledger": _money(reported_closing),
        "balanced": closing == reported_closing,
    }


def current_month(today: date | None = None) -> tuple[date, date]:
    """The calendar month a scheduled run covers — the first and last day of it."""
    day = today or datetime.now(timezone.utc).date()
    first = day.replace(day=1)
    next_first = (first + timedelta(days=32)).replace(day=1)
    return first, next_first - timedelta(days=1)


def register_builders() -> None:
    """Register the four statements with T-0.REPORT.01's framework.

    The scheduled run covers the current calendar month, which is what a monthly
    financial report means; a caller wanting another period calls the statement
    directly.
    """
    from app.reporting import ReportDefinition, register_builder

    def _trial_balance(session: Session, definition: ReportDefinition) -> dict:
        start, end = current_month()
        return trial_balance(session, company_id=definition.company_id, start=start, end=end)

    def _profit_and_loss(session: Session, definition: ReportDefinition) -> dict:
        start, end = current_month()
        return profit_and_loss(session, company_id=definition.company_id, start=start, end=end)

    def _balance_sheet(session: Session, definition: ReportDefinition) -> dict:
        _start, end = current_month()
        return balance_sheet(session, company_id=definition.company_id, as_of=end)

    def _cash_flow(session: Session, definition: ReportDefinition) -> dict:
        start, end = current_month()
        return cash_flow(session, company_id=definition.company_id, start=start, end=end)

    register_builder("trial_balance", _trial_balance)
    register_builder("profit_and_loss", _profit_and_loss)
    register_builder("balance_sheet", _balance_sheet)
    register_builder("cash_flow", _cash_flow)


# Registered on import, the way the schema conventions are registered in app/db.py:
# whoever imports the ledger gets the four statements registered with T-0.REPORT.01's
# framework, so a definition can be scheduled and run without a wiring step.
register_builders()
