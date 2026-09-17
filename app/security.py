"""T-0.SEC.01 — roles, permissions and field-level restrictions.

Authorisation is data, not code: a **role** holds **capabilities** (what it may
do) and **field restrictions** (which fields of an entity it may read or write),
and a **subject** — the actor the request stated — holds roles. Adding a role,
granting a capability or restricting a field is a row change, so a permission
change takes effect on the next request instead of the next deploy.

Three rules this module is built around:

* **Refusal happens where the data is served, not in the UI.** :func:`require`
  is called by the API layer before it does anything; a caller without the
  capability is refused at the boundary. Hiding a button is not a permission.
* **A restricted field is absent, not empty.** :func:`readable_fields` removes
  the field from the payload it is given and :func:`reject_restricted_fields`
  refuses a write that sets one, so "not allowed to see it" cannot be confused
  with "not filled in".
* **Every refusal is attributable.** Each refusal appends a row to the audit
  trail (T-0.AUDIT.02) naming the actor, what was attempted and which roles the
  actor held, so "who tried what and was refused" is answerable later.

Nothing here chooses an authentication provider or a session mechanism — the
actor identity arrives with the request (the API layer states it; T-0.SEC.01's
successors in Phases 1–6 use the same helpers).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Iterable

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint, Uuid, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.audit import AuditLog
from app.db import Base

# The action names a refusal is recorded under, so the trail can be read for them.
REFUSED = "refused"


class AccessDenied(Exception):
    """A refusal: the caller may not do this, or may not use this field."""

    code = "forbidden"

    def __init__(self, message: str, *, capability: str | None = None, field: str | None = None):
        super().__init__(message)
        self.message = message
        self.capability = capability
        self.field = field


class PermissionDenied(AccessDenied):
    """The caller's roles do not hold the capability."""


class FieldAccessDenied(AccessDenied):
    """The caller may not read or write that field."""


