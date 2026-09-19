"""T-1.ACCT.01 — the chart of accounts: one tree under the plan's five classes.

§2.1 asks for a "CoA – tree hierarchy … with localization support", `account` is
the §5 entity of the same name, and DOMAIN-MODELS.md §3 fixes its shape. This
module owns the tree and the rules that keep it a tree:

* **Five classes, and a child keeps its parent's.** An asset is never a child of
  an income account. This is the rule that makes a trial balance meaningful, so it
  is enforced in the database too (a deferred constraint trigger, the same shape
  as the ledger's balance rule in :mod:`app.ledger.posting`) — a writer that
  bypasses :func:`create_account` is refused at COMMIT.
* **Codes are unique per company**, and the code is what a posting line states
  (DOMAIN-MODELS.md §3). Posting through the primitive resolves the code to this
  row, so a line cannot name an account nobody created.
* **No cycles**, arbitrary depth. Walking up from a new parent must never reach
  the account being moved.
* **A master is retired by marking**, never removed (T-0.AUDIT.01), and a parent
  with live children cannot be retired while they are live — otherwise the tree
  would grow orphans.

The accounts themselves are the locale's business, not this module's: the five
classes are the plan's, and which accounts a market seeds is the pack's
(T-0.LOC.01). :func:`import_coa_template` copies a validated pack template in,
parent first, so importing needs no manual fix-up.
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
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.audit import SoftDeleteMixin, deny_hard_delete, soft_delete
from app.db import Base

# The five account classes §2.1 names, in the order a statement reads them.
ACCOUNT_CLASSES = ("asset", "liability", "equity", "income", "expense")


class AccountError(ValueError):
    """The chart of accounts refused what was asked of it."""


class UnknownClassError(AccountError):
    """A class outside the five §2.1 names — refused, never defaulted."""


class ClassMismatchError(AccountError):
    """A child cannot mix classes with its parent."""


class DuplicateAccountCode(AccountError):
    """The company already has an account with that code."""


class UnknownAccountError(AccountError):
    """No such account (or no such code) in this company."""


class AccountHasChildrenError(AccountError):
    """A parent with live children cannot be retired while they are live."""


class Account(SoftDeleteMixin, Base):
    """One account in the company's chart — a node of the tree."""

    __tablename__ = "account"
    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_account_company_code"),
        CheckConstraint(
            "class IN (" + ", ".join(f"'{name}'" for name in ACCOUNT_CLASSES) + ")",
            name="ck_account_class",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    # What a posting line states, and what a person types. Unique per company.
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # `class` is a Python keyword, so the attribute carries the underscore and the
    # column keeps the name DOMAIN-MODELS.md §3 fixes.
    account_class: Mapped[str] = mapped_column("class", String(16), nullable=False)
    # The tree: same company as the child, same class, no cycles.
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("account.id"), index=True
    )

    parent: Mapped[Account | None] = relationship(
        back_populates="children", remote_side="Account.id"
    )
    children: Mapped[list[Account]] = relationship(
        back_populates="parent", order_by="Account.code"
    )


