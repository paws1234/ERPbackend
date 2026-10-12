"""T-8.MIG.01 check — `alembic upgrade head` builds the schema the models declare.

    DATABASE_URL=postgresql+psycopg://… python tests/check_migrations.py

The migration tree (`alembic.ini`, `migrations/`) is a **second entry point** into this
schema. The first is `Base.metadata.create_all`, which every other check uses: it builds the
tables from the metadata and then fires the `after_create` hooks that install what is *not*
a table — the company-scoping policies of `app/db.py`, the audit and ledger triggers — while
the install CLI (`app/bootstrap.py`) walks the migration tree. Two entry points that produce
two schemas would mean a deployment and a check disagree about what the application runs on.

`alembic check` covers the half Alembic can compare (tables, columns, indexes, constraints)
but not the DDL those hooks install, which is not in the metadata it reads. So the migrated
schema is compared here **against a schema `create_all` really built** — every model table,
its columns and their shapes, row-level security where `app/db.py` turns it on, its policy and
every trigger — which is the property `migrations/env.py` and `app/db.py` both point at.

It fails (non-zero exit) if any of these stops holding:

1. `alembic upgrade head` on an empty database succeeds — as an operator's command, in its
   own process, with `DATABASE_URL` naming that database
2. the schema it leaves is the `create_all` schema: every table the models declare, its
   columns and their shapes, row-level security where `app/db.py` turns it on, its policy,
   and every trigger `app/audit.py` and the ledger install
3. `alembic check` reports no drift against the migrated schema — the migrated schema is the
   metadata's schema, so a model that arrives without a migration is an operation Alembic
   would run rather than a silent difference
4. a second `alembic upgrade head` changes nothing — the migration is idempotent, which is
   what lets `python -m app.bootstrap` run twice (T-8.MIG.02)
5. a model the migration tree has no revision for **fails** `alembic check`, naming it — which
   is what makes the build red when a model arrives with no migration

**Two throwaway databases of its own.** The comparison needs two schemas at once — the
migrated one and the `create_all` one — and this migration environment scopes everything to
`public` (`migrations/env.py` reads `nspname = 'public'`), so two schemas cannot share one
database. `DATABASE_URL` is used to *reach the server*; nothing here reads or writes the
database it names, so this check is safe to run beside the others on the same scratch
server. The databases are created and dropped by this file, and dropping needs `CREATEDB`.

Scratch databases only: both are dropped at the end, and neither is `DATABASE_URL`'s.
"""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from sqlalchemy import create_engine, make_url
from sqlalchemy.engine import Engine

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.bootstrap import model_modules  # noqa: E402
from app.db import Base  # noqa: E402

ALEMBIC = [sys.executable, "-m", "alembic", "-c", str(ROOT / "alembic.ini")]

# The model the drift case adds: a table the migration tree has no revision for. Written into
# a *copy* of the tree (below), so the tree under test is left as it was found.
PROBE_MODULE = '''"""The drift case of `tests/check_migrations.py` — a model no migration has."""

from sqlalchemy import Column, Integer

from app.db import Base


class DriftProbe(Base):
    """A table the migration tree has no revision for."""

    __tablename__ = "drift_probe"

    id = Column(Integer, primary_key=True)
'''


def child_database(url: str, name: str) -> str:
    """The same server, one database over."""
    return make_url(url).set(database=name).render_as_string(hide_password=False)


def child_env(url: str) -> dict[str, str]:
    """This process's environment pointed at `url` — and without an inherited `PYTHONPATH`.

    `alembic.ini` puts the tree it belongs to on the child's `sys.path` itself
    (`prepend_sys_path`), and the drift case's *copy* is only the tree under test if its `app/`
    is the one imported; a `PYTHONPATH` inherited from the caller could shadow it.
    """
    env = {name: value for name, value in os.environ.items() if name != "PYTHONPATH"}
    env["DATABASE_URL"] = url
    return env


def create_database(admin: Engine, name: str) -> None:
    # ponytail: this check creates two databases, so it needs CREATEDB on the scratch server
    # (`DATABASE_URL`'s own database is untouched). Upgrade path: give a deployment's check
    # role CREATEDB, or run the comparison against a database it may reset.
    with admin.execution_options(isolation_level="AUTOCOMMIT").connect() as connection:
        connection.exec_driver_sql(f'CREATE DATABASE "{name}"')


def drop_database(admin: Engine, name: str) -> None:
    # FORCE: a connection left open by a failed run must not keep the drop from happening.
    with admin.execution_options(isolation_level="AUTOCOMMIT").connect() as connection:
        connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def alembic(url: str, *args: str) -> subprocess.CompletedProcess:
    """Run the migration command in its own process, against the database `url` names.

    Its own process on purpose: `migrations/env.py` reads the URL from `app/config.py`, which
    reads it at import, so a run that shared this process's environment could not be pointed
    at a second database — and the operator's command is the CLI anyway.
    """
    return subprocess.run(
        [*ALEMBIC, *args],
        cwd=ROOT,
        env=child_env(url),
        capture_output=True,
        text=True,
    )


