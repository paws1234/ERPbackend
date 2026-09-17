"""T-0.LOC.01 — the localization pack framework and the Philippines pack.

§3 asks for "CoA templates, tax rules, statutory reports per country". A pack is
**data with a version**, not code: `app/localization/philippines/pack.json` holds
the CoA template, the tax rules, the statutory deduction rules and report
definitions, the holiday calendar, the bank file format and the fiscal calendar
placeholder, and this module loads, validates and reports on it. A new market is
a new directory, and changing a rate is an edit to that pack rather than a
release of the platform.

Two rules the loader enforces rather than documents:

* **A pack is validated before it is used.** :func:`load_pack` refuses a pack
  whose account codes repeat, whose parents do not exist, whose classes are
  unknown, whose tax rates are outside 0–100 %, whose statutory rules have no
  basis, or whose holiday list is not dated — so a broken pack cannot reach a
  company's books.
* **Nothing is assumed on the market's behalf.** The Philippines' fiscal year
  start is still undecided in plan §8 (2026-09-17), so the pack carries
  `fiscal_year_start: null` and :func:`fiscal_year_start` refuses to answer until
  it is confirmed with the pack — an invented January would be a silent guess in
  every period afterwards, so creating a company in this market stops until
  somebody confirms it.

What the pack does **not** do: no statutory computation, no tax engine, no
posting. Those belong to the modules that consume the rules (T-2.PROC.08 for
supplier tax, Phase 5 payroll for statutory deductions). This module hands them
the rules and their basis.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from pathlib import Path

# Where the packs live: one directory per market, each with a `pack.json`.
PACKS_DIR = Path(__file__).resolve().parent

# The five account classes §2.1 names.
ACCOUNT_CLASSES = ("asset", "liability", "equity", "income", "expense")

# The kinds of statutory deduction a pack may describe.
STATUTORY_KINDS = ("contribution", "withholding", "loan", "other")


class PackError(ValueError):
    """The pack is not usable: it failed validation, or something it needs is undecided."""


def _refuse(problem: str) -> None:
    raise PackError(problem)


def packs() -> list[str]:
    """The markets this deployment ships a pack for."""
    return sorted(
        path.parent.name for path in PACKS_DIR.glob("*/pack.json")
    )


def load_pack(market: str) -> dict:
    """Load and validate one market's pack.

    Raises :class:`PackError` with the first problem found, naming the pack and
    the entry — a pack is either usable or it says exactly why it is not.
    """
    path = PACKS_DIR / market / "pack.json"
    if not path.exists():
        _refuse(f"no pack for {market!r}; known markets: {', '.join(packs())}")
    pack = json.loads(path.read_text())
    _validate(market, pack)
    return pack


def _validate(market: str, pack: dict) -> None:
    def fail(problem: str) -> None:
        _refuse(f"{market}: {problem}")

    for required in (
        "market",
        "version",
        "currency",
        "coa_template",
        "tax_rules",
        "statutory_rules",
        "statutory_reports",
        "holidays",
        "bank_file_format",
        "fiscal_year_start",
    ):
        if required not in pack:
            fail(f"the pack has no {required!r}")

    coa = pack["coa_template"]
    if not coa.get("accounts"):
        fail("the chart of accounts is empty")
    codes: set[str] = set()
    for account in coa["accounts"]:
        code = account.get("code")
        if not code or code in codes:
            fail(f"account code {code!r} is missing or repeated")
        codes.add(code)
        if account.get("class") not in ACCOUNT_CLASSES:
            fail(f"account {code} has an unknown class {account.get('class')!r}")
        parent = account.get("parent")
        if parent is not None and parent not in codes:
            # parents are listed before their children, so a missing parent is a
            # pack that would import into a broken tree
            fail(f"account {code} names a parent {parent!r} that is not above it")

    for rule in pack["tax_rules"]:
        rate = rule.get("rate_percent")
        if not isinstance(rate, (int, float)) or not 0 <= rate <= 100:
            fail(f"tax rule {rule.get('code')!r} has a rate outside 0–100: {rate!r}")
        if not rule.get("applies_to"):
            fail(f"tax rule {rule.get('code')!r} does not say what it applies to")

    for rule in pack["statutory_rules"]:
        if rule.get("kind") not in STATUTORY_KINDS:
            fail(f"statutory rule {rule.get('code')!r} has an unknown kind {rule.get('kind')!r}")
        if not rule.get("basis"):
            fail(f"statutory rule {rule.get('code')!r} states no basis")
        if not rule.get("schedule"):
            fail(f"statutory rule {rule.get('code')!r} states no schedule")

    for report in pack["statutory_reports"]:
        if not report.get("form") or not report.get("authority"):
            fail(f"statutory report {report.get('form')!r} names no authority")

    if not pack["holidays"]:
        fail("the holiday calendar is empty")
    for holiday in pack["holidays"]:
        if not holiday.get("date") or not holiday.get("name"):
            fail(f"holiday {holiday!r} is not dated and named")
        if holiday.get("type") not in ("regular", "special"):
            fail(f"holiday {holiday['date']} has an unknown type {holiday.get('type')!r}")

    bank = pack["bank_file_format"]
    if not bank.get("name") or not bank.get("columns"):
        fail("the bank file format names no columns")

    fiscal = pack["fiscal_year_start"]
    if fiscal is not None and not (
        isinstance(fiscal, dict) and 1 <= fiscal.get("month", 0) <= 12
    ):
        fail(f"fiscal_year_start {fiscal!r} is neither null nor a month 1–12")


def coa_template(market: str) -> list[dict]:
    """The chart of accounts to import — validated, and ready for T-1.ACCT.01.

    The template is data whether or not the market's fiscal calendar is settled:
    importing accounts is harmless. Creating the *company* is not, and that is
    :func:`fiscal_year_start`'s gate.
    """
    return [dict(account) for account in load_pack(market)["coa_template"]["accounts"]]


def fiscal_year_start(market: str, *, confirmed: int | None = None) -> int:
    """The month this market's fiscal year opens in, or a refusal.

    Called by whoever creates a company (T-0.CORE.03's ``Company`` requires the
    month). The pack carries ``null`` for the Philippines because plan §8 leaves
    the question open, so this refuses until the pack is confirmed or the caller
    states the month it was confirmed as — an invented January would be a silent
    guess in every period afterwards.
    """
    pack = load_pack(market)
    settled = pack["fiscal_year_start"]
    if settled is not None:
        return settled["month"]
    if confirmed is None:
        _refuse(
            f"{market}: the fiscal year start is not confirmed in the pack"
            " (plan §8 leaves it open as of 2026-09-17) — confirm it with the pack"
            " and record it in the pack, or state the confirmed month here"
        )
    if not 1 <= confirmed <= 12:
        _refuse(f"{market}: {confirmed!r} is not a month 1–12")
    return confirmed


def accounts_by_class(market: str) -> dict[str, list[str]]:
    """The template's account codes grouped by class — what a CoA screen shows."""
    pack = load_pack(market)
    grouped: dict[str, list[str]] = {name: [] for name in ACCOUNT_CLASSES}
    for account in pack["coa_template"]["accounts"]:
        grouped[account["class"]].append(account["code"])
    return grouped


