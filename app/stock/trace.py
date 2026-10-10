"""T-6.TRACE.03 — the trace: where a batch or a unit came from, and where it went.

§2.2 asks for "full track-and-trace", which is a question asked under pressure: *this* lot is
suspect, what did we make from it and who has it? The answer is a walk over the stock ledger,
because the ledger is where every movement already states **which document** produced it
(`source_type`/`source_id`) and **which identity** moved (`batch_id`/`serial_id`) — T-1.INV.08's
batch and T-1.INV.09's serial. Nothing here is recorded twice for the trace's sake:

* **Backward** is every receipt of that identity: the supplier's goods receipt (which names the
  supplier and the order), a production receipt (which names the work order, and from which the
  *consumed* materials are walked further back), a count or an adjustment (which names what it
  was).
* **Forward** is every issue: a shipment (which names the customer), a work order issue (which
  names the order, and from which the *produced* goods are walked further forward), an internal
  transfer (which names the location), an adjustment (which names the count).
* **The chain crosses production.** A batch consumed by a work order is the origin of whatever
  that work order produced, so the walk continues through the order in either direction: raw
  material reaches the customers of the finished goods, and a finished lot reaches the supplier
  of its raw material. Each hop states its depth, so a chain through several steps is readable
  rather than flat.
* **A recall is the same walk, counted.** :func:`recall` answers what a recall asks: the
  customers, work orders, locations and quantities the identity (or what it became) reached.
* **It exports.** :func:`trace_csv` writes the hops as CSV, the way the aging reports do, so an
  operator can hand the chain to somebody who does not have a login.

The period narrows what a report **lists**, never what the chain **follows**: a recall cannot be
limited to a month, and a trace that stopped at a window would answer "who has it" with "nobody
since the 1st".
"""

from __future__ import annotations

import csv
import io
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


from sqlalchemy import select
from sqlalchemy.orm import Session

from app.manufacturing.issues import issues_of
from app.manufacturing.receipts import receipts_of
from app.stock.batches import Batch, batch_by_code
from app.stock.entries import StockLedgerEntry
from app.stock.items import Item, item_by_sku
from app.stock.serials import Serial, serial_by_code

# The chain is walked hop by hop; anything deeper than this is a cycle or a data error, and
# either way is worth refusing rather than following for ever.
MAX_DEPTH = 12


class TraceError(ValueError):
    """The trace cannot be asked that: no such identity, or nothing to trace."""


@dataclass(frozen=True)
class Hop:
    """One movement of the traced identity, and the document that caused it."""

    direction: str  # backward (it arrived) | forward (it left)
    depth: int
    at: date
    movement: str  # in | out
    item: str
    location: str
    quantity: str
    batch: str | None
    serial: str | None
    document: str
    document_type: str
    document_id: str
    # The ledger entry itself, so two walks over the same chain cannot count one movement twice.
    movement_id: str
    # What the hop names beyond the movement: the supplier, the customer, the work order, the
    # order the receipt was against — whoever a recall has to call.
    party: str | None = None
    work_order: str | None = None
    related: str | None = None


@dataclass(frozen=True)
class Trace:
    """One identity's chain: what it came from, what it became, and who has it."""

    identifier: str
    kind: str  # batch | serial
    item: str
    backward: list[Hop] = field(default_factory=list)
    forward: list[Hop] = field(default_factory=list)
    # The production steps the chain passes through, at depth, in the order they were reached.
    steps: list[dict] = field(default_factory=list)
    period: tuple[date | None, date | None] = (None, None)

    @property
    def hops(self) -> list[Hop]:
        return [*self.backward, *self.forward]

    def customers(self) -> list[str]:
        return sorted({hop.party for hop in self.forward if hop.party})

    def suppliers(self) -> list[str]:
        return sorted({hop.party for hop in self.backward if hop.party})

    def locations(self) -> list[str]:
        return sorted({hop.location for hop in self.hops})


def _quantity(value: Decimal) -> str:
    return format(Decimal(value).quantize(Decimal("0.000001")), "f")


def _movements(session: Session, *, company_id: uuid.UUID, batch: Batch | None,
               serial: Serial | None) -> list[StockLedgerEntry]:
    """Every movement of one identity, oldest first — the material the trace is made of."""
    where = [StockLedgerEntry.company_id == company_id]
    where.append(
        StockLedgerEntry.batch_id == batch.id
        if batch is not None
        else StockLedgerEntry.serial_id == serial.id
    )
    return list(
        session.scalars(
            select(StockLedgerEntry)
            .where(*where)
            .order_by(StockLedgerEntry.posting_date, StockLedgerEntry.created_at)
        )
    )


