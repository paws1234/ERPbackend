"""T-2.AP.02 — aging the payables, per supplier and per company.

An aging report is only worth reading if two things are true of it, and this module is
built around exactly those:

* **Every figure traces to an open invoice.** The report does not summarise a balance
  it keeps; it walks T-2.AP.01's open invoices and adds up what each is still owed,
  so each bucket can be opened up into the invoices that made it
  (:data:`Report.invoices`).
* **The buckets are stated, and they are configuration.** The plan leaves aging
  buckets per company with no values (`aging buckets (per company — not stated)`), so
  they are a parameter and the report carries them: a bucket boundary nobody can see
  is a number nobody can argue with. The default below is a five-band one and is
  labelled as this platform's default, not as a fact about the business.

Partial settlement needs no special handling: a bucket holds what is **open**, and
`open_amount` is the invoice total less what has settled it, so a partial payment
reduces the bucket the invoice sits in and nothing else.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.ap.invoices import SupplierInvoice, open_amount, open_invoices, settled_amount
from app.procurement.suppliers import Supplier

# One money scale for the whole platform.
MONEY_SCALE = Decimal("0.000001")

# (label, from days past due, to days past due) — `None` means "and beyond". The plan
# states no buckets, so these are this platform's default and the report always says
# which ones it used.
DEFAULT_BUCKETS: tuple[tuple[str, int, int | None], ...] = (
    ("current", 0, 0),
    ("1-30", 1, 30),
    ("31-60", 31, 60),
    ("61-90", 61, 90),
    ("90+", 91, None),
)


class AgingError(ValueError):
    """The aging report refused the buckets it was given."""


def checked_buckets(buckets: Any = None) -> tuple[tuple[str, int, int | None], ...]:
    """The buckets to report on, refused unless they actually partition the days.

    A set of buckets that overlaps, leaves a gap or has two open ends would silently
    drop or double-count invoices — the one failure an aging report must not have, so
    the set is validated before anything is added up.
    """
    rows = tuple(
        (str(label), int(low), None if high is None else int(high))
        # `None` means "the default"; an empty sequence means "no buckets", which is a
        # mistake to refuse rather than a request for the default.
        for label, low, high in (DEFAULT_BUCKETS if buckets is None else buckets)
    )
    if not rows:
        raise AgingError("an aging report needs at least one bucket")
    for label, low, high in rows:
        if low < 0 or (high is not None and high < low):
            raise AgingError(f"bucket {label!r} spans {low}..{high}, which is not a span")
    if rows[0][1] != 0:
        raise AgingError(
            f"the first bucket starts at {rows[0][1]} days; nothing starts before day 0,"
            " so an invoice not yet due would fall outside every bucket"
        )
    if any(row[2] is None for row in rows[:-1]):
        raise AgingError("only the last bucket may be open-ended, or the ones after it"
                         " are unreachable")
    if rows[-1][2] is not None:
        raise AgingError(
            "the last bucket must be open-ended (`to` of None), or invoices aged past"
            " it would vanish from the report"
        )
    for (previous_label, _, previous_high), (label, low, _) in zip(rows, rows[1:]):
        if low != previous_high + 1:
            raise AgingError(
                f"bucket {label!r} starts at day {low} but {previous_label!r} ends at"
                f" day {previous_high}: the days between fall in no bucket"
            )
    return rows


def _bucket_for(days: int, buckets: tuple[tuple[str, int, int | None], ...]) -> str:
    """Which bucket a day count falls in. A negative count (not yet due) is 'current'."""
    reached = days if days > 0 else 0
    for label, low, high in buckets:
        if low <= reached and (high is None or reached <= high):
            return label
    raise AgingError(f"no bucket covers day {days}; the buckets are validated, so this"
                     " cannot happen")


class Report:
    """One aging run: the buckets used, the invoices aged, and the totals."""

    def __init__(self, *, as_of, buckets, rows):
        self.as_of = as_of
        self.buckets = buckets
        self.invoices = rows
        self.totals: dict[str, Decimal] = {label: Decimal(0) for label, _, _ in buckets}
        for row in rows:
            self.totals[row["bucket"]] += row["open_amount"]
        self.totals = {
            label: value.quantize(MONEY_SCALE) for label, value in self.totals.items()
        }
        self.total = sum(self.totals.values(), Decimal(0)).quantize(MONEY_SCALE)

    @property
    def bucket_labels(self) -> list[str]:
        return [label for label, _, _ in self.buckets]

    def by_supplier(self) -> list[dict]:
        """The same figures per supplier — what a collections list is worked from."""
        grouped: dict[str, dict] = {}
        for row in self.invoices:
            entry = grouped.setdefault(
                row["supplier"],
                {
                    "supplier": row["supplier"],
                    "supplier_name": row["supplier_name"],
                    "buckets": {label: Decimal(0) for label in self.bucket_labels},
                    "total": Decimal(0),
                },
            )
            entry["buckets"][row["bucket"]] += row["open_amount"]
            entry["total"] += row["open_amount"]
        ordered = []
        for entry in grouped.values():
            entry["buckets"] = {
                label: value.quantize(MONEY_SCALE)
                for label, value in entry["buckets"].items()
            }
            entry["total"] = entry["total"].quantize(MONEY_SCALE)
            ordered.append(entry)
        return sorted(ordered, key=lambda entry: entry["supplier"])


def aging(
    session: Session,
    *,
    company_id: uuid.UUID,
    as_of: Any = None,
    buckets: Any = None,
    supplier: Supplier | None = None,
) -> Report:
    """Age every open supplier balance as at `as_of`, per supplier and per company."""
    from datetime import date

    moment = as_of or date.today()
    chosen = checked_buckets(buckets)
    rows = []
    for invoice in open_invoices(session, company_id=company_id, supplier=supplier):
        outstanding = open_amount(session, invoice)
        if outstanding <= 0:
            continue  # settled: it is not an open balance, so it is not aged
        days = (moment - invoice.due_date).days
        rows.append(
            {
                "invoice": invoice.number,
                "supplier": invoice.supplier.party.code,
                "supplier_name": invoice.supplier.party.name,
                "invoice_date": invoice.invoice_date,
                "due_date": invoice.due_date,
                "days_past_due": days,
                "bucket": _bucket_for(days, chosen),
                "currency": invoice.currency,
                "gross_amount": invoice.gross_amount,
                "settled": settled_amount(session, invoice),
                "open_amount": outstanding,
            }
        )
    return Report(as_of=moment, buckets=chosen, rows=rows)


def aging_csv(report: Report) -> str:
    """The report as CSV: one row per aged invoice, then the bucket totals.

    The buckets are spelled out in the header, so an exported aging report still says
    what its columns mean.
    """
    lines = [
        "invoice,supplier,supplier_name,invoice_date,due_date,days_past_due,bucket,"
        "currency,gross_amount,settled,open_amount"
    ]
    for row in report.invoices:
        lines.append(
            f"{row['invoice']},{row['supplier']},{row['supplier_name']},"
            f"{row['invoice_date']},{row['due_date']},{row['days_past_due']},"
            f"{row['bucket']},{row['currency']},{row['gross_amount']},"
            f"{row['settled']},{row['open_amount']}"
        )
    lines.append("")
    for label, low, high in report.buckets:
        span = f"{low}+" if high is None else (f"{low}" if low == high else f"{low}-{high}")
        lines.append(f"{label} ({span} days),,,{report.totals[label]}")
    lines.append(f"total as at {report.as_of},,,{report.total}")
    return "\n".join(lines) + "\n"
