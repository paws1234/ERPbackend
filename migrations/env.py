"""T-8.MIG.01 — the migration environment: what "migrated" means for this schema.

**Why the environment carries DDL.** Two things about this schema are not tables. The
company scoping (`app/db.py`) turns on row-level security on every company-scoped table
and writes its policy; the trail (`app/audit.py`) installs a trigger per audited table;
the ledger installs its constraint triggers on the tables it owns. All of them ride on
SQLAlchemy's ``after_create`` hook, which is why a ``create_all`` schema has them and a
schema built by a migration tool would not — the ceiling ``app/db.py`` recorded against
`company_scoping_ddl` ("a table added later by a migration tool would not pass through
here"), with the upgrade path this file is.

Rather than restate that DDL here — a second copy that would drift — this environment
**replays the metadata's own hooks** (:func:`_install_schema_ddl`): the same listeners
that fire on ``create_all`` are invoked against the migrated schema. The result is
compared, table by table, against a ``create_all`` schema by
``tests/check_migrations.py``: identical policies, identical triggers. One source of
truth, two entry points.

**What is excluded from autogenerate.** ``pgqueuer`` installs its own tables, types,
functions and triggers, which the application's metadata does not own and must not try
to drop; the version table is Alembic's own. Both are named in
:data:`INSTALLATION_OWNED_TABLES`, and the queue's tables are installed here by
pgqueuer's own DDL (:func:`_install_queue`) rather than by a hand-written ``create_table``
that would silently drift from the library's.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import config as app_config  # noqa: E402
from app.bootstrap import model_modules  # noqa: E402
from app.db import Base  # noqa: E402

# Importing every model module is what makes the metadata complete: `create_all` builds
# what the shared metadata knows, and a module nobody imported is a table nobody creates
# (`app/bootstrap.py` states the same rule for the install).
for _module in model_modules():
    importlib.import_module(_module)

target_metadata = Base.metadata

# Tables the application does not own. Autogenerate must not see them: a table that is
# in the database and not in the metadata reads as "removed" and would be dropped, which
# is how a queue library's tables go missing on the next `alembic upgrade head`.
INSTALLATION_OWNED_TABLES = frozenset(
    {
        # Alembic's own book-keeping.
        "alembic_version",
        # pgqueuer's, installed by `_install_queue` below from the library's own DDL.
        # Named here, in the tree that would otherwise drop them, on purpose: a library
        # upgrade that renames one of these has to pass this list.
        "pgqueuer",
        "pgqueuer_log",
        "pgqueuer_statistics",
        "pgqueuer_schedules",
    }
)


def include_name(name: str | None, type_: str, parent_names: dict) -> bool:
    """Keep the installation's own tables out of autogenerate (and so out of `check`)."""
    if type_ == "table":
        return name not in INSTALLATION_OWNED_TABLES
    return True


def run_migrations_offline() -> None:
    """Emit the migration as SQL against a URL, without connecting."""
    context.configure(
        url=app_config.SETTINGS.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_name=include_name,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _present_tables(connection) -> set[str]:
    """The base tables that exist in the public schema right now."""
    rows = connection.exec_driver_sql(
        """
        SELECT c.relname
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND c.relkind = 'r'
        """
    ).fetchall()
    return {row[0] for row in rows}


def _install_schema_ddl(connection) -> None:
    """Install the DDL that rides on ``create_all``, by replaying its own listeners.

    Called after the versioned steps have run, in the same transaction, so a migrated
    schema carries the scoping policies, the trail triggers and the ledger's constraint
    triggers exactly as a ``create_all`` schema does. Idempotent: every one of those
    listeners replaces what it finds (``DROP POLICY IF EXISTS``, ``DROP TRIGGER IF EXISTS``),
    which is what makes it safe to run on every upgrade rather than only on the first.

    Only when the schema is **complete**: a downgrade to ``base`` leaves no tables to
    carrying a policy, and a half-migrated schema is not a schema this DDL describes.
    """
    wanted = {table.name for table in target_metadata.sorted_tables}
    present = _present_tables(connection)
    if not wanted <= present:
        return
    for table in target_metadata.sorted_tables:
        table.dispatch.after_create(table, connection)
    target_metadata.dispatch.after_create(target_metadata, connection)


def _install_queue(connection) -> None:
    """Install the queue's own schema, from the library that owns it.

    The queue is a dependency with tables of its own; a migration that hand-wrote them
    would be a fork of the library's schema. This runs the library's installer instead,
    which is idempotent.
    """
    from app.queue import install_queue_schema

    install_queue_schema(connection)


def run_migrations_online() -> None:
    """Apply the migrations to the configured database, then install the schema's DDL."""
    connectable = engine_from_config(
        {"sqlalchemy.url": app_config.SETTINGS.database_url},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    # `alembic check` and `revision --autogenerate` run this environment to *compare*
    # metadata with the database; the schema's DDL is then already installed (or the
    # comparison is about to fail anyway), and replaying it during a comparison would be
    # a write inside a read.
    cmd_opts = getattr(context.config, "cmd_opts", None)
    comparing = bool(getattr(cmd_opts, "autogenerate", False))
    try:
        with connectable.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                include_name=include_name,
                compare_type=True,
            )
            with context.begin_transaction():
                context.run_migrations()
                if not comparing:
                    _install_schema_ddl(connection)
                    _install_queue(connection)
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
