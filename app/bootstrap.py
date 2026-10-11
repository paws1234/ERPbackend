"""Installation — the schema, one company, its chart of accounts, and its first administrator.

    DATABASE_URL=postgresql+psycopg://... python -m app.bootstrap \\
        --company-code ACME --company-name "Acme Trading" \\
        --market philippines --fiscal-year-start-month 1 \\
        --admin-subject you@example.com --admin-password '…'

**Why this module exists.** Every task in the ledger was verified against a *check*, and a check
builds its own schema with ``Base.metadata.create_all`` before it does anything (T-0.CORE.02).
No task owned *installing* the platform, so the stack T-0.DEPLOY.03 brings up came online as
three healthy containers over a database with no tables at all: ``/api/v1/health`` answered and
every other route failed with ``relation ... does not exist``. The one script that could have
stood in for an installer, ``tests/compose_seed.py``, lived in the test tree — not in the image,
which copies ``app/`` alone — and grew stale there instead of being run.

**What it does, in one transaction.** **Migrate**, then seed: the schema comes from
``alembic upgrade head`` (T-8.MIG.01's tree, which also installs what is not a table — the
company-scoping policies of :mod:`app.db`, the audit and ledger triggers of :mod:`app.audit`,
and the queue's own tables — by replaying the metadata's own ``after_create`` listeners);
then create the company, stating its market, its fiscal calendar and its reporting currency;
import that market's chart of accounts (T-0.LOC.01's pack); and, when a subject is named, give
it the capabilities the application declares. One transaction for the rows, so a refusal — a
company code already taken, a pack that leaves a section empty, a fiscal year start nobody has
confirmed — leaves nothing behind instead of half an install.

**Not a migration tool, and no longer a schema builder.** ``create_all`` used to be how this
installer made its schema, which meant every deployment started on a schema no migration
described (finding 3 of the 2026-10-10 audit). The install now runs the migration tree, so an
installation and a deployment upgrade walk the same path, and a second install of the same
schema is a no-op because ``alembic upgrade head`` is. Not the only way to create a company,
since T-7.ADMIN.03 added ``POST /api/v1/companies`` — but the *first* install still comes from
here, because a create-company request needs a company to be scoped to and an administrator to
hold the capability, and a fresh database has neither.

**Ceilings, recorded rather than discovered later.**
* The first administrator holds **every capability the application declares**, read out of the
  guards themselves (:func:`declared_capabilities`). That is a policy, and it is the honest one
  available here: the first caller of a fresh install holds nothing, and although T-7.ADMIN.02
  now serves the role routes, none of them can be reached before somebody holds the capability
  they ask for. ``--capability`` narrows the first administrator, and once it exists the
  ordinary model applies — further roles, narrower grants, field restrictions (T-0.SEC.01).
* The administrator it creates needs a password to sign in (T-7.AUTH.04): ``--admin-password``,
  or ``ERP_ADMIN_PASSWORD``. An install that names a subject and no password says so rather than
  leaving an account nobody can use.
* No TLS, reverse proxy or rate limit. Those stay the deployment stack's, as
  ``SECURITY-REVIEW.md`` records; T-9.EDGE.01 puts the proxy in front of the application.
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

from app import config
from app.audit import set_actor
from app.company import Company
from app.db import scope_to_company
from app.identity import (
    add_membership,
    create_user,
    set_password,
    user_by_login,
)
from app.ledger.accounts import import_coa_template
from app.localization import PackError, company_template
from app.localization import packs as installed_packs
from app.security import Role, assign, define_role, grant

# Who the audit trail records for the install itself, unless the operator names someone.
DEFAULT_ACTOR = "bootstrap"

# The migration tree this install applies (T-8.MIG.01). The installer builds its Alembic
# config from this file rather than from a copy of the settings, so the CLI an operator runs
# and the install walk exactly the same tree.
ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

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


def run_migrations(engine: Engine) -> None:
    """Apply the migration tree — the same ``alembic upgrade head`` an operator's CLI runs.

    Built programmatically from `alembic.ini` and `migrations/`, with the URL taken from the
    engine this install was given: the command-line tool and the installer therefore walk the
    same tree, and there is no second copy of the DDL for one of them to fall behind on.
    """
    from alembic import command
    from alembic.config import Config

    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_INI.parent / "migrations"))
    config.set_main_option("sqlalchemy.url", engine.url.render_as_string(hide_password=False))
    command.upgrade(config, "head")


def create_schema(engine: Engine) -> list[str]:
    """Bring the database to the schema this build declares; return the modules imported for it.

    Importing every model module first is the whole trick (and stays even though Alembic now
    builds the tables from that same metadata): a module with a syntax error, or one whose
    import fails, is a broken *install* rather than a surprise at the first request against
    its table. The Alembic environment imports them too, in its own process.

    Idempotent — and that is a property that had to be built, not assumed. ``alembic upgrade
    head`` on a database already at head does nothing, and the schema-wide DDL the migration
    environment re-installs (policies, triggers) replaces what it finds rather than failing on
    it. What *was* broken, and is the reason this function no longer calls ``create_all``:
    T-0.DEPLOY.03's stack came up on a schema no migration described, so a deployment had no
    supported way to change a table (finding 3 of the 2026-10-10 audit).
    """
    imported = model_modules()
    for name in imported:
        importlib.import_module(name)
    run_migrations(engine)
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
    password: str | None = None,
) -> Role:
    """Give one subject the capabilities the application declares, or the ones named.

    The role is a row like any other, so an install that wants to start narrower says so with
    ``capabilities`` — and everything after the first administrator is the ordinary model
    (T-0.SEC.01): further roles, narrower grants, field restrictions.

    Since T-7.INST.01 the subject is also given the **identity** that can use the role: a user
    row, a membership of this company, and — when a password is stated — a credential. Before
    that, an install named a subject that no sign-in could ever produce, so the administrator
    it created was a role nobody could hold.
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
    user = user_by_login(session, admin)
    if user is None:
        email = admin if "@" in admin else None
        user = create_user(session, login=admin, email=email, display_name=admin)
    add_membership(session, user=user, company_id=company_id)
    if password is not None:
        set_password(session, user, password)
    elif user.password_hash is None:
        # Stated rather than left implicit: the role is granted and the identity exists, but
        # nobody can sign in as it yet. An operator that wants a usable install states the
        # password (--admin-password), which is the ordinary path.
        print(
            f"note: {admin!r} has no password yet, so it cannot sign in until one is set",
            file=sys.stderr,
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
    admin_password: str | None = None,
    capabilities: list[str] | None = None,
    actor: str = DEFAULT_ACTOR,
) -> uuid.UUID:
    """Install: the schema, one company with its chart, and — when a subject is named — its first
    administrator with the identity that can use it. Returns the company id, which the install
    prints for the operator; a client no longer states it, because the session does
    (T-7.AUTH.02).

    The schema is migrated outside the transaction the rows are written in: DDL is not rolled
    back with the rows, and leaving a migrated-but-empty schema behind is a state a retry
    handles (`alembic upgrade head` on a database already at head does nothing). The company and
    everything filed under it are one transaction, so an install is all or nothing.
    """
    if admin_password is not None and not admin_subject:
        raise BootstrapError("a password is only meaningful with --admin-subject")
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
                password=admin_password,
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
        "--admin-password",
        help=(
            "the password that subject signs in with; without it the administrator exists but"
            " nobody can sign in as it"
        ),
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

    # The URL is read (and validated) at start by `app/config.py`: a malformed one has
    # already stopped the process, naming the variable, rather than reaching the driver.
    engine = create_engine(config.SETTINGS.database_url)
    try:
        company_id = bootstrap(
            engine,
            company_code=args.company_code,
            company_name=args.company_name,
            market=args.market,
            fiscal_year_start_month=args.fiscal_year_start_month,
            base_currency=args.base_currency,
            admin_subject=args.admin_subject,
            admin_password=args.admin_password or os.environ.get("ERP_ADMIN_PASSWORD"),
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
        if not (args.admin_password or os.environ.get("ERP_ADMIN_PASSWORD")):
            print(
                "no --admin-password (or ERP_ADMIN_PASSWORD) was given, so that administrator"
                " cannot sign in yet",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":  # pragma: no cover - the entry point itself
    raise SystemExit(main())
