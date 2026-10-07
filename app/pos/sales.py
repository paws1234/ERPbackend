"""T-3.POS.01 — the point-of-sale sale: scan, price, tender, post, receipt.

**Online only.** Offline operation is Phase 6 (T-6.OFFLINE.01); a sale here talks to
the database in one transaction like every other document, and the till reaches it
through the published API rather than through a connection of its own (T-0.API.02).

Five decisions shape this module:

* **Scanning resolves, and a scan that resolves to nothing is refused.** The barcode
  lookup is T-1.INV.01's `item_from_barcode`, so a variant's code resolves to its
  variant and a code nobody carries is an error at the till rather than a line sold
  at zero.
* **The price is the engine's, not the till's.** Each line is priced through
  T-3.SALES.06's `resolve_price` — the customer's tier, the quantity and the campaign
  ride the one resolution order the rest of the platform uses — and the line records
  the rule that produced it, so a receipt can say why the price was what it was.
* **The tax is the pack's.** T-3.AR.01's :mod:`app.sales.tax` resolves the one rule a
  sales order, its invoice and a POS sale share, so the till and the invoice that
  bills the same goods cannot charge different VAT.
* **Completing a sale is what moves anything.** While it is open, a sale is a basket:
  no stock has moved and nothing is posted. :func:`complete_sale` refuses one whose
  tenders do not cover it, then issues stock from the till's location through
  T-1.INV.05's `issue` (which values it, writes the stock ledger and posts the cost
  side) and posts the revenue and its tax. An abandoned basket therefore leaves no
  trace in either ledger.
* **The receipt is rebuilt from the sale, never from today's rules.** :func:`receipt`
  reads the stored lines, prices, taxes and tenders, so reprinting it a year later
  reproduces what the customer was given rather than what the engine would say now.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
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

from app.company import company_base_currency
from app.db import Base
from app.ledger.currency import currency_by_code
from app.ledger.mapping import mapped_account
from app.ledger.posting import JournalEntry, post_journal_entry
from app.sales.customers import Customer
from app.sales.pricing import resolve_price
from app.sales.tax import tax_on
from app.stock.items import Item, ItemVariant, item_from_barcode
from app.stock.locations import Location
from app.stock.transactions import issue

if TYPE_CHECKING:  # a real import would be a cycle: the shift module reads this one
    from app.pos.shifts import PosShift

MONEY = Numeric(20, 6)
MONEY_SCALE = Decimal("0.000001")

# A basket until it is completed; `completed` is what moved the stock and the ledger.
# `void` is the end of a sale that no longer stands: an abandoned basket (nothing ever
# moved) or a refunded sale (whose effects are reversed) — T-3.POS.04 owns the voiding.
OPEN, COMPLETED, VOID = "open", "completed", "void"
SALE_STATUSES = (OPEN, COMPLETED, VOID)

# The tenders a till takes (§2.5's `payment_tender_types`). Each says *where the money
# went*, which is what decides the account its half of the entry is booked to:
# `cash` into the drawer, anything else into the bank.
CASH, CARD, GATEWAY = "cash", "card", "gateway"
TENDER_TYPES = (CASH, CARD, GATEWAY)
BANK_TENDERS = (CARD, GATEWAY)

# What the money is booked through — AP's own keys (T-2.AP.04), so a till receipt and
# a bank statement meet the same accounts.
CASH_KEY = "cash"
BANK_KEY = "bank"
REVENUE_KEY = "revenue"
OUTPUT_TAX_KEY = "output_tax"

# The document type a stock issue from a till carries.
DOC_TYPE = "pos_sale"


class PosError(ValueError):
    """The point-of-sale sale refused what was asked of it."""


class DuplicateSaleError(PosError):
    """That sale number is already taken in this company."""


class SaleStateError(PosError):
    """The asked-for change does not apply to the sale's state."""


class EmptySaleError(PosError):
    """A sale with nothing on it would post nothing and issue nothing."""


class UnsettledSaleError(PosError):
    """The tenders do not cover the sale, so it is not paid for."""


