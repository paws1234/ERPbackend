"""T-2.PROC.08 — the pack's tax rules applied to procurement, and supplier tax data.

§2.3 asks for "tax compliance", §3's "Localization" row says a market's rules are
data, and T-0.LOC.01 already ships them. What this module adds is the part
procurement owns:

* **One basis for the whole document chain.** A purchase order, its goods receipt and
  its supplier invoice are compared against each other by T-2.MATCH.01, so they must
  carry tax computed the *same* way — same rule, same rate, same rounding — or the
  comparison compares unlike things. :func:`procurement_rule` resolves that rule once
  and **refuses** when a pack configures different rates for those three documents,
  because that would silently break the match.
* **Nothing about the market is in the code.** The pack is found from the packs this
  deployment ships (:func:`app.localization.packs`), never from a market name typed
  here, and where a deployment ships more than one there is no single answer — so it
  refuses and says so rather than picking one.
* **A supplier's tax data is checked before a document is approved, not after.**
  :func:`findings` reports what is missing or unusable — no registration at all, or a
  non-zero rate that will be charged with no TIN behind it — and
  :func:`require_supplier_tax` refuses at the boundary a document is approved at.

What this module does **not** do: file anything with a revenue authority, or invent
the identifier formats a market requires. The pack states the rules; the tables hold
the values; statutory filing outputs are not named in the plan.

ponytail: the market is inferred from the installed packs because `company` has no
`market` column (T-0.CORE.03 did not add one). Ceiling: a deployment shipping two
packs cannot tell which one a company uses. Upgrade path: add `market` to `company`
(T-0.LOC.01's `company_template` already takes one) and read it here.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.localization import amount_from, packs, tax_rules
from app.procurement.suppliers import (
    TAX_IDENTIFIER_KINDS,
    Supplier,
    SupplierTaxIdentifier,
)

# The three procurement documents that must share one tax basis — the pair
# T-2.MATCH.01 compares, plus the receipt in between.
PROCUREMENT_DOCUMENTS = ("purchase_order", "goods_receipt", "supplier_invoice")

# The identifiers a supplier is expected to hold. `tin` is the registration an input
# VAT rate is charged against; the rest narrow it (branch, withholding).
REQUIRED_IDENTIFIER = "tin"


class TaxError(ValueError):
    """The tax rules refused what was asked of them."""


class NoTaxPackError(TaxError):
    """No single pack describes this deployment's market — refused, never guessed."""


class NoTaxRuleError(TaxError):
    """The pack states no rule for that document type."""


class TaxBasisMismatchError(TaxError):
    """The pack taxes the procurement documents differently — the match would compare
    unlike things, so nothing is computed at all."""


class MissingSupplierTaxError(TaxError):
    """A supplier's tax data is not fit for the document, stated as findings."""


def active_market() -> str:
    """The market this deployment ships a pack for.

    Read from the installed packs, never from a name typed into this module: a
    market's rules are data (T-0.LOC.01). With more than one pack installed there is
    no single answer, so this refuses rather than picking the first.
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
    """The pack's rules that apply to one document type.

    The pack is data with a version (T-0.LOC.01) and the market comes from
    :func:`active_market`, so the only thing stated here is *which document* is being
    taxed.
    """
    stated = [rule for rule in tax_rules(active_market(), str(document_type)) if rule]
    if not stated:
        raise NoTaxRuleError(
            f"the pack states no tax rule for {document_type!r}; nothing is applied"
            " rather than a rate being assumed"
        )
    return stated


def procurement_rule() -> dict:
    """The one rule a purchase order, its receipt and its invoice all carry.

    Refuses when the pack configures those documents differently: the 3-way match
    compares their tax line by line, so a pack that taxes them at different rates
    makes the comparison meaningless, and saying so is better than computing three
    answers that cannot be reconciled.
    """
    rates = {}
    for document_type in PROCUREMENT_DOCUMENTS:
        rule = rules_for(document_type)[0]
        rates[document_type] = (_rule_code(rule), _rate(rule))
    distinct = set(rates.values())
    if len(distinct) != 1:
        raise TaxBasisMismatchError(
            "the pack taxes the procurement documents differently"
            f" ({', '.join(f'{doc} = {code} @ {rate}%' for doc, (code, rate) in rates.items())});"
            " a purchase order, its receipt and its invoice have to share one basis or"
            " T-2.MATCH.01 compares unlike things"
        )
    return rules_for(PROCUREMENT_DOCUMENTS[0])[0]


def _rate(rule: dict) -> Decimal:
    return Decimal(str(rule["rate_percent"]))


def _rule_code(rule: dict) -> str:
    return str(rule.get("code", ""))


def tax_on(basis: Any, *, document_type: str | None = None) -> dict:
    """Tax on an amount for one procurement document.

    Returns the rule's code and rate together with the exact tax and total, so a
    caller stores the *result* and never re-derives it: two documents in the chain
    then carry the same figure by construction. `amount_from` (T-0.LOC.01) does the
    arithmetic — one rounding rule for the whole platform.
    """
    rule = procurement_rule() if document_type is None else rules_for(document_type)[0]
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


def findings(session: Session, supplier: Supplier, *, document_type: str) -> list[str]:
    """What is wrong with a supplier's tax data for one document — as a list, not a raise.

    Reported rather than raised so a screen can show everything at once; a caller that
    must not proceed calls :func:`require_supplier_tax` instead. A **zero-rated or
    exempt** rule needs no registration behind it, so a missing TIN is only a finding
    where the rule actually charges something.
    """
    problems: list[str] = []
    held = {identifier.kind: identifier for identifier in supplier.tax_identifiers}
    unknown = [kind for kind in held if kind not in TAX_IDENTIFIER_KINDS]
    if unknown:  # pragma: no cover — the table's own check refuses these first
        problems.append(f"unknown tax identifier kind(s): {', '.join(sorted(unknown))}")
    blank = [
        identifier.kind
        for identifier in supplier.tax_identifiers
        if not str(identifier.value or "").strip()
    ]
    if blank:
        problems.append(f"blank tax identifier value(s): {', '.join(sorted(blank))}")
    rule = procurement_rule() if document_type not in PROCUREMENT_DOCUMENTS else rules_for(document_type)[0]
    if _rate(rule) > 0 and REQUIRED_IDENTIFIER not in held:
        problems.append(
            f"no {REQUIRED_IDENTIFIER} is recorded for {supplier.party.code!r}, but"
            f" {_rule_code(rule)} charges {_rate(rule)}%"
        )
    return problems


def require_supplier_tax(
    session: Session, supplier: Supplier, *, document_type: str
) -> Supplier:
    """Refuse a document whose supplier's tax data is not fit for it.

    This is the check a document calls **before** it is approved, so a supplier
    without the registration the pack charges against is caught while the document is
    still editable rather than after it has been posted.
    """
    problems = findings(session, supplier, document_type=document_type)
    if problems:
        raise MissingSupplierTaxError(
            f"supplier {supplier.party.code!r} is not fit for {document_type}: "
            + "; ".join(problems)
        )
    return supplier


def tax_identifier(supplier: Supplier, kind: str) -> SupplierTaxIdentifier | None:
    """One registration a supplier holds, or ``None`` — never a made-up value."""
    return next(
        (row for row in supplier.tax_identifiers if row.kind == str(kind).strip().lower()),
        None,
    )
