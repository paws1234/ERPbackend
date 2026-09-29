"""T-2.MATCH.01 — the three-way match: order ↔ receipt ↔ supplier invoice.

§2.3 asks for 3-way matching and §6 metric 3 measures it. This module is where that
happens, and it is built around three things a finance team actually needs from it:

* **Each comparison is separate and stated.** Quantity, price and tax are compared
  **per line** and reported one by one. A single "does not match" tells a buyer
  nothing; "line 2, ordered 100.00, invoiced 120.00, tolerance 2 %" tells them what to
  do next, so :func:`match_invoice` returns that, and :class:`MatchRun` stores it.
* **The tolerances cannot be waved through.** They are a row per company
  (`three_way_match_tolerance` is not stated in the plan, so it is configuration), and
  they are **capped**: a tolerance loose enough to let a wrong price pass on its own is
  refused when it is set, not discovered in an audit. That is the task's own
  criterion — "a tolerance cannot be set so loose that a wrong-price invoice passes
  silently".
* **The verdict is history.** A run is appended, never rewritten, so "was this invoice
  matched when it was paid" is answerable afterwards, and the match rate is measured
  from the runs rather than from a counter.

The tax comparison uses T-2.PROC.08's one procurement basis, so the three documents
are compared on the basis they were all taxed on.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.ap.invoices import SupplierInvoice, SupplierInvoiceLine
from app.audit import append_only
from app.db import Base
from app.procurement.orders import PurchaseOrderLine
from app.procurement.receipts import GoodsReceiptLine
from app.procurement.tax import tax_on

# One money scale for the whole platform.
MONEY = Numeric(20, 6)
PERCENT = Numeric(9, 4)

# The verdicts a run can have. `matched` is the clean case the rate counts.
MATCHED, PARTIAL, FAILED = "matched", "partial", "failed"

# The three things compared, in the order a reader cares about them.
DIMENSIONS = ("quantity", "price", "tax")

# The target §6 metric 3 states, and the ceiling this platform puts on a tolerance.
# The plan states no tolerance values at all, so the ceiling is a platform decision —
# and it exists because a tolerance is a *rounding* allowance, not a licence to accept
# a different price.
TARGET_PERCENT = Decimal("95")
MAX_TOLERANCE_PERCENT = Decimal("5")


class MatchError(ValueError):
    """The match refused what was asked of it."""


class ToleranceError(MatchError):
    """The tolerances asked for are not usable — too loose, or negative."""


class NotMatchableError(MatchError):
    """The invoice cannot be matched: it is not posted, or names nothing to compare."""


class MatchTolerance(Base):
    """How much a difference may be before it is a mismatch, per company.

    Percentage allowances against the ordered figure, one per dimension. All zero means
    "must agree exactly", which is a real answer and the safest one.
    """

    __tablename__ = "match_tolerance"
    __table_args__ = (
        UniqueConstraint("company_id", name="uq_match_tolerance_company"),
        CheckConstraint("quantity_percent >= 0", name="ck_match_tolerance_quantity"),
        CheckConstraint("price_percent >= 0", name="ck_match_tolerance_price"),
        CheckConstraint("tax_percent >= 0", name="ck_match_tolerance_tax"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    quantity_percent: Mapped[Decimal] = mapped_column(PERCENT, nullable=False, default=Decimal(0))
    price_percent: Mapped[Decimal] = mapped_column(PERCENT, nullable=False, default=Decimal(0))
    tax_percent: Mapped[Decimal] = mapped_column(PERCENT, nullable=False, default=Decimal(0))


class MatchRun(Base):
    """One invoice checked against its order and receipt, with what was found.

    Appended, never rewritten: "what did the match say when this invoice was paid" is a
    question about history, and the match rate is measured from these rows.
    """

    __tablename__ = "match_run"
    __table_args__ = (
        CheckConstraint(
            "status IN ('matched', 'partial', 'failed')", name="ck_match_run_status"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("supplier_invoice.id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity_ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    price_ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    tax_ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # The per-line findings, so a verdict can be explained without re-running it.
    details: Mapped[dict] = mapped_column(JSONB, nullable=False)
    matched_on: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    invoice: Mapped[SupplierInvoice] = relationship()


# A run is history (T-0.AUDIT.01).
append_only(MatchRun.__table__)


def _amount(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _plain(value: Any) -> Any:
    """The same structure with every ``Decimal`` as a string.

    The findings are stored as JSONB, and money crossing a JSON boundary is an exact
    decimal **string** everywhere in this platform (DOMAIN-MODELS.md §2) — the same rule
    the API follows. A Decimal left in the payload would also simply fail to serialise.
    """
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def set_tolerance(
    session: Session,
    *,
    company_id: uuid.UUID,
    quantity_percent: Any = 0,
    price_percent: Any = 0,
    tax_percent: Any = 0,
) -> MatchTolerance:
    """Set this company's tolerances, refusing one loose enough to hide a difference.

    Each value is a percentage allowance on the *ordered* figure. Anything above
    :data:`MAX_TOLERANCE_PERCENT` is refused: a wide enough tolerance turns the match
    into a formality, and a wrong price would pass it without anybody being told —
    which is the failure the criterion names.
    """
    values = {
        "quantity_percent": _amount(quantity_percent),
        "price_percent": _amount(price_percent),
        "tax_percent": _amount(tax_percent),
    }
    for name, value in values.items():
        if value < 0:
            raise ToleranceError(f"{name} is not negative, got {value}")
        if value > MAX_TOLERANCE_PERCENT:
            raise ToleranceError(
                f"{name} of {value}% is above this platform's ceiling of"
                f" {MAX_TOLERANCE_PERCENT}%; a tolerance that wide would let a wrong"
                " invoice pass the match silently"
            )
    row = session.scalar(
        select(MatchTolerance).where(MatchTolerance.company_id == company_id)
    )
    if row is None:
        row = MatchTolerance(company_id=company_id, **values)
        session.add(row)
    else:
        for name, value in values.items():
            setattr(row, name, value)
    session.flush()
    return row


def tolerance(session: Session, *, company_id: uuid.UUID) -> MatchTolerance:
    """This company's tolerances; with none configured, everything must agree exactly."""
    row = session.scalar(
        select(MatchTolerance).where(MatchTolerance.company_id == company_id)
    )
    if row is None:
        return MatchTolerance(
            company_id=company_id,
            quantity_percent=Decimal(0),
            price_percent=Decimal(0),
            tax_percent=Decimal(0),
        )
    return row


