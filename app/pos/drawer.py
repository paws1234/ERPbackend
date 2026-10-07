"""T-3.POS.02 — the drawer: what goes in and out of the till outside a sale.

A sale's tendering is T-3.POS.01's (`PosTender`, `tender`, `sale_change`) — this
module is the other half of a drawer's day, the movements that are **not** a sale:

* **A movement is a document, and it says why.** Taking a note out to pay the window
  cleaner is a `paid_out`; putting the float in is a `paid_in`. Both are rows with an
  amount, a reason and the terminal, and a movement without a reason is refused: cash
  that left the drawer with no explanation is the one thing a cash count cannot
  account for.
* **Movements belong where a shift can count them.** Each names its terminal and its
  date, so T-3.POS.03's shift totals (and T-3.POS.04's Z-Report) can add them to what
  the sales took without keeping a second figure of their own.
* **They move no ledger**, deliberately: the money was already the company's, or still
  is. Paying a courier in cash is an expense whose document is the courier's invoice,
  not this row; what this records is that the *drawer* holds less than the sales say.
  The Z-Report's variance is where that has to be explained.

The change rule lives here too, because it is a tendering rule: a sale's change is
what the customer handed over less what the sale took, and it can never be negative —
a non-cash tender cannot produce change at all (a card is read for the amount it is
charged), and a cash tender is capped by `applied <= tendered` in the schema.
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
    Numeric,
    String,
    Uuid,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.db import Base
from app.pos.sales import CASH, PosSale, sale_change, sale_tendered

MONEY = Numeric(20, 6)
MONEY_SCALE = Decimal("0.000001")

# The two ways cash moves outside a sale.
PAID_IN, PAID_OUT = "paid_in", "paid_out"
MOVEMENT_TYPES = (PAID_IN, PAID_OUT)

# What a movement's sign does to the drawer.
SIGNS = {PAID_IN: Decimal(1), PAID_OUT: Decimal(-1)}


class DrawerError(ValueError):
    """The drawer refused what was asked of it."""


class DrawerMovement(Base):
    """One note in or out of the drawer, with the reason somebody gave for it."""

    __tablename__ = "pos_drawer_movement"
    __table_args__ = (
        CheckConstraint(
            "movement_type IN ('paid_in', 'paid_out')", name="ck_pos_drawer_type"
        ),
        CheckConstraint("amount > 0", name="ck_pos_drawer_amount"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    terminal: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    movement_type: Mapped[str] = mapped_column(String(16), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    # Why the cash moved. Required, because a drawer that cannot explain a gap cannot be
    # reconciled by anybody.
    reason: Mapped[str] = mapped_column(String(200), nullable=False)
    # The sale it belongs to, where it is a sale's own float movement rather than a
    # stand-alone one. Null for the stand-alone case.
    sale_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("pos_sale.id"), index=True
    )
    # The shift that was trading on this terminal when the cash moved, so two shifts on
    # one till in one day do not each count the other's movements (T-3.POS.03). Null is
    # "no shift was open": opening a shift on that terminal that day takes charge of
    # them, and any left unattributed are the drawer's own, reported by the day report.
    shift_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("pos_shift.id"), index=True
    )
    moved_on: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    moved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    actor: Mapped[str] = mapped_column(String(64), nullable=False)

    sale: Mapped[PosSale | None] = relationship()


def _amount(value: Any) -> Decimal:
    """A stated amount, or a refusal — never an exception the boundary cannot name.

    A float is refused because it is already inexact, and anything that will not parse
    is refused here rather than raised as `decimal.InvalidOperation` further in, which
    is not a `ValueError` and would leave the API answering 500 to a mistyped figure.
    """
    if isinstance(value, float):
        raise DrawerError(f"an amount is an exact decimal, not the float {value!r}")
    if isinstance(value, Decimal):
        amount = value
    else:
        try:
            amount = Decimal(str(value).strip())
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise DrawerError(f"{value!r} is not a drawer amount") from exc
    if not amount.is_finite():
        # `numeric` would store it and every sum it touched would be nonsense, so it is
        # refused while it can still be named.
        raise DrawerError(f"{value!r} is not a finite drawer amount")
    return amount


def record_movement(
    session: Session,
    *,
    company_id: uuid.UUID,
    terminal: str,
    movement_type: str,
    amount: Any,
    reason: str,
    actor: str,
    moved_on: date | None = None,
    sale: PosSale | None = None,
) -> DrawerMovement:
    """Record cash in or out of the drawer, with the reason and who did it."""
    kind = str(movement_type).strip().lower()
    if kind not in MOVEMENT_TYPES:
        raise DrawerError(
            f"{movement_type!r} is not a drawer movement ({', '.join(MOVEMENT_TYPES)})"
        )
    stated = str(reason or "").strip()
    if not stated:
        raise DrawerError(
            f"a {kind} of {amount} needs a reason; cash that left the drawer without one"
            " cannot be accounted for by anybody"
        )
    who = str(actor or "").strip()
    if not who:
        raise DrawerError("a drawer movement names who made it")
    till = str(terminal or "").strip()
    if not till:
        raise DrawerError("a drawer movement names the terminal it happened at")
    value = _amount(amount)
    if value <= 0:
        raise DrawerError(f"a drawer movement is above zero, got {value}")
    if sale is not None and sale.company_id != company_id:
        raise DrawerError("that sale belongs to another company")
    # Imported here rather than at the top: the shifts read the drawer, so a module-level
    # import would be a cycle. The shift is read from the same table the boundary would.
    from app.pos.shifts import current_shift

    trading = current_shift(session, company_id=company_id, terminal=till)
    movement = DrawerMovement(
        company_id=company_id,
        terminal=till,
        movement_type=kind,
        amount=value,
        reason=stated,
        sale_id=None if sale is None else sale.id,
        shift_id=None if trading is None else trading.id,
        moved_on=moved_on or date.today(),
        actor=who,
    )
    session.add(movement)
    session.flush()
    return movement


def paid_in(session: Session, *, company_id: uuid.UUID, terminal: str, amount: Any,
            reason: str, actor: str, on: date | None = None) -> DrawerMovement:
    """Cash put into the drawer — the opening float, or a top-up."""
    return record_movement(
        session, company_id=company_id, terminal=terminal, movement_type=PAID_IN,
        amount=amount, reason=reason, actor=actor, moved_on=on,
    )


def paid_out(session: Session, *, company_id: uuid.UUID, terminal: str, amount: Any,
             reason: str, actor: str, on: date | None = None) -> DrawerMovement:
    """Cash taken out of the drawer for something that is not a customer's refund."""
    return record_movement(
        session, company_id=company_id, terminal=terminal, movement_type=PAID_OUT,
        amount=amount, reason=reason, actor=actor, moved_on=on,
    )


