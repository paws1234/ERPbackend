"""T-6.ANALYTICS.02 — the catalogue: what can be scheduled, over how long, and for whom.

§3 asks for "scheduled financial and operational reports". The runner is T-0.REPORT.01 and the
statements are T-1.ACCT.07; this module is the **catalogue** they are read through — one entry
per report the platform can produce, stating:

* **what it covers** (`scope`, in words, and the period granularity a run of it means), so a
  schedule says what it will be a report *of* rather than only when it fires;
* **what a caller must hold** (`capability`) — the module's own capability, not one blanket
  report permission, so "who may schedule the payroll summary" is answered per report;
* **when it runs by default** (`schedule`, a five-field cron expression) and **who receives it**
  (`recipients`, given when it is scheduled — the catalogue states the report, the install
  states the addresses).

Every entry is schedulable through :func:`schedule`, which is the same `register` row change
T-0.REPORT.01 defines: registering a report is configuration, not a deploy. A builder for each
code is registered here, so the catalogue cannot list a report the platform cannot produce —
and a code registered without an entry (a phase's own report) still runs; the catalogue only
*says* what an operator can turn on.

The builders read the **run's window**, never the clock: a report for August produced in
September is August's figures, which is what makes a re-run for a period meaningful (and what
makes it safe to skip: T-0.REPORT.01 refuses to deliver the same period twice).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ap.aging import aging as payables_aging
from app.ap.reconciliation import reconcile as payables_reconciliation
from app.ar.aging import aging as receivables_aging
from app.ar.reconciliation import reconcile as receivables_reconciliation
from app.ledger.statements import register_builders as register_statement_builders
from app.payroll.engine import lines_of, run_for as payroll_run_for
from app.pos.reports import day_report
from app.procurement.orders import PurchaseOrder
from app.procurement.scoring import scorecards
from app.procurement.suppliers import Supplier
from app.reporting import (
    DAY,
    MONTH,
    QUARTER,
    Window,
    register,
    register_builder,
)
from app.manufacturing.work_orders import WorkOrder
from app.sales.customers import Customer

MONEY = Decimal("0.01")


class CatalogueError(ValueError):
    """A report nobody can schedule: no such code."""


def _money(value: Any) -> str:
    return format(Decimal(value).quantize(MONEY), "f")


def _jsonable(value: Any) -> Any:
    """A payload a run can store: a run's payload is a JSONB column.

    The domain functions hand back `date`s and `Decimal`s, which is right for a caller that
    renders them — and which would fail the *whole* run at the database boundary. Converting
    here, once, is what keeps a report's shape the report's own business.
    """
    if isinstance(value, dict):
        return {str(key): _jsonable(one) for key, one in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(one) for one in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


@dataclass(frozen=True)
class Entry:
    """One schedulable report: what it is, what it covers, and who may turn it on."""

    code: str
    name: str
    kind: str  # financial | operational
    scope: str
    capability: str
    schedule: str
    period: str = MONTH


def _financial_statements(session: Session, definition, window: Window) -> dict:
    """Placeholder: the statements register their own builders (T-1.ACCT.07)."""
    raise CatalogueError("statement builders are registered by app/ledger/statements.py")


def _receivables_aging(session: Session, definition, window: Window) -> dict:
    report = receivables_aging(
        session, company_id=definition.company_id, as_of=window.end
    )
    reconciliation = receivables_reconciliation(
        session, company_id=definition.company_id, as_of=window.end
    )
    return {
        "report": "receivables_aging",
        "as_of": window.end.isoformat(),
        "rows": [
            {
                "invoice": row["invoice"],
                "customer": row["customer"],
                "due_date": row["due_date"].isoformat(),
                "bucket": row["bucket"],
                "currency": row["currency"],
                "open_amount": _money(row["open_amount"]),
            }
            for row in report.invoices
        ],
        "total": _money(report.total),
        "buckets": report.bucket_labels,
        "reconciled_to": "the receivables control account",
        "control_difference": _money(
            sum((abs(row["difference"]) for row in reconciliation["currencies"]), Decimal(0))
        ),
    }


def _payables_aging(session: Session, definition, window: Window) -> dict:
    report = payables_aging(session, company_id=definition.company_id, as_of=window.end)
    reconciliation = payables_reconciliation(
        session, company_id=definition.company_id, as_of=window.end
    )
    return {
        "report": "payables_aging",
        "as_of": window.end.isoformat(),
        "rows": [
            {
                "invoice": row["invoice"],
                "supplier": row["supplier"],
                "due_date": row["due_date"].isoformat(),
                "bucket": row["bucket"],
                "currency": row["currency"],
                "open_amount": _money(row["open_amount"]),
            }
            for row in report.invoices
        ],
        "total": _money(report.total),
        "buckets": report.bucket_labels,
        "reconciled_to": "the payables control account",
        "control_difference": _money(
            sum((abs(row["difference"]) for row in reconciliation["currencies"]), Decimal(0))
        ),
    }


def _supplier_scorecards(session: Session, definition, window: Window) -> dict:
    suppliers = list(
        session.scalars(
            select(Supplier)
            .where(Supplier.company_id == definition.company_id)
            .order_by(Supplier.id)
        )
    )
    cards = scorecards(session, suppliers=suppliers, start=window.start, end=window.end)
    rated = [card for card in cards if card.get("rated")]
    return {
        "report": "supplier_scorecards",
        "from": window.start.isoformat(),
        "to": window.end.isoformat(),
        "suppliers": len(cards),
        "rated": len(rated),
        # A supplier nothing was received from is named as unrated rather than scored zero:
        # a zero would read as a judgement nobody made.
        "rows": [
            {
                "supplier": card["supplier"],
                "rated": bool(card.get("rated")),
                "score": card.get("score"),
                "weights": card.get("weights"),
            }
            for card in cards
        ],
    }


def _production_output(session: Session, definition, window: Window) -> dict:
    orders = list(
        session.scalars(
            select(WorkOrder)
            .where(WorkOrder.company_id == definition.company_id)
            .order_by(WorkOrder.number)
        )
    )
    from app.manufacturing.job_cards import output_of

    rows = []
    produced = Decimal(0)
    rejected = Decimal(0)
    for order in orders:
        if not (window.start <= order.created_on <= window.end):
            continue
        output = output_of(session, order)
        produced += output["produced"]
        rejected += output["rejected"]
        rows.append(
            {
                "order": order.number,
                "item": order.item.sku,
                "status": order.status,
                "produced": format(output["produced"], "f"),
                "rejected": format(output["rejected"], "f"),
                "net": format(output["net"], "f"),
            }
        )
    return {
        "report": "production_output",
        "from": window.start.isoformat(),
        "to": window.end.isoformat(),
        "orders": len(rows),
        "rows": rows,
        "produced": format(produced, "f"),
        "rejected": format(rejected, "f"),
        "net": format((produced - rejected), "f"),
    }


def _payroll_summary(session: Session, definition, window: Window) -> dict:
    period = f"{window.period.year:04d}-{window.period.month:02d}"
    run = payroll_run_for(session, company_id=definition.company_id, period=period)
    if run is None:
        # No run for the period is a fact about the period, not an error: the summary says so
        # rather than reporting a payroll nobody ran as zeros.
        return {
            "report": "payroll_summary",
            "period": period,
            "employees": 0,
            "gross": "0.00",
            "net": "0.00",
            "rows": [],
        }
    lines = lines_of(session, run)
    return {
        "report": "payroll_summary",
        "period": period,
        "employees": len(lines),
        "gross": _money(sum((Decimal(line.gross) for line in lines), Decimal(0))),
        "net": _money(sum((Decimal(line.net) for line in lines), Decimal(0))),
        "rows": [
            {
                "employee": line.employee.party.code,
                "gross": _money(line.gross),
                "net": _money(line.net),
            }
            for line in lines
        ],
    }


def _pos_day(session: Session, definition, window: Window) -> dict:
    report = day_report(session, company_id=definition.company_id, on=window.end)
    return {
        "report": "pos_day_report",
        "on": window.end.isoformat(),
        "net": _money(report.get("net", 0)),
        "gross": _money(report.get("gross", 0)),
        "tax": _money(report.get("tax", 0)),
        "sales": report.get("sales", 0),
        "tenders": report.get("tenders", {}),
    }


def _credit_exposure(session: Session, definition, window: Window) -> dict:
    """Customers over their limit, as at the run's period end — the operational credit report."""
    from app.ar.exposure import exposure_against_limit

    customers = list(
        session.scalars(
            select(Customer)
            .where(Customer.company_id == definition.company_id)
            .order_by(Customer.id)
        )
    )
    rows = [
        {
            "customer": customer.party.code,
            **exposure_against_limit(session, customer, as_of=window.end),
        }
        for customer in customers
    ]
    breached = [row for row in rows if row["breached"]]
    return {
        "report": "credit_exposure",
        "as_of": window.end.isoformat(),
        "customers": len(rows),
        "over_limit": len(breached),
        # Every customer is listed with the figure and its limit, so "nobody is over" is
        # visible as a fact rather than inferred from an empty list.
        "rows": rows,
    }


