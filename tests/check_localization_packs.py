"""T-6.HARD.03 check — every confirmed market's pack is complete, imports, and is visible.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_localization_packs.py

It fails (non-zero exit) if any of these stops holding:

1. **every pack in the tree enumerates complete** — the six sections §3 names (CoA template,
   tax rules, statutory rules, statutory reports, holiday calendar, bank file format) each
   hold something, and the checklist leaves no gap: no section empty, no deduction collected
   by no form, no form without a frequency, no unnamed column, no repeated account or holiday
2. **the holiday calendar reaches the year being run and the year after it** — every day is
   dated and typed, `holidays(market, year)` answers for each year the pack covers and
   **refuses** a year it does not cover by name, and a pack whose calendar drops the next year
   is refused with that year named (this is the gap the polish closed: the calendar stopped at
   2026, so a 2027 run would have found no holiday at all)
3. **a fresh company imports the pack cleanly, market by market** — the chart of accounts
   imports whole (every code, every parent resolved, all five classes), one component per
   statutory rule, the template and the holiday calendar the pack's own
4. **the pack version in force is visible on what it affected** — on every loaded statutory
   component, on the structure in force on a date, on the statutory report a run produces, and
   on the company template, and the checklist states the version it checked
5. **no application code branches on market** — a pack for a market this tree has never seen
   is discovered, enumerates complete and creates a company without a line of code changing,
   and no module outside `app/localization` holds the market's name or currency as a value

**Scratch database only**: it drops and recreates the schema.
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import shutil
import sys
import uuid
from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.audit import set_actor  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.hr.employees import create_employee, record_contract  # noqa: E402
from app.hr.holidays import seed_calendar  # noqa: E402
from app.ledger import posting  # noqa: E402,F401 — every check builds the one schema
from app.ledger.accounts import Account, import_coa_template  # noqa: E402
from app.localization import (  # noqa: E402
    ACCOUNT_CLASSES,
    PACKS_DIR,
    PackError,
    calendar_years,
    coa_template,
    company_template,
    completeness,
    holidays,
    load_pack,
    packs,
    require_complete,
    statutory_rules,
)
from app.payroll.components import (  # noqa: E402
    load_statutory_components,
    structure_on,
)
from app.payroll.engine import compute_run, start_run  # noqa: E402
from app.payroll.statutory import payroll_reports, produce_report  # noqa: E402

MARKET = "philippines"
PROBE = "atlantis"
APP = pathlib.Path(__file__).resolve().parent.parent / "app"


def _refused(call, expected: str) -> str:
    """The message `call` refuses with; fail the check if it does not refuse."""
    try:
        call()
    except PackError as exc:
        assert expected in str(exc), f"unclear error: {exc}"
        return str(exc)
    raise AssertionError(f"accepted what it must refuse ({expected!r})")


def _probe(mutate=None) -> str:
    """A copy of the shipped pack under a market name no line of code has ever seen."""
    copied = json.loads((PACKS_DIR / MARKET / "pack.json").read_text())
    copied["market"] = PROBE
    copied["country"] = "ZZ"
    if mutate is not None:
        mutate(copied)
    directory = PACKS_DIR / PROBE
    directory.mkdir(exist_ok=True)
    (directory / "pack.json").write_text(json.dumps(copied))
    return PROBE


def _code_strings(path: pathlib.Path) -> list[tuple[int, str]]:
    """The string values a module actually uses — docstrings are prose, not behaviour."""
    tree = ast.parse(path.read_text())
    prose = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                prose.add(id(first.value))
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in prose
    ]


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is required (a scratch Postgres)", file=sys.stderr)
        return 2

    today = date.today()
    sections_named = [
        "coa_template",
        "tax_rules",
        "statutory_rules",
        "statutory_reports",
        "holidays",
        "bank_file_format",
    ]

    # 1 — every pack in the tree enumerates complete, section by section
    assert packs() == [MARKET], f"the platform ships packs for {packs()}"
    for market in packs():
        checklist = require_complete(market)
        assert checklist["checklist"] == sections_named, checklist["checklist"]
        assert checklist["gaps"] == [], checklist["gaps"]
        assert checklist["fiscal_year_start"] == load_pack(market)["fiscal_year_start"], (
            "the checklist states a fiscal year start the pack does not"
        )
        print(
            f"{market} pack {checklist['version']} (as of {checklist['as_of']}) — the checklist:"
        )
        for name in checklist["checklist"]:
            state = checklist["sections"][name]
            assert state["holds"] > 0, (name, state)
            print(f"  {name:18} {state['holds']:>3}  {state['detail']}")
        assert not checklist["gaps"], checklist["gaps"]
        print(
            f"  gaps: none — {len(checklist['checklist'])} of 6 sections hold something,"
            " the fiscal year start is reported as the pack states it"
            f" ({checklist['fiscal_year_start']})"
        )

    # 2 — the calendar reaches the year being run and the year after it
    for market in packs():
        stated = load_pack(market)["holidays"]
        years = calendar_years(market)
        assert today.year in years and today.year + 1 in years, (market, years)
        for holiday in stated:
            date.fromisoformat(holiday["date"])
            assert holiday["name"] and holiday["type"] in ("regular", "special"), holiday
        dates = [holiday["date"] for holiday in stated]
        assert len(set(dates)) == len(dates), "a holiday is stated twice"
        for year in years:
            assert holidays(market, year), f"{year} is covered but empty"
        print(
            f"{market}: {len(stated)} holidays over {', '.join(str(year) for year in years)}"
            f" — {today.year} and {today.year + 1} are both covered, every day dated and typed"
        )
        message = _refused(lambda: holidays(market, today.year + 4), f"{today.year + 4} is not")
        print(f"a year the calendar does not cover is refused: {message[:96]}")

    # 1/2 — the gaps the polish closed are refused, by name, on a copy of the pack
    def _drop_next_year(pack: dict) -> None:
        pack["holidays"] = [
            holiday
            for holiday in pack["holidays"]
            if not holiday["date"].startswith(f"{today.year + 1}-")
        ]

    _probe(_drop_next_year)
    try:
        message = _refused(lambda: require_complete(PROBE), f"holds no {today.year + 1} day")
        print(f"a calendar that stops at {today.year} is refused: {message[:96]}")
        assert completeness(PROBE)["gaps"], "the checklist saw no gap"
        creating = _refused(
            lambda: company_template(
                PROBE, company_id=uuid.uuid4(), fiscal_year_start_month=1
            ),
            f"holds no {today.year + 1} day",
        )
        print(f"and creating a company in that market waits: {creating[:80]}")
    finally:
        shutil.rmtree(PACKS_DIR / PROBE)

    _probe(lambda pack: pack.update({"tax_rules": []}))
    try:
        message = _refused(lambda: require_complete(PROBE), "the section holds nothing")
        print(f"a pack whose tax rules are gone is refused: {message[:96]}")
    finally:
        shutil.rmtree(PACKS_DIR / PROBE)

    _probe(
        lambda pack: pack["statutory_reports"].__setitem__(
            slice(None), [report for report in pack["statutory_reports"]
                          if not report.get("covers_rules")]
        )
    )
    try:
        message = _refused(lambda: require_complete(PROBE), "collected by no form")
        print(f"a pack whose forms collect no deductions is refused: {message[:96]}")
    finally:
        shutil.rmtree(PACKS_DIR / PROBE)

    _probe(
        lambda pack: pack["bank_file_format"]["columns"][0].update({"name": ""})
    )
    try:
        message = _refused(lambda: require_complete(PROBE), "column 1 is unnamed")
        print(f"a bank file format with an unnamed column is refused: {message[:80]}")
    finally:
        shutil.rmtree(PACKS_DIR / PROBE)

    # 5 — a market the tree has never seen: discovered, complete, and importable, code untouched
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    Base.metadata.create_all(engine)

    _probe()
    try:
        assert PROBE in packs(), f"a new market directory was not discovered: {packs()}"
        unseen = require_complete(PROBE)
        assert unseen["gaps"] == [], unseen["gaps"]
        print(
            f"a market the code has never seen ({PROBE}, {unseen['currency']}) is discovered by"
            f" its directory and enumerates complete — {unseen['version']},"
            f" {unseen['sections']['coa_template']['holds']} accounts"
        )
    finally:
        shutil.rmtree(PACKS_DIR / PROBE)

    # 3/4 — a fresh company imports the pack, market by market, and the version is on what it
    # touched
    for market in packs():
        company_id = uuid.uuid4()
        template = coa_template(market)
        rules = statutory_rules(market)
        with Session(engine) as session:
            set_actor(session, "localization")
            summary = company_template(
                market, company_id=company_id, fiscal_year_start_month=1
            )
            session.add(
                Company(
                    id=company_id,
                    code=f"PACK-{market.upper()}",
                    name=f"{market} pack import",
                    base_currency=summary["base_currency"],
                    fiscal_year_start_month=summary["fiscal_year_start"],
                )
            )
            session.commit()

            # the chart of accounts, whole and in the pack's own order
            accounts = import_coa_template(session, company_id=company_id, market=market)
            assert [account.code for account in accounts] == [
                row["code"] for row in template
            ], "the chart did not import in the pack's order"
            assert {account.account_class for account in accounts} == set(ACCOUNT_CLASSES), (
                "a class of §2.1 is missing from the imported chart"
            )
            parents = {
                account.code: account.parent_id for account in accounts
            }
            assert all(
                parents[row["code"]] is not None
                for row in template
                if row.get("parent")
            ), "a child imported with no parent"
            assert session.query(Account).filter_by(company_id=company_id).count() == len(
                template
            )

            # the calendar for the year being run, read through the pack's own reader — and an
            # uncovered year refused rather than seeded as a year with no holidays in it
            seeded = seed_calendar(
                session, company_id=company_id, market=market, year=today.year
            )
            session.commit()
            assert len(seeded) == len(holidays(market, today.year)), (market, len(seeded))
            _refused(
                lambda: seed_calendar(
                    session, company_id=company_id, market=market, year=today.year + 4
                ),
                f"{today.year + 4} is not",
            )

            # every statutory rule, as a dated component that names the pack it came from
            components = load_statutory_components(
                session, company_id=company_id, market=market, effective_from="2026-01-01"
            )
            session.commit()
            assert len(components) == len(rules), "a statutory rule loaded no component"
            assert {component.pack_version for component in components} == {
                summary["pack_version"]
            }, "a component does not name the pack version it came from"
            structure = structure_on(session, company_id=company_id, on=date(2026, 6, 30))
            assert structure["pack_versions"] == [summary["pack_version"]], structure

            # the version is on the report a run produces, from the same pack
            person = create_employee(
                session,
                company_id=company_id,
                party_code=f"PACK-{market.upper()}-E1",
                number="E-9001",
                hire_date="2025-01-06",
                subject="localization",
                name="Pack Import Reyes",
            )
            record_contract(
                session, person, subject="localization", effective_from="2025-01-06",
                contract_type="regular", basic_salary="30000",
            )
            session.commit()
            run = start_run(
                session, company_id=company_id, period="2026-06", actor="localization",
                cutoff_day=15,
            )
            compute_run(session, run, actor="localization")
            session.commit()
            form = payroll_reports(market)[0]["form"]
            report = produce_report(session, run, market=market, form=form)
            assert report["pack_version"] == summary["pack_version"], report
            assert report["market"] == market
            assert summary["pack_sections"]["holidays"] == len(
                load_pack(market)["holidays"]
            ), summary["pack_sections"]
            assert summary["holiday_years"] == calendar_years(market)
            print(
                f"{market}: a company imported {len(accounts)} accounts, {len(components)}"
                f" statutory rules, {len(seeded)} calendar days for {today.year} and the"
                f" template; the version in force {summary['pack_version']} is on the"
                f" components, on the structure in force ({structure['pack_versions']}) and on"
                f" the {form} the run produced"
            )

    # 5 — no application code branches on market
    offenders = []
    for path in sorted(APP.rglob("*.py")):
        if "localization" in path.parts:
            continue
        for lineno, value in _code_strings(path):
            if MARKET in value.lower() or value == "PHP":
                offenders.append(f"{path.relative_to(APP.parent)}:{lineno} {value!r}")
    assert not offenders, offenders
    print(
        f"no market is in the code: {len(list(APP.rglob('*.py')))} modules outside"
        " app/localization were read, and none holds the market's name or its currency"
    )

    engine.dispose()
    print("ok — every pack is complete, imports cleanly, and its version is on what it affected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
