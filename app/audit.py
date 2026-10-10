"""T-0.AUDIT.01 — append-only ledgers and soft-deleted masters.

Two rules from §3 ("Audit & Immutability"), enforced where the ledger's balance
rule is enforced: **in the database**, so no writer — ORM, script or hand-edit —
can opt out.

* **A ledger is append-only.** A posted journal or stock entry is never updated
  and never deleted; a mistake is corrected by posting a correcting entry.
  :func:`append_only` makes the table refuse both, in the storage boundary.
* **A master is soft-deleted.** Retiring a master marks it (``deleted_at``) and
  leaves the row for the documents that point at it; it is never removed.
  :class:`SoftDeleteMixin` carries the mark and :func:`deny_hard_delete` refuses
  the removal, while :func:`soft_delete` is the only way to retire one. Reads
  exclude soft-deleted masters unless the caller asks for them by name
  (:data:`INCLUDE_SOFT_DELETED`), so the filter lives with the convention instead
  of in every query.

Both halves are registered by the module that owns the table (``app/company.py``,
``app/ledger/posting.py``), the same way company scoping is registered in
:mod:`app.db` — the rule is a property of the schema, not of a caller.

Postgres-only by design, like the ledger's constraint triggers: on any other
backend the DDL fails loudly rather than leaving history editable.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DDL, DateTime, ForeignKey, String, Uuid, event, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, Session, mapped_column, with_loader_criteria

from app.db import COMPANY_COLUMN, Base

# Read option that asks for retired masters too: `include_soft_deleted=True` on
# the session or on one statement. Anything else sees only the live rows.
INCLUDE_SOFT_DELETED = "include_soft_deleted"

# '%%' because SQLAlchemy's DDL wrapper interpolates the statement.
_REFUSE_LEDGER_CHANGE = DDL(
    """
CREATE OR REPLACE FUNCTION refuse_ledger_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '%% is append-only: %% is refused; post a correcting entry instead',
        TG_TABLE_NAME, TG_OP;
END;
$$;
"""
)

_REFUSE_MASTER_DELETE = DDL(
    """
CREATE OR REPLACE FUNCTION refuse_master_delete() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '%% is a master: DELETE is refused; mark it deleted_at instead',
        TG_TABLE_NAME;
