"""The install check — `python -m app.bootstrap` leaves a database the application can serve.

    DATABASE_URL=postgresql+psycopg://… python tests/check_bootstrap.py

Every other check builds its own schema with `Base.metadata.create_all`, which is why an install
that did nothing at all could pass all 108 of them: the checks were never the thing a deployment
needs. This one starts from an **empty database** and the **command an operator runs** — in its
own process, so the schema is whatever that command built and not whatever this file had already
imported — and then drives the app against what it left. It fails (non-zero exit) if any of these
stops holding:

1. **the install serves.** `python -m app.bootstrap`, and nothing else, produces a database the
   application answers from: the company it created is served, and the chart of accounts it
   imported is the market pack's — every account the pack declares, by code.
2. **the schema is the whole schema.** Every module under `app/` that declares a table is
   imported by the installer (its own scan compared with this file's), and every table those
   modules declare exists in the database the installer left. A module nobody imports is a table
   nobody creates, and the run has to happen in a process that did not import them for it.
3. **the tables the API writes are among them** — driven, not listed: a posting taken through
   the app, with an idempotency key, is answered `201` and replayed, so the key table is written
   rather than merely present. The seed that prompted this check built a *partial* schema and
   failed exactly here.
4. **the install is atomic and refuses a second one.** A company code that is taken is refused by
   name, and nothing is written: the chart, the company and its roles are what they were. Making
   the schema again is a no-op rather than a `DuplicateObject` error.
5. **the administrator can actually do things.** The role holds every capability the application
   declares — including the ones stated outside `app/api.py` (a dashboard tile's, the portal's
   module constant) — and that set covers every capability the access matrix in
   `SECURITY-REVIEW.md` names, so a capability the scan misses is red here rather than a `403`
   nobody can explain.
6. **the pack's open question is not answered for it.** The Philippines pack carries `null` for
   the fiscal year start (plan §8 leaves it open), so an install that does not state the month is
   refused by the pack, with nothing written — not given an invented January.
"""

from __future__ import annotations

import importlib
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, inspect, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.api import BASE, app  # noqa: E402
from app.bootstrap import (  # noqa: E402
    BootstrapError,
    bootstrap,
    create_schema,
    declared_capabilities,
    model_modules,
)
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.localization import PackError, coa_template  # noqa: E402
from app.security import Role  # noqa: E402

MARKET = "philippines"
CODE = "INSTALL-CHECK"
NAME = "Install check trading"
ADMIN = "installer@example.com"

# What the command prints when it installs a company; the id in it is what a client states.
INSTALLED = re.compile(r"^company ([0-9a-f-]{36}) installed for", re.MULTILINE)

REVIEW = ROOT / "SECURITY-REVIEW.md"
TABLE_MARKER = re.compile(r"^[ \t]*__tablename__[ \t]*=", re.MULTILINE)
CAPABILITY_IN_MATRIX = re.compile(r"[a-z_]+\.[a-z_.]+")


