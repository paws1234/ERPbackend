"""T-3.AR.03 — recurring billing: templates, their cadence, and the invoices they raise.

A recurring invoice is an ordinary invoice (T-3.AR.01) that a **schedule** raises:
nothing about it is a second kind of document, which is what the task's third
criterion asks for — *a generated invoice is indistinguishable from a manual one
for aging and dunning purposes*. So this module owns exactly three things and
delegates the invoice itself:

* **The template** — the customer, the currency, the lines and their prices, and
  the cadence to bill them on. It is a document of its own, not a draft invoice
  copied repeatedly: a price change is a change to the template, and the invoices
  already raised are untouched by it.
* **The schedule** — derived from the template, never stored per period: period
  *n* starts one cadence after `starts_on`, so the cadence is stated once and a
  month's invoice cannot drift because a previous run happened late. A template
  that has been **paused** raises nothing, and one that has **ended** stops.
* **The run record** — one row per invoice actually raised, unique on
  (template, period). That uniqueness is the idempotency: the job is meant to be
  re-runnable and a second run over the same period finds the row and does
  nothing. The invoice's own number is derived from the same key
  (``<code>-<period>``), so even a lost run row cannot produce a second invoice —
  the number is already taken and the duplicate rule refuses it.

**Failures are reported, not swallowed.** One template that cannot be billed — a
retired customer, a line the pack refuses — does not stop the others: it comes
back in :attr:`Generation.failures` with the reason, and because no run row was
written for its period, the next run retries it. What a job silently skipped is
money nobody billed.
"""

from __future__ import annotations

import calendar
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.ar.invoices import CustomerInvoice, create_invoice, post_invoice
from app.audit import append_only
from app.db import Base, scope_to_company
from app.sales.customers import Customer

MONEY = Numeric(20, 6)

# The cadences the platform states, in the months each is worth — a week is the odd
# one out and is handled as days. A cadence this module does not know is refused
# rather than guessed at: billing a customer on a rhythm nobody agreed to is worse
# than not billing them.
CYCLE_MONTHS = {"monthly": 1, "quarterly": 3, "annual": 12}
WEEKLY = "weekly"
CYCLES = (WEEKLY, *CYCLE_MONTHS)


class RecurringError(ValueError):
    """The recurring schedule refused what was asked of it."""


class UnknownCycleError(RecurringError):
    """The stated cadence is not one this platform bills on."""


class EmptyTemplateError(RecurringError):
    """A template with no lines would raise an invoice for nothing."""


class RecurringTemplate(Base):
    """A standing agreement to bill this customer on this cadence."""

    __tablename__ = "recurring_template"
    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_recurring_template_company_code"),
        CheckConstraint(
            "cycle IN ('weekly', 'monthly', 'quarterly', 'annual')",
            name="ck_recurring_template_cycle",
        ),
        CheckConstraint(
            "ends_on IS NULL OR ends_on >= starts_on",
            name="ck_recurring_template_window",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customer.id"), nullable=False, index=True
    )
    description: Mapped[str] = mapped_column(String(200), nullable=False)
    cycle: Mapped[str] = mapped_column(String(16), nullable=False)
    # The first period's start. Every later period is measured from this date, so the
    # cadence is stated once and does not depend on when a job last ran.
    starts_on: Mapped[date] = mapped_column(Date, nullable=False)
    # The last period that may be billed: a period starting after it is not raised.
    ends_on: Mapped[date | None] = mapped_column(Date)
    # Paused, not deleted: the agreement stands and the invoices already raised under
    # it are untouched, which is what a customer on hold needs.
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Null means the customer's own currency (T-3.SALES.01), exactly as an invoice's
    # unstated currency does.
    currency: Mapped[str | None] = mapped_column(String(3))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    customer: Mapped[Customer] = relationship()
    lines: Mapped[list[RecurringTemplateLine]] = relationship(
        back_populates="template", order_by="RecurringTemplateLine.line_no"
    )
    runs: Mapped[list[RecurringInvoiceRun]] = relationship(
        back_populates="template", order_by="RecurringInvoiceRun.period_key"
    )