END;
$$;
"""
)


def _refusal_trigger(table, label: str, function: str, events: str) -> DDL:
    return DDL(
        f"CREATE TRIGGER {table.name}_{label} BEFORE {events} ON {table.name}"
        f" FOR EACH ROW EXECUTE FUNCTION {function}()"
    )


def append_only(table) -> None:
    """Make `table` refuse every UPDATE and DELETE, in the database.

    For ledgers: journal entries and lines now, stock ledger entries from
    T-1.INV.03. Returns nothing; the refusal is the database's.
    """
    event.listen(table, "after_create", _REFUSE_LEDGER_CHANGE)
    event.listen(
        table,
        "after_create",
        _refusal_trigger(table, "append_only", "refuse_ledger_change", "UPDATE OR DELETE"),
    )


def deny_hard_delete(table) -> None:
    """Make `table` refuse DELETE, leaving the row for its documents.

    For masters paired with :class:`SoftDeleteMixin`. Updates stay allowed —
    a master may be edited; only its removal is refused.
    """
    event.listen(table, "after_create", _REFUSE_MASTER_DELETE)
    event.listen(
        table,
        "after_create",
        _refusal_trigger(table, "no_hard_delete", "refuse_master_delete", "DELETE"),
    )


class SoftDeleteMixin:
    """A master retired by marking, never by removing the row.

    ``deleted_at`` is null for a live master and set once it is retired;
    :func:`soft_delete` is the only writer.
    """

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


@event.listens_for(Session, "do_orm_execute")
def _hide_soft_deleted(state) -> None:
    """Keep retired masters out of ordinary reads.

    Applied to every ORM SELECT that loads entities — never to a column load, a
    relationship load or a Core/text statement — unless the caller opts in with
    the :data:`INCLUDE_SOFT_DELETED` execution option.
    """
    if (
        state.is_select
        and not state.is_column_load
        and not state.is_relationship_load
        and not state.execution_options.get(INCLUDE_SOFT_DELETED, False)
    ):
        state.statement = state.statement.options(
            with_loader_criteria(
                SoftDeleteMixin,
                lambda cls: cls.deleted_at.is_(None),
                include_aliases=True,
            )
        )


def soft_delete(session: Session, master: Any, *, at: datetime | None = None) -> Any:
    """Retire a master by marking it; the row stays, so its documents stay valid."""
    if not isinstance(master, SoftDeleteMixin):
        raise TypeError(
            f"{type(master).__name__} is not a soft-deletable master"
            " — declare it with SoftDeleteMixin and deny_hard_delete"
        )
    master.deleted_at = at or datetime.now(timezone.utc)
    return master


# --- T-0.AUDIT.02 — the audit trail -----------------------------------------
# Every master change and every transaction change is recorded — who, when, what
# changed and which document it came from (§3 "Audit & Immutability", §6 metric
# 7). Captured by a trigger on the table itself rather than by each service
# method, so a new writer cannot forget it: the same reasoning as the company
# dimension in app/db.py and the balance rule in app/ledger/posting.py.
#
# The actor and the originating document arrive on the session
# (:func:`set_actor`, :func:`set_origin`) from the request that made the change.
# When nobody set them the row says ``unknown`` rather than failing the write —
# a gap in attribution is recorded as a gap, not as an unattributed change.

# Session settings the trigger reads.
ACTOR_SETTING = "app.actor"
ORIGIN_TYPE_SETTING = "app.origin_type"
ORIGIN_ID_SETTING = "app.origin_id"
UNKNOWN_ACTOR = "unknown"

# Where the stated values are held on the session, so the attribution is restored to every
# transaction the session opens rather than lapsing when one of them commits.
_ACTOR = "audit_actor"
_ORIGIN = "audit_origin"

# The table the trail is written to; never audited itself (it would recurse).
TRAIL_TABLE = "audit_log"

# Child tables (app/db.py's CHILD_TABLES) whose rows change **on their own**, so the change
# has no trail row today: granting a capability writes a `permission` row and leaves the role
# untouched, restricting a field writes a `field_permission` row, a party that becomes a
# customer gains a `party_role` row, and counting an item rewrites a `physical_count_line`
# while its count sits still. A child written only with its parent (`journal_line`,
# `approval_level`, the append-only `approval_decision`) is deliberately absent: it cannot
# change without the parent, whose own row names the change.
AUDIT_THROUGH_PARENT = ("permission", "field_permission", "party_role", "physical_count_line")


def _audited_children(tables: set[str]) -> dict[str, tuple[str, str, str]]:
    """The child tables to resolve through a parent, for the schema being created.

    Keyed by table name, valued by ``(parent, foreign key, parent key)``. Only pairs whose
    parent is in this schema: the trigger names the parent table, so a schema holding a subset
    (one module's own check, where ``physical_count`` is absent) installs a function that
    mentions only what it has. A name in :data:`AUDIT_THROUGH_PARENT` that is not a child table
    at all is a mistake in the list rather than a gap, and is refused here.
    """
    from app.db import CHILD_TABLES

    unknown = [name for name in AUDIT_THROUGH_PARENT if name not in CHILD_TABLES]
    if unknown:
        raise RuntimeError(
            f"{unknown} are named for the trail but are not child tables in app/db.py"
        )
    return {
        name: CHILD_TABLES[name]
        for name in AUDIT_THROUGH_PARENT
        if name in tables and CHILD_TABLES[name][0] in tables
    }


def _owning_through_parent(children: dict[str, tuple[str, str, str]]) -> str:
    """The `CASE` that reads a child table's company from the parent that carries it.

    A child row has no dimension of its own — ``app/db.py``'s ``CHILD_TABLES`` says which
    parent carries it — so the trail row belongs to that parent's company, read from the
    parent row the child points at.
    """
    if not children:
        # A schema holding none of them (a module's own check) gets no CASE at all.
        return "NULL::uuid"
    arms = "\n".join(
        f"            WHEN '{name}' THEN"
        f" (SELECT p.{COMPANY_COLUMN} FROM {parent} p"
        f" WHERE p.{key} = coalesce(row_after ->> '{foreign_key}',"
        f" row_before ->> '{foreign_key}')::uuid)"
        for name, (parent, foreign_key, key) in sorted(children.items())
    )
    return f"CASE TG_TABLE_NAME\n{arms}\n            END"


def _subject_through_parent(children: dict[str, tuple[str, str, str]]) -> str:
    """The `CASE` naming, for a child table, the row it belongs to — its parent.

    A child row's own key says nothing a reader can follow: "which party gained this role" is
    the child's ``party_id``, so a child's trail row carries its **parent's** id and is read
    back by the master whose change it was.
    """
    if not children:
        return "NULL::text"
    arms = "\n".join(
        f"            WHEN '{name}' THEN"
        f" coalesce(row_after ->> '{foreign_key}', row_before ->> '{foreign_key}')"
        for name, (_parent, foreign_key, _key) in sorted(children.items())
    )
    return f"CASE TG_TABLE_NAME\n{arms}\n            END"


class AuditLog(Base):
    """One change: who made it, when, what moved and which document it came from."""

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    # Actor identity (the RBAC subject of T-0.SEC.01) — 'unknown' when nobody
    # stated one; never null, so every row is attributable to somebody or to a
    # recorded gap.
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    # insert | update | soft_delete | restore | delete — delete only survives for
    # a table the master convention does not protect.
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    entity: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64), index=True)
    before_values: Mapped[dict | None] = mapped_column(JSONB)
    after_values: Mapped[dict | None] = mapped_column(JSONB)
    # The document that produced the change, where there is one (a posting knows
    # its invoice; a master edit usually does not).
    origin_type: Mapped[str | None] = mapped_column(String(64))
    origin_id: Mapped[str | None] = mapped_column(String(64))


# '%%' because SQLAlchemy's DDL wrapper interpolates the statement. The two `{...}` holes are
# filled in when the schema is created, with the child tables that schema actually holds.
_AUDIT_FUNCTION = """
CREATE OR REPLACE FUNCTION audit_row_change() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    row_before jsonb;
    row_after  jsonb;
    act        text;
    subject    text;
    owning     uuid;
BEGIN
    IF TG_OP <> 'INSERT' THEN row_before := to_jsonb(OLD); END IF;
    IF TG_OP <> 'DELETE' THEN row_after := to_jsonb(NEW); END IF;

    act := lower(TG_OP);
    -- A retirement is an update of deleted_at; name it for what it means, so a
    -- reader of the trail does not have to diff the payload to see it.
    IF act = 'update' AND row_before ->> 'deleted_at' IS NULL
       AND row_after ->> 'deleted_at' IS NOT NULL THEN
        act := 'soft_delete';
    ELSIF act = 'update' AND row_before ->> 'deleted_at' IS NOT NULL
       AND row_after ->> 'deleted_at' IS NULL THEN
        act := 'restore';
    END IF;

    subject := coalesce(
        -- A child row's identity is the row it belongs to: the change was that party's,
        -- that role's, that count's. Everything else is its own id.
        {subject_through_parent},
        coalesce(row_after ->> 'id', row_before ->> 'id')
    );
    -- The owning company is the row's own dimension; the company master is the
    -- dimension itself, so there it is the row's id; a child row has none, so it is
    -- read from the parent that carries it.
    owning := coalesce(
        nullif(coalesce(row_after ->> 'company_id', row_before ->> 'company_id'), '')::uuid,
        CASE WHEN TG_TABLE_NAME = 'company'
             THEN coalesce(row_after ->> 'id', row_before ->> 'id')::uuid END,
        {owning_through_parent}
    );

    INSERT INTO audit_log (id, company_id, actor, action, entity, entity_id,
                           before_values, after_values, origin_type, origin_id)
    VALUES (gen_random_uuid(),
            owning,
            coalesce(nullif(current_setting('app.actor', true), ''), 'unknown'),
            act,
            TG_TABLE_NAME,
            subject,
            row_before,
            row_after,
            nullif(current_setting('app.origin_type', true), ''),
            nullif(current_setting('app.origin_id', true), ''));
    RETURN NULL;
END;
$$;
"""


def _audited(metadata) -> list:
    """The tables whose changes the trail records.

    Every table carrying the company dimension, plus the company master, plus the child
    tables whose rows change **on their own** (:data:`AUDIT_THROUGH_PARENT`). Child tables
    written only with their parent (``journal_line``) are not audited separately: they cannot
    change without it, and the parent's row already names the change, so a line's own trail
    row would be a duplicate.
    """
    from app.db import GLOBAL_TABLES

    return [
        table
        for table in metadata.tables.values()
        if table.name != TRAIL_TABLE
        and (
            COMPANY_COLUMN in table.c
            or table.name in GLOBAL_TABLES
            or table.name in AUDIT_THROUGH_PARENT
        )
    ]


def _install_audit(metadata, connection, **_kw) -> None:
    """Create the trail's trigger function and one trigger per audited table."""
    children = _audited_children(set(metadata.tables))
    connection.exec_driver_sql(
        _AUDIT_FUNCTION.format(
            owning_through_parent=_owning_through_parent(children),
            subject_through_parent=_subject_through_parent(children),
        )
    )
    for table in _audited(metadata):
        # Dropped first for the same reason the scoping policy is (app/db.py): this hook runs on
        # every `create_all`, not only on a table's own creation, and Postgres has no
        # `CREATE TRIGGER IF NOT EXISTS`.
        connection.exec_driver_sql(
            f"DROP TRIGGER IF EXISTS {table.name}_audited ON {table.name}"
        )
        connection.exec_driver_sql(
            f"CREATE TRIGGER {table.name}_audited"
            f" AFTER INSERT OR UPDATE OR DELETE ON {table.name}"
            f" FOR EACH ROW EXECUTE FUNCTION audit_row_change()"
        )


event.listen(Base.metadata, "after_create", _install_audit)

# The trail is itself append-only (T-0.AUDIT.01): an audit record that can be
# edited is not an audit record. There is no application API that writes or
# updates this table — the trigger is the only writer.
append_only(AuditLog.__table__)


def _apply_setting(connection, name: str, value: str) -> None:
    connection.exec_driver_sql(f"SELECT set_config('{name}', %s, true)", (str(value),))


@event.listens_for(Session, "after_begin")
def _reapply_attribution(session: Session, transaction, connection) -> None:
    """Put the actor and the origin on a transaction that just began.

    Both are transaction-scoped settings, so a service that commits half-way through a request
    — the inbound integration handler, a dunning run, an outbound delivery — used to leave
    everything it wrote afterwards attributed to nobody. The values the caller stated are held
    on the session and re-applied to every transaction it opens, so attribution survives the
    commit. The session is one request's (the API opens and closes it per request), so nothing
    leaks across requests.
    """
    actor = session.info.get(_ACTOR)
    if actor is not None:
        _apply_setting(connection, ACTOR_SETTING, actor)
    origin = session.info.get(_ORIGIN)
    if origin is not None:
        _apply_setting(connection, ORIGIN_TYPE_SETTING, origin[0])
        _apply_setting(connection, ORIGIN_ID_SETTING, origin[1])


def set_actor(session: Session, actor: str) -> None:
    """State who is making the changes in this transaction (the RBAC subject).

    Held on the session as well as set on the transaction, so the attribution is not lost when
    the work commits and carries on (:func:`_reapply_attribution`).
    """
    session.info[_ACTOR] = str(actor)
    _apply_setting(session.connection(), ACTOR_SETTING, actor)


def set_origin(session: Session, document_type: str, document_id: Any) -> None:
    """State the document this transaction changes, so the trail can trace back."""
    session.info[_ORIGIN] = (str(document_type), str(document_id))
    connection = session.connection()
    _apply_setting(connection, ORIGIN_TYPE_SETTING, document_type)
    _apply_setting(connection, ORIGIN_ID_SETTING, document_id)


def read_trail(
    session: Session,
    *,
    entity: str | None = None,
    entity_id: Any = None,
    origin: tuple[str, Any] | None = None,
) -> list[AuditLog]:
    """Read the trail back, newest last — optionally for one row or one document.

    The scope is the caller's: the session's company (a row-level-security policy
    on ``audit_log``) decides which companies' trail it can see at all.
    """
    statement = select(AuditLog).order_by(AuditLog.occurred_at)
    if entity is not None:
        statement = statement.where(AuditLog.entity == entity)
    if entity_id is not None:
        statement = statement.where(AuditLog.entity_id == str(entity_id))
    if origin is not None:
        statement = statement.where(
            AuditLog.origin_type == origin[0], AuditLog.origin_id == str(origin[1])
        )
    return list(session.scalars(statement))