def _goods_receipt(session: Session, document_id: uuid.UUID) -> dict | None:
    from app.procurement.orders import PurchaseOrder
    from app.procurement.receipts import GoodsReceipt
    from app.procurement.suppliers import Supplier

    receipt = session.get(GoodsReceipt, document_id)
    if receipt is None:
        return None
    order = session.get(PurchaseOrder, receipt.order_id)
    supplier = session.get(Supplier, receipt.supplier_id).party.code
    return {
        "document": f"goods receipt {receipt.number} from {supplier}",
        "party": supplier,
        "related": f"order {order.number}" if order is not None else None,
    }


def _production_receipt(session: Session, document_id: uuid.UUID) -> dict | None:
    from app.manufacturing.receipts import WorkOrderReceipt
    from app.manufacturing.work_orders import WorkOrder

    receipt = session.get(WorkOrderReceipt, document_id)
    if receipt is None:
        return None
    order = session.get(WorkOrder, receipt.work_order_id)
    return {
        "document": f"produced by work order {order.number}",
        "work_order": order.number,
        "related": f"receipt of {_quantity(receipt.quantity)}",
        # What this order consumed is the next hop back, and what it produced is the next hop
        # forward: the same order answers both directions.
        "work_order_id": order.id,
    }


def _production_issue(session: Session, document_id: uuid.UUID) -> dict | None:
    from app.manufacturing.issues import WorkOrderIssue
    from app.manufacturing.work_orders import WorkOrder

    issue = session.get(WorkOrderIssue, document_id)
    if issue is None:
        return None
    order = session.get(WorkOrder, issue.work_order_id)
    return {
        "document": f"consumed by work order {order.number}",
        "work_order": order.number,
        "related": f"issue of {_quantity(issue.quantity)}",
        "work_order_id": order.id,
    }


def _shipment(session: Session, document_id: uuid.UUID) -> dict | None:
    from app.sales.fulfilment import Shipment
    from app.sales.orders import SalesOrder

    shipment = session.get(Shipment, document_id)
    if shipment is None:
        return None
    order = session.get(SalesOrder, shipment.order_id)
    customer = order.customer.party.code if order is not None else None
    return {
        "document": f"shipped on {shipment.number} to {customer}",
        "party": customer,
        "related": f"order {order.number}" if order is not None else None,
    }


def _count(session: Session, document_id: uuid.UUID) -> dict | None:
    from app.stock.counts import PhysicalCount

    count = session.get(PhysicalCount, document_id)
    if count is None:
        return None
    return {
        "document": f"physical count of {count.location.code} on {count.posting_date}",
        "related": f"count {count.id}",
    }


def _document(session: Session, entry: StockLedgerEntry) -> dict:
    """What produced one movement: the document, and whoever it names.

    Every reader is guarded — a movement may name a document this company's schema does not
    carry (a phase's own check builds a subset) — and what is not found is named by its own
    type and id rather than dropped, because an unnamed hop is exactly what a trace must not
    have.
    """
    readers = {
        "goods_receipt": _goods_receipt,
        "work_order_receipt": _production_receipt,
        "work_order_issue": _production_issue,
        "stock_issue": _shipment,
        "physical_count": _count,
        "inventory_adjustment": _count,
    }
    reader = readers.get(str(entry.source_type))
    found = reader(session, entry.source_id) if reader is not None else None
    if found is None:
        # An unnamed hop is exactly what a trace must not have: what was not found is stated by
        # its own type and id rather than dropped.
        return {
            "document": f"{entry.source_type} {entry.source_id}",
            "work_order": None,
            "party": None,
            "related": None,
        }
    return {
        "document": found["document"],
        "work_order": found.get("work_order"),
        "party": found.get("party"),
        "related": found.get("related"),
        "work_order_id": found.get("work_order_id"),
    }


def _identity_of(
    session: Session, entry: StockLedgerEntry
) -> tuple[Batch | None, Serial | None]:
    """The batch and the unit one movement names (the ledger stores the ids, not the rows)."""
    return (
        session.get(Batch, entry.batch_id) if entry.batch_id else None,
        session.get(Serial, entry.serial_id) if entry.serial_id else None,
    )


def _hop(
    session: Session, entry: StockLedgerEntry, *, direction: str, depth: int
) -> Hop:
    batch, serial = _identity_of(session, entry)
    resolved = _document(session, entry)
    document = resolved["document"]
    if resolved["work_order"] is None and resolved["party"] is None:
        # A movement with no document outside the company (a transfer between bins) is named by
        # the location it reached, which is the only thing that distinguishes it.
        document = f"{document} — moved to {entry.location.code}"
    return Hop(
        direction=direction,
        depth=depth,
        at=entry.posting_date,
        movement="in" if entry.quantity > 0 else "out",
        item=entry.item.sku,
        location=entry.location.code,
        quantity=_quantity(abs(entry.quantity)),
        batch=batch.code if batch is not None else None,
        serial=serial.code if serial is not None else None,
        document=document,
        document_type=str(entry.source_type),
        document_id=str(entry.source_id),
        movement_id=str(entry.id),
        party=resolved["party"],
        work_order=resolved["work_order"],
        related=resolved["related"],
    )


