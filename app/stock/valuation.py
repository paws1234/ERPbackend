"""T-1.INV.04 — the valuation engine: FIFO, Moving Average and Standard Cost.

§2.2 asks for "Stock Ledger & Valuation (FIFO, Moving Average, Standard Cost)" and
"Real-time stock valuation". DOMAIN-MODELS.md §5.1 resolves the method on the
**company** (`valuation_scope` is per company, decided 2026-09-17, default Moving
Average — the `costing_method` column on `company`).

Three decisions:

* **Valuation is computed from the ledger, on read.** No layer table, no nightly
  job, nothing to fall out of step with the movements: every figure here is the
  ledger walked in posting order. That is also why changing the method never
  rewrites history — it changes the arithmetic applied to the same rows.
* **The three methods really do differ.** The same movements give a moving-average
  figure, a FIFO figure (oldest layers consumed first) and a standard figure
  (`quantity × standard cost`, ignoring what was actually paid) — the check shows
  three different, method-correct answers from one movement sequence.
* **A method that cannot value says so.** Standard Cost with no standard set on the
  item raises rather than valuing at zero: a zero-valued inventory is worse than a
  refusal, because nothing about it looks wrong.

`value_issue` is the piece T-1.INV.05's transactions call: the cost of issuing N
units at this instant, from the same walk, so the value an issue stores is the
value the engine computes and not the caller's guess.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.company import COSTING_METHODS, Company
from app.stock.entries import StockLedgerEntry, movements
from app.stock.items import Item, ItemError

MONEY_SCALE = Decimal("0.000001")


class ValuationError(ItemError):
    """The valuation could not be produced as asked."""


class UnknownCostingMethodError(ValuationError):
    """A method outside the three §2.2 names — refused, never defaulted."""


class MissingStandardCostError(ValuationError):
    """Standard Cost without a standard set — refused rather than valued at zero."""


def costing_method(session: Session, *, company_id: uuid.UUID) -> str:
    """How this company values stock."""
    company = session.get(Company, company_id)
    if company is None:
        raise ValuationError(f"no company {company_id}")
    return company.costing_method


def set_costing_method(session: Session, company: Company, *, method: str) -> Company:
    """Choose the costing method — a company-level decision, not per item.

    Nothing already valued is rewritten: the ledger is append-only and the
    valuation is computed from it, so this changes what the *next* read answers.
    """
    wanted = str(method).strip().lower()
    if wanted not in COSTING_METHODS:
        raise UnknownCostingMethodError(
            f"unknown costing method {method!r}; the methods are"
            f" {', '.join(COSTING_METHODS)}"
        )
    company.costing_method = wanted
    session.flush()
    return company


def _pairs(entries: Iterable[StockLedgerEntry]) -> list[tuple[Decimal, Decimal]]:
    """The movements as `(quantity, value)` pairs, in the order they happened."""
    return [(entry.quantity, entry.value) for entry in entries]


def _standard_cost(item: Item) -> Decimal:
    if item.standard_cost is None:
        raise MissingStandardCostError(
            f"{item.sku!r} has no standard cost, so Standard Cost cannot value it;"
            " set one on the item or choose another costing method"
        )
    return item.standard_cost


def _moving_average(pairs: list[tuple[Decimal, Decimal]]) -> Decimal:
    """The value of what is left, at the running average cost."""
    quantity = Decimal(0)
    value = Decimal(0)
    for moved, stated in pairs:
        if moved > 0:
            quantity += moved
            value += stated
            continue
        if quantity > 0:
            value += (value / quantity) * moved
        else:
            # Nothing to average against (an issue before any receipt, which only a
            # corrected ledger can hold): the stated value is the only truth there is.
            value += stated
        quantity += moved
    return value.quantize(MONEY_SCALE)


def _fifo(pairs: list[tuple[Decimal, Decimal]]) -> Decimal:
    """The value of what is left, oldest layers consumed first."""
    layers: list[list[Decimal]] = []
    for moved, stated in pairs:
        if moved > 0:
            layers.append([moved, (stated / moved) if moved else Decimal(0)])
            continue
        needed = -moved
        while needed > 0 and layers:
            taken = min(layers[0][0], needed)
            layers[0][0] -= taken
            needed -= taken
            if layers[0][0] == 0:
                layers.pop(0)
    return sum((quantity * unit for quantity, unit in layers), Decimal(0)).quantize(MONEY_SCALE)


def _value_of(pairs: list[tuple[Decimal, Decimal]], method: str, item: Item) -> Decimal:
    if method == "standard_cost":
        return sum((moved for moved, _stated in pairs), Decimal(0)) * _standard_cost(item)
    if method == "moving_average":
        return _moving_average(pairs)
    return _fifo(pairs)


def valuation(
    session: Session,
    *,
    company_id: uuid.UUID,
    item: Item,
    location_id: uuid.UUID | None = None,
    variant_id: uuid.UUID | None = None,
    batch_id: uuid.UUID | None = None,
    as_of: date | None = None,
    method: str | None = None,
) -> dict:
    """The quantity and value on hand, by the company's costing method.

    Available on read — no batch job — and narrowed to a location, a variant or a
    batch where the caller asks, which is what per-batch valuation needs
    (T-1.INV.08).
    """
    chosen = str(method or costing_method(session, company_id=company_id)).strip().lower()
    if chosen not in COSTING_METHODS:
        raise UnknownCostingMethodError(
            f"unknown costing method {chosen!r}; the methods are {', '.join(COSTING_METHODS)}"
        )
    entries = [
        entry
        for entry in movements(
            session, company_id=company_id, item_id=item.id, location_id=location_id, as_of=as_of
        )
        if (variant_id is None or entry.variant_id == variant_id)
        and (batch_id is None or entry.batch_id == batch_id)
    ]
    pairs = _pairs(entries)
    quantity = sum((moved for moved, _stated in pairs), Decimal(0))
    value = _value_of(pairs, chosen, item).quantize(MONEY_SCALE)
    unit_cost = (value / quantity).quantize(MONEY_SCALE) if quantity else Decimal(0)
    return {
        "item": item.sku,
        "method": chosen,
        "quantity": format(quantity, "f"),
        "value": format(value, "f"),
        "unit_cost": format(unit_cost, "f"),
    }


def value_issue(
    session: Session,
    *,
    company_id: uuid.UUID,
    item: Item,
    quantity,
    location_id: uuid.UUID | None = None,
    variant_id: uuid.UUID | None = None,
    batch_id: uuid.UUID | None = None,
    as_of: date | None = None,
    method: str | None = None,
) -> Decimal:
    """What issuing `quantity` units costs, as a **positive** figure.

    The engine's own answer, from the same walk: the difference a synthetic issue
    makes to the value on hand. T-1.INV.05 negates it for the ledger entry, because
    an issue is a negative movement.
    """
    amount = quantity if isinstance(quantity, Decimal) else Decimal(str(quantity))
    if amount <= 0:
        raise ValuationError(f"an issue takes a positive quantity, got {amount}")
    chosen = str(method or costing_method(session, company_id=company_id)).strip().lower()
    if chosen not in COSTING_METHODS:
        raise UnknownCostingMethodError(
            f"unknown costing method {chosen!r}; the methods are {', '.join(COSTING_METHODS)}"
        )
    entries = [
        entry
        for entry in movements(
            session, company_id=company_id, item_id=item.id, location_id=location_id, as_of=as_of
        )
        if (variant_id is None or entry.variant_id == variant_id)
        and (batch_id is None or entry.batch_id == batch_id)
    ]
    pairs = _pairs(entries)
    before = _value_of(pairs, chosen, item)
    after = _value_of([*pairs, (-amount, Decimal(0))], chosen, item)
    return (before - after).quantize(MONEY_SCALE)