class RecurringTemplateLine(Base):
    """One line the template bills, with the price as it now stands."""

    __tablename__ = "recurring_template_line"
    __table_args__ = (
        UniqueConstraint("template_id", "line_no", name="uq_recurring_template_line_no"),
        CheckConstraint("line_no >= 1", name="ck_recurring_template_line_starts_at_one"),
        CheckConstraint("quantity > 0", name="ck_recurring_template_line_quantity"),
        CheckConstraint("unit_price >= 0", name="ck_recurring_template_line_price"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    template_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recurring_template.id"), nullable=False, index=True
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(String(200), nullable=False)
    item_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("item.id"), index=True)
    quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    uom: Mapped[str | None] = mapped_column(String(16))
    unit_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    # The pack's classification this line is charged under, where it is not the
    # document's default (T-3.AR.01's `tax_rule_code`).
    tax_rule_code: Mapped[str | None] = mapped_column(String(32))

    template: Mapped[RecurringTemplate] = relationship(back_populates="lines")


class RecurringInvoiceRun(Base):
    """One period billed: the record that makes the job re-runnable.

    Append-only, and unique on (template, period): a second run over the same period
    finds this row and does nothing, which is what "idempotent" means for a job
    whose output is a document somebody will be asked to pay.
    """

    __tablename__ = "recurring_invoice_run"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "template_id", "period_key",
            name="uq_recurring_run_period_once",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    template_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recurring_template.id"), nullable=False, index=True
    )
    # The period this invoice is for, as the cadence names it ("2026-11", "2026-W45").
    period_key: Mapped[str] = mapped_column(String(16), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customer_invoice.id"), nullable=False, index=True
    )
    generated_on: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    template: Mapped[RecurringTemplate] = relationship(back_populates="runs")
    invoice: Mapped[CustomerInvoice] = relationship()


# A run is history: it is written once and never edited or removed.
append_only(RecurringInvoiceRun.__table__)