def drift_check(url: str) -> subprocess.CompletedProcess:
    """Run `alembic check` where a model has arrived with no migration.

    In a copy of the tree, holding one extra module the migration has never seen — the
    situation behind principle 2 and the ceiling `app/db.py` discharged — and with the real
    command, because "the build fails" is a claim about `alembic check`'s exit code and not
    about this file's reading of it. A copy, so the tree under test is left exactly as found.
    """
    with tempfile.TemporaryDirectory(prefix="erpv1-drift-") as tmp:
        copy = Path(tmp)
        shutil.copy2(ROOT / "alembic.ini", copy / "alembic.ini")
        for part in ("migrations", "app"):
            shutil.copytree(ROOT / part, copy / part, ignore=shutil.ignore_patterns("__pycache__"))
        (copy / "app" / "_drift_probe.py").write_text(PROBE_MODULE)
        return subprocess.run(
            [sys.executable, "-m", "alembic", "-c", str(copy / "alembic.ini"), "check"],
            cwd=copy,
            env=child_env(url),
            capture_output=True,
            text=True,
        )


def snapshot(url: str) -> dict[str, object]:
    """What the migrated and `create_all` schemas must agree on, read out of the catalog."""
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            tables = {
                row[0]: row[1]
                for row in connection.exec_driver_sql(
                    "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity"
                    "  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                    " WHERE n.nspname = 'public' AND c.relkind = 'r'"
                )
            }
            columns = {
                (row[0], row[1]): row[2:]
                for row in connection.exec_driver_sql(
                    "SELECT table_name, column_name, data_type, is_nullable, column_default,"
                    "       character_maximum_length, numeric_precision, numeric_scale"
                    "  FROM information_schema.columns WHERE table_schema = 'public'"
                )
            }
            policies = {
                (row[0], row[1]): (row[2], row[3])
                for row in connection.exec_driver_sql(
                    "SELECT tablename, policyname, qual, with_check FROM pg_policies"
                    " WHERE schemaname = 'public'"
                )
            }
            triggers = {
                (row[0], row[1]): row[2]
                for row in connection.exec_driver_sql(
                    "SELECT c.relname, t.tgname, pg_get_triggerdef(t.oid)"
                    "  FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid"
                    "  JOIN pg_namespace n ON n.oid = c.relnamespace"
                    " WHERE n.nspname = 'public' AND NOT t.tgisinternal"
                )
            }
    finally:
        engine.dispose()
    return {
        "tables": tables,
        "columns": columns,
        "policies": policies,
        "triggers": triggers,
    }


