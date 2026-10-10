"""T-6.ANALYTICS.01 — the dashboard: figures a reader can open, and a permission per figure.

A dashboard is where a number gets believed, so the two things that make a figure worth
believing are built into the tile rather than left to the reader:

* **It reconciles to something independent.** Every tile carries the figure *and* the
  reconciliation it must agree with, taken from the subledger's own reconciliation
  (`app/ar/reconciliation.py`, `app/ap/reconciliation.py`), from the statement's own arithmetic
  (`app/ledger/statements.py`) or from the control account the ledger posts to. The difference
  is reported, never absorbed (the same rule T-2.AR.02/T-2.AP.02 state for the subledgers), so a
  dashboard that disagrees with its ledger says so instead of quietly rounding.
* **It opens in one step.** A tile's figure is computed from rows — the open invoices, the
  accounts that made the profit, the items on hand, the stock ledger's own history — and the
  rows are returned by the same request when the caller asks for that tile's drill-down
  (``?drill_down=<code>``). Nothing is recomputed for the second view, so the figure and the
  records behind it cannot disagree: they are the same run.

**Permission per tile.** Each tile names the capability that guards it, and the dashboard asks
for it before computing anything. A caller without it gets the tile **absent** — not zeroed, not
blanked — and named in ``withheld`` with the refusal's own sentence, because a figure of 0.00 is
a figure. Every refusal goes on the trail (``require`` writes it), so "who asked for what they
may not see" is answerable; hiding a tile is not a permission, it is what the API does with the
answer.

Nothing here caches: every call reads the ledger as it stands and stamps ``generated_at``, so a
dashboard is current by construction rather than by a refresh job (T-6.HARD.01 measured what
reading it costs; T-0.REPORT.01 is the framework for reports that are *delivered* instead of
looked at).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ap.aging import aging as payables_aging
from app.ap.reconciliation import reconcile as payables_reconciliation
from app.ar.aging import aging as receivables_aging
from app.ar.reconciliation import reconcile as receivables_reconciliation
from app.ledger.mapping import MissingMappingError, mapped_account
from app.ledger.statements import profit_and_loss, trial_balance
from app.security import AccessDenied, require
from app.procurement import receipts as _receipts  # noqa: F401 — the AP invoices' FK target
from app.stock.items import Item
from app.stock.valuation import ValuationError, valuation

# Money crosses a boundary as an exact decimal string, the rule DOMAIN-DOCS §2 states for the
# whole platform — a dashboard is not the place to start using floats.
MONEY = Decimal("0.01")

# How many rows a drill-down returns. A dashboard opened on a year-old ledger would otherwise
# answer with every open invoice ever raised; the figure is the whole arithmetic either way, and
# the answer says how many rows there are behind it and whether they were cut.
MAX_BASIS = 200


class WindowError(ValueError):
    """A period that cannot be read: it ends before it starts."""


class UnknownTileError(ValueError):
    """A drill-down for a tile the dashboard does not have."""


def _money(value: Any) -> str:
    return format(Decimal(value).quantize(MONEY), "f")


@dataclass(frozen=True)
class Tile:
    """One figure, what it is made of, and what it has to agree with."""

    code: str
    label: str
    capability: str
    figures: dict[str, Any]
    reconciled_to: dict[str, Any]
    basis_label: str
    # The rows the figures are computed from, kept whole so the drill-down returns *the same*
    # rows the figure was summed from rather than a second query that might have moved on.
    basis: list[dict[str, Any]] = field(default_factory=list)


def _window(as_of: date, start: date | None) -> tuple[date, date]:
    """The period a flow figure covers: the caller's window, or the month `as_of` is in.

    A dashboard opened on a date and asked "how much profit" needs a period; the month is the
    one T-1.ACCT.07's own statement builders use, so the dashboard and a delivered statement
    are talking about the same window unless the caller says otherwise.
    """
    if start is not None:
        if start > as_of:
            raise WindowError(
                f"a window of {start}..{as_of} ends before it starts; there is nothing to"
                " dashboard in it"
            )
        return start, as_of
    return as_of.replace(day=1), as_of


def _account_balance(
    session: Session, *, company_id: uuid.UUID, code: str, end: date
) -> Decimal:
    """One account's balance to `end`, from the trial balance the statement layer reads."""
    statement = trial_balance(session, company_id=company_id, end=end)
    for row in statement["rows"]:
        if row["account"] == code:
            return Decimal(row["balance"])
    return Decimal(0)