def _amount(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _checked_cycle(cycle: str) -> str:
    stated = str(cycle or "").strip().lower()
    if stated not in CYCLES:
        raise UnknownCycleError(
            f"{cycle!r} is not a cadence this platform bills on ({', '.join(CYCLES)})"
        )
    return stated


def _add_months(day: date, months: int) -> date:
    """`day` moved on by whole months, clamped to the target month's last day.

    A template that starts on the 31st is billed on the 30th in a 30-day month and on
    the 28th in February rather than rolling into the next month, which would double
    up a period.
    """
    index = day.month - 1 + months
    year = day.year + index // 12
    month = index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def period_start(template: RecurringTemplate, index: int) -> date:
    """Where period `index` (0 = the first) begins, from the template's own start."""
    if template.cycle == WEEKLY:
        return template.starts_on + timedelta(days=7 * index)
    return _add_months(template.starts_on, CYCLE_MONTHS[template.cycle] * index)


def period_key(template: RecurringTemplate, start: date) -> str:
    """The period's name, as the cadence writes it — the key a run row is unique on."""
    if template.cycle == WEEKLY:
        year, week, _ = start.isocalendar()
        return f"{year}-W{week:02d}"
    return f"{start.year}-{start.month:02d}"


def create_template(
    session: Session,
    *,
    company_id: uuid.UUID,
    code: str,
    customer: Customer,
    cycle: str,
    starts_on: date,
    lines: Any,
    description: str | None = None,
    currency: str | None = None,
    ends_on: date | None = None,
) -> RecurringTemplate:
    """Record a standing agreement to bill `lines` on `cycle`, from `starts_on`."""
    if customer.company_id != company_id:
        raise RecurringError("that customer belongs to another company")
    wanted = str(code).strip()
    if not wanted:
        raise RecurringError("a template code is required")
    if session.scalar(
        select(RecurringTemplate).where(
            RecurringTemplate.company_id == company_id, RecurringTemplate.code == wanted
        )
    ) is not None:
        raise RecurringError(f"template {wanted!r} already exists in this company")
    entries = list(lines)
    if not entries:
        raise EmptyTemplateError(
            f"template {wanted!r} has no lines, so its invoices would be for nothing"
        )
    if ends_on is not None and ends_on < starts_on:
        raise RecurringError(
            f"template {wanted!r} ends on {ends_on}, before it starts on {starts_on}"
        )
    template = RecurringTemplate(
        company_id=company_id,
        code=wanted,
        customer_id=customer.id,
        description=str(description or wanted).strip(),
        cycle=_checked_cycle(cycle),
        starts_on=starts_on,
        ends_on=ends_on,
        currency=currency,
    )
    session.add(template)
    session.flush()
    for raw in entries:
        quantity = _amount(raw["quantity"])
        price = _amount(raw["unit_price"])
        if quantity <= 0 or price < 0:
            raise RecurringError(
                f"a template line states a positive quantity and a non-negative price;"
                f" got {quantity}, {price}"
            )
        template.lines.append(
            RecurringTemplateLine(
                company_id=company_id,
                template_id=template.id,
                line_no=len(template.lines) + 1,
                description=str(raw.get("description", "")).strip() or "—",
                item_id=raw.get("item_id"),
                quantity=quantity,
                uom=raw.get("uom"),
                unit_price=price,
                tax_rule_code=raw.get("tax_rule_code"),
            )
        )
    session.flush()
    return template


def pause_template(
    session: Session, template: RecurringTemplate, *, paused: bool = True
) -> RecurringTemplate:
    """Pause the schedule, or resume it. The agreement and its past invoices stand."""
    template.paused = bool(paused)
    session.flush()
    return template


def template_by_code(
    session: Session, *, company_id: uuid.UUID, code: str
) -> RecurringTemplate:
    """The template a caller names, or a refusal naming what is missing."""
    found = session.scalar(
        select(RecurringTemplate).where(
            RecurringTemplate.company_id == company_id,
            RecurringTemplate.code == str(code).strip(),
        )
    )
    if found is None:
        raise RecurringError(f"no recurring template {code!r} in this company")
    return found


def runs_for(session: Session, template: RecurringTemplate) -> list[RecurringInvoiceRun]:
    """Every period this template has billed, in period order."""
    return list(
        session.scalars(
            select(RecurringInvoiceRun)
            .where(RecurringInvoiceRun.template_id == template.id)
            .order_by(RecurringInvoiceRun.period_start)
        )
    )


def periods_due(
    session: Session,
    template: RecurringTemplate,
    *,
    as_of: date,
) -> list[tuple[str, date]]:
    """The periods this template has reached by `as_of` and has not billed yet."""
    billed = {
        row.period_key
        for row in session.scalars(
            select(RecurringInvoiceRun).where(
                RecurringInvoiceRun.template_id == template.id
            )
        )
    }
    due: list[tuple[str, date]] = []
    index = 0
    while True:
        start = period_start(template, index)
        if start > as_of:
            break
        if template.ends_on is not None and start > template.ends_on:
            break
        key = period_key(template, start)
        if key not in billed:
            due.append((key, start))
        index += 1
    return due


class Generation:
    """What one run of the schedule did: raised, refused, and left alone."""

    def __init__(self):
        self.generated: list[dict] = []
        self.failures: list[dict] = []
        self.skipped: list[dict] = []

    @property
    def raised(self) -> int:
        return len(self.generated)

    def __repr__(self) -> str:  # pragma: no cover - a convenience for a caller's log
        return (
            f"Generation(raised={self.raised}, failed={len(self.failures)},"
            f" skipped={len(self.skipped)})"
        )


def generate_due(
    session: Session,
    *,
    company_id: uuid.UUID,
    as_of: date,
    templates: list[RecurringTemplate] | None = None,
    number_for: Callable[[RecurringTemplate, str], str] | None = None,
    post: bool = True,
) -> Generation:
    """Raise every invoice the cadence has reached by `as_of`, once each.

    Re-runnable by construction: a period that already has a run row is not billed
    again, and the invoice's own number carries the period, so the duplicate rule
    behind it refuses a second one even if the row were lost. Each period's invoice
    and its run row are committed **together**, so a crash cannot leave an invoice
    nobody recorded.

    A template that cannot be billed is reported in :attr:`Generation.failures` and
    the others carry on — and because no run row was written for it, the next run
    tries again.
    """
    result = Generation()
    if templates is None:
        templates = list(
            session.scalars(
                select(RecurringTemplate)
                .where(RecurringTemplate.company_id == company_id)
                .order_by(RecurringTemplate.code)
            )
        )
    for template in templates:
        if template.paused:
            result.skipped.append(
                {"template": template.code, "reason": "the schedule is paused"}
            )
            continue
        for key, start in periods_due(session, template, as_of=as_of):
            number = (
                number_for(template, key) if number_for else f"{template.code}-{key}"
            )
            try:
                invoice = create_invoice(
                    session,
                    company_id=company_id,
                    number=number,
                    customer=template.customer,
                    invoice_date=start,
                    currency=template.currency,
                    lines=[
                        {
                            "description": line.description,
                            "item_id": line.item_id,
                            "quantity": line.quantity,
                            "uom": line.uom,
                            "unit_price": line.unit_price,
                            "tax_rule_code": line.tax_rule_code,
                        }
                        for line in template.lines
                    ],
                )
                if post:
                    post_invoice(session, invoice, posting_date=start)
                session.add(
                    RecurringInvoiceRun(
                        company_id=company_id,
                        template_id=template.id,
                        period_key=key,
                        period_start=start,
                        invoice_id=invoice.id,
                        generated_on=as_of,
                    )
                )
                session.commit()
            except Exception as exc:  # a boundary: one template must not stop the rest
                session.rollback()
                # The rollback ends the transaction, and the company binding is
                # transaction-scoped (app.db): state it again, or every later statement
                # in this run would be outside the tenant.
                scope_to_company(session, company_id)
                result.failures.append(
                    {"template": template.code, "period": key,
                     "reason": f"{type(exc).__name__}: {exc}"}
                )
            else:
                scope_to_company(session, company_id)
                result.generated.append(
                    {"template": template.code, "period": key, "invoice": number,
                     "invoice_id": str(invoice.id), "period_start": start}
                )
    return result
