"""T-3.POS.03 — the retail shift: a till's day, opened and closed.

A shift is what makes a cash drawer countable. It is not a master of its own: it
belongs to one **terminal** for one stretch of time, and while it is open it is the
window every sale and every drawer movement at that till falls inside.

* **One shift per terminal at a time.** The uniqueness is a **partial unique index**
  on (company, terminal) where the shift is open, so "a shift cannot be opened twice
  on one terminal" is a fact of the schema rather than of a service's memory — the
  same shape T-3.SALES.03 used to make "one quotation, one order" hold.
* **Trading outside a shift is refused when the company asks for one.** A company
  states `cash_drawer_required` (T-3.POS.03's own variable); unstated means "no
  drawer management", which is the ordinary shop, and stating it makes
  :func:`app.pos.sales.complete_sale` refuse a sale whose terminal has no open shift.
* **Closing reports expected against counted.** The expected figure is **derived**
  (T-3.POS.02's `drawer_state`: the shift's cash sales, the change they gave back and
  the drawer's movements) and the counted figure is what a person found in the
  drawer; the variance is the difference between them. A non-zero variance is not a
  refusal — it is a fact of a till — but it needs a **reason**, because a variance
  nobody explained is the one thing a count cannot resolve.
* **A closed shift takes no more sales.** The sale's window ends at `closed_at`, so
  nothing can be added to a day that has been signed off.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    func,
    select,
    text,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.company import cash_drawer_required_for
from app.db import Base
from app.ledger.posting import JournalEntry
from app.pos.drawer import (
    CASH,
    PAID_IN,
    DrawerMovement,
    drawer_state,
    movements_for,
    tender_breakdown,
)
from app.pos.sales import COMPLETED, VOID, PosSale

MONEY = Numeric(20, 6)
MONEY_SCALE = Decimal("0.000001")

OPEN, CLOSED = "open", "closed"
SHIFT_STATUSES = (OPEN, CLOSED)


class ShiftError(ValueError):
    """The shift refused what was asked of it."""


class ShiftAlreadyOpenError(ShiftError):
    """That terminal is already trading on an open shift."""


class ShiftStateError(ShiftError):
    """The asked-for change does not apply to the shift's state."""


class VarianceReasonRequired(ShiftError):
    """The count differs from what the drawer should hold, and nobody said why."""


