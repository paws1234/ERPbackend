"""T-3.AR.01 — the pack's tax rules applied to the selling side.

§2.5 sells goods and services, §3's "Localization" row says a market's rules are
**data**, and T-0.LOC.01 already ships them: the Philippines pack's `VAT-OUT-12`
applies to `sales_invoice`, `sales_order` and `pos_sale`. This module is the
selling-side half of the shape T-2.PROC.08 established for buying, and it exists
for the same reasons:

* **One basis for the whole selling chain.** An order, the invoice raised from it
  and the POS sale at the till are the same sale at three moments, so they must
  charge the same tax — same rule, same rate, same rounding — or the invoice and
  the Z-Report that reconcile against it are reconciling unlike things. The rule
  is resolved once, and a pack that configures those three documents differently
  is **refused** rather than applied three ways.
* **Nothing about the market is in the code.** The pack is found from the packs
  this deployment ships (:func:`app.localization.packs`), never from a market name
  typed here, and where a deployment ships more than one there is no single answer
  — so this refuses and says so rather than picking one.
* **The classification is data too.** :func:`tax_on` takes the rule *code* a
  caller wants (`VAT-ZERO`, `VAT-EXEMPT`), so a zero-rated export or an exempt
  line is a decision recorded on the document rather than a rate this module
  remembers. A code the pack does not apply to a selling document is refused.

What this module does **not** do: file anything with a revenue authority, or
decide which sales are zero-rated — the pack states the rates, the document states
the classification.

ponytail: the market is inferred from the installed packs, because `company` has
no `market` column (T-0.CORE.03 did not add one), exactly as
``app.procurement.tax`` does. Ceiling: a deployment shipping two packs cannot tell
which one a company uses. Upgrade path: add `market` to `company` (T-0.LOC.01's
`company_template` already takes one) and read it here.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.localization import amount_from, packs, tax_rules

# The three selling documents that must share one tax basis: the offer, the
# invoice raised from it, and the till sale that posts the same revenue.
SALES_DOCUMENTS = ("sales_order", "sales_invoice", "pos_sale")


class TaxError(ValueError):
    """The tax rules refused what was asked of them."""


class NoTaxPackError(TaxError):
    """No single pack describes this deployment's market — refused, never guessed."""


class NoTaxRuleError(TaxError):
    """The pack states no rule for what was asked — nothing is assumed."""


class TaxBasisMismatchError(TaxError):
    """The pack taxes the selling documents differently — refused, not averaged."""


class UnknownTaxRuleError(TaxError):
    """The named classification is not one the pack applies to a selling document."""


def active_market() -> str:
    """The market this deployment ships a pack for.

    Read from the installed packs, never from a name typed into this module: a
    market's rules are data (T-0.LOC.01). With more than one pack installed there
    is no single answer, so this refuses rather than picking the first.
    """
    installed = packs()
    if len(installed) != 1:
        raise NoTaxPackError(
            f"this deployment ships {len(installed)} packs ({', '.join(installed) or 'none'});"
            " a company's market is not recorded, so there is no single set of tax rules"
            " to apply — state the market on the company first"
        )
    return installed[0]


def rules_for(document_type: str) -> list[dict]:
    """The pack's rules that apply to one selling document type."""
    stated = [rule for rule in tax_rules(active_market(), str(document_type)) if rule]
    if not stated:
        raise NoTaxRuleError(
            f"the pack states no tax rule for {document_type!r}; nothing is applied"
            " rather than a rate being assumed"
        )
    return stated


def _rate(rule: dict) -> Decimal:
    return Decimal(str(rule["rate_percent"]))


def _rule_code(rule: dict) -> str:
    return str(rule.get("code", ""))


def sales_rule() -> dict:
    """The one rule a sales order, its invoice and a POS sale all charge.

    Refuses when the pack configures those documents differently: the invoice and
    the till's Z-Report are reconciled against each other (T-3.POS.05), so a pack
    that taxes them at different rates makes those reconciliations meaningless, and
    saying so is better than computing three answers that cannot agree.
    """
    rates = {}
    for document_type in SALES_DOCUMENTS:
        rule = rules_for(document_type)[0]
        rates[document_type] = (_rule_code(rule), _rate(rule))
    distinct = set(rates.values())
    if len(distinct) != 1:
        raise TaxBasisMismatchError(
            "the pack taxes the selling documents differently"
            f" ({', '.join(f'{doc} = {code} @ {rate}%' for doc, (code, rate) in rates.items())});"
            " an order, its invoice and a POS sale have to share one basis or the"
            " documents that reconcile against each other compare unlike things"
        )
    return rules_for(SALES_DOCUMENTS[0])[0]


def rule_by_code(code: str) -> dict:
    """One named classification the pack applies to a selling document.

    A code the pack does not apply to a selling document is refused: applying a
    buying-side rule to a sale would charge a rate nobody classified.
    """
    wanted = str(code).strip()
    for document_type in SALES_DOCUMENTS:
        for rule in rules_for(document_type):
            if _rule_code(rule) == wanted:
                return rule
    raise UnknownTaxRuleError(
        f"the pack applies no rule {wanted!r} to a selling document"
        f" ({', '.join(SALES_DOCUMENTS)}); the codes it does state are"
        f" {', '.join(sorted({_rule_code(r) for d in SALES_DOCUMENTS for r in rules_for(d)}))}"
    )


def tax_on(basis: Any, *, rule_code: str | None = None) -> dict:
    """Tax on an amount, under one rule of the active pack.

    Returns the rule's code and rate together with the exact tax and total, so a
    caller stores the *result* and never re-derives it. An unnamed classification
    is the pack's default selling rule — the same one :func:`sales_rule` resolves
    for the whole chain — so "tax is applied per the active pack" is what happens
    when nothing special is stated. `amount_from` (T-0.LOC.01) does the
    arithmetic — one rounding rule for the whole platform.
    """
    rule = sales_rule() if rule_code is None else rule_by_code(rule_code)
    amount = basis if isinstance(basis, Decimal) else Decimal(str(basis))
    if amount < 0:
        raise TaxError(f"tax is computed on an amount, not {amount}")
    tax = amount_from(rule, basis=amount)
    return {
        "rule_code": _rule_code(rule),
        "rate_percent": _rate(rule),
        "basis": amount.quantize(Decimal("0.000001")),
        "tax": tax,
        "total": (amount + tax).quantize(Decimal("0.000001")),
    }