# --- The tree rules, at the storage boundary ---------------------------------
# The checks in the functions below are the caller's copy of the rules; these
# triggers are the rules. Deferred, so a whole import is judged as a set at
# COMMIT, and a writer that bypasses the helpers — raw SQL, a restored dump — is
# refused there. Postgres-only by design, like the ledger's balance rule: on any
# other backend the DDL fails loudly rather than leaving the tree unguarded.
#
# ponytail: the cycle walk reads the account table once per changed row, so an
# import of n accounts costs O(n·depth). Ceiling: a chart of accounts is hundreds
# of rows, imported rarely. Upgrade path: check cycles in the import loop and let
# the trigger handle only the class rule.
# '%%' because SQLAlchemy's DDL wrapper interpolates the statement.
_TREE_FUNCTION = DDL(
    """
CREATE OR REPLACE FUNCTION account_tree_rules() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    parent_class   text;
    parent_company uuid;
    cyclic         boolean;
BEGIN
    IF NEW.parent_id IS NOT NULL THEN
        SELECT class, company_id INTO parent_class, parent_company
          FROM account WHERE id = NEW.parent_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'account %% names a parent %% that does not exist',
                NEW.code, NEW.parent_id;
        END IF;
        IF parent_company <> NEW.company_id THEN
            RAISE EXCEPTION 'account %% and its parent %% belong to different companies',
                NEW.code, NEW.parent_id;
        END IF;
        IF parent_class <> NEW.class THEN
            RAISE EXCEPTION
                'account %% is %% and its parent is %%; a child cannot mix classes',
                NEW.code, NEW.class, parent_class;
        END IF;

        WITH RECURSIVE up(id, parent_id, depth) AS (
            SELECT id, parent_id, 1 FROM account WHERE id = NEW.parent_id
            UNION ALL
            SELECT a.id, a.parent_id, up.depth + 1
              FROM account a JOIN up ON a.id = up.parent_id
             WHERE up.depth < 100
        )
        SELECT EXISTS (SELECT 1 FROM up WHERE id = NEW.id) INTO cyclic;
        IF cyclic THEN
            RAISE EXCEPTION
                'account %% cannot be moved under %%: that parent is inside its own subtree',
                NEW.code, NEW.parent_id;
        END IF;
    END IF;

    IF NEW.deleted_at IS NOT NULL
       AND EXISTS (SELECT 1 FROM account c
                    WHERE c.parent_id = NEW.id AND c.deleted_at IS NULL) THEN
        RAISE EXCEPTION
            'account %% still has live children; retire them first',
            NEW.code;
    END IF;
    RETURN NULL;
END;
$$;
"""
)

_TREE_TRIGGER = DDL(
    """
CREATE CONSTRAINT TRIGGER account_tree_rules
    AFTER INSERT OR UPDATE ON account
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION account_tree_rules();
"""
)

for _ddl in (_TREE_FUNCTION, _TREE_TRIGGER):
    event.listen(Account.__table__, "after_create", _ddl)

# A master: retired by marking, never removed (T-0.AUDIT.01).
deny_hard_delete(Account.__table__)


# --- Creating and moving accounts --------------------------------------------


def account_by_code(
    session: Session, *, company_id: uuid.UUID, code: str
) -> Account:
    """The live account a posting line states, or a refusal.

    Used by the posting primitive (T-1.ACCT.03): a line cannot name an account
    the company does not have, which is what turns "account" from free text into
    a reference.
    """
    account = session.scalar(
        select(Account).where(Account.company_id == company_id, Account.code == str(code))
    )
    if account is None:
        raise UnknownAccountError(
            f"no account {code!r} in this company; create it first"
            " (T-1.ACCT.01) or post to an existing one"
        )
    return account


def _checked_class(account_class: str) -> str:
    wanted = str(account_class).strip().lower()
    if wanted not in ACCOUNT_CLASSES:
        raise UnknownClassError(
            f"unknown account class {account_class!r}; the classes are"
            f" {', '.join(ACCOUNT_CLASSES)}"
        )
    return wanted


def _parent(session: Session, *, company_id: uuid.UUID, parent_id: uuid.UUID | None) -> Account | None:
    """The parent row, checked for existence and company before the trigger sees it."""
    if parent_id is None:
        return None
    parent = session.scalar(
        select(Account).where(Account.id == parent_id, Account.company_id == company_id)
    )
    if parent is None:
        raise UnknownAccountError(f"no account {parent_id} in this company to parent under")
    return parent