class PosShift(Base):
    """One till's stretch of trading: the float it opened with and the count it closed on."""

    __tablename__ = "pos_shift"
    __table_args__ = (
        # One open shift per terminal — partial, so a terminal's history may hold as many
        # closed shifts as it likes.
        Index(
            "uq_pos_shift_open_terminal",
            "company_id",
            "terminal",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
        CheckConstraint("status IN ('open', 'closed')", name="ck_pos_shift_status"),
        CheckConstraint("opening_float >= 0", name="ck_pos_shift_float"),
        # A count is a count: it may be short (negative variance) but a drawer never
        # holds less than nothing.
        CheckConstraint("counted_cash IS NULL OR counted_cash >= 0", name="ck_pos_shift_count"),
        CheckConstraint(
            "status = 'open' OR counted_cash IS NOT NULL",
            name="ck_pos_shift_closed_has_count",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    terminal: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=OPEN)
    # The cash the drawer started with, and the day it covers.
    opening_float: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    opened_on: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    opened_by: Mapped[str] = mapped_column(String(64), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_by: Mapped[str | None] = mapped_column(String(64))
    # What the drawer should have held — derived at the moment of closing and stored
    # beside the count, because a shift's variance is a fact about the moment it was
    # closed and must not be restated by a later correction to a sale.
    expected_cash: Mapped[Decimal | None] = mapped_column(MONEY)
    counted_cash: Mapped[Decimal | None] = mapped_column(MONEY)
    variance: Mapped[Decimal | None] = mapped_column(MONEY)
    # Why the count and the drawer disagree. Required exactly when they do.
    variance_reason: Mapped[str | None] = mapped_column(String(200))

    sales: Mapped[list[PosSale]] = relationship(
        back_populates="shift", order_by="PosSale.number"
    )


def _amount(value: Any) -> Decimal:
    """A stated amount, or a refusal — never an exception the boundary cannot name.

    A float is refused because it is already inexact, and anything that will not parse
    is refused here rather than raised as `decimal.InvalidOperation` further in, which
    is not a `ValueError` and would leave the API answering 500 to a mistyped figure.
    """
    if isinstance(value, float):
        raise ShiftError(f"an amount is an exact decimal, not the float {value!r}")
    if isinstance(value, Decimal):
        amount = value
    else:
        try:
            amount = Decimal(str(value).strip())
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise ShiftError(f"{value!r} is not a shift amount") from exc
    if not amount.is_finite():
        # `numeric` would store it and every sum it touched would be nonsense, so it is
        # refused while it can still be named.
        raise ShiftError(f"{value!r} is not a finite shift amount")
    return amount


def current_shift(
    session: Session, *, company_id: uuid.UUID, terminal: str
) -> PosShift | None:
    """The shift this terminal is trading on, or ``None`` — never more than one."""
    return session.scalar(
        select(PosShift).where(
            PosShift.company_id == company_id,
            PosShift.terminal == str(terminal),
            PosShift.status == OPEN,
        )
    )


def open_shift(
    session: Session,
    *,
    company_id: uuid.UUID,
    terminal: str,
    opening_float: Any = 0,
    actor: str,
    on: date | None = None,
    at: datetime | None = None,
) -> PosShift:
    """Open a shift on a terminal with its opening float.

    Refused when the terminal is already trading: two open shifts on one till would
    make "what did this shift take" unanswerable, and the partial unique index refuses
    it even if this check is bypassed.
    """
    till = str(terminal or "").strip()
    if not till:
        raise ShiftError("a shift names the terminal it is opened on")
    who = str(actor or "").strip()
    if not who:
        raise ShiftError("a shift names who opened it")
    if current_shift(session, company_id=company_id, terminal=till) is not None:
        raise ShiftAlreadyOpenError(
            f"terminal {till!r} is already trading on an open shift; close it first"
        )
    float_ = _amount(opening_float)
    if float_ < 0:
        raise ShiftError(f"an opening float is not negative: {float_}")
    moment = at or datetime.now(timezone.utc)
    shift = PosShift(
        company_id=company_id,
        terminal=till,
        status=OPEN,
        opening_float=float_,
        opened_on=on or moment.date(),
        opened_at=moment,
        opened_by=who,
    )
    session.add(shift)
    session.flush()
    # The drawer's cash moved before this shift opened — a float paid in, a withdrawal —
    # and it is this drawer's. Claiming it here means the day's other shifts cannot
    # count it and this one cannot be left short by money it never saw.
    for movement in session.scalars(
        select(DrawerMovement).where(
            DrawerMovement.company_id == company_id,
            DrawerMovement.terminal == till,
            DrawerMovement.moved_on == shift.opened_on,
            DrawerMovement.shift_id.is_(None),
        )
    ):
        movement.shift_id = shift.id
    session.flush()
    return shift


def shift_sales(session: Session, shift: PosShift) -> list[PosSale]:
    """The sales this shift rang up, oldest first — refunded ones included.

    A sale that a **later** refund reversed is still a sale this shift took: it went
    through the till, the customer paid, and the money is in the drawer the shift
    counted. Refunding it is a document of its own, on its own day (T-3.POS.04), so
    reading these by `status` alone would let a refund made tomorrow restate a shift
    whose report has been signed off. What tells a rung-up sale from an abandoned
    basket is its entry: only a completed sale has one.
    """
    return list(
        session.scalars(
            select(PosSale)
            .where(
                PosSale.company_id == shift.company_id,
                PosSale.shift_id == shift.id,
                PosSale.journal_entry_id.is_not(None),
            )
            .order_by(PosSale.sold_on, PosSale.number)
        )
    )


def refunds_on(
    session: Session,
    *,
    company_id: uuid.UUID,
    on: date,
    terminal: str | None = None,
) -> list[PosSale]:
    """The sales refunded on a day, by the day the **reversal entry** was posted.

    Not by the day the sale was rung up: a refund happens when it happens, and a refund
    of last week's sale belongs to today's takings — the period that has to absorb the
    money going back out. Dated by the entry, because the entry is what the ledger has.
    """
    statement = (
        select(PosSale)
        .join(JournalEntry, JournalEntry.id == PosSale.reversal_entry_id)
        .where(
            PosSale.company_id == company_id,
            PosSale.reversal_entry_id.is_not(None),
            JournalEntry.posting_date == on,
        )
    )
    if terminal is not None:
        statement = statement.where(PosSale.terminal == str(terminal))
    return list(session.scalars(statement.order_by(PosSale.number)))


def refund_cash(sales: list[PosSale]) -> Decimal:
    """The cash these refunds give back out of the drawer.

    What the customer paid in cash and is handed back — the `applied` part of the cash
    tenders, because the change they were given at the time is not theirs to keep twice.
    A refund of a card sale moves no cash at all.
    """
    total = Decimal(0)
    for sale in sales:
        total += sum(
            (record.applied for record in sale.tenders if record.tender_type == CASH),
            Decimal(0),
        )
    return total.quantize(MONEY_SCALE)


def abandoned_baskets(
    session: Session, *, company_id: uuid.UUID, on: date, terminal: str | None = None
) -> list[PosSale]:
    """The baskets voided on a day that never became sales — nothing to reverse."""
    statement = select(PosSale).where(
        PosSale.company_id == company_id,
        PosSale.status == VOID,
        PosSale.journal_entry_id.is_(None),
        PosSale.sold_on == on,
    )
    if terminal is not None:
        statement = statement.where(PosSale.terminal == str(terminal))
    return list(session.scalars(statement.order_by(PosSale.number)))


def shift_movements(session: Session, shift: PosShift) -> list[DrawerMovement]:
    """The drawer movements this shift owns, oldest first.

    Attributed at record time (or claimed when the shift opened): the movements of the
    shift that was trading. Reading them by the shift and not by terminal-and-day is
    what stops two shifts on one till each counting the other's cash — the second
    shift's expected cash would be out by the first's movements, and a correct count
    would be refused for a variance nobody made.
    """
    return list(
        session.scalars(
            select(DrawerMovement)
            .where(
                DrawerMovement.company_id == shift.company_id,
                DrawerMovement.shift_id == shift.id,
            )
            .order_by(DrawerMovement.moved_at, DrawerMovement.id)
        )
    )


def shift_totals(session: Session, shift: PosShift) -> dict:
    """What the shift took and what its drawer should hold — every figure derived.

    The sales, their tax, their tenders and the drawer's movements, added from the
    documents each time. A shift that kept a running balance could disagree with the
    sales it is made of; this cannot.
    """
    sales = shift_sales(session, shift)
    movements = shift_movements(session, shift)
    # A paid-in recorded as the shift's own opening float is that figure, not a movement
    # beside it: counting both would put the float in the drawer twice. Matched on the
    # movement's kind as well as on that reason — a cash withdrawal somebody described as
    # a float is a withdrawal, and dropping it would hide money that left the drawer.
    float_movements = [
        row
        for row in movements
        if not (row.movement_type == PAID_IN and row.reason == OPENING_FLOAT_REASON)
    ]
    # A refund made while this drawer was trading is cash out of the drawer the count
    # will not find — the sale it reverses stays counted above, so without this the
    # expectation would be too high by exactly what was handed back.
    refunded = refunds_on(
        session, company_id=shift.company_id, on=shift.opened_on, terminal=shift.terminal
    )
    state = drawer_state(sales, float_movements)
    expected = (
        state["expected"] - refund_cash(refunded) + Decimal(shift.opening_float)
    ).quantize(MONEY_SCALE)
    return {
        "shift": shift.id,
        "terminal": shift.terminal,
        "status": shift.status,
        "opening_float": Decimal(shift.opening_float).quantize(MONEY_SCALE),
        "sales": len(sales),
        "net": sum((sale.net_amount for sale in sales), Decimal(0)).quantize(MONEY_SCALE),
        "tax": sum((sale.tax_amount for sale in sales), Decimal(0)).quantize(MONEY_SCALE),
        "gross": sum((sale.gross_amount for sale in sales), Decimal(0)).quantize(MONEY_SCALE),
        "tenders": tender_breakdown(sales),
        "movements": state["movements"],
        "change_paid": state["change_paid"],
        "expected_cash": expected,
        "refunded": sum(
            (sale.gross_amount for sale in refunded), Decimal(0)
        ).quantize(MONEY_SCALE),
        "refunds": [sale.number for sale in refunded],
    }


# The reason a float movement carries when it is already the shift's own opening float.
OPENING_FLOAT_REASON = "opening float"


def close_shift(
    session: Session,
    shift: PosShift,
    *,
    counted_cash: Any,
    actor: str,
    reason: str | None = None,
    at: datetime | None = None,
) -> PosShift:
    """Close the shift on a counted drawer, stating the expected figure and the variance.

    The count is what a person found; the expected figure is what the shift's own
    sales and movements say should be there. A non-zero variance is recorded — it is
    what happened — but it must be **explained**: closing a drawer that does not
    balance without saying why is how a shortfall becomes nobody's.
    """
    if shift.status == CLOSED:
        raise ShiftStateError(f"shift on {shift.terminal!r} is already closed")
    who = str(actor or "").strip()
    if not who:
        raise ShiftError("a shift names who closed it")
    counted = _amount(counted_cash)
    if counted < 0:
        raise ShiftError(f"a counted drawer is not negative: {counted}")
    totals = shift_totals(session, shift)
    expected = totals["expected_cash"]
    variance = (counted - expected).quantize(MONEY_SCALE)
    stated = None if reason is None else str(reason).strip()
    if variance != 0 and not stated:
        raise VarianceReasonRequired(
            f"the drawer counted {counted} against an expected {expected}"
            f" (variance {variance}); a variance needs a reason"
        )
    shift.expected_cash = expected
    shift.counted_cash = counted
    shift.variance = variance
    shift.variance_reason = stated
    shift.status = CLOSED
    shift.closed_at = at or datetime.now(timezone.utc)
    shift.closed_by = who
    session.flush()
    return shift


def closed_shifts(
    session: Session, *, company_id: uuid.UUID, on: date | None = None,
    terminal: str | None = None
) -> list[PosShift]:
    """The shifts of a company, oldest first — what a day report aggregates."""
    statement = select(PosShift).where(
        PosShift.company_id == company_id, PosShift.status == CLOSED
    )
    if on is not None:
        statement = statement.where(PosShift.opened_on == on)
    if terminal is not None:
        statement = statement.where(PosShift.terminal == str(terminal))
    return list(session.scalars(statement.order_by(PosShift.opened_on, PosShift.terminal)))


def require_open_shift(session: Session, sale: PosSale) -> PosShift | None:
    """The shift a sale may be completed on, or a refusal naming what is missing.

    Returns ``None`` where the company does not require a drawer. Where it does, a
    terminal with no open shift cannot complete a sale: the money would land in a
    drawer nobody opened and no count would ever see it.
    """
    shift = current_shift(session, company_id=sale.company_id, terminal=sale.terminal)
    if shift is not None:
        return shift
    if cash_drawer_required_for(session, company_id=sale.company_id):
        raise ShiftError(
            f"terminal {sale.terminal!r} has no open shift and this company requires"
            " one, so sale"
            f" {sale.number!r} cannot be completed: open a shift first (T-3.POS.03)"
        )
    return None
