"""T-6.OFFLINE.01 — the till that kept selling: the queue a terminal replays, and the report it gets back.

T-3.POS.01's sale is online by construction — the till reaches it through the API, and
completing one is what issues stock and posts the revenue. A terminal that loses the
network has to keep selling anyway, so this module is the *server's* half of that: it
takes a terminal's queued sales, replays each one through **the same calls an online
sale goes through**, and answers with one reconciliation report for the whole run.

Four decisions:

* **A synced sale is the same sale.** Each queued line is scanned, each queued tender
  taken, and the sale completed by `app.pos.sales` itself — so the stock movements and
  the journal entry are T-3.POS.01's, figure for figure, rather than a second posting
  path that would have to be kept in step with it. The backend stays the system of
  record; the terminal is a queue with a till on it.
* **One bad sale does not take the run.** A queued sale the server refuses — a payment
  that does not cover the basket, a scan of a code nobody carries — is reported with the
  platform's own words and rolled back on its own, so the rest of a long queue still
  synchronises. A refusal here is a *reading* of the refusal that already exists, never a
  second posting path: the sale is completed by T-3.POS.01's own calls or not at all.
* **Once means once.** A queued sale is identified by the number the till rang it under,
  so a batch that is sent twice— the classic dropped-response retry — lands as
  `duplicate` for the sales already stored and `accepted` for the ones that are not.
  The sales numbers are per company, exactly as T-3.POS.01 requires, and the endpoint
  is retry-safe on the API's own `Idempotency-Key` as well.
* **The reconciliation difference is a document, not a log line.** One `PosSyncReport`
  per terminal per sync run: what was queued, what was accepted, what came back as a
  duplicate, what was refused and why — with the period the queue covers, so a long
  offline stretch is one report rather than one per sale.
* **An oversell is reported, never swallowed.** A queued sale that the location cannot
  issue is refused with the item, the quantity the till sold and what the location
  actually holds, and it is *not* completed: this platform never takes a location
  negative (T-1.INV.05), so the till's queue keeps it and a person resolves it. The
  report states whether the terminal was configured to allow selling beyond its cached
  view (`oversell_allowed`), because a shortfall when it was not is a stale cache on the
  terminal rather than a policy — the two read very differently on a report.

The **client** half is T-6.OFFLINE.01's own: `ERPfrontend/lib/offline.ts` holds the
queue across a terminal restart, refuses a sale its cached view says cannot be filled
unless `POS_OFFLINE_OVERSELL` says otherwise, and replays the queue to `POST
/api/v1/pos/sync`.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    Uuid,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.db import Base
from app.pos.sales import (
    COMPLETED,
    PosError,
    PosSale,
    complete_sale,
    open_sale,
    scan,
    tender,
)
from app.sales.customers import customer_by_code
from app.stock.entries import on_hand
from app.stock.items import convert_quantity, item_from_barcode
from app.stock.locations import Location

# What a queued sale turned out to be: the three answers a terminal can act on.
ACCEPTED, DUPLICATE, REJECTED = "accepted", "duplicate", "rejected"
OUTCOMES = (ACCEPTED, DUPLICATE, REJECTED)


class OfflineError(PosError):
    """The queue refused what was handed to it."""


class PosSyncReport(Base):
    """One terminal's sync run: what it queued, and what happened to each sale."""

    __tablename__ = "pos_sync_report"
    __table_args__ = (
        CheckConstraint("queued >= 0", name="ck_pos_sync_report_queued"),
        CheckConstraint("accepted >= 0 AND duplicates >= 0 AND rejected >= 0",
                        name="ck_pos_sync_report_counts"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    terminal: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    location_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("location.id"), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # The policy the terminal was running under, stated on the report: a shortfall with
    # this off is a stale cache on the till, with it on it was a choice.
    oversell_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # The period the queue covered — an offline stretch, stated as the days it spans.
    queued_from: Mapped[date | None] = mapped_column(Date)
    queued_to: Mapped[date | None] = mapped_column(Date)
    queued: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    accepted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # JSON, because both are the run's own detail: one line per queued sale, and one line
    # per item the terminal could not have had.
    outcomes: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    differences: Mapped[str] = mapped_column(Text, nullable=False, default="[]")

    location: Mapped[Location] = relationship()


def _sales(payload: Iterable[Any]) -> list[dict]:
    """The queue as a list of mappings, refusing anything that is not one."""
    out: list[dict] = []
    for entry in payload:
        if not isinstance(entry, dict):
            raise OfflineError("a queued sale is an object with a number and its lines")
        out.append(entry)
    return out


def _number(entry: dict) -> str:
    stated = str(entry.get("number") or "").strip()
    if not stated:
        raise OfflineError("a queued sale names the number the till rang it under")
    return stated


def _shortfalls(
    session: Session, *, sale: dict, location: Location
) -> list[dict]:
    """What the location cannot issue for one queued sale, item by item.

    Read before the sale is built, so a refused sale leaves nothing behind — not a
    basket, not a movement. The barcode resolves the same way T-3.POS.01's scan resolves
    it and the quantity converts through the item's own UOM table, so "can this be
    filled?" is asked in the unit the stock ledger is kept in.
    """
    wanted: dict[tuple[uuid.UUID, uuid.UUID | None], dict] = {}
    for line in sale.get("lines") or []:
        barcode = str(line.get("barcode") or "").strip()
        item, variant = item_from_barcode(session, company_id=location.company_id, value=barcode)
        quantity = convert_quantity(
            session,
            item,
            quantity=line.get("quantity") or 1,
            from_uom=line.get("uom") or item.base_uom,
            to_uom=item.base_uom,
        )
        key = (item.id, None if variant is None else variant.id)
        row = wanted.setdefault(
            key,
            {"barcode": barcode, "item": item.sku, "quantity": Decimal(0), "variant": None},
        )
        if variant is not None:
            row["variant"] = variant.sku
        row["quantity"] += quantity

    short: list[dict] = []
    for (item_id, variant_id), row in wanted.items():
        held = Decimal(
            on_hand(
                session,
                company_id=location.company_id,
                item_id=item_id,
                location_id=location.id,
                variant_id=variant_id,
            )["quantity"]
        )
        if row["quantity"] > held:
            short.append(
                {
                    "barcode": row["barcode"],
                    "item": row["item"],
                    "variant": row["variant"],
                    # Amounts cross as exact decimal strings, as they do everywhere else
                    # (DOMAIN-MODELS §2): this is stored JSON, and a reader of the report
                    # is a person or a terminal, not a float.
                    "quantity": format(row["quantity"], "f"),
                    "on_hand": format(held, "f"),
                    "location": location.code,
                }
            )
    return short


def _day(value: Any) -> date | None:
    """A queued day, whether the till sent an ISO date or the date itself."""
    if value is None or isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise OfflineError(f"{value!r} is not a date a sale can be queued on") from exc


def _queue_span(sales: list[dict]) -> tuple[date | None, date | None]:
    days = [day for entry in sales if (day := _day(entry.get("sold_on"))) is not None]
    return (min(days), max(days)) if days else (None, None)


def sync_sales(
    session: Session,
    *,
    company_id: uuid.UUID,
    terminal: str,
    location: Location,
    sales: Iterable[Any],
    oversell_allowed: bool = False,
    received_at: datetime | None = None,
) -> PosSyncReport:
    """Replay a terminal's queue, and answer with one report for the run.

    The sales are taken in the order the till queued them, which is the order they
    happened in: a numbering that skipped backwards would be a till whose clock or queue
    is wrong, and the report would hide it.
    """
    if location.company_id != company_id:
        raise OfflineError("that location belongs to another company")
    till = str(terminal or "").strip()
    if not till:
        raise OfflineError("a sync names the terminal it is for")
    queue = _sales(sales)
    if not queue:
        raise OfflineError(f"terminal {till!r} queued nothing to synchronise")

    report = PosSyncReport(
        company_id=company_id,
        terminal=till,
        location_id=location.id,
        received_at=received_at or datetime.now(timezone.utc),
        oversell_allowed=bool(oversell_allowed),
        queued=len(queue),
    )
    report.queued_from, report.queued_to = _queue_span(queue)
    session.add(report)
    session.flush()

    outcomes: list[dict] = []
    differences: list[dict] = []
    for entry in queue:
        number = _number(entry)
        stored = session.scalar(
            select(PosSale).where(PosSale.company_id == company_id, PosSale.number == number)
        )
        if stored is not None:
            # Already here: the batch is being sent again (a dropped answer, a till that
            # was switched off mid-send), and the first answer is the one that counts.
            if stored.status == COMPLETED:
                report.duplicates += 1
                outcomes.append({"number": number, "outcome": DUPLICATE})
            else:
                report.rejected += 1
                outcomes.append(
                    {
                        "number": number,
                        "outcome": REJECTED,
                        "reason": (
                            f"sale {number!r} is open at the server and is not this queue's"
                            " to complete"
                        ),
                    }
                )
            continue

        short = _shortfalls(session, sale=entry, location=location)
        if short:
            # ponytail: a sale the location cannot fill is refused and reported rather
            # than completed against a negative location, because `issue` refuses a
            # negative location and this platform has no negative-stock policy (T-1.INV.05
            # states the invariant; the plan names no adjustment path for a till). The
            # ceiling is that the terminal's sale stays unsynchronised until a person
            # resolves it. Upgrade: a reasoned stock adjustment (T-1.INV.06's count) wired
            # to this report, once the plan asks how a shortfall is to be made good.
            report.rejected += 1
            outcomes.append(
                {
                    "number": number,
                    "outcome": REJECTED,
                    "reason": (
                        "the location cannot issue what the till sold: "
                        + "; ".join(
                            f"{row['item']} wants {row['quantity']}, {row['location']}"
                            f" holds {row['on_hand']}"
                            for row in short
                        )
                    ),
                }
            )
            for row in short:
                differences.append(
                    {
                        "number": number,
                        **row,
                        "oversell_allowed": bool(oversell_allowed),
                    }
                )
            continue

        # One queued sale is one savepoint: a sale the till queued wrong — a number a
        # person already rang up at the server, a payment that does not cover the basket,
        # a barcode nobody carries — is refused by name and leaves nothing behind, while
        # the rest of the queue still syncs. Without this, one bad sale would take the
        # whole run with it, which is the opposite of what an offline till needs.
        savepoint = session.begin_nested()
        try:
            sale = open_sale(
                session,
                company_id=company_id,
                number=number,
                terminal=till,
                location=location,
                customer=(
                    customer_by_code(
                        session, company_id=company_id, code=entry["customer_code"]
                    )
                    if entry.get("customer_code")
                    else None
                ),
                currency=entry.get("currency"),
                sold_on=_day(entry.get("sold_on")),
            )
            for line in entry.get("lines") or []:
                scan(
                    session,
                    sale,
                    barcode=str(line.get("barcode") or ""),
                    base_price=line.get("base_price"),
                    quantity=line.get("quantity") or 1,
                    uom=line.get("uom"),
                    campaign=line.get("campaign"),
                )
            for payment in entry.get("tenders") or []:
                tender(
                    session,
                    sale,
                    tender_type=payment.get("tender_type"),
                    amount=payment.get("amount"),
                    reference=payment.get("reference"),
                )
            complete_sale(session, sale)
        except PosError as exc:
            # A domain refusal, not a fault: reported with the platform's own words and
            # rolled back to the savepoint, so the refused sale leaves no basket.
            savepoint.rollback()
            report.rejected += 1
            outcomes.append({"number": number, "outcome": REJECTED, "reason": str(exc)})
            continue
        savepoint.commit()
        report.accepted += 1
        outcomes.append({"number": number, "outcome": ACCEPTED, "sale": str(sale.id)})

    report.outcomes = json.dumps(outcomes)
    report.differences = json.dumps(differences)
    session.flush()
    return report


def report_of(report: PosSyncReport) -> dict:
    """A report as the terminal (or a person) reads it."""
    return {
        "terminal": report.terminal,
        "location": report.location.code,
        "received_at": report.received_at,
        "oversell_allowed": bool(report.oversell_allowed),
        "queued_from": report.queued_from,
        "queued_to": report.queued_to,
        "queued": report.queued,
        "accepted": report.accepted,
        "duplicates": report.duplicates,
        "rejected": report.rejected,
        "outcomes": json.loads(report.outcomes),
        "differences": json.loads(report.differences),
    }


def reports_of(
    session: Session, *, company_id: uuid.UUID, terminal: str | None = None
) -> list[PosSyncReport]:
    """This company's sync runs, newest last — one per terminal per run, never per sale."""
    statement = select(PosSyncReport).where(PosSyncReport.company_id == company_id)
    if terminal is not None:
        statement = statement.where(PosSyncReport.terminal == str(terminal).strip())
    return list(
        session.scalars(
            statement.order_by(PosSyncReport.received_at, PosSyncReport.id)
        )
    )


__all__ = [
    "ACCEPTED",
    "DUPLICATE",
    "OfflineError",
    "OUTCOMES",
    "PosSyncReport",
    "REJECTED",
    "report_of",
    "reports_of",
    "sync_sales",
]
