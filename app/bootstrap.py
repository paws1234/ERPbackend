"""Installation — the schema, one company, its chart of accounts, and its first administrator.

    DATABASE_URL=postgresql+psycopg://... python -m app.bootstrap \\
        --company-code ACME --company-name "Acme Trading" \\
        --market philippines --fiscal-year-start-month 1 \\
        --admin-subject you@example.com

**Why this module exists.** Every task in the ledger was verified against a *check*, and a check
builds its own schema with ``Base.metadata.create_all`` before it does anything (T-0.CORE.02).
No task owned *installing* the platform, so the stack T-0.DEPLOY.03 brings up came online as
three healthy containers over a database with no tables at all: ``/api/v1/health`` answered and
every other route failed with ``relation ... does not exist``. The one script that could have
stood in for an installer, ``tests/compose_seed.py``, lived in the test tree — not in the image,
which copies ``app/`` alone — and grew stale there instead of being run.

**What it does, in one transaction.** Create the schema (the company-scoping policies of
:mod:`app.db` and the audit triggers of :mod:`app.audit` ride on the metadata's own
``after_create`` hooks, so they land here rather than in a step of their own); create the
company, stating its market, its fiscal calendar and its reporting currency; import that
market's chart of accounts (T-0.LOC.01's pack); and, when a subject is named, give it the
capabilities the application declares. One transaction for the rows, so a refusal — a company
code already taken, a pack that leaves a section empty, a fiscal year start nobody has
confirmed — leaves nothing behind instead of half an install.

**What it deliberately is not.** Not a migration tool: ``create_all`` adds the tables it does
not find and never alters one it does, which is the ceiling :mod:`app.db` records for the schema
it builds. Not an HTTP route either — the capability model is per company, so a create-company
request has no company to be scoped to and no role that could hold the capability; onboarding is
an operator's command, where the operator is whoever holds the database credentials.

**Ceilings, recorded rather than discovered later.**
* The first administrator holds **every capability the application declares**, read out of the
  guards themselves (:func:`declared_capabilities`). That is a policy, and it is the honest one
  available here: the first caller of a fresh install holds nothing, and there is no route that
  manages roles, so an install without it answers ``403`` to everything with no way in.
  ``--capability`` narrows it, and once the first administrator exists the ordinary model
  applies — further roles, narrower grants, field restrictions (T-0.SEC.01).
* No TLS, reverse proxy or rate limit. Those stay the deployment stack's, as
  ``SECURITY-REVIEW.md`` records.
"""

from __future__ import annotations

import argparse
import importlib
import os
import re
import sys
import uuid
from pathlib import Path

from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session

from app.audit import set_actor
from app.company import Company
from app.db import Base, scope_to_company
from app.ledger.accounts import import_coa_template
from app.localization import PackError, company_template
from app.localization import packs as installed_packs
from app.security import Role, assign, define_role, grant

# Who the audit trail records for the install itself, unless the operator names someone.
DEFAULT_ACTOR = "bootstrap"

# The role the first administrator holds, as (code, name).
ADMINISTRATOR = ("administrator", "Administrator")

# A module declares a table with this assignment; that is what makes it a model module.
_TABLE = re.compile(r"^[ \t]*__tablename__[ \t]*=", re.MULTILINE)
# A capability is stated in the guard that enforces it — a `require(...)` call, a dashboard
# tile's own capability — or as a module constant where a helper asks for it (`portal.supplier`).
_CAPABILITY = re.compile(r'capability="([a-z][a-z_.]*)"')
_CAPABILITY_CONSTANT = re.compile(
    r'^CAPABILITY[ \t]*=[ \t]*"([a-z][a-z_.]*)"', re.MULTILINE
)


class BootstrapError(RuntimeError):
    """The install cannot be made as asked. Nothing is written."""


def _package_dir() -> Path:
    """Where this application's modules live, for the two source reads below."""
    return Path(__file__).resolve().parent