def compare(migrated: dict, created: dict, model_tables: set[str]) -> list[str]:
    """Every difference between the two schemas, named — empty when they are one schema."""
    problems: list[str] = []
    for table in sorted(model_tables):
        if table not in migrated["tables"]:
            problems.append(f"{table}: the migration does not create it")
            continue
        if table not in created["tables"]:
            problems.append(f"{table}: create_all does not create it")
            continue
        if migrated["tables"][table] != created["tables"][table]:
            problems.append(
                f"{table}: row-level security differs — migrated"
                f" {migrated['tables'][table]}, create_all {created['tables'][table]}"
            )

        mine = {key: value for key, value in migrated["columns"].items() if key[0] == table}
        theirs = {key: value for key, value in created["columns"].items() if key[0] == table}
        for key in sorted(set(mine) | set(theirs)):
            if key not in mine:
                problems.append(f"{table}.{key[1]}: create_all has it, the migration does not")
            elif key not in theirs:
                problems.append(f"{table}.{key[1]}: the migration has it, create_all does not")
            elif mine[key] != theirs[key]:
                problems.append(
                    f"{table}.{key[1]}: migrated {mine[key]}, create_all {theirs[key]}"
                )

        for what, keyed in (("policy", "policies"), ("trigger", "triggers")):
            mine = {
                key: value
                for key, value in migrated[keyed].items()
                if key[0] == table
            }
            theirs = {
                key: value
                for key, value in created[keyed].items()
                if key[0] == table
            }
            for key in sorted(set(mine) | set(theirs)):
                if key not in mine:
                    problems.append(f"{table}: create_all has the {what} {key[1]!r}, the migration does not")
                elif key not in theirs:
                    problems.append(f"{table}: the migration has the {what} {key[1]!r}, create_all does not")
                elif mine[key] != theirs[key]:
                    problems.append(
                        f"{table}: the {what} {key[1]!r} differs — migrated {mine[key]},"
                        f" create_all {theirs[key]}"
                    )
    return problems


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is required (a scratch Postgres to create two databases on)", file=sys.stderr)
        return 2

    # The metadata this comparison is made of: every module that declares a table, imported
    # here so the tables are known by this check rather than by whichever module happened to
    # be imported first.
    for name in model_modules():
        importlib.import_module(name)
    model_tables = set(Base.metadata.tables)

    suffix = uuid.uuid4().hex[:8]
    migrated_name, created_name = f"erpv1_migrated_{suffix}", f"erpv1_create_all_{suffix}"
    migrated_url, created_url = (
        child_database(url, migrated_name),
        child_database(url, created_name),
    )
    # `DATABASE_URL`'s own database, which is the one connection this check is sure exists;
    # it is used to create the two below and for nothing else.
    admin = create_engine(url)

    try:
        create_database(admin, migrated_name)
        create_database(admin, created_name)
        print(f"two empty databases: {migrated_name} and {created_name}")

        # 1 — the operator's command, on an empty database, in its own process
        upgraded = alembic(migrated_url, "upgrade", "head")
        assert upgraded.returncode == 0, (
            f"alembic upgrade head failed on an empty database:\n"
            f"{upgraded.stdout}{upgraded.stderr}"
        )
        print(f"alembic upgrade head left {len(snapshot(migrated_url)['tables'])} tables")

        # 3 — the migrated schema is the metadata's schema, by Alembic's own comparison
        checked = alembic(migrated_url, "check")
        assert checked.returncode == 0, (
            "alembic check reports drift between the migrated schema and the models, so a"
            f" model has no migration:\n{checked.stdout}{checked.stderr}"
        )
        assert "no new upgrade operations" in (checked.stdout + checked.stderr).lower(), (
            f"alembic check said something other than 'no new upgrade operations':"
            f"\n{checked.stdout}{checked.stderr}"
        )
        print("alembic check: no drift between the migrated schema and the models")

        # 4 — a second upgrade is a no-op, which is what makes the install idempotent
        before = snapshot(migrated_url)
        again = alembic(migrated_url, "upgrade", "head")
        assert again.returncode == 0, (
            f"a second alembic upgrade head failed:\n{again.stdout}{again.stderr}"
        )
        after = snapshot(migrated_url)
        assert before == after, (
            "a second alembic upgrade head changed the schema:"
            f" {_first_difference(before, after)}"
        )
        print("a second `upgrade head` changed nothing")

        # 2 — the same schema from the other entry point, compared table by table
        created_engine = create_engine(created_url)
        try:
            Base.metadata.create_all(created_engine)
        finally:
            created_engine.dispose()
        migrated, created = snapshot(migrated_url), snapshot(created_url)

        unowned = sorted(set(migrated["tables"]) - model_tables)
        assert unowned, (
            "the migrated schema has no installation-owned tables at all, so this check is not"
            " comparing what it thinks it is (alembic_version and the queue's own are expected)"
        )
        assert not (set(created["tables"]) - model_tables), (
            "create_all built tables no model declares:"
            f" {sorted(set(created['tables']) - model_tables)}"
        )
        problems = compare(migrated, created, model_tables)
        assert not problems, (
            f"the migrated schema and the create_all schema differ in {len(problems)} place(s):\n  "
            + "\n  ".join(problems)
        )
        print(
            f"the two schemas are one schema: {len(model_tables)} model tables, table by table,"
            f" column by column, policy by policy and trigger by trigger"
        )
        print(
            f"the migration additionally owns {len(unowned)} table(s) the models do not declare:"
            f" {', '.join(unowned)}"
        )

        # 5 — a model with no migration is reported, not absorbed
        drifted = drift_check(migrated_url)
        assert drifted.returncode != 0, (
            "alembic check passed with a model the migration tree has no revision for, so such"
            f" a model would not fail the build:\n{drifted.stdout}{drifted.stderr}"
        )
        assert "drift_probe" in drifted.stdout + drifted.stderr, (
            "alembic check failed without naming the model it has no migration for"
            f" ('drift_probe'):\n{drifted.stdout}{drifted.stderr}"
        )
        print(
            "a model with no migration ('drift_probe') is reported by alembic check and fails"
            " it — a table added without a revision is a red build"
        )

        print("ok — the migration builds the models' schema, idempotently, and drift fails the build")
        return 0
    finally:
        drop_database(admin, migrated_name)
        drop_database(admin, created_name)
        admin.dispose()


def _first_difference(before: dict, after: dict) -> str:
    """The first thing two snapshots disagree about, for the assertion message."""
    for key, value in before.items():
        if key not in after:
            return f"{key} is gone"
        for entry in sorted(set(value) | set(after[key]), key=str):
            if value.get(entry) != after[key].get(entry):
                return f"{key}[{entry!r}]: {value.get(entry)!r} → {after[key].get(entry)!r}"
    return "nothing obvious — compare the snapshots"


if __name__ == "__main__":
    raise SystemExit(main())