def create_account(
    session: Session,
    *,
    company_id: uuid.UUID,
    code: str,
    name: str,
    account_class: str,
    parent_id: uuid.UUID | None = None,
) -> Account:
    """Create one account, under `parent_id` when given.

    The class must be one of the five, and when there is a parent it must be the
    parent's — the same rule the trigger enforces, stated here so the caller gets
    a sentence rather than a database error.
    """
    wanted = _checked_class(account_class)
    parent = _parent(session, company_id=company_id, parent_id=parent_id)
    if parent is not None and parent.account_class != wanted:
        raise ClassMismatchError(
            f"account {code!r} is {wanted} and its parent {parent.code!r} is"
            f" {parent.account_class}; a child cannot mix classes"
        )
    if session.scalar(
        select(Account).where(Account.company_id == company_id, Account.code == str(code))
    ) is not None:
        raise DuplicateAccountCode(f"this company already has an account {code!r}")

    account = Account(
        company_id=company_id,
        code=str(code),
        name=name,
        account_class=wanted,
        parent_id=parent.id if parent is not None else None,
    )
    session.add(account)
    session.flush()
    return account


def reparent(session: Session, account: Account, *, parent_id: uuid.UUID | None) -> Account:
    """Move an account to another parent, keeping the tree rules.

    An account a posting already references may not be moved into another class:
    its lines were read against the class they were posted under, and the class
    of a referenced account is part of what those lines mean. (A same-class move
    stays allowed — reorganising a tree does not change any balance.)
    """
    parent = _parent(session, company_id=account.company_id, parent_id=parent_id)
    if parent is not None and parent.account_class != account.account_class:
        raise ClassMismatchError(
            f"account {account.code!r} is {account.account_class} and the new parent"
            f" {parent.code!r} is {parent.account_class}; a child cannot mix classes"
        )
    account.parent_id = parent.id if parent is not None else None
    session.flush()
    return account


def retire_account(session: Session, account: Account) -> Account:
    """Retire an account by marking it, once none of its children are live."""
    live_children = session.scalars(
        select(Account).where(Account.parent_id == account.id)
    ).all()
    if live_children:
        raise AccountHasChildrenError(
            f"account {account.code!r} still has live children"
            f" ({', '.join(child.code for child in live_children)}); retire them first"
        )
    return soft_delete(session, account)


def import_coa_template(
    session: Session, *, company_id: uuid.UUID, market: str
) -> list[Account]:
    """Create this company's chart from `market`'s localization pack.

    The template is validated by T-0.LOC.01 (codes unique, parents present and
    listed above their children, classes known), so the import is a straight
    walk in the pack's own order and needs no fix-up. A second import of the same
    pack is refused by the codes' uniqueness rather than half-applied.
    """
    from app.localization import coa_template

    created: list[Account] = []
    by_code: dict[str, Account] = {}
    for row in coa_template(market):
        parent_code = row.get("parent")
        account = create_account(
            session,
            company_id=company_id,
            code=row["code"],
            name=row["name"],
            account_class=row["class"],
            parent_id=by_code[parent_code].id if parent_code else None,
        )
        by_code[account.code] = account
        created.append(account)
    return created


def tree(session: Session, *, company_id: uuid.UUID) -> list[dict]:
    """The whole chart as a nested tree, parents before their children.

    Read from the live rows only — a retired account is absent, as everywhere
    else (T-0.AUDIT.01) — and assembled in one pass rather than one query per
    node.
    """
    accounts = list(
        session.scalars(
            select(Account)
            .where(Account.company_id == company_id)
            .order_by(Account.code)
        )
    )
    nodes = {
        account.id: {
            "id": str(account.id),
            "code": account.code,
            "name": account.name,
            "account_class": account.account_class,
            "parent_id": str(account.parent_id) if account.parent_id else None,
            "children": [],
        }
        for account in accounts
    }
    roots: list[dict] = []
    for account in accounts:
        node = nodes[account.id]
        parent = nodes.get(account.parent_id) if account.parent_id else None
        if parent is None:
            roots.append(node)
        else:
            parent["children"].append(node)
    return roots
