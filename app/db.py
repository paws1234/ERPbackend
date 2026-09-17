"""Shared SQLAlchemy declarative base — one metadata for every model module.

Every model in this repository inherits from ``Base`` so that a single
``create_all`` / migration covers the whole schema.

The company scoping convention (T-0.CORE.03, :mod:`app.company`) is registered
here rather than inside a model module, because it is a property of the *schema*
and not of any one table — a module that forgot to import it could otherwise
build an unscoped schema. It has two halves:

* **Every table carries the company dimension.** A table is company-scoped when
  it has a ``company_id`` column. A table without one is allowed only if it is
  declared in ``GLOBAL_TABLES`` (no company owns its rows) or in ``CHILD_TABLES``
  (isolated through its parent). Anything else fails the build, so a new table
  cannot be added without a deliberate decision about which company owns it.
* **Isolation is the database's job, not each caller's memory.** Creating the
  schema turns on row-level security on every company-scoped table, with a policy
  comparing the row's ``company_id`` with the company the session is bound to
  (:func:`scope_to_company`). A session with no company bound sees no rows at
  all, and neither the ORM nor raw SQL can read around the policy. The table
  owner bypasses RLS, as Postgres intends — that is the migration / integrity
  gate role.

Postgres-only by design, like the ledger's constraint triggers: on any other
backend the DDL fails loudly instead of leaving isolation unenforced.
"""

from __future__ import annotations

import uuid

from sqlalchemy import MetaData, event
from sqlalchemy.orm import DeclarativeBase, Session

# The column that scopes a row to one company.
COMPANY_COLUMN = "company_id"

# The Postgres setting a session carries its company in; the policies read it.
COMPANY_SETTING = "app.company_id"

# Tables that carry no company dimension, with the reason. Being absent from both
# maps is what fails the build, so adding a table here is the deliberate act.
GLOBAL_TABLES = {
    # The dimension itself: a company switcher has to be able to list companies.
    "company": "the company master is the dimension every other row points at",
}

# Child tables: no ``company_id`` of their own, isolated through their parent.
# table -> (parent table, the child's foreign key column, the parent's key column)
CHILD_TABLES = {
    "journal_line": ("journal_entry", "entry_id", "id"),
}


class UnscopedTableError(RuntimeError):
    """Raised when a table has no company dimension and is not declared global."""


_TENANT_FUNCTION = f"""
CREATE OR REPLACE FUNCTION current_company() RETURNS uuid
LANGUAGE sql STABLE AS $$
    SELECT nullif(current_setting('{COMPANY_SETTING}', true), '')::uuid
$$;
"""


class Base(DeclarativeBase):
    """Declarative base every ERP model inherits from."""


def _policy_predicate(table) -> str | None:
    """The SQL deciding which rows of `table` a scoped session may touch."""
    if COMPANY_COLUMN in table.c:
        return f"{COMPANY_COLUMN} = current_company()"
    child = CHILD_TABLES.get(table.name)
    if child is not None:
        parent, foreign_key, key = child
        return (
            f"EXISTS (SELECT 1 FROM {parent} p WHERE p.{key} = {table.name}.{foreign_key}"
            f" AND p.{COMPANY_COLUMN} = current_company())"
        )
    return None


def company_scoping_ddl(metadata: MetaData, connection) -> None:
    """Scope every table of `metadata` to one company, or refuse to build.

    Runs from the ``after_create`` hook below; callable directly, which is how
    the T-0.CORE.03 check proves it refuses an unscoped table. Every table is
    validated **before** any DDL is emitted, so a refusal leaves the schema
    exactly as it was.
    """
    predicates = {}
    for table in metadata.tables.values():
        if table.name in GLOBAL_TABLES:
            continue
        predicate = _policy_predicate(table)
        if predicate is None:
            raise UnscopedTableError(
                f"table {table.name!r} has no {COMPANY_COLUMN} and is declared neither"
                " global nor a child of a company-scoped table — declare it in"
                " app/db.py or give it the company dimension"
            )
        predicates[table.name] = predicate

    connection.exec_driver_sql(_TENANT_FUNCTION)
    for name, predicate in predicates.items():
        connection.exec_driver_sql(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY")
        connection.exec_driver_sql(
            f"CREATE POLICY company_isolation ON {name}"
            f" USING ({predicate}) WITH CHECK ({predicate})"
        )


@event.listens_for(Base.metadata, "after_create")
def _scope_created_schema(metadata: MetaData, connection, **_kw) -> None:
    """Apply the company scoping whenever the schema is created."""
    # ponytail: the guard rides on `create_all`, which is how every schema in this
    # repository is built today. Ceiling: a table added later by a migration tool
    # would not pass through here. Upgrade path: when migrations arrive
    # (T-0.CICD.01 / T-0.DEPLOY.01 own the pipeline), call `company_scoping_ddl`
    # from the migration too.
    company_scoping_ddl(metadata, connection)


def scope_to_company(session: Session, company_id: uuid.UUID) -> None:
    """Bind the session's current transaction to one company.

    Isolation itself is the database's (see the module docstring); this only
    states *which* company the transaction is for. The setting is
    transaction-scoped, so it is called once per unit of work by whatever opens
    one — the API layer (T-0.API.01) — and lapses with it.
    """
    session.connection().exec_driver_sql(
        f"SELECT set_config('{COMPANY_SETTING}', %s, true)", (str(company_id),)
    )