class Role(Base):
    """A named set of capabilities and field restrictions, per company."""

    __tablename__ = "role"
    __table_args__ = (UniqueConstraint("company_id", "code", name="uq_role_company_code"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)

    permissions: Mapped[list[Permission]] = relationship(back_populates="role")
    restrictions: Mapped[list[FieldPermission]] = relationship(back_populates="role")


class Permission(Base):
    """One capability a role holds. Nothing is granted by default."""

    __tablename__ = "permission"
    __table_args__ = (UniqueConstraint("role_id", "capability", name="uq_permission_once"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    role_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("role.id"), nullable=False, index=True
    )
    # What it may do, named "<resource>.<action>" — the modules own the names
    # ("journal.post", "journal.read", "company.read", …). No enumeration here:
    # a new capability is a row, like everything else in this module.
    capability: Mapped[str] = mapped_column(String(64), nullable=False)

    role: Mapped[Role] = relationship(back_populates="permissions")


class FieldPermission(Base):
    """A restriction on one field of one entity, for one role.

    Absent means unrestricted: roles are additive, so a role reads and writes a
    field unless a row says otherwise.
    """

    __tablename__ = "field_permission"
    __table_args__ = (
        UniqueConstraint("role_id", "entity", "field", name="uq_field_permission_once"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    role_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("role.id"), nullable=False, index=True
    )
    entity: Mapped[str] = mapped_column(String(64), nullable=False)
    field: Mapped[str] = mapped_column(String(64), nullable=False)
    can_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    can_write: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    role: Mapped[Role] = relationship(back_populates="restrictions")


class RoleAssignment(Base):
    """One role a subject (an actor identity) holds."""

    __tablename__ = "role_assignment"
    __table_args__ = (UniqueConstraint("subject", "role_id", name="uq_role_assignment_once"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    subject: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    role_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("role.id"), nullable=False)

    role: Mapped[Role] = relationship()
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


def define_role(session: Session, *, company_id: uuid.UUID, code: str, name: str) -> Role:
    """Create a role. It holds nothing until something is granted to it."""
    role = Role(company_id=company_id, code=code, name=name)
    session.add(role)
    session.flush()
    return role


def grant(session: Session, role: Role, *capabilities: str) -> Role:
    """Give a role capabilities — the only way anything is ever allowed."""
    held = {permission.capability for permission in role.permissions}
    role.permissions.extend(
        Permission(capability=capability)
        for capability in capabilities
        if capability not in held
    )
    session.flush()
    return role


def restrict(
    session: Session,
    role: Role,
    *,
    entity: str,
    field: str,
    can_read: bool = True,
    can_write: bool = True,
) -> FieldPermission:
    """Restrict one field of one entity for a role."""
    restriction = FieldPermission(
        role_id=role.id, entity=entity, field=field, can_read=can_read, can_write=can_write
    )
    session.add(restriction)
    session.flush()
    return restriction


def assign(session: Session, *, company_id: uuid.UUID, subject: str, role: Role) -> RoleAssignment:
    """Give a subject a role. A subject may hold several."""
    assignment = RoleAssignment(company_id=company_id, subject=subject, role_id=role.id)
    session.add(assignment)
    session.flush()
    return assignment


def roles_for(session: Session, *, company_id: uuid.UUID, subject: str) -> list[Role]:
    """The roles a subject holds — read fresh each request, so a change is live."""
    return list(
        session.scalars(
            select(Role)
            .join(RoleAssignment, RoleAssignment.role_id == Role.id)
            .where(RoleAssignment.company_id == company_id, RoleAssignment.subject == subject)
        )
    )


def capabilities(session: Session, *, company_id: uuid.UUID, subject: str) -> set[str]:
    """Everything the subject's roles allow — the union of their grants."""
    held: set[str] = set()
    for role in roles_for(session, company_id=company_id, subject=subject):
        held.update(permission.capability for permission in role.permissions)
    return held


def _restrictions(
    session: Session, *, company_id: uuid.UUID, subject: str, entity: str
) -> list[FieldPermission]:
    return list(
        session.scalars(
            select(FieldPermission)
            .join(Role, Role.id == FieldPermission.role_id)
            .join(RoleAssignment, RoleAssignment.role_id == Role.id)
            .where(
                RoleAssignment.company_id == company_id,
                RoleAssignment.subject == subject,
                FieldPermission.entity == entity,
            )
        )
    )


def record_refusal(
    session: Session,
    *,
    company_id: uuid.UUID,
    actor: str,
    attempted: str,
    entity: str,
    entity_id: Any = None,
    roles: Iterable[str] = (),
) -> AuditLog:
    """Write the refusal into the trail, so it is attributable like any change.

    Committed on the spot: a refusal is raised by a guard that runs *before* the
    work it guards, so nothing else is pending in the transaction, and a refusal
    that vanished with the rollback it caused would be no record at all.
    """
    entry = AuditLog(
        company_id=company_id,
        actor=str(actor),
        action=REFUSED,
        entity=entity,
        entity_id=None if entity_id is None else str(entity_id),
        after_values={
            "attempted": attempted,
            "held_roles": sorted(roles),
        },
    )
    session.add(entry)
    session.commit()
    return entry


def require(
    session: Session,
    *,
    company_id: uuid.UUID,
    subject: str,
    capability: str,
    entity: str,
    entity_id: Any = None,
) -> None:
    """Refuse unless the subject's roles hold `capability`, and record the refusal.

    Called by the boundary before it acts: an unauthorised caller never reaches
    the data, and the attempt is on the record either way.
    """
    held = roles_for(session, company_id=company_id, subject=subject)
    allowed = set()
    for role in held:
        allowed.update(permission.capability for permission in role.permissions)
    if capability in allowed:
        return

    record_refusal(
        session,
        company_id=company_id,
        actor=subject,
        attempted=capability,
        entity=entity,
        entity_id=entity_id,
        roles=(role.code for role in held),
    )
    raise PermissionDenied(
        f"{subject!r} may not {capability!r}"
        + (f" (holds {', '.join(sorted(role.code for role in held))})" if held else " (no roles)"),
        capability=capability,
    )


def readable_fields(
    session: Session,
    *,
    company_id: uuid.UUID,
    subject: str,
    entity: str,
    payload: dict,
) -> dict:
    """The payload without the fields the subject may not read.

    Absent rather than nulled: a field the caller may not see must not be
    mistaken for one that is simply empty.
    """
    hidden = {
        restriction.field
        for restriction in _restrictions(
            session, company_id=company_id, subject=subject, entity=entity
        )
        if not restriction.can_read
    }
    return {field: value for field, value in payload.items() if field not in hidden}


def reject_restricted_fields(
    session: Session,
    *,
    company_id: uuid.UUID,
    subject: str,
    entity: str,
    payload: dict,
    entity_id: Any = None,
) -> None:
    """Refuse a write that sets a field the subject may not write.

    Only fields the caller actually set are considered, so a default the API
    filled in is not held against it.
    """
    forbidden = {
        restriction.field
        for restriction in _restrictions(
            session, company_id=company_id, subject=subject, entity=entity
        )
        if not restriction.can_write
    }
    attempted = sorted(field for field, value in payload.items() if field in forbidden and value is not None)
    if not attempted:
        return

    record_refusal(
        session,
        company_id=company_id,
        actor=subject,
        attempted=f"write {entity}.{', '.join(attempted)}",
        entity=entity,
        entity_id=entity_id,
        roles=(role.code for role in roles_for(session, company_id=company_id, subject=subject)),
    )
    raise FieldAccessDenied(
        f"{subject!r} may not write {entity}.{', '.join(attempted)}", field=attempted[0]
    )