def _within(stated: Decimal, expected: Decimal, percent: Decimal) -> tuple[bool, Decimal]:
    """Whether `stated` is inside `percent` of `expected`, and the difference.

    A zero expectation is compared exactly: a percentage of nothing is nothing, so a
    zero-tolerance rule is the only honest one there.
    """
    difference = (stated - expected).quantize(Decimal("0.000001"))
    if expected == 0:
        return difference == 0, difference
    allowed = (abs(expected) * percent / 100).quantize(Decimal("0.000001"))
    return abs(difference) <= allowed, difference


def match_invoice(
    session: Session, invoice: SupplierInvoice, *, on: date | None = None
) -> MatchRun:
    """Compare an invoice line by line with its order and receipt, and record the run.

    Each line is compared on three dimensions and each is reported separately: the
    quantity invoiced against the quantity received, the price invoiced against the
    price ordered, and the tax invoiced against the tax this pack's rule implies for the
    ordered lines. A line with no order or receipt behind it is a finding in itself —
    an invoice nobody ordered cannot be "within tolerance" of anything.
    """
    if invoice.status != "posted":
        raise NotMatchableError(
            f"invoice {invoice.number!r} is {invoice.status}; there is nothing posted to"
            " match against yet"
        )
    limits = tolerance(session, company_id=invoice.company_id)
    day = on or invoice.invoice_date

    findings: list[dict] = []
    quantity_ok = price_ok = tax_ok = True
    expected_tax = Decimal(0)

    for line in invoice.lines:
        finding: dict[str, Any] = {
            "line_no": line.line_no,
            "description": line.description,
            "invoiced_quantity": line.quantity,
            "invoiced_unit_price": line.unit_price,
            "invoiced_tax": line.tax_amount,
            "problems": [],
        }
        order_line = (
            session.get(PurchaseOrderLine, line.order_line_id)
            if line.order_line_id is not None
            else None
        )
        receipt_line = (
            session.get(GoodsReceiptLine, line.receipt_line_id)
            if line.receipt_line_id is not None
            else None
        )
        if order_line is None or receipt_line is None:
            finding["problems"].append(
                {
                    "dimension": "linkage",
                    "detail": "the invoice line names no ordered and received line, so"
                    " there is nothing to compare it with",
                }
            )
            quantity_ok = price_ok = tax_ok = False
            findings.append(finding)
            continue

        finding["ordered_quantity"] = order_line.quantity
        finding["received_quantity"] = receipt_line.quantity
        finding["ordered_unit_price"] = order_line.unit_price

        ok, difference = _within(line.quantity, receipt_line.quantity, limits.quantity_percent)
        finding["quantity_difference"] = difference
        if not ok:
            quantity_ok = False
            finding["problems"].append(
                {
                    "dimension": "quantity",
                    "expected": receipt_line.quantity,
                    "stated": line.quantity,
                    "difference": difference,
                    "tolerance_percent": limits.quantity_percent,
                }
            )

        ok, difference = _within(line.unit_price, order_line.unit_price, limits.price_percent)
        finding["price_difference"] = difference
        if not ok:
            price_ok = False
            finding["problems"].append(
                {
                    "dimension": "price",
                    "expected": order_line.unit_price,
                    "stated": line.unit_price,
                    "difference": difference,
                    "tolerance_percent": limits.price_percent,
                }
            )

        # The tax the pack's rule implies for what was ordered, so the comparison is on
        # the same basis the documents were taxed on (T-2.PROC.08).
        implied = tax_on(order_line.unit_price * line.quantity)["tax"]
        expected_tax += implied
        finding["expected_tax"] = implied
        ok, difference = _within(line.tax_amount, implied, limits.tax_percent)
        finding["tax_difference"] = difference
        if not ok:
            tax_ok = False
            finding["problems"].append(
                {
                    "dimension": "tax",
                    "expected": implied,
                    "stated": line.tax_amount,
                    "difference": difference,
                    "tolerance_percent": limits.tax_percent,
                }
            )
        findings.append(finding)

    if quantity_ok and price_ok and tax_ok:
        status = MATCHED
    elif quantity_ok or price_ok or tax_ok:
        # Some comparisons held: the invoice is partly right, and which part is on the
        # findings rather than in the verdict.
        status = PARTIAL
    else:
        status = FAILED

    run = MatchRun(
        company_id=invoice.company_id,
        invoice_id=invoice.id,
        status=status,
        quantity_ok=quantity_ok,
        price_ok=price_ok,
        tax_ok=tax_ok,
        details=_plain(
            {
                "invoice": invoice.number,
                "supplier_reference": invoice.supplier_reference,
                "expected_tax": expected_tax,
                "invoiced_tax": invoice.tax_amount,
                "tolerances": {
                    "quantity_percent": limits.quantity_percent,
                    "price_percent": limits.price_percent,
                    "tax_percent": limits.tax_percent,
                },
                "lines": findings,
            }
        ),
        matched_on=day,
    )
    session.add(run)
    session.flush()
    return run


