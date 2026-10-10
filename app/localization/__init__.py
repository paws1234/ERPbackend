"""T-0.LOC.01 — the localization pack framework and the Philippines pack.

§3 asks for "CoA templates, tax rules, statutory reports per country". A pack is
**data with a version**, not code: `app/localization/philippines/pack.json` holds
the CoA template, the tax rules, the statutory deduction rules and report
definitions, the holiday calendar, the bank file format and the fiscal calendar
placeholder, and this module loads, validates and reports on it. A new market is
a new directory, and changing a rate is an edit to that pack rather than a
release of the platform.

Three rules the loader enforces rather than documents:

* **A pack is validated before it is used.** :func:`load_pack` refuses a pack
  whose account codes repeat, whose parents do not exist, whose classes are
  unknown, whose tax rates are outside 0–100 %, whose statutory rules have no
  basis, whose holiday list is not dated, or whose statutory report collects a
  rule the pack does not state — so a broken pack cannot reach a company's books.
* **A pack that loads can still leave a gap.** Empty sections, a deduction no
  form collects and a calendar that stops before the year being run are not
  entry-level mistakes, so :func:`load_pack` cannot see them: :func:`completeness`
  enumerates all six sections and names each gap, :func:`require_complete` refuses
  a company creation while one is left, and :func:`holidays` refuses a year the
  calendar does not cover rather than answering with an empty list (an empty
  calendar reads as "every day is a working day").
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
from datetime import date
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

    rule_codes = {rule["code"] for rule in pack["statutory_rules"]}
    for report in pack["statutory_reports"]:
        if not report.get("form") or not report.get("authority"):
            fail(f"statutory report {report.get('form')!r} names no authority")
        # A report that payroll feeds names the rules it collects — and naming one the pack
        # does not state is a report that would silently leave a deduction out of itself.
        for covered in report.get("covers_rules", []):
            if covered not in rule_codes:
                fail(
                    f"statutory report {report['form']} covers the rule {covered!r}, which the"
                    " pack does not state"
                )

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


def statutory_reports(market: str) -> list[dict]:
    """Every statutory report the pack states, in the pack's order.

    Which forms exist, who they go to, what they cover and — where a payroll run feeds one —
    which rules it collects. A form that collects nothing from payroll (a VAT return, say) is
    stated without that list, which is how T-5.PAY.04 tells somebody else's return from its own
    without knowing anything about the market.
    """
    return list(load_pack(market)["statutory_reports"])


def calendar_years(market: str) -> list[int]:
    """The years the pack's holiday calendar covers, in order."""
    return sorted(
        {
            int(holiday["date"][:4])
            for holiday in load_pack(market)["holidays"]
            if holiday.get("date")
        }
    )


def holidays(market: str, year: int) -> list[dict]:
    """One year's non-working days, as the pack states them.

    A year the calendar does not cover is **refused**, not answered with an empty
    list: an empty calendar reads as "every day is a working day", so a rostering,
    attendance or payroll question about an uncovered year would be answered
    silently wrong rather than naming the pack that has to be extended.
    """
    covered = calendar_years(market)
    if year not in covered:
        _refuse(
            f"{market}: the holiday calendar covers"
            f" {', '.join(str(covered_year) for covered_year in covered)} — {year} is not among"
            " them; extend the pack (and bump its version) rather than treating every day of"
            " that year as a working day"
        )
    return [
        dict(holiday)
        for holiday in load_pack(market)["holidays"]
        if holiday["date"].startswith(f"{year}-")
    ]


def bank_file_format(market: str) -> dict:
    """The bank's transfer file layout, so AP payments (Phase 2) can write it."""
    return dict(load_pack(market)["bank_file_format"])


