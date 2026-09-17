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