def latest_match(session: Session, invoice: SupplierInvoice) -> MatchRun | None:
    """The most recent run for one invoice — what a review screen shows."""
    return session.scalar(
        select(MatchRun)
        .where(MatchRun.invoice_id == invoice.id)
        .order_by(MatchRun.created_at.desc(), MatchRun.id)
        .limit(1)
    )


def runs(
    session: Session,
    *,
    company_id: uuid.UUID,
    start: date | None = None,
    end: date | None = None,
) -> list[MatchRun]:
    """Every run in a period, oldest first, counting only each invoice's latest.

    An invoice matched twice (after a correction) is one invoice, so the rate is not
    moved by how often somebody re-ran the check.
    """
    statement = select(MatchRun).where(MatchRun.company_id == company_id)
    if start is not None:
        statement = statement.where(MatchRun.matched_on >= start)
    if end is not None:
        statement = statement.where(MatchRun.matched_on <= end)
    newest: dict[uuid.UUID, MatchRun] = {}
    for run in session.scalars(statement.order_by(MatchRun.created_at)):
        newest[run.invoice_id] = run
    return list(newest.values())


def match_rate(
    session: Session,
    *,
    company_id: uuid.UUID,
    start: date | None = None,
    end: date | None = None,
    target: Any = TARGET_PERCENT,
) -> dict:
    """§6 metric 3: the share of invoices that matched cleanly, and whether it is met.

    Measured over the latest run of each invoice in the period, against
    `three_way_match_target`. The figure is returned as an exact decimal string, and
    `met` says whether it reaches the target — the metric is reported, not asserted.
    """
    considered = runs(session, company_id=company_id, start=start, end=end)
    clean = [run for run in considered if run.status == MATCHED]
    partial = [run for run in considered if run.status == PARTIAL]
    failed = [run for run in considered if run.status == FAILED]
    wanted = _amount(target)
    if not considered:
        return {
            "rate_percent": None,
            "target_percent": wanted,
            "met": False,
            "matched": 0,
            "partial": 0,
            "failed": 0,
            "considered": 0,
            "reason": "no invoice was matched in this period",
        }
    rate = (
        Decimal(len(clean)) * 100 / Decimal(len(considered))
    ).quantize(Decimal("0.0001"))
    return {
        "rate_percent": rate,
        "target_percent": wanted,
        "met": rate >= wanted,
        "matched": len(clean),
        "partial": len(partial),
        "failed": len(failed),
        "considered": len(considered),
        "reason": None,
    }


