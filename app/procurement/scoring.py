"""T-2.PROC.09 — supplier performance scored from the documents, not from a rating.

A scorecard is worth having only if every figure on it can be traced back to a
document somebody can open. So nothing here is typed in:

* **on-time** — the receipt's date against the order's *required* date;
* **quantity accuracy** — what the order asked for against what actually arrived;
* **quality** — what the warehouse rejected against what it accepted (both already on
  the receipt line, T-2.PROC.07);
* **price** — the awarded price against the price the requisition was estimated at,
  which is the price variance Phase 2 can evidence from its own documents. Comparing
  an *invoice's* price is T-2.MATCH.01's job, because that is a three-way comparison,
  not a supplier history.

Three rules the module is built around:

* **No receipts means unrated.** A supplier nobody has bought from is shown as
  unrated with no score at all — a zero would read as "terrible" when the truth is
  "unknown", and the two must not be confused.
* **The weights are shown.** `scoring_weights` is per company in the plan and not
  stated anywhere, so the caller states them and the scorecard reports the weights it
  used: a score whose weighting is invisible cannot be argued with.
* **Every input names its document.** Each metric carries the order and receipt
  numbers it was derived from, so a disputed score is resolvable by reading them.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.procurement.orders import PurchaseOrder, PurchaseOrderLine
from app.procurement.receipts import GoodsReceipt, GoodsReceiptLine, receipts_for_order
from app.procurement.suppliers import Supplier

# One money scale for the whole platform.
MONEY_SCALE = Decimal("0.000001")

# The metrics a scorecard is made of, in the order it reads.
METRICS = ("on_time", "quantity", "quality", "price")

# What each metric weighs when the caller states nothing. Equal weights are the only
# neutral choice: the plan leaves `scoring_weights` undecided, so inventing a
# preference for, say, punctuality would be a made-up judgement about a business.
EQUAL_WEIGHTS = {metric: Decimal(1) for metric in METRICS}


class ScoringError(ValueError):
    """The scorecard refused what was asked of it."""


def scoring_weights(**overrides: Any) -> dict[str, Decimal]:
    """The weights to score with, each defaulting to 1 unless the caller states one.

    A weight of zero is allowed — it drops the metric from the score — but a negative
    one is refused: "worse than nothing" is not a performance weight.
    """
    weights = dict(EQUAL_WEIGHTS)
    for metric, value in overrides.items():
        if metric not in METRICS:
            raise ScoringError(
                f"unknown metric {metric!r}; a scorecard weighs {', '.join(METRICS)}"
            )
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
        if amount < 0:
            raise ScoringError(f"a weight is not negative, got {amount} for {metric!r}")
        weights[metric] = amount
    if sum(weights.values()) <= 0:
        raise ScoringError("at least one metric must carry a weight")
    return weights


def _percent(part: Decimal, whole: Decimal) -> Decimal:
    """`part / whole` as 0–100, at money scale — exact, never through float."""
    if whole <= 0:
        return Decimal(100)
    return ((part / whole) * 100).quantize(MONEY_SCALE)


def _lines(session: Session, supplier: Supplier) -> list[tuple[PurchaseOrder, GoodsReceipt, GoodsReceiptLine]]:
    """Every received line of one supplier's orders: (order, receipt, line)."""
    orders = session.scalars(
        select(PurchaseOrder)
        .where(PurchaseOrder.supplier_id == supplier.id)
        .order_by(PurchaseOrder.created_at, PurchaseOrder.number)
    ).all()
    rows = []
    for order in orders:
        for receipt in receipts_for_order(session, order):
            if receipt.status != "posted":
                continue
            for line in receipt.lines:
                rows.append((order, receipt, line))
    return rows