def _movements_for_document(session: Session, document) -> list[StockLedgerEntry]:
    """The stock movements one document caused, whatever kind of document it is.

    The ledger stores the document's **type** as a string, so the map from a row back to its
    type lives here rather than on the row: a work order's issue and its receipt are two
    different types with two different ids, and both are walked.
    """
    from app.manufacturing.issues import WorkOrderIssue
    from app.manufacturing.receipts import WorkOrderReceipt
    from app.sales.fulfilment import Shipment
    from app.stock.entries import movements_for_source

    sources = {
        WorkOrderIssue: "work_order_issue",
        WorkOrderReceipt: "work_order_receipt",
        Shipment: "stock_issue",
    }
    source_type = sources.get(type(document))
    if source_type is None:
        return []
    return list(
        movements_for_source(
            session,
            company_id=document.company_id,
            source_type=source_type,
            source_id=document.id,
        )
    )


def _identity(session: Session, *, company_id: uuid.UUID, batch: str | None,
              serial: str | None, item: str | None) -> tuple[Batch | None, Serial | None, Item]:
    """The batch or unit named by code, found through the item it belongs to.

    A code is unique within an item and two items may reuse one, so the item is looked up first
    (by the caller's sku) and the identity inside it — and a company that does not say which
    item is answered with the one the code was found on.
    """
    if item is not None:
        found_item = item_by_sku(session, company_id=company_id, sku=item)
        if batch is not None:
            return batch_by_code(session, item=found_item, code=batch), None, found_item
        return None, serial_by_code(session, item=found_item, code=serial), found_item

    for candidate in session.scalars(select(Item).where(Item.company_id == company_id)):
        try:
            if batch is not None:
                return batch_by_code(session, item=candidate, code=batch), None, candidate
            return None, serial_by_code(session, item=candidate, code=serial), candidate
        except Exception:  # noqa: BLE001 — another item simply does not have this code
            continue
    raise TraceError(
        f"no batch or unit {batch or serial!r} is registered in this company; nothing to trace"
    )


def trace(
    session: Session,
    *,
    company_id: uuid.UUID,
    batch: str | None = None,
    serial: str | None = None,
    item: str | None = None,
    start: date | None = None,
    end: date | None = None,
) -> Trace:
    """Walk one batch or unit: backward to where it came from, forward to where it went.

    `batch`/`serial` name the identity by code (the way a person says it) and `item` narrows the
    lookup where two items reuse a code. The walk follows the chain **through production**: what
    consumed this batch and what that produced, continuing in the same direction, so raw
    material reaches the customers of the finished goods and a finished lot reaches the supplier
    of its raw material. Each hop carries its depth, so a chain through several steps is
    readable rather than flat.
    """
    if (batch is None) == (serial is None):
        raise TraceError("trace one batch or one serial, not both and not neither")
    walked_batch, walked_serial, walked_item = _identity(
        session, company_id=company_id, batch=batch, serial=serial, item=item
    )

    backward: list[Hop] = []
    forward: list[Hop] = []
    steps: list[dict] = []
    visited: set[uuid.UUID] = set()
    # One movement is one hop, however many walks reach it: the forward walk and the backward
    # walk both pass through this identity's own entries.
    collected: set[str] = set()

    def walk(one_batch: Batch | None, one_serial: Serial | None, direction: str, depth: int) -> None:
        if depth > MAX_DEPTH:
            raise TraceError(
                f"the chain is deeper than {MAX_DEPTH} steps; a sequence that long is a data"
                " error rather than a trace"
            )
        identity = one_batch.id if one_batch is not None else one_serial.id
        if identity in visited:
            return
        visited.add(identity)
        for movement in _movements(
            session, company_id=company_id, batch=one_batch, serial=one_serial
        ):
            arrived = movement.quantity > 0
            hop = _hop(
                session, movement, direction="backward" if arrived else "forward", depth=depth
            )
            # One movement is one hop, but the *chain* still continues through it however many
            # walks reached it: the record is deduplicated, the walk is not.
            if hop.movement_id not in collected:
                collected.add(hop.movement_id)
                (backward if arrived else forward).append(hop)
            if hop.work_order is None or (hop.direction != direction):
                # A hop the walk was not travelling in does not continue the chain; it is still
                # a hop, because it is a movement of this identity.
                continue
            order = _work_order(session, movement)
            if order is None or order.id in {step["id"] for step in steps}:
                continue
            steps.append(
                {
                    "id": order.id,
                    "work_order": order.number,
                    "item": order.item.sku,
                    "depth": depth,
                    "arrived_as": one_batch.code if one_batch else one_serial.code,
                }
            )
            # Forward, the order's receipts are what this batch became; backward, its issues
            # are what it was made of. Both are the ledger's own movements, read here.
            next_documents = (
                receipts_of(session, order)
                if direction == "forward"
                else issues_of(session, order)
            )
            for document in next_documents:
                for produced in _movements_for_document(session, document):
                    if produced.batch_id is None and produced.serial_id is None:
                        continue
                    next_batch, next_serial = _identity_of(session, produced)
                    walk(next_batch, next_serial, direction, depth + 1)

    walk(walked_batch, walked_serial, "forward", 0)
    visited.clear()
    walk(walked_batch, walked_serial, "backward", 0)

    # A hop outside the window is still part of the chain — the walk is whole, and the period
    # narrows what this report *lists*. A recall limited to a month would answer "who has it"
    # with "nobody since the 1st".
    period = (start, end)
    if start is not None or end is not None:
        keep = lambda hop: (start is None or hop.at >= start) and (end is None or hop.at <= end)  # noqa: E731
        backward = [hop for hop in backward if keep(hop)]
        forward = [hop for hop in forward if keep(hop)]
    backward.sort(key=lambda hop: (hop.at, hop.depth))
    forward.sort(key=lambda hop: (hop.at, hop.depth))
    return Trace(
        identifier=str(batch or serial),
        kind="batch" if batch is not None else "serial",
        item=walked_item.sku,
        backward=backward,
        forward=forward,
        steps=[{key: value for key, value in step.items() if key != "id"} for step in steps],
        period=period,
    )