def _profit_and_loss(
    session: Session, *, company_id: uuid.UUID, as_of: date, start: date | None
) -> Tile:
    begin, end = _window(as_of, start)
    statement = profit_and_loss(session, company_id=company_id, start=begin, end=end)
    # The same window, read a second way: the trial balance's own per-account totals, by class.
    ledger = trial_balance(session, company_id=company_id, start=begin, end=end)
    income_from_ledger = -sum(
        (Decimal(row["balance"]) for row in ledger["rows"] if row["class"] == "income"),
        Decimal(0),
    )
    expenses_from_ledger = sum(
        (Decimal(row["balance"]) for row in ledger["rows"] if row["class"] == "expense"),
        Decimal(0),
    )
    from_ledger = (income_from_ledger - expenses_from_ledger).quantize(MONEY)
    from_statement = Decimal(statement["net_profit"])
    return Tile(
        code="finance.profit_and_loss",
        label=f"Profit and loss, {begin} to {end}",
        capability="report.read",
        figures={
            "income": _money(statement["total_income"]),
            "expenses": _money(statement["total_expenses"]),
            "net_profit": _money(statement["net_profit"]),
        },
        reconciled_to={
            "against": "trial_balance",
            "figure": _money(from_ledger),
            "difference": _money(from_statement - from_ledger),
            "balanced": from_statement == from_ledger,
        },
        basis_label="the income and expense accounts the profit is made of",
        # Each row says which side it is on, so the drill-down adds up to the figures beside
        # it without the reader having to know the chart of accounts.
        basis=[{**row, "class": "income"} for row in statement["income"]]
        + [{**row, "class": "expense"} for row in statement["expenses"]],
    )


def _subledger_tile(
    *,
    as_of: date,
    code: str,
    label: str,
    against: str,
    report,
    reconciliation: dict,
) -> Tile:
    """One subledger: what the invoices say is owed, against what the control account holds."""
    rows = reconciliation["currencies"]
    per_currency = {
        str(row["currency"]): {
            "outstanding": _money(row["subledger"]),
            "control": _money(row["control"]),
            "difference": _money(row["difference"]),
            "balanced": row["balanced"],
        }
        for row in rows
    }
    return Tile(
        code=code,
        label=label,
        capability="invoice.read",
        figures={
            "as_of": as_of.isoformat(),
            "currencies": per_currency,
            "open_documents": len(report.invoices),
        },
        reconciled_to={
            "against": against,
            # The two subledgers' own reconciliation reports differ in what they state above
            # the currencies (the AP one adds a reason when there is nothing to compare), so
            # the tile carries it when it is there rather than inventing one.
            "measure": reconciliation.get("measure", "position"),
            "difference_total": _money(
                sum((abs(row["difference"]) for row in rows), Decimal(0))
            ),
            "balanced": reconciliation["balanced"],
            "problem": reconciliation.get("reason"),
        },
        basis_label="the open documents behind the balance",
        basis=[
            {
                "document": row["invoice"],
                # A customer's row names its customer, a supplier's its supplier: the tile
                # states one column, because the reader is looking at what is owed.
                "party": row.get("customer") or row.get("supplier"),
                "party_name": row.get("customer_name") or row.get("supplier_name"),
                "due_date": row["due_date"].isoformat(),
                "days_past_due": row["days_past_due"],
                "bucket": row["bucket"],
                "currency": row["currency"],
                "open_amount": _money(row["open_amount"]),
            }
            for row in report.invoices
        ],
    )


def _receivables(
    session: Session, *, company_id: uuid.UUID, as_of: date, start: date | None
) -> Tile:
    return _subledger_tile(
        as_of=as_of,
        code="finance.receivables",
        label=f"Receivables outstanding as at {as_of}",
        against="the receivables control account",
        report=receivables_aging(session, company_id=company_id, as_of=as_of),
        reconciliation=receivables_reconciliation(session, company_id=company_id, as_of=as_of),
    )


def _payables(
    session: Session, *, company_id: uuid.UUID, as_of: date, start: date | None
) -> Tile:
    return _subledger_tile(
        as_of=as_of,
        code="finance.payables",
        label=f"Payables outstanding as at {as_of}",
        against="the payables control account",
        report=payables_aging(session, company_id=company_id, as_of=as_of),
        reconciliation=payables_reconciliation(session, company_id=company_id, as_of=as_of),
    )