def scorecard(
    session: Session,
    *,
    supplier: Supplier,
    weights: dict[str, Decimal] | None = None,
    start: date | None = None,
    end: date | None = None,
) -> dict:
    """One supplier's scorecard from the documents, with every input traceable.

    Returns `rated: False` with no score when nothing has been received in the
    window, and otherwise the four figures, the weights used and the documents each
    figure came from.
    """
    chosen = scoring_weights(**(weights or {}))

    rows = [
        row
        for row in _lines(session, supplier)
        if (start is None or row[1].received_on >= start)
        and (end is None or row[1].received_on <= end)
    ]
    if not rows:
        return {
            "supplier": supplier.party.code,
            "rated": False,
            "reason": "no receipts in the window — unrated rather than scored zero",
            "weights": chosen,
            "metrics": {metric: None for metric in METRICS},
            "inputs": [],
        }

    # Several receipts may deliver one order line.  Score that line once, using
    # accumulated accepted/rejected quantities, rather than rewarding split receipts.
    grouped: dict[uuid.UUID, dict[str, Any]] = {}
    for order, receipt, line in rows:
        group = grouped.setdefault(
            line.order_line_id,
            {
                "order": order,
                "receipt": receipt,
                "order_line_id": line.order_line_id,
                "line_no": line.line_no,
                "quantity": Decimal(0),
                "rejected": Decimal(0),
            },
        )
        group["quantity"] += line.quantity
        group["rejected"] += line.rejected_quantity
        if receipt.received_on >= group["receipt"].received_on:
            group["receipt"] = receipt

    on_time_hits = 0
    ordered = Decimal(0)
    received = Decimal(0)
    accepted = Decimal(0)
    rejected = Decimal(0)
    estimate_gap = Decimal(0)
    estimate_base = Decimal(0)
    inputs = []

    for group in grouped.values():
        order = group["order"]
        receipt = group["receipt"]
        order_line: PurchaseOrderLine = session.get(
            PurchaseOrderLine, group["order_line_id"]
        )
        received_quantity = group["quantity"]
        rejected_quantity = group["rejected"]
        punctual = receipt.received_on <= order.required_date
        on_time_hits += 1 if punctual else 0
        ordered += order_line.quantity
        received += received_quantity
        accepted += received_quantity
        rejected += rejected_quantity
        # price: what was awarded against what the requisition estimated
        estimated = _estimated_price(session, order_line)
        if estimated is not None and estimated > 0:
            estimate_gap += abs(order_line.unit_price - estimated) * order_line.quantity
            estimate_base += estimated * order_line.quantity
        inputs.append(
            {
                "order": order.number,
                "receipt": receipt.number,
                "line_no": group["line_no"],
                "required_on": order.required_date,
                "received_on": receipt.received_on,
                "punctual": punctual,
                "ordered": order_line.quantity,
                "received_quantity": received_quantity,
                "rejected_quantity": rejected_quantity,
                "awarded_unit_price": order_line.unit_price,
                "estimated_unit_price": estimated,
            }
        )

    delivered = accepted + rejected
    metrics = {
        "on_time": _percent(Decimal(on_time_hits), Decimal(len(grouped))),
        "quantity": _percent(
            min(ordered, received) if ordered > 0 else received, ordered
        ),
        "quality": _percent(accepted, delivered) if delivered > 0 else Decimal(100),
        "price": (
            _percent(max(Decimal(0), estimate_base - estimate_gap), estimate_base)
            if estimate_base > 0
            else Decimal(100)
        ),
    }
    total_weight = sum(chosen[metric] for metric in METRICS)
    score = (
        sum((metrics[metric] * chosen[metric] for metric in METRICS), Decimal(0))
        / total_weight
    ).quantize(MONEY_SCALE)

    return {
        "supplier": supplier.party.code,
        "rated": True,
        "reason": None,
        "weights": chosen,
        "metrics": metrics,
        "score": score,
        "inputs": inputs,
    }


def _estimated_price(session: Session, order_line: PurchaseOrderLine) -> Decimal | None:
    """What the requisition behind an ordered line estimated it at, if it says.

    Read through the chain the order already keeps (order line → requisition line), so
    the price variance traces to a document rather than to a remembered figure.
    """
    from app.procurement.requisitions import RequisitionLine

    line = session.get(RequisitionLine, order_line.requisition_line_id)
    if line is None:  # pragma: no cover — the foreign key forbids it
        return None
    return line.estimated_unit_price


def scorecards(
    session: Session,
    *,
    suppliers: list[Supplier],
    weights: dict[str, Decimal] | None = None,
    start: date | None = None,
    end: date | None = None,
) -> list[dict]:
    """One scorecard per supplier, in the order given — what a scorecard screen reads."""
    return [
        scorecard(session, supplier=supplier, weights=weights, start=start, end=end)
        for supplier in suppliers
    ]


def on_time_rate(session: Session, *, supplier: Supplier) -> Decimal:
    """Just the punctuality figure — what a reorder screen asks for on its own."""
    return scorecard(session, supplier=supplier)["metrics"]["on_time"] or Decimal(0)
