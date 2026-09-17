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

# The table the trail is written to; never audited itself (it would recurse).
TRAIL_TABLE = "audit_log"


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


# '%%' because SQLAlchemy's DDL wrapper interpolates the statement.
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

    subject := coalesce(row_after ->> 'id', row_before ->> 'id');
    -- The owning company is the row's own dimension; the company master is the
    -- dimension itself, so there it is the row's id.
    owning := coalesce(
        nullif(coalesce(row_after ->> 'company_id', row_before ->> 'company_id'), '')::uuid,
        CASE WHEN TG_TABLE_NAME = 'company'
             THEN coalesce(row_after ->> 'id', row_before ->> 'id')::uuid END
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

    Every table carrying the company dimension, plus the company master. Child
    tables (``journal_line``) are not audited separately: they cannot change
    without their parent (both are append-only) and the parent's row already
    names the change, so a line's own trail row would be a duplicate.
    """
    from app.db import GLOBAL_TABLES

    return [
        table
        for table in metadata.tables.values()
        if table.name != TRAIL_TABLE
        and (COMPANY_COLUMN in table.c or table.name in GLOBAL_TABLES)
    ]


def _install_audit(metadata, connection, **_kw) -> None:
    """Create the trail's trigger function and one trigger per audited table."""
    connection.exec_driver_sql(_AUDIT_FUNCTION)
    for table in _audited(metadata):
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


def set_actor(session: Session, actor: str) -> None:
    """State who is making the changes in this transaction (the RBAC subject)."""
    session.connection().exec_driver_sql(
        f"SELECT set_config('{ACTOR_SETTING}', %s, true)", (str(actor),)
    )


def set_origin(session: Session, document_type: str, document_id: Any) -> None:
    """State the document this transaction changes, so the trail can trace back."""
    connection = session.connection()
    connection.exec_driver_sql(
        f"SELECT set_config('{ORIGIN_TYPE_SETTING}', %s, true)", (str(document_type),)
    )
    connection.exec_driver_sql(
        f"SELECT set_config('{ORIGIN_ID_SETTING}', %s, true)", (str(document_id),)
    )


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