def _work_order(session: Session, movement: StockLedgerEntry):
    """The work order a production movement belongs to — the step the chain goes through."""
    from app.manufacturing.issues import WorkOrderIssue
    from app.manufacturing.receipts import WorkOrderReceipt
    from app.manufacturing.work_orders import WorkOrder

    if movement.source_type == "work_order_receipt":
        receipt = session.get(WorkOrderReceipt, movement.source_id)
        return None if receipt is None else session.get(WorkOrder, receipt.work_order_id)
    if movement.source_type == "work_order_issue":
        issue = session.get(WorkOrderIssue, movement.source_id)
        return None if issue is None else session.get(WorkOrder, issue.work_order_id)
    return None


def recall(
    session: Session, *, company_id: uuid.UUID, batch: str | None = None,
    serial: str | None = None, item: str | None = None
) -> dict:
    """What a recall asks: who has it, what was made from it, and where it stands.

    The same walk as :func:`trace`, counted — and deliberately **not** limited to a period,
    because a recall that answered "nobody since the 1st of the month" would be worse than no
    recall at all.
    """
    walked = trace(session, company_id=company_id, batch=batch, serial=serial, item=item)
    # The quantities are the traced identity's **own** movements: what the other lots in the
    # chain moved is their business, and adding them together would answer a question nobody
    # asked. What it became is the steps below.
    quantities: dict[str, str] = {}
    for hop in walked.hops:
        if hop.depth != 0:
            continue
        quantities[hop.movement] = format(
            Decimal(quantities.get(hop.movement, "0")) + Decimal(hop.quantity), "f"
        )
    return {
        "identifier": walked.identifier,
        "kind": walked.kind,
        "item": walked.item,
        "customers": walked.customers(),
        "suppliers": walked.suppliers(),
        "locations": walked.locations(),
        "work_orders": [step["work_order"] for step in walked.steps],
        "steps": walked.steps,
        "quantities": quantities,
        "hops": len(walked.hops),
    }


def trace_csv(walked: Trace) -> str:
    """The chain as CSV: one row per hop, with the document each hop came from."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            f"trace of {walked.kind} {walked.identifier}",
            f"item {walked.item}",
            f"period {walked.period[0] or 'start'}..{walked.period[1] or 'end'}",
        ]
    )
    writer.writerow(
        ["direction", "depth", "date", "movement", "item", "location", "quantity", "batch",
         "serial", "document", "document type", "document id", "party", "work order"]
    )
    for hop in walked.hops:
        writer.writerow(
            [hop.direction, hop.depth, hop.at.isoformat(), hop.movement, hop.item,
             hop.location, hop.quantity, hop.batch or "", hop.serial or "", hop.document,
             hop.document_type, hop.document_id, hop.party or "", hop.work_order or ""]
        )
    return buffer.getvalue()