def _stock(session: Session, *, company_id: uuid.UUID, as_of: date, start: date | None) -> Tile:
    items = list(
        session.scalars(select(Item).where(Item.company_id == company_id).order_by(Item.sku))
    )
    rows = []
    problems = []
    total = Decimal(0)
    for item in items:
        try:
            worth = valuation(session, company_id=company_id, item=item, as_of=as_of)
        except ValuationError as exc:
            # An item nobody can value is named rather than dropped: a total that quietly
            # leaves it out is a total that cannot be reconciled with anything.
            problems.append({"item": item.sku, "problem": str(exc)})
            continue
        value = Decimal(worth["value"])
        total += value
        if value == 0:
            continue  # nothing on hand is not part of what is on hand
        rows.append(
            {
                "item": worth["item"],
                "method": worth["method"],
                "quantity": worth["quantity"],
                "value": worth["value"],
                "unit_cost": worth["unit_cost"],
            }
        )
    total = total.quantize(MONEY)
    try:
        account = mapped_account(session, company_id=company_id, key="inventory")
        control = _account_balance(
            session, company_id=company_id, code=account.code, end=as_of
        ).quantize(MONEY)
        against = f"the inventory control account {account.code}"
        reconciled = {
            "against": against,
            "figure": _money(control),
            "difference": _money(total - control),
            "balanced": total == control and not problems,
        }
    except MissingMappingError as exc:
        reconciled = {
            "against": "the inventory control account",
            "problem": str(exc),
            "balanced": False,
        }
    return Tile(
        code="operations.stock",
        label=f"Inventory on hand as at {as_of}",
        capability="stock.read",
        figures={
            "value": _money(total),
            "items": len(rows),
            "costing_method": rows[0]["method"] if rows else None,
            "problems": problems,
        },
        reconciled_to=reconciled,
        basis_label="the items on hand, valued by the company's costing method",
        basis=rows,
    )


# The dashboard, in the order a reader reads it: what the period earned, what is owed to us,
# what we owe, and what is on the shelves. Adding a tile is adding a row here.
TILES: tuple[tuple[str, str, str, Callable[..., Tile]], ...] = (
    ("finance.profit_and_loss", "Profit and loss", "report.read", _profit_and_loss),
    ("finance.receivables", "Receivables", "invoice.read", _receivables),
    ("finance.payables", "Payables", "invoice.read", _payables),
    ("operations.stock", "Inventory", "stock.read", _stock),
)


def dashboard(
    session: Session,
    *,
    company_id: uuid.UUID,
    subject: str,
    as_of: date | None = None,
    start: date | None = None,
    drill_down: str | None = None,
) -> dict:
    """The tiles this subject may see, and the records behind one of them.

    A tile the caller may not see is **absent** and named in ``withheld`` with the refusal's own
    sentence — the figure is not zeroed or blanked, because a figure of nothing is still a
    figure. The rows behind a figure are returned only for the tile named in `drill_down`, and
    they are the rows the figure was computed from: the same run, not a second query.
    """
    moment = as_of or datetime.now(timezone.utc).date()
    begin, _end = _window(moment, start)
    known = {code for code, _label, _capability, _builder in TILES}
    if drill_down is not None and drill_down not in known:
        raise UnknownTileError(
            f"no dashboard tile {drill_down!r}; the tiles are {', '.join(sorted(known))}"
        )

    tiles: list[dict] = []
    withheld: list[dict] = []
    for code, label, capability, builder in TILES:
        try:
            require(
                session,
                company_id=company_id,
                subject=subject,
                capability=capability,
                entity="dashboard_tile",
                entity_id=code,
            )
        except AccessDenied as exc:
            # Recorded on the trail by `require` itself; the tile is simply not in the answer.
            session.rollback()
            withheld.append(
                {"code": code, "label": label, "capability": capability, "reason": exc.message}
            )
            continue
        tile = builder(session, company_id=company_id, as_of=moment, start=start)
        answer: dict[str, Any] = {
            "code": tile.code,
            "label": tile.label,
            "capability": tile.capability,
            "figures": tile.figures,
            "reconciled_to": tile.reconciled_to,
            "basis_label": tile.basis_label,
        }
        if drill_down == tile.code:
            answer["basis"] = tile.basis[:MAX_BASIS]
            answer["basis_rows"] = len(tile.basis)
            answer["basis_truncated"] = len(tile.basis) > MAX_BASIS
        tiles.append(answer)

    return {
        "as_of": moment.isoformat(),
        "start": begin.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "drill_down": drill_down,
        "tiles": tiles,
        "withheld": withheld,
    }