def explain(run: MatchRun) -> str:
    """One line a person can read: the verdict and the first thing that went wrong."""
    if run.status == MATCHED:
        return f"{run.details['invoice']} matched on all three comparisons"
    first = next(
        (
            (finding, problem)
            for finding in run.details.get("lines", [])
            for problem in finding.get("problems", [])
        ),
        None,
    )
    if first is None:  # pragma: no cover — a non-matched run always has a finding
        return f"{run.details['invoice']} is {run.status}"
    finding, problem = first
    if problem["dimension"] == "linkage":
        return (
            f"{run.details['invoice']} is {run.status}: line {finding['line_no']} —"
            f" {problem['detail']}"
        )
    return (
        f"{run.details['invoice']} is {run.status}: line {finding['line_no']}"
        f" {problem['dimension']} expected {problem.get('expected')} but was"
        f" {problem.get('stated')} (tolerance {problem.get('tolerance_percent')}%)"
    )


# --- T-2.MATCH.02 — holding what did not match, and releasing it --------------
# A mismatch is not a decision, it is a question. This half owns the answer: a held
# invoice cannot be paid, and it leaves the hold only through a stated, authorised
# override that is recorded — never by re-running the match until it agrees, which is
# why releasing writes no new `MatchRun`: the verdict stays exactly as it was, and the
# exception stays visible for reporting.

HELD, RELEASED = "held", "released"

# The capability a release needs. Named here, granted through T-0.SEC.01 — a role has
# it or it does not, and no code decides who is senior enough.
OVERRIDE_CAPABILITY = "match.override"


class MatchHoldError(MatchError):
    """The hold or release refused what was asked of it."""


class NotHeldError(MatchHoldError):
    """A release arrived for an invoice that is not held."""


class AlreadyHeldError(MatchHoldError):
    """The invoice is already held; resolve that hold instead of opening another."""


class InvoiceHeldError(MatchHoldError):
    """Something tried to pay an invoice that is held — refused, by name."""


class MatchHold(Base):
    """One invoice held because its match did not pass, and how it was resolved."""

    __tablename__ = "match_hold"
    __table_args__ = (
        CheckConstraint("state IN ('held', 'released')", name="ck_match_hold_state"),
        # One open hold per invoice: two would be two answers to "why is this held".
        UniqueConstraint("invoice_id", "state", name="uq_match_hold_open"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("supplier_invoice.id"), nullable=False, index=True
    )
    # The run that put it on hold — the exception being resolved, kept so the override
    # can be read against the verdict it overrode.
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("match_run.id"), nullable=False, index=True
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False, default=HELD)
    opened_on: Mapped[date] = mapped_column(Date, nullable=False)
    opened_reason: Mapped[str] = mapped_column(Text, nullable=False)
    resolved_on: Mapped[date | None] = mapped_column(Date)
    resolved_by: Mapped[str | None] = mapped_column(String(64))
    resolve_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    invoice: Mapped[SupplierInvoice] = relationship()
    run: Mapped[MatchRun] = relationship()


def hold_invoice(
    session: Session,
    invoice: SupplierInvoice,
    *,
    reason: str,
    run: MatchRun | None = None,
    on: date | None = None,
) -> MatchHold:
    """Hold an invoice whose latest match did not pass.

    Refused for an invoice that matched cleanly: a hold is a statement that something is
    wrong, and holding a correct invoice wastes somebody's afternoon. The reason is
    required — an unexplained hold is a document nobody can resolve.
    """
    stated = str(reason or "").strip()
    if not stated:
        raise MatchHoldError("a hold states why; an unexplained exception cannot be resolved")
    run = run or latest_match(session, invoice)
    if run is None:
        raise MatchHoldError(
            f"invoice {invoice.number!r} has not been matched; hold it after the match"
            " has run, so the exception has a verdict behind it"
        )
    if run.status == MATCHED:
        raise MatchHoldError(
            f"invoice {invoice.number!r} matched on all three comparisons; there is"
            " nothing to hold"
        )
    if current_hold(session, invoice) is not None:
        raise AlreadyHeldError(f"invoice {invoice.number!r} is already held")
    hold = MatchHold(
        company_id=invoice.company_id,
        invoice_id=invoice.id,
        run_id=run.id,
        state=HELD,
        opened_on=on or run.matched_on,
        opened_reason=stated,
    )
    session.add(hold)
    session.flush()
    return hold


