"""T-0.LOC.01 check — the pack framework, and the Philippines pack used for real.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/postgres \
        python tests/check_localization.py

It fails (non-zero exit) if any of these stops holding:

1. the pack structure is versioned and validated: a pack that repeats an account
   code, names an unknown class, misses a parent, quotes a rate outside 0–100 %,
   states a statutory rule with no basis or lists an undated holiday is refused
   with the reason
2. the Philippines pack loads clean, and its chart of accounts is import-ready:
   every parent exists above its child, the five classes of §2.1 are present, and
   codes are unique
3. **nothing is assumed on the market's behalf**: the fiscal year start is not
   confirmed in the pack, so creating a company for this market is refused until
   somebody confirms it — and a confirmed month is taken from the caller, never
   defaulted
4. the pack carries the tax rules, statutory deduction rules, statutory report
   definitions, holiday calendar and bank file format §3 asks for, and no market
   beyond the Philippines is invented
5. the fiscal year start is stored **with the pack** once confirmed, so the next
   run reads it rather than asking again

**Scratch database only**: it drops and recreates the schema.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger import posting  # noqa: E402,F401 — every check builds the one schema
from app.localization import (  # noqa: E402
    ACCOUNT_CLASSES,
    PACKS_DIR,
    PackError,
    accounts_by_class,
    bank_file_format,
    coa_template,
    company_template,
    fiscal_year_start,
    holidays,
    load_pack,
    packs,
    statutory_rules,
    tax_rules,
)

MARKET = "philippines"
PROBE = "_probe"


def _refused(call, expected: str) -> str:
    try:
        call()
    except PackError as exc:
        assert expected in str(exc), f"unclear error: {exc}"
        return str(exc)
    raise AssertionError(f"accepted what it must refuse ({expected!r})")


def _probe_pack(mutate) -> str:
    """Write a broken copy of the pack and return the message refusing it."""
    source = PACKS_DIR / MARKET / "pack.json"
    broken = json.loads(source.read_text())
    mutate(broken)
    directory = PACKS_DIR / PROBE
    directory.mkdir(exist_ok=True)
    (directory / "pack.json").write_text(json.dumps(broken))
    try:
        return _refused(lambda: load_pack(PROBE), "")
    finally:
        shutil.rmtree(directory)


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is required (a scratch Postgres)", file=sys.stderr)
        return 2

    # 4 — no market beyond the Philippines is invented
    assert packs() == [MARKET], f"the platform ships packs for {packs()}"

    # 2 — the pack loads, and its CoA is import-ready
    pack = load_pack(MARKET)
    assert pack["market"] == MARKET and pack["version"] == "1.0.0", pack
    accounts = coa_template(MARKET)
    assert len(accounts) >= 40, f"the CoA template is thin: {len(accounts)} accounts"
    codes = [account["code"] for account in accounts]
    assert len(set(codes)) == len(codes), "an account code repeats"
    grouped = accounts_by_class(MARKET)
    assert set(grouped) == set(ACCOUNT_CLASSES), grouped
    assert all(grouped[name] for name in ACCOUNT_CLASSES), "a class holds no account"
    print(
        f"{pack['version']} loads: {len(accounts)} accounts, "
        + ", ".join(f"{name} {len(grouped[name])}" for name in ACCOUNT_CLASSES)
    )

    # 1 — a broken pack is refused with its reason, not loaded hopefully
    problems = {
        "repeated": _probe_pack(
            lambda broken: broken["coa_template"]["accounts"].append(
                dict(broken["coa_template"]["accounts"][0])
            )
        ),
        "unknown class": _probe_pack(
            lambda broken: broken["coa_template"]["accounts"][0].update({"class": "capital"})
        ),
        "parent": _probe_pack(
            lambda broken: broken["coa_template"]["accounts"][1].update({"parent": "9999"})
        ),
        "rate": _probe_pack(lambda broken: broken["tax_rules"][0].update({"rate_percent": 112})),
        "basis": _probe_pack(lambda broken: broken["statutory_rules"][0].update({"basis": ""})),
        "undated": _probe_pack(lambda broken: broken["holidays"][0].update({"date": ""})),
    }
    for label, message in problems.items():
        print(f"a pack with a bad {label} is refused: {message[:70]}")

    # 3 — the fiscal year start is not assumed
    message = _refused(
        lambda: fiscal_year_start(MARKET),
        "fiscal year start is not confirmed",
    )
    print(f"creating a company waits for the pack: {message[:80]}")
    assert fiscal_year_start(MARKET, confirmed=1) == 1, "a confirmed month was not taken"
    assert fiscal_year_start(MARKET, confirmed=7) == 7
    _refused(lambda: fiscal_year_start(MARKET, confirmed=13), "not a month")

    # 4 — the pack carries what §3 asks for
    assert len(tax_rules(MARKET)) >= 5 and tax_rules(MARKET, "supplier_invoice")
    assert len(statutory_rules(MARKET)) >= 6
    assert len(statutory_rules(MARKET, "contribution")) >= 6
    assert len(statutory_rules(MARKET, "withholding")) >= 2
    assert len(pack["statutory_reports"]) >= 7
    assert len(holidays(MARKET, 2026)) == len(pack["holidays"]) >= 15
    assert bank_file_format(MARKET)["columns"]
    print(
        f"the pack carries {len(tax_rules(MARKET))} tax rules, "
        f"{len(statutory_rules(MARKET))} statutory rules, "
        f"{len(pack['statutory_reports'])} statutory reports, "
        f"{len(pack['holidays'])} holidays and the bank file format"
    )

    # 5 — a confirmed start is written with the pack, so the next run reads it
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    Base.metadata.create_all(engine)

    company_id = uuid.uuid4()
    with Session(engine) as session:
        # Creating the company is T-0.CORE.03's business — and its master demands
        # the fiscal year start, which is why the pack must answer before this
        # line can be written at all.
        summary = company_template(MARKET, company_id=company_id, fiscal_year_start_month=1)
        session.add(
            Company(
                id=company_id,
                code="PH-COMPANY",
                name="Philippines test company",
                base_currency=summary["base_currency"],
                fiscal_year_start_month=summary["fiscal_year_start"],
            )
        )
        session.commit()
        assert session.get(Company, company_id).base_currency == "PHP"

    confirmed = json.loads((PACKS_DIR / MARKET / "pack.json").read_text())
    assert confirmed["fiscal_year_start"] is None, (
        "the pack was edited during a check — the answer comes from the pack's owner,"
        " not from a test run"
    )
    # The confirmed month is stored with the pack when its owner confirms it; the
    # check proves the reader prefers that over asking again, without editing the
    # shipped pack.
    confirmed["fiscal_year_start"] = {"month": 1, "confirmed_on": "2026-09-17"}
    directory = PACKS_DIR / PROBE
    directory.mkdir(exist_ok=True)
    (directory / "pack.json").write_text(json.dumps(confirmed))
    try:
        assert fiscal_year_start(PROBE) == 1, "a confirmed pack was not read"
        print("a pack whose fiscal year start is confirmed answers without asking again")
    finally:
        shutil.rmtree(directory)

    engine.dispose()
    print("ok — the pack structure is validated and the Philippines pack is complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