def movements_for(
    session: Session,
    *,
    company_id: uuid.UUID,
    terminal: str | None = None,
    on: date | None = None,
    start: date | None = None,
    end: date | None = None,
) -> list[DrawerMovement]:
    """The drawer's movements in the window asked for, one terminal or all of them.

    An unnamed terminal means every till of the company — what T-3.POS.04's day report
    reads when it wants the movements the shifts' own terminals do not account for.
    """
    statement = select(DrawerMovement).where(DrawerMovement.company_id == company_id)
    if terminal is not None:
        statement = statement.where(DrawerMovement.terminal == str(terminal))
    if on is not None:
        statement = statement.where(DrawerMovement.moved_on == on)
    if start is not None:
        statement = statement.where(DrawerMovement.moved_on >= start)
    if end is not None:
        statement = statement.where(DrawerMovement.moved_on <= end)
    return list(
        session.scalars(
            statement.order_by(DrawerMovement.moved_on, DrawerMovement.moved_at)
        )
    )


def movement_total(movements: list[DrawerMovement]) -> Decimal:
    """What the movements did to the drawer: paid in less paid out."""
    return sum(
        (SIGNS[movement.movement_type] * movement.amount for movement in movements),
        Decimal(0),
    ).quantize(MONEY_SCALE)


def cash_sales_total(sales: list[PosSale]) -> Decimal:
    """What the cash tenders of these sales put in the drawer — the notes, not the net.

    A customer who hands over 200.00 for a 181.44 sale puts 200.00 in the drawer and
    takes 18.56 back out, so the sum of the `tendered` amounts is what a count finds,
    which is why the tender stores it apart from what the sale took.
    """
    total = Decimal(0)
    for sale in sales:
        total += sum(
            (record.tendered for record in sale.tenders if record.tender_type == CASH),
            Decimal(0),
        )
    return total.quantize(MONEY_SCALE)


def change_paid(sales: list[PosSale]) -> Decimal:
    """The change these sales gave back out of the drawer, in total."""
    return sum((sale_change(sale) for sale in sales), Decimal(0)).quantize(MONEY_SCALE)


def tender_breakdown(sales: list[PosSale]) -> dict[str, dict[str, Decimal]]:
    """What each tender type took and how much each covered — per path, not lumped.

    The three figures a Z-Report needs per tender: what was handed over, what the
    sales took, and how many sales it appeared on.
    """
    breakdown: dict[str, dict[str, Decimal]] = {}
    for sale in sales:
        for record in sale.tenders:
            entry = breakdown.setdefault(
                record.tender_type, {"tendered": Decimal(0), "applied": Decimal(0)}
            )
            entry["tendered"] += record.tendered
            entry["applied"] += record.applied
    return {
        kind: {
            "tendered": totals["tendered"].quantize(MONEY_SCALE),
            "applied": totals["applied"].quantize(MONEY_SCALE),
        }
        for kind, totals in sorted(breakdown.items())
    }


def drawer_state(sales: list[PosSale], movements: list[DrawerMovement]) -> dict:
    """What the drawer should hold: cash in from sales and movements, less change out.

    The one figure a closing count is compared against — derived from the documents
    every time, so a shift cannot carry a balance that drifts from its own sales.
    """
    cash_in = cash_sales_total(sales)
    change = change_paid(sales)
    moved = movement_total(movements)
    expected = (cash_in - change + moved).quantize(MONEY_SCALE)
    return {
        "cash_tendered": cash_in,
        "change_paid": change,
        "movements": moved,
        "expected": expected,
        "tendered_net": (sale_tendered_sum(sales) - change).quantize(MONEY_SCALE),
    }


def sale_tendered_sum(sales: list[PosSale]) -> Decimal:
    """Everything handed over across these sales, on every tender."""
    return sum((sale_tendered(sale) for sale in sales), Decimal(0)).quantize(MONEY_SCALE)
