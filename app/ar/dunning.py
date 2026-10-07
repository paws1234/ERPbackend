"""T-3.AR.04 — dunning: reminder levels, escalation, and delivery through the boundary.

Dunning is the AR side of "an invoice is overdue": the level a customer has reached is
decided by **days past due**, and each run reminds the invoices that have reached a
level they have not yet been reminded at.

Three decisions shape this module:

* **A level is configuration, and the levels partition the days.** The spans are rows
  (:class:`DunningLevel`), validated the way T-3.AR.02 validates aging buckets — no gap,
  no overlap, one open end — because a set of levels that leaves day 47 in no level
  silently stops reminding exactly the invoice that most needs it.
* **One reminder per run per invoice, at the level it has reached.** An invoice that
  has escalated to the second level gets the second level's reminder and *not* the
  first one again: the level is chosen by the days the invoice is past due, and the
  unique (invoice, level) in the database is what makes "reminded at this level" true
  however often the job runs. Re-running the job for the same period therefore
  duplicates nothing.
* **Delivery goes through T-0.INT.01 and nothing else.** Each reminder is sent with
  :func:`app.integrations.send_outbound`, so its attempts, its failure and its last
  error are rows in the delivery log — a reminder that did not go out is visible
  rather than assumed. The reminder row records which channel, which destination and
  which delivery carried it.

A settled invoice is not dunned: `open_amount` is what a reminder is about, and an
invoice with nothing open is not overdue, whatever its due date says. Interest and
penalty charging is **not** built here — the plan does not name it, and inventing a
charge is not a reminder.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.ar.invoices import CustomerInvoice, open_amount, open_invoices
from app.audit import append_only
from app.db import Base, scope_to_company
from app.integrations import OutboundDelivery, send_outbound
from app.sales.customers import Customer, primary_contact

MONEY = Numeric(20, 6)
MONEY_SCALE = Decimal("0.000001")

# The channels a reminder may go out on — the ones §3's Integrations row names for
# "email / SMS". A level naming anything else is refused rather than silently mapped
# onto one of these.
CHANNELS = ("email", "sms")


class DunningError(ValueError):
    """The dunning schedule refused what was asked of it."""


class DunningLevel(Base):
    """One reminder level: the days past due it covers, and how it is delivered."""

    __tablename__ = "dunning_level"
    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_dunning_level_company_code"),
        CheckConstraint("from_days >= 0", name="ck_dunning_level_from_day"),
        CheckConstraint(
            "to_days IS NULL OR to_days >= from_days", name="ck_dunning_level_span"
        ),
        CheckConstraint("channel IN ('email', 'sms')", name="ck_dunning_level_channel"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    from_days: Mapped[int] = mapped_column(Integer, nullable=False)
    # `None` is "and beyond": the last level is open-ended, or an invoice that keeps
    # ageing would fall out of dunning entirely.
    to_days: Mapped[int | None] = mapped_column(Integer)
    channel: Mapped[str] = mapped_column(String(8), nullable=False)
    # What the message says. A template *name* rather than free text repeated per
    # reminder, so the wording is one thing to change.
    template: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DunningReminder(Base):
    """One reminder that went to (or was attempted for) one invoice at one level.

    Append-only, and unique on (invoice, level, run): that triple is what "this
    invoice has already been reminded at this level for this period" means, and it is
    a fact of the schema rather than of the job's memory. The outcome is known before
    the row is written — a delivery is attempted first — so nothing here is ever
    updated.
    """

    __tablename__ = "dunning_reminder"
    __table_args__ = (
        # One reminder per invoice per level **per run**: running a period twice
        # duplicates nothing, while a later period reminds again at whatever level the
        # invoice has reached by then — which is what a dunning cycle is for.
        UniqueConstraint(
            "company_id", "invoice_id", "level_code", "run_key",
            name="uq_dunning_reminder_once_per_run",
        ),
        CheckConstraint("days_past_due >= 0", name="ck_dunning_reminder_days"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customer_invoice.id"), nullable=False, index=True
    )
    level_code: Mapped[str] = mapped_column(String(32), nullable=False)
    # The run this reminder was generated by — the period it belongs to, so a run can
    # be read back as what it did.
    run_key: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    days_past_due: Mapped[int] = mapped_column(Integer, nullable=False)
    open_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    channel: Mapped[str] = mapped_column(String(8), nullable=False)
    destination: Mapped[str | None] = mapped_column(String(200))
    reminder_on: Mapped[date] = mapped_column(Date, nullable=False)
    # The delivery T-0.INT.01 recorded, where there was one.
    delivery_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("outbound_delivery.id"), index=True
    )
    delivered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Why it did not go out, where it did not: no contact on the customer, no
    # transport registered for the channel, or the boundary's own last error.
    failure: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    invoice: Mapped[CustomerInvoice] = relationship()
    delivery: Mapped[OutboundDelivery | None] = relationship()


# A reminder is history: it is written once and never edited or removed.
append_only(DunningReminder.__table__)


def checked_levels(
    levels: Any,
) -> tuple[tuple[str, int, int | None], ...]:
    """The levels as spans, refused unless they actually partition the days they cover.

    The rule is T-3.AR.02's for aging buckets, with the words dunning uses: a gap, an
    overlap or two open ends would each drop or double an invoice, and a reminder that
    never goes out is worse than one that goes twice.

    Where the schedule **starts** is the company's to choose — one deployment reminds
    from the day an invoice falls due, another from the day after — so the first
    level's `from_days` is honoured rather than forced to zero. What it cannot do is
    start below zero (nothing is overdue before it is due) or leave a gap after it.
    """
    rows = tuple(
        (str(code), int(low), None if high is None else int(high))
        for code, low, high in levels
    )
    if not rows:
        raise DunningError("a dunning schedule needs at least one level")
    for code, low, high in rows:
        if low < 0 or (high is not None and high < low):
            raise DunningError(f"level {code!r} spans {low}..{high}, which is not a span")
    if any(row[2] is None for row in rows[:-1]):
        raise DunningError(
            "only the last level may be open-ended, or the ones after it are unreachable"
        )
    if rows[-1][2] is not None:
        raise DunningError(
            "the last level must be open-ended (`to` of None), or an invoice aged past"
            " it would stop being dunned"
        )
    for (previous, _, previous_high), (code, low, _) in zip(rows, rows[1:]):
        if low != previous_high + 1:
            raise DunningError(
                f"level {code!r} starts at day {low} but {previous!r} ends at day"
                f" {previous_high}: the days between fall in no level"
            )
    return rows


def define_level(
    session: Session,
    *,
    company_id: uuid.UUID,
    code: str,
    name: str,
    from_days: int,
    to_days: int | None,
    channel: str,
    template: str | None = None,
) -> DunningLevel:
    """Add one reminder level. The schedule's shape is checked when the run reads it.

    Levels are defined one at a time (a company adds "first notice", then "final
    notice"), so the partition is validated over the whole set rather than at each
    insert: half a schedule is allowed to exist, an incorrect one is not allowed to be
    used.
    """
    wanted = str(code).strip()
    if not wanted:
        raise DunningError("a dunning level needs a code")
    if str(channel) not in CHANNELS:
        raise DunningError(
            f"level {wanted!r} states channel {channel!r}; a reminder goes out on"
            f" {', '.join(CHANNELS)}"
        )
    if session.scalar(
        select(DunningLevel).where(
            DunningLevel.company_id == company_id, DunningLevel.code == wanted
        )
    ) is not None:
        raise DunningError(f"dunning level {wanted!r} already exists in this company")
    level = DunningLevel(
        company_id=company_id,
        code=wanted,
        name=str(name).strip() or wanted,
        from_days=int(from_days),
        to_days=None if to_days is None else int(to_days),
        channel=str(channel),
        template=None if template is None else str(template),
    )
    session.add(level)
    session.flush()
    return level


def levels(session: Session, *, company_id: uuid.UUID) -> list[DunningLevel]:
    """The schedule as defined, ordered by the days each level covers."""
    return list(
        session.scalars(
            select(DunningLevel)
            .where(DunningLevel.company_id == company_id)
            .order_by(DunningLevel.from_days, DunningLevel.code)
        )
    )


def level_for(
    session: Session, *, company_id: uuid.UUID, days: int
) -> DunningLevel | None:
    """The one level an invoice this many days past due has reached, or none yet.

    ``None`` means the invoice has not reached the schedule's first level — a company
    that starts reminding at 30 days leaves a 10-day-late invoice alone, on purpose and
    visibly, rather than in a level nobody defined.

    The levels are validated as a partition before the lookup, so "exactly one level"
    is a property of the schedule rather than of which row a scan happens to reach
    first — and a schedule that is not a partition is refused here rather than
    reminding some invoices and quietly skipping others.
    """
    schedule = levels(session, company_id=company_id)
    checked_levels([(row.code, row.from_days, row.to_days) for row in schedule])
    reached = days if days > 0 else 0
    for row in schedule:
        if reached >= row.from_days and (row.to_days is None or reached <= row.to_days):
            return row
    # Below the schedule's first level. A company that starts reminding at 30 days
    # leaves a 10-day-late invoice alone — and the schedule is allowed to start there —
    # so falling out of the loop is an answer, not an impossible state.
    return None


class DunningRun:
    """What one run did: the reminders written, and the invoices left alone."""

    def __init__(self, *, run_key: str, as_of: date):
        self.run_key = run_key
        self.as_of = as_of
        self.reminders: list[DunningReminder] = []
        self.settled: list[str] = []
        self.already: list[str] = []
        self.not_due: list[str] = []

    @property
    def delivered(self) -> int:
        return sum(1 for row in self.reminders if row.delivered)

    @property
    def failed(self) -> list[DunningReminder]:
        return [row for row in self.reminders if not row.delivered]

    def __repr__(self) -> str:  # pragma: no cover - a convenience for a caller's log
        return (
            f"DunningRun({self.run_key}: {len(self.reminders)} reminder(s),"
            f" {self.delivered} delivered, {len(self.failed)} failed,"
            f" {len(self.settled)} settled, {len(self.already)} already reminded)"
        )


def _destination(customer: Customer, channel: str) -> str | None:
    """Where a reminder goes: the customer's primary contact, on the level's channel."""
    contact = primary_contact(customer)
    if contact is None:
        return None
    return contact.email if channel == "email" else contact.phone


def run_dunning(
    session: Session,
    *,
    company_id: uuid.UUID,
    as_of: Any = None,
    run_key: str | None = None,
    customer: Customer | None = None,
) -> DunningRun:
    """Remind every overdue invoice at the level its days past due have reached.

    One reminder per invoice per run — the level is chosen by the days, not by how
    many runs have happened — and an invoice already reminded at that level is left
    alone, which is what makes running the job twice for a period duplicate nothing.
    A **settled** invoice is skipped: nothing open, nothing to dun.

    The reminder is written whatever becomes of the delivery, and the delivery itself
    goes through T-0.INT.01, so a send that failed is a row in the delivery log and a
    reminder that could not be sent at all says why on its own row.
    """
    from datetime import date as _date

    moment = as_of or _date.today()
    key = str(run_key or moment.isoformat())
    run = DunningRun(run_key=key, as_of=moment)
    reminded = {
        (row.invoice_id, row.level_code, row.run_key)
        for row in session.scalars(
            select(DunningReminder).where(
                DunningReminder.company_id == company_id,
                DunningReminder.run_key == key,
            )
        )
    }
    for invoice in open_invoices(
        session, company_id=company_id, customer=customer, as_of=moment
    ):
        outstanding = open_amount(session, invoice, as_of=moment)
        if outstanding <= 0:
            run.settled.append(invoice.number)
            continue
        days = (moment - invoice.due_date).days
        if days < 0:
            run.not_due.append(invoice.number)
            continue
        level = level_for(session, company_id=company_id, days=days)
        if level is None:
            run.not_due.append(invoice.number)
            continue
        if (invoice.id, level.code, key) in reminded:
            run.already.append(invoice.number)
            continue
        # The delivery is attempted **before** the reminder is written, because the
        # reminder is append-only: its outcome has to be known when its row is made,
        # not written back onto it afterwards.
        #
        # ponytail: a crash between a successful send and this insert repeats one
        # reminder on the next run of the same period. Ceiling: at-least-once delivery
        # of a message nobody is charged for. Upgrade path: an append-only "intent"
        # row before the send and an append-only outcome row after it.
        destination = _destination(invoice.customer, level.channel)
        delivery_id = None
        delivered = False
        failure: str | None = None
        if destination is None:
            failure = f"no {level.channel} address on the customer's primary contact"
        else:
            try:
                delivery = send_outbound(
                    session,
                    company_id=company_id,
                    channel=level.channel,
                    destination=destination,
                    payload={
                        "invoice": invoice.number,
                        "due_date": invoice.due_date.isoformat(),
                        "days_past_due": days,
                        "open_amount": str(outstanding),
                        "currency": invoice.currency,
                        "level": level.code,
                        "template": level.template,
                    },
                )
            except Exception as exc:  # a boundary: whatever the channel refused with
                failure = f"{type(exc).__name__}: {exc}"
            else:
                delivery_id = delivery.id
                delivered = delivery.status == "sent"
                failure = None if delivered else delivery.last_error
        # `send_outbound` commits, and the company binding is transaction-scoped: state
        # it again so this row lands inside the tenant.
        scope_to_company(session, company_id)
        reminder = DunningReminder(
            company_id=company_id,
            invoice_id=invoice.id,
            level_code=level.code,
            run_key=key,
            days_past_due=days,
            open_amount=outstanding,
            channel=level.channel,
            destination=destination,
            reminder_on=moment,
            delivery_id=delivery_id,
            delivered=delivered,
            failure=failure,
        )
        session.add(reminder)
        session.commit()
        scope_to_company(session, company_id)
        reminded.add((invoice.id, level.code, key))
        run.reminders.append(reminder)
    return run


def reminders_for(
    session: Session, invoice: CustomerInvoice
) -> list[DunningReminder]:
    """Every reminder this invoice has had, oldest first — its dunning history."""
    return list(
        session.scalars(
            select(DunningReminder)
            .where(DunningReminder.invoice_id == invoice.id)
            .order_by(DunningReminder.reminder_on, DunningReminder.level_code)
        )
    )