def declaring_modules() -> set[str]:
    """The modules that declare a table, read from the tree by this check's own rule.

    A second read of the same tree, not a call to the installer's `model_modules()`: the two
    are compared below so a rule that finds a module in one place and not the other is red. A
    module one of them misses is the difference between a table created and a table absent.
    """
    package = ROOT / "app"
    found = set()
    for path in sorted(package.rglob("*.py")):
        if not TABLE_MARKER.search(path.read_text()):
            continue
        parts = list(path.relative_to(package).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        found.add(".".join(("app", *parts)))
    return found


def matrix_capabilities() -> set[str]:
    """Every capability the access matrix in SECURITY-REVIEW.md names, per route."""
    found: set[str] = set()
    for line in REVIEW.read_text().splitlines():
        if not line.startswith("| `"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        found.update(CAPABILITY_IN_MATRIX.findall(cells[1]))
    return found


def tree_codes(nodes: list[dict]) -> set[str]:
    """Every code in a served account tree, parents and their descendants."""
    codes: set[str] = set()
    for node in nodes:
        codes.add(node["code"])
        codes |= tree_codes(node.get("children") or [])
    return codes


def counts(engine) -> tuple[int, int, int]:
    """Companies, accounts and roles in the database as it stands."""
    from app.ledger.accounts import Account

    with Session(engine) as session:
        return (
            len(session.scalars(select(Company)).all()),
            len(session.scalars(select(Account)).all()),
            len(session.scalars(select(Role)).all()),
        )


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is required (a scratch Postgres)", file=sys.stderr)
        return 2

    engine = create_engine(url)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")

    # What a complete schema is, per this file's own read of the tree: every module that
    # declares a table, imported here so the metadata is *this* check's view of the schema.
    # The installer runs in its own process below, so a module it forgets to import cannot be
    # covered for it by what this process has already imported.
    declared = declaring_modules()
    for name in sorted(declared):
        importlib.import_module(name)
    complete = set(Base.metadata.tables)

    # The installer reads the tree by its own rule; this file reads it by another. A module one
    # finds and the other does not is the difference between a table created and a table absent.
    by_the_installer = set(model_modules())
    assert declared == by_the_installer, (
        "the installer's read of which modules declare a table differs from this file's:"
        f" {sorted(declared ^ by_the_installer)}"
    )

    # 1, 2 — the command an operator runs, on an empty database, in its own process
    installed = subprocess.run(
        [
            sys.executable, "-m", "app.bootstrap",
            "--company-code", CODE,
            "--company-name", NAME,
            "--market", MARKET,
            "--fiscal-year-start-month", "1",
            "--admin-subject", ADMIN,
        ],
        cwd=ROOT,
        env=os.environ,
        capture_output=True,
        text=True,
    )
    assert installed.returncode == 0, (
        f"the documented install command failed:\n{installed.stdout}{installed.stderr}"
    )

    tables = set(inspect(engine).get_table_names())
    absent = sorted(complete - tables)
    assert not absent, (
        f"the install left {len(absent)} table(s) the models declare out of the database:"
        f" {absent}"
    )
    assert not tables - complete, (
        f"the install created tables no model declares: {sorted(tables - complete)}"
    )
    print(
        f"the install created all {len(tables)} tables that the {len(declared)} model modules"
        " declare"
    )

    # The id the command printed is the id a client states, and it names the installed company.
    printed = INSTALLED.search(installed.stdout)
    assert printed is not None, f"the install printed no company id: {installed.stdout!r}"
    company_id = uuid.UUID(printed.group(1))
    with Session(engine) as session:
        company = session.get(Company, company_id)
        assert company is not None and company.code == CODE, (
            f"the printed company id {printed.group(1)} is not the company installed"
        )
    expected_accounts = coa_template(MARKET)
    print(
        f"installed company {company_id} with {len(expected_accounts)} accounts"
        f" from the {MARKET} pack"
    )

    # 5 — the administrator holds the app's capabilities, and the scan reaches past app/api.py
    capabilities = set(declared_capabilities())
    unlisted = sorted(set(matrix_capabilities()) - capabilities)
    assert not unlisted, f"capabilities the access matrix names and the scan misses: {unlisted}"
    for outside_api in ("invoice.read", "stock.read", "portal.supplier"):
        assert outside_api in capabilities, (
            f"{outside_api} is stated outside app/api.py and the scan did not find it"
        )
    with Session(engine) as session:
        role = session.scalar(select(Role).where(Role.code == "administrator"))
        assert role is not None, "the install left no administrator role"
        held = {permission.capability for permission in role.permissions}
    assert held == capabilities, (
        f"the administrator holds {len(held)} of the {len(capabilities)} capabilities the app"
        f" declares; missing {sorted(capabilities - held)}"
    )
    print(
        f"the administrator holds all {len(capabilities)} capabilities the app declares,"
        f" which cover all {len(matrix_capabilities())} the access matrix names"
    )

    # 1 and 3 — the database is served, through the app, on tables the installer created
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"X-Company-Id": str(company_id), "X-Actor": ADMIN}

    served = client.get(f"{BASE}/companies/current", headers=headers)
    assert served.status_code == 200, f"the installed company is not served: {served.text}"
    assert served.json()["name"] == NAME and served.json()["code"] == CODE

    tree = client.get(f"{BASE}/accounts/tree", headers=headers)
    assert tree.status_code == 200, f"the imported chart is not served: {tree.text}"
    codes = tree_codes(tree.json())
    expected_codes = {row["code"] for row in expected_accounts}
    assert codes == expected_codes, (
        f"the chart served is not the pack's: missing {sorted(expected_codes - codes)},"
        f" extra {sorted(codes - expected_codes)}"
    )
    print(f"the company and all {len(codes)} pack accounts are served by the app")

    body = {
        "posting_date": "2026-10-10",
        "currency": "PHP",
        "memo": "install check",
        "lines": [
            {"account": "1000", "debit": "120.00"},
            {"account": "4000", "credit": "120.00"},
        ],
    }
    key = {"Idempotency-Key": f"install-check-{uuid.uuid4()}"}
    posted = client.post(f"{BASE}/journal-entries", headers={**headers, **key}, json=body)
    assert posted.status_code == 201, f"a posting is not taken: {posted.text}"
    replayed = client.post(f"{BASE}/journal-entries", headers={**headers, **key}, json=body)
    assert replayed.status_code == 201 and replayed.json() == posted.json(), (
        "the idempotency key did not replay: the key table the install created is not being used"
    )
    print("a posting is taken and replayed through the app, on the schema the install created")

    # 4 — the install refuses a second one and leaves nothing behind
    before = counts(engine)
    try:
        bootstrap(
            engine,
            company_code=CODE,
            company_name="Second attempt",
            market=MARKET,
            fiscal_year_start_month=1,
            admin_subject="someone@example.com",
        )
    except BootstrapError as exc:
        assert CODE in str(exc), f"the refusal does not name the company code: {exc}"
        print(f"a second install of the same company is refused: {exc}")
    else:
        raise AssertionError("the second install was allowed, so the first was not an install")
    assert counts(engine) == before, "the refused install wrote something anyway"

    repeated = create_schema(engine)
    assert repeated == model_modules() and counts(engine) == before, (
        "creating the schema again is not the no-op a retry needs"
    )
    print("the second install wrote nothing, and creating the schema again is a no-op")

    # 6 — the pack's open question is the pack's to answer, and this one is still open:
    # `fiscal_year_start` refuses the Philippines because the pack carries null for it.
    try:
        bootstrap(
            engine,
            company_code="UNCONFIRMED",
            company_name="Unconfirmed calendar",
            market=MARKET,
            admin_subject=ADMIN,
        )
    except PackError as exc:
        assert "fiscal year start" in str(exc), f"the refusal does not name the question: {exc}"
        print(f"an unconfirmed fiscal year start is refused by the pack: {exc}")
    else:
        raise AssertionError(
            "the install invented a fiscal year start the pack leaves open (plan §8)"
        )
    assert counts(engine) == before, "the refusal wrote something anyway"

    print("ok — one command installs a database the application serves")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