def tax_rules(market: str, document_type: str | None = None) -> list[dict]:
    """The market's tax rules, optionally just those for one document type."""
    rules = load_pack(market)["tax_rules"]
    if document_type is None:
        return [dict(rule) for rule in rules]
    return [dict(rule) for rule in rules if document_type in rule["applies_to"]]


def statutory_rules(market: str, kind: str | None = None) -> list[dict]:
    """The market's statutory deduction rules, optionally of one kind."""
    rules = load_pack(market)["statutory_rules"]
    if kind is None:
        return [dict(rule) for rule in rules]
    return [dict(rule) for rule in rules if rule["kind"] == kind]


def holidays(market: str, year: int) -> list[dict]:
    """One year's non-working days, as the pack states them."""
    return [
        dict(holiday)
        for holiday in load_pack(market)["holidays"]
        if holiday["date"].startswith(f"{year}-")
    ]


def bank_file_format(market: str) -> dict:
    """The bank's transfer file layout, so AP payments (Phase 2) can write it."""
    return dict(load_pack(market)["bank_file_format"])


def amount_from(rule: dict, *, basis: Decimal) -> Decimal:
    """Apply one rate rule to a base amount, exactly.

    A convenience the consuming modules may use or ignore — the pack states rates
    in percent, and the platform multiplies exact decimals (never floats), so the
    arithmetic is stated once here rather than in each module.
    """
    rate = Decimal(str(rule.get("rate_percent", 0))) / Decimal(100)
    return (basis * rate).quantize(Decimal("0.000001"))


def company_template(
    market: str, *, company_id: uuid.UUID, fiscal_year_start_month: int | None = None
) -> dict:
    """What a company created in this market starts with — the pack's own summary.

    Returned, not applied: creating the company master is T-0.CORE.03's business
    and importing the CoA is T-1.ACCT.01's. The fiscal year start is the pack's
    or the caller's confirmed month, and it is asked for here so that creating a
    company in this market cannot quietly skip the question.
    """
    pack = load_pack(market)
    return {
        "company_id": str(company_id),
        "market": pack["market"],
        "pack_version": pack["version"],
        "base_currency": pack["currency"],
        "fiscal_year_start": fiscal_year_start(
            market, confirmed=fiscal_year_start_month
        ),
        "coa_template": pack["coa_template"]["name"],
        "tax_pack": [rule["code"] for rule in pack["tax_rules"]],
        "statutory_pack": [rule["code"] for rule in pack["statutory_rules"]],
        "statutory_reports": [report["form"] for report in pack["statutory_reports"]],
        "holidays": len(pack["holidays"]),
        "bank_file_format": pack["bank_file_format"]["name"],
    }