BUILDERS: dict[str, Callable[[Session, Any, Window], dict]] = {
    "receivables_aging": _receivables_aging,
    "payables_aging": _payables_aging,
    "supplier_scorecards": _supplier_scorecards,
    "production_output": _production_output,
    "payroll_summary": _payroll_summary,
    "pos_day_report": _pos_day,
    "credit_exposure": _credit_exposure,
}

# The catalogue, in reading order: the statements first (a month each, `report.read` as
# T-0.REPORT.01 defaults), then the operational reports, each carrying the capability of the
# module whose data it is rather than a single report permission.
CATALOGUE: tuple[Entry, ...] = (
    Entry("trial_balance", "Trial balance", "financial",
          "every account's debit and credit totals for the period",
          "report.read", "0 6 1 * *"),
    Entry("profit_and_loss", "Profit and loss", "financial",
          "income and expenses for the period, and the result", "report.read", "0 6 1 * *"),
    Entry("balance_sheet", "Balance sheet", "financial",
          "assets, liabilities and equity as at the period end", "report.read", "0 6 1 * *"),
    Entry("cash_flow", "Cash flow", "financial",
          "cash movements for the period, by activity", "report.read", "0 6 1 * *"),
    Entry("receivables_aging", "Receivables aging", "operational",
          "open customer invoices aged at the period end, against the control account",
          "invoice.read", "30 6 * * *", DAY),
    Entry("payables_aging", "Payables aging", "operational",
          "open supplier invoices aged at the period end, against the control account",
          "invoice.read", "35 6 * * *", DAY),
    Entry("credit_exposure", "Credit exposure", "operational",
          "each customer's exposure against its limit as at the period end", "order.read",
          "40 6 * * *", DAY),
    Entry("supplier_scorecards", "Supplier scorecards", "operational",
          "every supplier's delivery and quality figures for the period", "rfq.read",
          "0 7 1 * *"),
    Entry("production_output", "Production output", "operational",
          "what each work order produced and rejected in the period", "report.read",
          "30 7 1 * *"),
    Entry("payroll_summary", "Payroll summary", "operational",
          "each employee's gross and net for the period's payroll run", "employee.read",
          "0 8 1 * *"),
    Entry("pos_day_report", "POS day report", "operational",
          "the day's sales, tax and tenders across the tills", "pos.read", "0 23 * * *", DAY),
)


