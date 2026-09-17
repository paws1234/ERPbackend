"""T-0.PARTY.01 — one party identity, as many roles as it holds.

Supplier (Phase 2), Customer (Phase 3) and Employee (Phase 5) are **roles on one
party**, not three parallel masters (§5 "Party (Customer / Supplier / Employee)").
A person or organisation is created once and given the roles it actually holds,
so the same counterparty cannot end up as three half-filled records.

What is deliberately *not* here: every role's own attributes. A credit limit is
the customer master's business (Phase 3), bank and payment terms the supplier's
(Phase 2), the employment contract the employee's (Phase 5). Putting any of them
on this table is how one party becomes three again, so the module carries only
what identifies the party and which roles it holds, and the check asserts that
column set has not grown.

Like every master, a party is retired by marking it (:mod:`app.audit`) and never
removed. The invariant "a party holds at least one role" is enforced at the
storage boundary too — a deferred constraint trigger, the same shape as the
ledger's balance rule — so a party cannot be left roleless by a writer that
avoids :func:`create_party`.
"""

from __future__ import annotations

import uuid

from sqlalchemy import (
    CheckConstraint,
    DDL,
    ForeignKey,
    String,
    UniqueConstraint,
    Uuid,
    event,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.audit import SoftDeleteMixin, deny_hard_delete
from app.db import Base

# The roles §5 names. No default: a party has the roles it was given, and a role
# outside this list is refused rather than stored.
ROLES = ("customer", "supplier", "employee")


class UnknownRoleError(ValueError):
    """Raised when a party is asked to hold no role, or one nobody has heard of."""


class Party(SoftDeleteMixin, Base):
    """One counterparty identity — shared by every role it holds."""

    __tablename__ = "party"
    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_party_company_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    # Short human key, unique within the company — what a person types.
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    # Tax identifier, where the party has one. Which identifier a document needs
    # is the localization pack's business (T-0.LOC.01, tax_pack in Phase 2) — this
    # column only stores the value, with no default and no format invented here.
    tax_id: Mapped[str | None] = mapped_column(String(32))

    roles: Mapped[list[PartyRole]] = relationship(
        back_populates="party", order_by="PartyRole.role"
    )

    def has_role(self, role: str) -> bool:
        """Whether this party holds `role`."""
        return any(held.role == role for held in self.roles)


class PartyRole(Base):
    """One role a party holds; a party holds as many as it has."""

    __tablename__ = "party_role"
    __table_args__ = (
        UniqueConstraint("party_id", "role", name="uq_party_role_once"),
        CheckConstraint(
            "role IN (" + ", ".join(f"'{role}'" for role in ROLES) + ")",
            name="ck_party_role_known",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    party_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("party.id"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)

    party: Mapped[Party] = relationship(back_populates="roles")


# --- The invariant, at the storage boundary ----------------------------------
# A roleless party is not a party: nothing can be done with it, and every later
# phase would have to guard against one. Deferred, so `create_party` may insert
# the party and its roles in any order inside one transaction, and a writer that
# bypasses `create_party` is still refused at COMMIT.
# '%%' because SQLAlchemy's DDL wrapper interpolates the statement.
_PARTY_ROLE_FUNCTION = DDL(
    """
CREATE OR REPLACE FUNCTION party_has_a_role() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    target uuid;
    held   integer;
BEGIN
    IF TG_TABLE_NAME = 'party' THEN
        target := NEW.id;
    ELSIF TG_OP = 'DELETE' THEN
        target := OLD.party_id;
    ELSE
        target := NEW.party_id;
    END IF;

    SELECT count(*) INTO held FROM party_role WHERE party_id = target;
    IF held = 0 THEN
        RAISE EXCEPTION 'party %% holds no role; a party is a customer, a supplier or an employee',
            target;
    END IF;
    RETURN NULL;
END;
$$;
"""
)

_PARTY_TRIGGER = DDL(
    """
CREATE CONSTRAINT TRIGGER party_must_hold_a_role
    AFTER INSERT OR UPDATE ON party
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION party_has_a_role();
"""
)

_PARTY_ROLE_TRIGGER = DDL(
    """
CREATE CONSTRAINT TRIGGER party_role_must_leave_one
    AFTER INSERT OR UPDATE OR DELETE ON party_role
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION party_has_a_role();
"""
)

for _ddl in (_PARTY_ROLE_FUNCTION, _PARTY_TRIGGER, _PARTY_ROLE_TRIGGER):
    event.listen(PartyRole.__table__, "after_create", _ddl)

# Masters retire by marking, never by removing the row (T-0.AUDIT.01).
deny_hard_delete(Party.__table__)


def create_party(
    session: Session,
    *,
    company_id: uuid.UUID,
    code: str,
    name: str,
    roles,
    tax_id: str | None = None,
) -> Party:
    """Create one party holding `roles` — at least one of them.

    `roles` is an iterable of role names; a repeat is a single role, and an
    unknown one is refused rather than stored. The row and its roles are flushed
    together, so the deferred invariant is judged at the caller's COMMIT and a
    failed document leaves no party behind.
    """
    wanted = list(dict.fromkeys(str(role).strip().lower() for role in roles))
    if not wanted:
        raise UnknownRoleError(
            f"a party holds at least one role: {', '.join(ROLES)}"
        )
    unknown = [role for role in wanted if role not in ROLES]
    if unknown:
        raise UnknownRoleError(
            f"unknown role(s) {', '.join(unknown)}; a party is"
            f" {', '.join(ROLES)}"
        )

    party = Party(company_id=company_id, code=code, name=name, tax_id=tax_id)
    party.roles = [PartyRole(role=role) for role in wanted]
    session.add(party)
    session.flush()
    return party