def completeness(market: str, *, on: date | None = None) -> dict:
    """The pack's completeness checklist: each of §3's six sections, what it holds, any gap.

    `load_pack` refuses a pack that is *malformed* — a repeated account code, a rate outside
    0–100 %, an undated holiday. This asks the other question, the one a pack that loads can
    still fail: a section that holds nothing, a deduction no form collects, a calendar that
    stops before the year being run. Each is named with the entry that leaves it, so the answer
    is a work list rather than a verdict, and :func:`require_complete` is the same list turned
    into a refusal.

    A gap is a missing *statement*, never a missing opinion: the fiscal year start is reported
    as undecided because the pack says so on purpose (plan §8), and a rule of kind `loan` is
    not collected by a form because a loan repayment is not remitted to a bureau — neither is
    counted against the pack.
    """
    today = date.today() if on is None else on
    pack = load_pack(market)
    sections: dict[str, dict] = {}

    def section(name: str, *, holds: int, detail: str, gaps: list[str]) -> None:
        sections[name] = {"holds": holds, "detail": detail, "gaps": gaps}

    # The chart of accounts: the five classes of §2.1 must each hold something, or the
    # company imports a chart with no revenue, no equity or no expense side at all.
    accounts = pack["coa_template"]["accounts"]
    classes = {account["class"] for account in accounts}
    section(
        "coa_template",
        holds=len(accounts),
        detail=f"{len(accounts)} accounts in {len(classes)} of the 5 classes",
        gaps=[f"the chart of accounts states no {name} account" for name in ACCOUNT_CLASSES
              if name not in classes],
    )

    # Tax rules: the rate and the document types the rule applies to are validated in
    # `_validate`; what is worth stating is a rule that names no posting account, because a
    # tax that cannot be posted is a tax somebody posts by hand.
    rules = pack["tax_rules"]
    document_types = sorted({kind for rule in rules for kind in rule["applies_to"]})
    unpostable = [rule["code"] for rule in rules if not rule.get("account")]
    section(
        "tax_rules",
        holds=len(rules),
        detail=(
            f"{len(rules)} rules over {len(document_types)} document types"
            + (f"; naming no posting account: {', '.join(unpostable)}" if unpostable else "")
        ),
        gaps=[],
    )

    # Statutory deductions: a contribution or a withholding that no form collects is a
    # deduction withheld with nowhere to remit it. A loan repayment is not remitted at all.
    statutory = pack["statutory_rules"]
    reports = pack["statutory_reports"]
    collected = {code for report in reports for code in report.get("covers_rules", [])}
    uncollected = [
        rule["code"] for rule in statutory
        if rule["kind"] != "loan" and rule["code"] not in collected
    ]
    kinds = sorted({rule["kind"] for rule in statutory})
    section(
        "statutory_rules",
        holds=len(statutory),
        detail=(
            f"{len(statutory)} rules of kinds {', '.join(kinds)};"
            f" {len(collected)} collected by a form"
        ),
        gaps=[f"the statutory rule {code} is collected by no form" for code in uncollected],
    )

    # Statutory reports: which form goes where, how often, and what it collects. A form the
    # pack feeds from payroll says so by naming the rules (`covers_rules`), which is how
    # T-5.PAY.04 tells a payroll return from somebody else's.
    payroll_forms = [report["form"] for report in reports if report.get("covers_rules")]
    authorities = sorted({report["authority"] for report in reports})
    section(
        "statutory_reports",
        holds=len(reports),
        detail=(
            f"{len(reports)} forms from {', '.join(authorities)};"
            f" {len(payroll_forms)} fed from payroll"
        ),
        gaps=[f"the form {report['form']} states no frequency" for report in reports
              if not report.get("frequency")],
    )

    # The holiday calendar: it must reach the next year from the one being run. A calendar
    # that stops early is not visibly wrong — every lookup in the uncovered year comes back
    # empty, and an empty calendar says every day is a working day.
    holidays_stated = pack["holidays"]
    covered = calendar_years(market)
    through = max(covered[-1], today.year + 1)
    missing_years = [
        year for year in range(min(covered[0], today.year), through + 1) if year not in covered
    ]
    dates = [day["date"] for day in holidays_stated]
    repeated = sorted({day for day in dates if dates.count(day) > 1})
    section(
        "holidays",
        holds=len(holidays_stated),
        detail=(
            f"{len(holidays_stated)} days over {', '.join(str(year) for year in covered)}"
            f" ({sum(1 for day in holidays_stated if day['type'] == 'regular')} regular,"
            f" {sum(1 for day in holidays_stated if day['type'] == 'special')} special)"
        ),
        gaps=(
            [f"the holiday calendar holds no {year} day, and the run date is {today.isoformat()}"
             for year in missing_years]
            + [f"the holiday calendar states {day} twice" for day in repeated]
        ),
    )

    # The bank file format: the columns a payment run writes, and what it cannot write
    # without (a file with no delimiter has no fields at all).
    bank = pack["bank_file_format"]
    columns = bank["columns"]
    section(
        "bank_file_format",
        holds=len(columns),
        detail=(
            f"{bank['name']}: {len(columns)} columns"
            f" ({sum(1 for column in columns if column.get('required'))} required),"
            f" {bank.get('encoding')}"
        ),
        gaps=(
            [f"the bank file format's column {index} is unnamed" for index, column in
             enumerate(columns, start=1) if not column.get("name")]
            + ([] if bank.get("delimiter") else ["the bank file format states no delimiter"])
            + ([] if any(column.get("required") for column in columns)
               else ["the bank file format requires no column"])
        ),
    )

    # A section that holds nothing is the gap the checklist exists for: it is not a
    # malformed entry, it is a company created with nothing to import.
    for name, state in sections.items():
        if state["holds"] == 0:
            state["gaps"].insert(0, "the section holds nothing")

    gaps = [
        f"{name}: {gap}" for name, state in sections.items() for gap in state["gaps"]
    ]
    return {
        "market": market,
        "version": pack["version"],
        "as_of": pack.get("as_of"),
        "currency": pack["currency"],
        "fiscal_year_start": pack["fiscal_year_start"],
        "on": today.isoformat(),
        "checklist": list(sections),
        "sections": sections,
        "years": covered,
        "gaps": gaps,
    }


def require_complete(market: str, *, on: date | None = None) -> dict:
    """The checklist, refused when it leaves a gap — the gate a fresh company passes.

    Called by :func:`company_template`, so a company is not created in a market whose pack
    leaves a section empty, a deduction uncollected or the calendar short: a gap that reaches
    a company's books is not visibly a gap at all.
    """
    report = completeness(market, on=on)
    if report["gaps"]:
        _refuse(
            f"{market}: the pack leaves {len(report['gaps'])} gap(s) — "
            + "; ".join(report["gaps"])
        )
    return report


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
    company in this market cannot quietly skip the question — and the pack's own
    completeness checklist is required first, so a company is not created in a
    market whose pack leaves a section empty or its calendar short.
    """
    pack = load_pack(market)
    checklist = require_complete(market)
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
        "holiday_years": checklist["years"],
        "pack_sections": {
            name: state["holds"] for name, state in checklist["sections"].items()
        },
        "bank_file_format": pack["bank_file_format"]["name"],
    }