def entries() -> list[dict]:
    """The catalogue as a caller reads it: what can be scheduled, and under what capability."""
    return [
        {
            "code": entry.code,
            "name": entry.name,
            "kind": entry.kind,
            "scope": entry.scope,
            "capability": entry.capability,
            "schedule": entry.schedule,
            "period": entry.period,
            "built": entry.code in BUILDERS or entry.code in _STATEMENT_CODES,
        }
        for entry in CATALOGUE
    ]


# The statements' builders live with the statements (T-1.ACCT.07) and are registered by
# `register_builders()`; the catalogue lists them rather than duplicating them.
_STATEMENT_CODES = frozenset({"trial_balance", "profit_and_loss", "balance_sheet", "cash_flow"})


def entry_for(code: str) -> Entry:
    for entry in CATALOGUE:
        if entry.code == code:
            return entry
    raise CatalogueError(
        f"no catalogue report {code!r}; the catalogue holds"
        f" {', '.join(entry.code for entry in CATALOGUE)}"
    )


def schedule(
    session: Session,
    *,
    company_id: uuid.UUID,
    code: str,
    recipients: list[str],
    schedule_by: str | None = None,
):
    """Turn one catalogue report on for a company: its own scope, capability and period.

    `schedule_by` overrides the catalogue's default cron expression; the report, its period and
    the capability it needs are the catalogue's, so scheduling cannot quietly change what a
    report is.
    """
    entry = entry_for(code)
    return register(
        session,
        company_id=company_id,
        code=entry.code,
        name=entry.name,
        schedule=schedule_by or entry.schedule,
        recipients=list(recipients),
        capability=entry.capability,
        period=entry.period,
    )


def schedule_all(
    session: Session, *, company_id: uuid.UUID, recipients: list[str]
) -> list[Any]:
    """Turn the whole catalogue on for a company — what a new install runs once."""
    return [
        schedule(session, company_id=company_id, code=entry.code, recipients=recipients)
        for entry in CATALOGUE
    ]


def register_builders() -> None:
    """Register the catalogue's own builders, and the statements' with it.

    Each is wrapped in :func:`_jsonable`: a builder that hands back a `date` would fail its own
    run at the JSONB boundary, and a report failing for a shape its caller could have converted
    is nobody's idea of a report.
    """
    register_statement_builders()
    for code, builder in BUILDERS.items():
        register_builder(
            code,
            lambda session, definition, window, builder=builder: _jsonable(
                builder(session, definition, window)
            ),
        )


# Registered on import, the way the schema conventions are registered in app/db.py.
register_builders()