def hold_failed_matches(
    session: Session, *, company_id: uuid.UUID, reason: str, on: date | None = None
) -> list[MatchHold]:
    """Hold every invoice whose latest match failed or only partly passed.

    What a nightly run does: the invoices that need somebody's attention are held in one
    go, each with the same stated reason. An invoice already held (or already released)
    **for this verdict** is left alone — re-opening a resolved exception every night
    would make the hold list useless — while a *new* failed verdict after a release is a
    new exception and is held.
    """
    made = []
    for run in runs(session, company_id=company_id):
        if run.status == MATCHED:
            continue
        invoice = session.get(SupplierInvoice, run.invoice_id)
        if invoice is None:
            continue
        already = session.scalar(
            select(MatchHold).where(
                MatchHold.invoice_id == invoice.id, MatchHold.run_id == run.id
            )
        )
        if already is not None:
            continue
        made.append(hold_invoice(session, invoice, reason=reason, run=run, on=on))
    return made


def current_hold(session: Session, invoice: SupplierInvoice) -> MatchHold | None:
    """The open hold on an invoice, or ``None`` — at most one can exist."""
    return session.scalar(
        select(MatchHold).where(
            MatchHold.invoice_id == invoice.id, MatchHold.state == HELD
        )
    )


def is_held(session: Session, invoice: SupplierInvoice) -> bool:
    """Whether the invoice is held right now — what a payment run asks."""
    return current_hold(session, invoice) is not None


def require_not_held(session: Session, invoice: SupplierInvoice) -> SupplierInvoice:
    """Refuse to go on with an invoice that is held.

    The gate T-2.AP.04's payment selection calls, so "a failed invoice cannot enter a
    payment batch while held" is enforced where the batch is built rather than trusted
    to the screen that built it.
    """
    hold = current_hold(session, invoice)
    if hold is not None:
        raise InvoiceHeldError(
            f"invoice {invoice.number!r} is held ({hold.opened_reason}); release it"
            " through an authorised override before paying it"
        )
    return invoice


def release(
    session: Session,
    hold: MatchHold,
    *,
    actor: str,
    reason: str,
    on: date | None = None,
) -> MatchHold:
    """Release a hold through a stated, authorised override.

    T-0.SEC.01 decides whether `actor`'s roles hold :data:`OVERRIDE_CAPABILITY`, so who
    may override is configuration rather than a judgement made in code — a refusal is
    raised *and* recorded on the trail by that module. The reason is required and
    recorded: this is the record of "why did we pay an invoice the match rejected", and
    the underlying verdict is deliberately left untouched.
    """
    from app.security import require

    if hold.state != HELD:
        raise NotHeldError(f"that hold on {hold.invoice.number!r} is already {hold.state}")
    stated = str(reason or "").strip()
    if not stated:
        raise MatchHoldError("a release states why the exception is resolved")
    require(
        session,
        company_id=hold.company_id,
        subject=actor,
        capability=OVERRIDE_CAPABILITY,
        entity="supplier_invoice",
        entity_id=hold.invoice_id,
    )
    hold.state = RELEASED
    hold.resolved_on = on or date.today()
    hold.resolved_by = str(actor)
    hold.resolve_reason = stated
    session.flush()
    return hold


def holds(
    session: Session,
    *,
    company_id: uuid.UUID,
    state: str | None = None,
    start: date | None = None,
    end: date | None = None,
) -> list[MatchHold]:
    """Holds in a period, oldest first, optionally only the open (or only the released) ones."""
    statement = select(MatchHold).where(MatchHold.company_id == company_id)
    if state is not None:
        statement = statement.where(MatchHold.state == str(state))
    if start is not None:
        statement = statement.where(MatchHold.opened_on >= start)
    if end is not None:
        statement = statement.where(MatchHold.opened_on <= end)
    return list(session.scalars(statement.order_by(MatchHold.opened_on, MatchHold.created_at)))


def override_count(
    session: Session,
    *,
    company_id: uuid.UUID,
    start: date | None = None,
    end: date | None = None,
) -> int:
    """How many holds were released in a period — the overrides, counted apart.

    Kept separate from the match rate on purpose: an invoice that only got paid after
    somebody overrode the match is not a clean match, and a rate that counted it as one
    would hide exactly the thing the metric exists to show.
    """
    return len(
        holds(session, company_id=company_id, state=RELEASED, start=start, end=end)
    )