def model_modules() -> list[str]:
    """The import path of every module under ``app/`` that declares a table.

    Read from the tree rather than listed here, for the reason the schema can be trusted at
    all: a model module added by a later phase is picked up by *running* the install, not by
    remembering to extend a list. Importing a module that declares nothing new is a no-op, so
    the cost is one read per file, once.
    """
    package = __package__ or "app"
    root = _package_dir()
    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if not _TABLE.search(path.read_text()):
            continue
        parts = list(path.relative_to(root).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        found.append(".".join((package, *parts)))
    return found


def declared_capabilities() -> list[str]:
    """Every capability the application declares, read out of the guards themselves.

    The vocabulary is not a list anywhere in this tree — it is the string each ``require(...)``,
    each dashboard tile and the portal's module constant state. Reading it from the source is
    what stops a role created here from silently lacking the capability a later phase adds,
    which would be a ``403`` with nothing in it to explain why. ``tests/check_bootstrap.py``
    holds the read to the access matrix ``SECURITY-REVIEW.md`` records, so a miss is red.
    """
    root = _package_dir()
    found: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        source = path.read_text()
        found.update(_CAPABILITY.findall(source))
        found.update(_CAPABILITY_CONSTANT.findall(source))
    return sorted(found)


def create_schema(engine: Engine) -> list[str]:
    """Create every table the models declare; return the modules that were imported for it.

    Importing every model module first is the whole trick: ``create_all`` builds what the shared
    metadata knows, so a module nobody imported is a table nobody creates — and that failure
    arrives as an error at the first request against the table, in production, rather than here.

    Idempotent — and that is a property that had to be built, not assumed. ``create_all`` checks
    before it creates, but the DDL that rides on the metadata's ``after_create`` hooks runs on
    *every* call, not only on a table's own creation: the scoping policies (:mod:`app.db`) and
    the audit triggers (:mod:`app.audit`). Both now replace what they find rather than failing on
    it, so a second install is a no-op instead of ``DuplicateObject`` — which is what a failed
    install, retried, used to meet.
    """
    imported = model_modules()
    for name in imported:
        importlib.import_module(name)
    Base.metadata.create_all(engine)
    return imported


def create_company(
    session: Session,
    *,
    code: str,
    name: str,
    market: str,
    fiscal_year_start_month: int | None = None,
    base_currency: str | None = None,
) -> Company:
    """Create one company and import its chart of accounts from ``market``'s pack.

    The market decides what a company starts with, and it is asked first
    (:func:`app.localization.company_template`), so a company is never created in a market whose
    pack leaves a section empty or whose calendar is short.

    Nothing here invents a default. ``fiscal_year_start_month`` is handed to the pack as the
    *confirmed* month, and the Philippines pack still carries ``null`` for it (plan §8 leaves it
    open), so an install that has not decided is refused by the pack rather than given an
    invented January — the same refusal :func:`app.localization.fiscal_year_start` states.
    ``base_currency`` follows the pack unless the caller states otherwise.

    The company row is flushed before anything is filed under it, and the session is scoped to it
    before the accounts are created: every row below carries the company dimension, and a
    database that enforces it as the rows are written (the row-level policies of :mod:`app.db`)
    refuses them otherwise. No commit here — the caller owns the transaction.
    """
    company_code = str(code).strip()
    if not company_code:
        raise BootstrapError("a company needs a code: the short key a person types")
    company_name = str(name).strip()
    if not company_name:
        raise BootstrapError("a company needs a name")

    taken = session.scalar(select(Company).where(Company.code == company_code))
    if taken is not None:
        raise BootstrapError(
            f"company {company_code!r} already exists ({taken.name!r}) — this is an install,"
            " not a migration, and it will not touch an existing company"
        )

    company_id = uuid.uuid4()
    template = company_template(
        market, company_id=company_id, fiscal_year_start_month=fiscal_year_start_month
    )
    currency = str(base_currency or template["base_currency"]).strip().upper()
    if len(currency) != 3:
        raise BootstrapError(f"{currency!r} is not a three-letter currency code")

    company = Company(
        id=company_id,
        code=company_code,
        name=company_name,
        base_currency=currency,
        fiscal_year_start_month=template["fiscal_year_start"],
    )
    session.add(company)
    session.flush()
    scope_to_company(session, company.id)
    import_coa_template(session, company_id=company.id, market=market)
    return company


def grant_administrator(
    session: Session,
    *,
    company_id: uuid.UUID,
    subject: str,
    capabilities: list[str] | None = None,
) -> Role:
    """Give one subject the capabilities the application declares, or the ones named.

    The role is a row like any other, so an install that wants to start narrower says so with
    ``capabilities`` — and everything after the first administrator is the ordinary model
    (T-0.SEC.01): further roles, narrower grants, field restrictions.
    """
    admin = str(subject).strip()
    if not admin:
        raise BootstrapError(
            "an administrator needs a subject: the actor the requests will name"
        )
    wanted = sorted(set(capabilities) if capabilities else declared_capabilities())
    if not wanted:
        raise BootstrapError(
            "no capabilities were found to grant, so the install would refuse every request —"
            " the application's guards declare none"
        )
    role = define_role(
        session, company_id=company_id, code=ADMINISTRATOR[0], name=ADMINISTRATOR[1]
    )
    grant(session, role, *wanted)
    assign(session, company_id=company_id, subject=admin, role=role)
    return role


def bootstrap(
    engine: Engine,
    *,
    company_code: str,
    company_name: str,
    market: str,
    fiscal_year_start_month: int | None = None,
    base_currency: str | None = None,
    admin_subject: str | None = None,
    capabilities: list[str] | None = None,
    actor: str = DEFAULT_ACTOR,
) -> uuid.UUID:
    """Install: the schema, one company with its chart, and — when a subject is named — its first
    administrator. Returns the company id, which is what a client states as ``X-Company-Id``.

    The schema is created outside the transaction the rows are written in: DDL is not rolled back
    with the rows, and leaving the tables behind is a state a retry handles (``create_all`` is
    idempotent). The company and everything filed under it are one transaction, so an install is
    all or nothing.
    """
    create_schema(engine)
    with Session(engine) as session:
        # The trail records who installed the platform, like every other change.
        set_actor(session, actor)
        company = create_company(
            session,
            code=company_code,
            name=company_name,
            market=market,
            fiscal_year_start_month=fiscal_year_start_month,
            base_currency=base_currency,
        )
        if admin_subject:
            grant_administrator(
                session,
                company_id=company.id,
                subject=admin_subject,
                capabilities=capabilities,
            )
        session.commit()
        return company.id


def main(argv: list[str] | None = None) -> int:
    """The command an operator runs, inside the stack's own backend image.

        docker compose run --rm backend python -m app.bootstrap --company-code ... 
    """
    parser = argparse.ArgumentParser(
        prog="python -m app.bootstrap",
        description=(
            "Install: create the schema, one company, its chart of accounts from the market's"
            " pack, and the company's first administrator."
        ),
    )
    parser.add_argument(
        "--company-code", required=True, help="the short key a person types; unique in the install"
    )
    parser.add_argument("--company-name", required=True)
    parser.add_argument(
        "--market",
        required=True,
        choices=installed_packs(),
        help="the localization pack this company is installed in",
    )
    parser.add_argument(
        "--fiscal-year-start-month",
        type=int,
        help=(
            "the month this company's fiscal year opens in; the pack refuses to guess it, so a"
            " market whose calendar is unconfirmed has to state it here"
        ),
    )
    parser.add_argument(
        "--base-currency", help="reporting currency; the pack's own unless stated"
    )
    parser.add_argument(
        "--admin-subject",
        help="the subject the first administrator's role is assigned to (omit for a company with no administrator)",
    )
    parser.add_argument(
        "--capability",
        action="append",
        dest="capabilities",
        metavar="NAME",
        help=(
            "narrow the administrator to this capability (repeatable); by default it holds every"
            " capability the application declares"
        ),
    )
    parser.add_argument(
        "--actor",
        default=DEFAULT_ACTOR,
        help=f"who the audit trail records for the install (default: {DEFAULT_ACTOR})",
    )
    args = parser.parse_args(argv)

    url = os.environ.get("DATABASE_URL")
    if not url:
        print(
            "DATABASE_URL is required: the install has to be told which database to build",
            file=sys.stderr,
        )
        return 2

    engine = create_engine(url)
    try:
        company_id = bootstrap(
            engine,
            company_code=args.company_code,
            company_name=args.company_name,
            market=args.market,
            fiscal_year_start_month=args.fiscal_year_start_month,
            base_currency=args.base_currency,
            admin_subject=args.admin_subject,
            capabilities=args.capabilities,
            actor=args.actor,
        )
    except (BootstrapError, PackError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1

    print(f"company {company_id} installed for {args.company_code!r}")
    if args.admin_subject:
        print(
            f"administrator {args.admin_subject!r} holds "
            f"{len(args.capabilities) if args.capabilities else len(declared_capabilities())}"
            " capability(ies) in it"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - the entry point itself
    raise SystemExit(main())
