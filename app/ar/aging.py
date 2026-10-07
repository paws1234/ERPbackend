"""T-3.AR.02 — aging the receivables, per customer and per company.

An aging report is only worth reading if two things are true of it, and this module
is built around exactly those:

* **Every figure traces to an open invoice.** The report does not summarise a
  balance it keeps; it walks T-3.AR.01's open invoices and adds up what each is
  still owed, so each bucket can be opened up into the invoices that made it
  (:attr:`Report.invoices`).
* **The buckets are stated, and they are configuration.** The plan leaves aging
  buckets per company with no values (`aging buckets (per company — not stated)`),
  so they are a parameter and the report carries them: a bucket boundary nobody can
  see is a number nobody can argue with. The default below is a five-band one and is
  labelled as this platform's default, not as a fact about the business.
* **The total is stated against the control account.** The task's third criterion is
  *the total equals the receivables control account*, which a reader can only check
  if the report says what the control account holds — so the report carries it, per
  currency (:meth:`Report.compare_to_control`), and shows the difference rather than
  absorbing one.

Partial settlement needs no special handling: a bucket holds what is **open**, and
`open_amount` is the invoice total less what has settled it, so a partial receipt
reduces the bucket the invoice sits in and nothing else.

**The headline total adds the rows as they stand, so it means one thing only while
the company invoices in one currency.** :attr:`Report.by_currency` is what to read
when more than one is in use, and the comparison with the control account is per
currency for exactly that reason: two currencies cannot be added without a rate
neither side stored (T-1.ACCT.05). The report says which currencies it holds
rather than presenting a sum that looks like a balance and is not one.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.ar.invoices import CustomerInvoice, open_amount, open_invoices, settled_amount
# The receivables control account is read through T-3.AR.07's own reader rather than
# re-derived here: it is the same figure the reconciliation compares, so the two can
# never drift apart.
from app.ar.reconciliation import control_balance, currencies_in_use
from app.sales.customers import Customer

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
        raise AgingError(
            "only the last bucket may be open-ended, or the ones after it are unreachable"
        )
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
    raise AgingError(
        f"no bucket covers day {days}; the buckets are validated, so this cannot happen"
    )


class Report:
    """One aging run: the buckets used, the invoices aged, and the totals.

    ``totals`` and ``total`` add every aged row, whatever currency it is stated in;
    ``totals_by_currency`` and ``total_by_currency`` split them, and the control
    comparison is per currency. On a single-currency company the two agree.
    """

    def __init__(self, *, as_of, buckets, rows):
        self.as_of = as_of
        self.buckets = buckets
        self.invoices = rows
        self.totals: dict[str, Decimal] = {label: Decimal(0) for label, _, _ in buckets}
        self.totals_by_currency: dict[str, dict[str, Decimal]] = {}
        for row in rows:
            self.totals[row["bucket"]] += row["open_amount"]
            currency_totals = self.totals_by_currency.setdefault(
                row["currency"], {label: Decimal(0) for label, _, _ in buckets}
            )
            currency_totals[row["bucket"]] += row["open_amount"]
        self.totals = {
            label: value.quantize(MONEY_SCALE) for label, value in self.totals.items()
        }
        self.total = sum(self.totals.values(), Decimal(0)).quantize(MONEY_SCALE)
        self.totals_by_currency = {
            currency: {
                label: value.quantize(MONEY_SCALE) for label, value in totals.items()
            }
            for currency, totals in self.totals_by_currency.items()
        }
        self.total_by_currency = {
            currency: sum(totals.values(), Decimal(0)).quantize(MONEY_SCALE)
            for currency, totals in self.totals_by_currency.items()
        }
        # Filled by `compare_to_control`; a report nobody compared keeps them empty.
        self.control: dict[str, Decimal] = {}
        self.difference: dict[str, Decimal] = {}

    def compare_to_control(self, session: Session, *, company_id: uuid.UUID) -> None:
        """State the receivables control account beside the subledger total, per currency.

        The control figure is **read from the ledger**, never kept here, so the two
        sides' only shared input is the postings themselves. The comparison is per
        currency because both sides are in the document's own currency, and mixing
        them would need a rate neither stored — and it sweeps every currency either
        side is stated in, so a currency the account holds with nothing owed in it is
        reported as the difference it is rather than skipped.
        """
        # Every currency either side is stated in, not only the ones the open invoices
        # happen to be in: a posting that reached the control account in a currency
        # nothing is owed in is exactly the difference a reconciliation exists to
        # report, and sweeping only the subledger's currencies would hide it. The
        # subledger's side of such a currency is zero.
        compared = sorted(
            set(self.total_by_currency) | set(currencies_in_use(session, company_id=company_id))
        )
        self.control = {
            currency: control_balance(
                session, company_id=company_id, currency=currency, as_of=self.as_of
            )
            for currency in compared
        }
        self.difference = {
            currency: (self.total_by_currency.get(currency, Decimal(0)) - held).quantize(
                MONEY_SCALE
            )
            for currency, held in self.control.items()
        }

    @property
    def balanced(self) -> bool:
        """Whether the subledger total and the control account agree, per currency."""
        return all(value == 0 for value in self.difference.values())

    @property
    def bucket_labels(self) -> list[str]:
        return [label for label, _, _ in self.buckets]

    def by_customer(self) -> list[dict]:
        """The same figures per customer — what a collections list is worked from."""
        grouped: dict[tuple[str, str], dict] = {}
        for row in self.invoices:
            key = (row["customer"], row["currency"])
            entry = grouped.setdefault(
                key,
                {
                    "customer": row["customer"],
                    "customer_name": row["customer_name"],
                    "currency": row["currency"],
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
        return sorted(ordered, key=lambda entry: (entry["customer"], entry["currency"]))

    def by_currency(self) -> list[dict]:
        """Totals kept separate so unlike currencies are never presented as one balance."""
        return [
            {
                "currency": currency,
                "buckets": dict(totals),
                "total": self.total_by_currency[currency],
            }
            for currency, totals in sorted(self.totals_by_currency.items())
        ]


def aging(
    session: Session,
    *,
    company_id: uuid.UUID,
    as_of: Any = None,
    buckets: Any = None,
    customer: Customer | None = None,
) -> Report:
    """Age every open customer balance as at `as_of`, per customer and per company."""
    moment = as_of or date.today()
    chosen = checked_buckets(buckets)
    rows = []
    for invoice in open_invoices(
        session, company_id=company_id, customer=customer, as_of=moment
    ):
        outstanding = open_amount(session, invoice, as_of=moment)
        if outstanding <= 0:
            continue  # settled: it is not an open balance, so it is not aged
        days = (moment - invoice.due_date).days
        rows.append(
            {
                "invoice": invoice.number,
                "customer": invoice.customer.party.code,
                "customer_name": invoice.customer.party.name,
                "invoice_date": invoice.invoice_date,
                "due_date": invoice.due_date,
                "days_past_due": days,
                "bucket": _bucket_for(days, chosen),
                "currency": invoice.currency,
                "gross_amount": invoice.gross_amount,
                "settled": settled_amount(session, invoice, as_of=moment),
                "open_amount": outstanding,
            }
        )
    report = Report(as_of=moment, buckets=chosen, rows=rows)
    report.compare_to_control(session, company_id=company_id)
    return report


def aging_csv(report: Report) -> str:
    """The report as CSV: one row per aged invoice, then the bucket totals.

    The buckets are spelled out in the header, so an exported aging report still
    says what its columns mean. The rows go through the csv module rather than through
    string joins: a customer name may hold a comma or a newline, and a report whose
    columns shift because somebody's name has a comma in it is not an export anybody
    can parse.
    """
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(
        ["invoice", "customer", "customer_name", "invoice_date", "due_date",
         "days_past_due", "bucket", "currency", "gross_amount", "settled",
         "open_amount"]
    )
    for row in report.invoices:
        writer.writerow(
            [row["invoice"], row["customer"], row["customer_name"], row["invoice_date"],
             row["due_date"], row["days_past_due"], row["bucket"], row["currency"],
             row["gross_amount"], row["settled"], row["open_amount"]]
        )
    writer.writerow([])
    for label, low, high in report.buckets:
        writer.writerow([f"{label} ({_span(low, high)} days)", "", "", report.totals[label]])
    if len(report.totals_by_currency) > 1:
        for currency, totals in sorted(report.totals_by_currency.items()):
            for label, low, high in report.buckets:
                writer.writerow(
                    [f"{currency} {label} ({_span(low, high)} days)", "", "", totals[label]]
                )
            writer.writerow(
                [f"{currency} total as at {report.as_of}", "", "",
                 report.total_by_currency[currency]]
            )
    writer.writerow([f"total as at {report.as_of}", "", "", report.total])
    for currency, held in sorted(report.control.items()):
        prefix = f"{currency} " if len(report.control) > 1 else ""
        writer.writerow([f"{prefix}control account as at {report.as_of}", "", "", held])
        writer.writerow(
            [f"{prefix}subledger less control", "", "", report.difference[currency]]
        )
    return out.getvalue()


def _span(low: int, high: int | None) -> str:
    """One bucket's row label: `30+`, a single day, or a closed range."""
    return f"{low}+" if high is None else (f"{low}" if low == high else f"{low}-{high}")