class PosSale(Base):
    """One trip to the till: the basket, what was tendered, and what it became."""

    __tablename__ = "pos_sale"
    __table_args__ = (
        UniqueConstraint("company_id", "number", name="uq_pos_sale_company_number"),
        CheckConstraint(
            "status IN ('open', 'completed', 'void')", name="ck_pos_sale_status"
        ),
        CheckConstraint("net_amount >= 0", name="ck_pos_sale_net"),
        CheckConstraint("tax_amount >= 0", name="ck_pos_sale_tax"),
        CheckConstraint("gross_amount >= 0", name="ck_pos_sale_gross"),
        # Change is what the customer is owed back, so it is never negative. There is no
        # column for it: `sale_change` derives it from the tenders, and a completed sale
        # can only reach this state with the tenders covering it — `applied <= tendered`
        # on every tender is what makes the difference non-negative by construction.
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    number: Mapped[str] = mapped_column(String(32), nullable=False)
    # The till this sale happened at, as the deployment names it. A terminal is not a
    # master here: the plan names `pos_mode` and `barcode_symbology`, not terminals,
    # and a shift (T-3.POS.03) is what a terminal's day is read through.
    terminal: Mapped[str] = mapped_column(String(32), nullable=False)
    # Where the goods came off the shelf. The issue at completion names it, so what a
    # till sold is findable in the stock ledger.
    location_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("location.id"), nullable=False, index=True
    )
    # Null is a walk-in sale — the common case at a till, and not a missing customer.
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("customer.id"), index=True
    )
    sold_on: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    # The shift it was completed inside, where the company manages shifts (T-3.POS.03).
    # Null is a till with no drawer management — not a missing figure.
    shift_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("pos_shift.id"), index=True
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    # Stated at the money scale from the outset: an empty basket is "0.000000", the same
    # shape every other amount on this document carries.
    net_amount: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, default=Decimal("0.000000")
    )
    tax_amount: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, default=Decimal("0.000000")
    )
    gross_amount: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, default=Decimal("0.000000")
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=OPEN)
    journal_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("journal_entry.id"), index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # T-3.POS.04: when a sale was voided, by whom and why. A void is never silent — the
    # Z-Report shows the voids and the refunds apart, and each one has a reason.
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voided_by: Mapped[str | None] = mapped_column(String(64))
    void_reason: Mapped[str | None] = mapped_column(String(200))
    reversal_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("journal_entry.id"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    location: Mapped[Location] = relationship()
    customer: Mapped[Customer | None] = relationship()
    entry: Mapped[JournalEntry | None] = relationship(
        foreign_keys="PosSale.journal_entry_id"
    )
    lines: Mapped[list[PosSaleLine]] = relationship(
        back_populates="sale", order_by="PosSaleLine.line_no"
    )
    tenders: Mapped[list[PosTender]] = relationship(
        back_populates="sale", order_by="PosTender.tender_no"
    )
    shift: Mapped[PosShift | None] = relationship(back_populates="sales")
    reversal_entry: Mapped[JournalEntry | None] = relationship(
        foreign_keys="PosSale.reversal_entry_id"
    )


class PosSaleLine(Base):
    """One scanned line, at the price the engine resolved and the pack's tax."""

    __tablename__ = "pos_sale_line"
    __table_args__ = (
        UniqueConstraint("sale_id", "line_no", name="uq_pos_sale_line_no"),
        CheckConstraint("line_no >= 1", name="ck_pos_sale_line_starts_at_one"),
        CheckConstraint("quantity > 0", name="ck_pos_sale_line_quantity"),
        CheckConstraint("unit_price >= 0", name="ck_pos_sale_line_price"),
        CheckConstraint("tax_amount >= 0", name="ck_pos_sale_line_tax"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    sale_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pos_sale.id"), nullable=False, index=True
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("item.id"), nullable=False, index=True
    )
    variant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("item_variant.id"), index=True
    )
    description: Mapped[str] = mapped_column(String(200), nullable=False)
    barcode: Mapped[str | None] = mapped_column(String(64))
    quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    uom: Mapped[str] = mapped_column(String(16), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    tax_rule_code: Mapped[str | None] = mapped_column(String(32))
    # The pricing rule that produced the price and where it sat in the resolution
    # order (T-3.SALES.06) — what lets a receipt say why the price is what it is.
    rule_code: Mapped[str | None] = mapped_column(String(80))
    rule_priority: Mapped[int | None] = mapped_column(Integer)
    # The issue that took it off the shelf, written at completion.
    movement_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("stock_ledger_entry.id"), index=True
    )

    sale: Mapped[PosSale] = relationship(back_populates="lines")
    item: Mapped[Item] = relationship()
    variant: Mapped[ItemVariant | None] = relationship()


class PosTender(Base):
    """One way the customer paid: how much was handed over, and how much of it counted.

    `tendered` is what changed hands; `applied` is what went against the sale. They
    differ by the change, which is why both are stored: a receipt shows the money the
    customer gave and the change they got, and the drawer (T-3.POS.02) counts the
    notes, not the net.
    """

    __tablename__ = "pos_tender"
    __table_args__ = (
        UniqueConstraint("sale_id", "tender_no", name="uq_pos_tender_no"),
        CheckConstraint("tender_no >= 1", name="ck_pos_tender_starts_at_one"),
        CheckConstraint(
            "tender_type IN ('cash', 'card', 'gateway')", name="ck_pos_tender_type"
        ),
        CheckConstraint("tendered > 0", name="ck_pos_tender_tendered"),
        CheckConstraint("applied >= 0", name="ck_pos_tender_applied"),
        CheckConstraint("applied <= tendered", name="ck_pos_tender_within_tendered"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    sale_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pos_sale.id"), nullable=False, index=True
    )
    # Which tender this was on the sale, in the order the till took them. Stored rather
    # than inferred from a timestamp: two tenders of one sale are taken in the same
    # instant, and a receipt that lists them in a random order is not reproducible.
    tender_no: Mapped[int] = mapped_column(Integer, nullable=False)
    tender_type: Mapped[str] = mapped_column(String(16), nullable=False)
    tendered: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    applied: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    # The terminal's own reference for a non-cash tender (an authorisation code, a
    # gateway payment id). Null for cash, which has none.
    reference: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    sale: Mapped[PosSale] = relationship(back_populates="tenders")


def _amount(value: Any) -> Decimal:
    """A stated amount, or a refusal — never an exception the boundary cannot name.

    A float is refused because it is already inexact, and anything that will not parse
    is refused here rather than raised as `decimal.InvalidOperation` further in, which
    is not a `ValueError` and would leave the API answering 500 to a mistyped figure.
    """
    if isinstance(value, float):
        raise PosError(f"an amount is an exact decimal, not the float {value!r}")
    if isinstance(value, Decimal):
        amount = value
    else:
        try:
            amount = Decimal(str(value).strip())
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise PosError(f"{value!r} is not a sale amount") from exc
    if not amount.is_finite():
        # `numeric` would store it and every sum it touched would be nonsense, so it is
        # refused while it can still be named.
        raise PosError(f"{value!r} is not a finite sale amount")
    return amount


def line_net(line: PosSaleLine) -> Decimal:
    """What one line sells for before its tax."""
    return (line.quantity * line.unit_price).quantize(MONEY_SCALE)


def sale_tendered(sale: PosSale) -> Decimal:
    """Everything handed over for this sale — the figure the customer would recognise."""
    return sum((tender.tendered for tender in sale.tenders), Decimal(0)).quantize(MONEY_SCALE)


def sale_applied(sale: PosSale) -> Decimal:
    """What the tenders actually covered — never more than the sale is worth."""
    return sum((tender.applied for tender in sale.tenders), Decimal(0)).quantize(MONEY_SCALE)


def sale_change(sale: PosSale) -> Decimal:
    """What the customer is owed back: what they gave less what the sale took."""
    return (sale_tendered(sale) - sale.gross_amount).quantize(MONEY_SCALE)


def recalculate(session: Session, sale: PosSale) -> PosSale:
    """Restate the sale's net, tax and gross from its lines as they now stand."""
    net = sum((line_net(line) for line in sale.lines), Decimal(0)).quantize(MONEY_SCALE)
    tax = sum((line.tax_amount for line in sale.lines), Decimal(0)).quantize(MONEY_SCALE)
    sale.net_amount = net
    sale.tax_amount = tax
    sale.gross_amount = (net + tax).quantize(MONEY_SCALE)
    session.flush()
    return sale


def open_sale(
    session: Session,
    *,
    company_id: uuid.UUID,
    number: str,
    terminal: str,
    location: Location,
    customer: Customer | None = None,
    currency: str | None = None,
    sold_on: date | None = None,
) -> PosSale:
    """Start a basket at one till. Nothing has moved yet — not stock, not the ledger."""
    if location.company_id != company_id:
        raise PosError("that location belongs to another company")
    wanted = str(number).strip()
    if not wanted:
        raise PosError("a sale number is required")
    if session.scalar(
        select(PosSale).where(PosSale.company_id == company_id, PosSale.number == wanted)
    ) is not None:
        raise DuplicateSaleError(f"sale {wanted!r} already exists in this company")
    if customer is not None and customer.company_id != company_id:
        raise PosError("that customer belongs to another company")
    till = str(terminal).strip()
    if not till:
        raise PosError("a sale names the terminal it happened at")
    sale = PosSale(
        company_id=company_id,
        number=wanted,
        terminal=till,
        location_id=location.id,
        customer_id=None if customer is None else customer.id,
        sold_on=sold_on or date.today(),
        currency=currency_by_code(
            session,
            company_id=company_id,
            # An unstated currency is the customer's own and then the company's base —
            # never a third guess (T-3.AR.01's rule, at a till).
            code=currency
            or (customer.transaction_currency if customer is not None else None)
            or company_base_currency(session, company_id=company_id),
        ).code,
        status=OPEN,
    )
    session.add(sale)
    session.flush()
    # A basket rung up inside a shift belongs to it from the start, so a basket that is
    # abandoned shows up in the shift it was started in (T-3.POS.04's voids) rather than
    # in nobody's. Imported here, not at module scope: the shift module reads this one.
    from app.pos.shifts import current_shift

    shop = current_shift(session, company_id=company_id, terminal=till)
    if shop is not None:
        sale.shift_id = shop.id
        session.flush()
    return sale


def scan(
    session: Session,
    sale: PosSale,
    *,
    barcode: str,
    base_price: Any,
    quantity: Any = 1,
    uom: str | None = None,
    campaign: str | None = None,
) -> PosSaleLine:
    """Scan one code onto the sale, at the price T-3.SALES.06 resolves for it.

    `base_price` is stated by the till — the plan names no price list and the item
    master holds none, so the number a discount is measured against is the one that
    was on the shelf. A code nobody carries is refused here, which is what stops an
    unknown scan becoming a line sold at zero.
    """
    if sale.status != OPEN:
        raise SaleStateError(
            f"sale {sale.number!r} is {sale.status}; its lines are settled and cannot"
            " be added to"
        )
    item, variant = item_from_barcode(session, company_id=sale.company_id, value=barcode)
    wanted = _amount(quantity)
    if wanted <= 0:
        raise PosError(f"a scan adds a positive quantity, got {wanted}")
    decision = resolve_price(
        session,
        company_id=sale.company_id,
        base_price=base_price,
        quantity=wanted,
        item_id=item.id,
        tier=sale.customer.tier if sale.customer is not None else None,
        campaign=campaign,
    )
    taken_uom = str(uom or item.base_uom)
    net = (wanted * decision.price).quantize(MONEY_SCALE)
    # The pack's selling rule — the one a sales order and an invoice charge too, which
    # is why it is not asked for by document type: they share a basis or the pack is
    # refused (`app.sales.tax.sales_rule`).
    tax = tax_on(net)
    line = PosSaleLine(
        company_id=sale.company_id,
        # Appended through the relationship, not written with the foreign key alone:
        # `recalculate` reads `sale.lines`, and a row that only knows its `sale_id`
        # would not be in it until the collection was reloaded.
        line_no=len(sale.lines) + 1,
        item_id=item.id,
        variant_id=None if variant is None else variant.id,
        # A variant is an attribute combination, so it is named by its own SKU: the
        # receipt has to say which one left the shelf.
        description=item.name if variant is None else f"{item.name} ({variant.sku})",
        barcode=str(barcode),
        quantity=wanted,
        uom=taken_uom,
        unit_price=decision.price,
        tax_amount=tax["tax"],
        tax_rule_code=tax["rule_code"],
        rule_code=decision.rule_code,
        rule_priority=decision.rule_priority,
    )
    sale.lines.append(line)
    session.flush()
    recalculate(session, sale)
    return line


def tender(
    session: Session,
    sale: PosSale,
    *,
    tender_type: str,
    amount: Any,
    reference: str | None = None,
) -> PosTender:
    """Take one payment against the sale.

    What the customer hands over and what it covers are both recorded: the amount
    applied is what the sale still owed, capped by what was handed over, so a sale
    can never be over-applied and the change is the difference. **A non-cash tender
    cannot produce change** — a card is read for the amount it is charged, so asking
    for cash back on one would book money nobody received.
    """
    if sale.status != OPEN:
        raise SaleStateError(f"sale {sale.number!r} is {sale.status} and takes no payment")
    kind = str(tender_type).strip().lower()
    if kind not in TENDER_TYPES:
        raise PosError(
            f"{tender_type!r} is not a tender this till takes ({', '.join(TENDER_TYPES)})"
        )
    given = _amount(amount)
    if given <= 0:
        raise PosError(f"a tender is above zero, got {given}")
    outstanding = (sale.gross_amount - sale_applied(sale)).quantize(MONEY_SCALE)
    applied = min(given, outstanding) if outstanding > 0 else Decimal(0).quantize(MONEY_SCALE)
    change = (given - applied).quantize(MONEY_SCALE)
    if change > 0 and kind != CASH:
        raise PosError(
            f"a {kind} tender of {given} would over-pay sale {sale.number!r} by {change};"
            " change comes out of the drawer, so only a cash tender may exceed what the"
            " sale takes"
        )
    record = PosTender(
        company_id=sale.company_id,
        tender_no=len(sale.tenders) + 1,
        tender_type=kind,
        tendered=given,
        applied=applied,
        reference=None if reference is None else str(reference),
    )
    # Appended through the relationship for the same reason a line is: `sale_applied`
    # reads `sale.tenders`.
    sale.tenders.append(record)
    session.flush()
    return record


def complete_sale(
    session: Session, sale: PosSale, *, on: datetime | None = None
) -> PosSale:
    """Finish the sale: issue its stock, post its revenue, and write the change down.

    Refused unless the tenders cover the sale — the one thing a till must not do is
    hand over goods nobody paid for. Everything the sale moves is moved here, so an
    abandoned basket leaves the stock ledger and the general ledger exactly as it
    found them.
    """
    if sale.status != OPEN:
        raise SaleStateError(f"sale {sale.number!r} is already {sale.status}")
    if not sale.lines:
        raise EmptySaleError(
            f"sale {sale.number!r} has no lines, so there is nothing to sell, issue or post"
        )
    # The shift is resolved before anything moves: when the company manages drawers, a
    # sale cannot be completed outside one, and the refusal must come before the stock
    # leaves the shelf.
    from app.pos.shifts import require_open_shift

    shift = require_open_shift(session, sale)
    if shift is not None:
        # The money lands in the drawer this shift is counting, so a basket opened
        # earlier but settled now belongs to the shift that took it.
        sale.shift_id = shift.id
    elif sale.shift_id is not None:
        # No shift is trading here now. A basket opened inside one that has since been
        # closed is not that shift's sale: its Z-Report has been signed off, and adding
        # to it afterwards would restate a closed report.
        sale.shift_id = None
    recalculate(session, sale)
    outstanding = (sale.gross_amount - sale_applied(sale)).quantize(MONEY_SCALE)
    if outstanding > 0:
        raise UnsettledSaleError(
            f"sale {sale.number!r} is owed {outstanding}; it is not complete until the"
            " tenders cover it"
        )
    if sale.gross_amount <= 0:
        # Nothing to pay is nothing to sell: the entry would have a single zero line or
        # none at all, which is not a posting. Refused by name, before the stock moves.
        raise PosError(
            f"sale {sale.number!r} is worth {sale.gross_amount}; a sale with nothing to"
            " pay is not a sale"
        )
    location = session.get(Location, sale.location_id)
    for line in sale.lines:
        item = session.get(Item, line.item_id)
        variant = session.get(ItemVariant, line.variant_id) if line.variant_id else None
        movement = issue(
            session,
            item=item,
            location=location,
            uom=line.uom,
            quantity=line.quantity,
            currency=sale.currency,
            source_type=DOC_TYPE,
            source_id=sale.id,
            posting_date=sale.sold_on,
            variant=variant,
        )
        line.movement_id = movement.id
    lines: list[dict[str, Any]] = []
    for record in sale.tenders:
        key = CASH_KEY if record.tender_type == CASH else BANK_KEY
        # One line per tender: a split sale reaches two accounts, and each line carries
        # what that tender applied. The terminal's own reference is on the tender row,
        # not on the entry — the entry is found from the sale, and the sale from the
        # till's own record of the authorisation.
        lines.append(
            {
                "account": mapped_account(
                    session, company_id=sale.company_id, key=key
                ).code,
                "debit": record.applied,
            }
        )
    lines.append(
        {
            "account": mapped_account(
                session, company_id=sale.company_id, key=REVENUE_KEY
            ).code,
            "credit": sale.net_amount,
        }
    )
    if sale.tax_amount > 0:
        lines.append(
            {
                "account": mapped_account(
                    session, company_id=sale.company_id, key=OUTPUT_TAX_KEY
                ).code,
                "credit": sale.tax_amount,
            }
        )
    entry = post_journal_entry(
        session,
        company_id=sale.company_id,
        posting_date=sale.sold_on,
        currency=sale.currency,
        memo=f"POS sale {sale.number} at {sale.terminal}",
        source_type=DOC_TYPE,
        source_id=sale.id,
        lines=lines,
    )
    sale.journal_entry_id = entry.id
    sale.status = COMPLETED
    sale.completed_at = on or datetime.now(timezone.utc)
    session.flush()
    return sale


def receipt(sale: PosSale) -> dict:
    """The customer's receipt, rebuilt from the stored sale.

    Every figure is read from the sale's own rows — the lines and the price each was
    sold at, the pack's tax as it was applied, the tenders and the change — so a
    reprint reproduces what was handed over rather than what today's rules would say.
    """
    return {
        "sale": sale.number,
        "terminal": sale.terminal,
        "sold_on": sale.sold_on,
        "currency": sale.currency,
        "lines": [
            {
                "line_no": line.line_no,
                "description": line.description,
                "barcode": line.barcode,
                "quantity": str(line.quantity),
                "uom": line.uom,
                "unit_price": str(line.unit_price),
                "net": str(line_net(line)),
                "tax": str(line.tax_amount),
                "tax_rule": line.tax_rule_code,
                "rule": line.rule_code,
            }
            for line in sale.lines
        ],
        "net": str(sale.net_amount),
        "tax": str(sale.tax_amount),
        "total": str(sale.gross_amount),
        "tenders": [
            {
                "tender_type": record.tender_type,
                "tendered": str(record.tendered),
                "applied": str(record.applied),
                "reference": record.reference,
            }
            for record in sale.tenders
        ],
        "tendered": str(sale_tendered(sale)),
        "change": str(sale_change(sale)),
    }


def sale_by_number(session: Session, *, company_id: uuid.UUID, number: str) -> PosSale:
    """The sale a later document quotes, or a refusal naming what is missing."""
    found = session.scalar(
        select(PosSale).where(
            PosSale.company_id == company_id, PosSale.number == str(number).strip()
        )
    )
    if found is None:
        raise PosError(f"no POS sale {number!r} in this company")
    return found


def completed_sales(
    session: Session,
    *,
    company_id: uuid.UUID,
    on: date | None = None,
    terminal: str | None = None,
) -> list[PosSale]:
    """Every completed sale of this company, oldest first — the Z-Report's population."""
    statement = select(PosSale).where(
        PosSale.company_id == company_id, PosSale.status == COMPLETED
    )
    if on is not None:
        statement = statement.where(PosSale.sold_on == on)
    if terminal is not None:
        statement = statement.where(PosSale.terminal == str(terminal))
    return list(session.scalars(statement.order_by(PosSale.sold_on, PosSale.number)))
