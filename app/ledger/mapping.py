"""T-1.ACCT.03 — how every sub-module posts, and the account mapping it posts to.

§2.1 asks for "Automatic double-entry posting from all sub-modules". The interface
itself already exists — T-0.CORE.01's :func:`app.ledger.posting.post_journal_entry`
is the only writer of `journal_entry` / `journal_line`, and it refuses an
unbalanced or single-line call — so this module owns the other half of the task:
**the account mapping**, so no module hard-codes an account code of its own.

* A mapping is a row: company × key → account. Inventory says `"inventory"`,
  the count adjustment says `"stock_adjustment"`, the FX engine says `"fx_gain"`,
  and which account that *is* stays a configuration question. A market's chart of
  accounts (T-0.LOC.01) names them differently from another's, and a company
  re-pointing its COGS account is a row change, not a release.
* **An unmapped key is refused, never defaulted.** Posting to a guessed account
  is how a ledger silently books to the wrong place, so
  :func:`mapped_account` raises :class:`MissingMappingError` and says the key is
  unmapped — the same choice T-0.WF.01 makes for a document with no chain.

**How a later phase posts** (T-1.INV.07, T-2.AP.*, T-3.*, T-4.*, T-5.* — the
worked example these tasks follow):

```python
entry = post_journal_entry(
    session,
    company_id=company_id,
    posting_date=movement.posting_date,
    currency=movement.currency,
    source_type="stock_movement",          # the document that produced it
    source_id=movement.id,
    lines=[
        {
            "account": mapped_account(session, company_id=company_id, key="inventory").code,
            "debit": movement.value,
        },
        {
            "account": mapped_account(session, company_id=company_id, key="stock_adjustment").code,
            "credit": movement.value,
        },
    ],
)
```

Three rules come with it, and they are the task's own criteria:

1. **One interface.** Nothing outside `app/ledger/` writes the two tables; the
   check scans the tree for another writer and fails if it finds one.
2. **Refusals are the primitive's.** An unbalanced or single-line call is refused
   there, for every module alike.
3. **A document and its postings commit together.** `post_journal_entry` flushes
   in the caller's transaction and never commits, so a document that fails after
   posting leaves no entry behind. The check proves it by failing a document on
   purpose.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint, Uuid, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db import Base
from app.ledger.accounts import Account, account_by_code


class MappingError(ValueError):
    """The account mapping refused what was asked of it."""


class MissingMappingError(MappingError):
    """A module posted to a key this company has not mapped — refused, not guessed."""


class AccountMapping(Base):
    """One account the platform books to, named by the key the posting module uses."""

    __tablename__ = "account_mapping"
    __table_args__ = (
        UniqueConstraint("company_id", "key", name="uq_account_mapping_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    # The key a module posts with ("inventory", "stock_adjustment", "cogs",
    # "fx_gain", "fx_loss", …). No enumeration in code: a phase names its own
    # keys, and a new one is a row, not a release.
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("account.id"), nullable=False, index=True
    )


def set_mapping(
    session: Session, *, company_id: uuid.UUID, key: str, account_code: str
) -> AccountMapping:
    """Point one key at one account, re-pointing it if it is already mapped.

    The account is resolved through T-1.ACCT.01, so a key can only point at an
    account this company really has — a typo is refused here rather than at the
    first posting.
    """
    account = account_by_code(session, company_id=company_id, code=account_code)
    mapping = session.scalar(
        select(AccountMapping).where(
            AccountMapping.company_id == company_id, AccountMapping.key == str(key)
        )
    )
    if mapping is None:
        mapping = AccountMapping(company_id=company_id, key=str(key), account_id=account.id)
        session.add(mapping)
    else:
        mapping.account_id = account.id
    session.flush()
    return mapping


def mapped_account(
    session: Session, *, company_id: uuid.UUID, key: str
):
    """The account a key is mapped to, or a refusal naming the key.

    This is what a posting module calls. An unmapped key raises rather than
    falling back to a default account: a posting booked to a guessed account is
    wrong in a way nobody notices until the statements are read.
    """
    mapping = session.scalar(
        select(AccountMapping).where(
            AccountMapping.company_id == company_id, AccountMapping.key == str(key)
        )
    )
    if mapping is None:
        raise MissingMappingError(
            f"no account is mapped for {key!r} in this company; map it before posting"
            " (T-1.ACCT.03: set_mapping) rather than booking to a guessed account"
        )
    account = session.get(Account, mapping.account_id)
    if account is None:
        raise MissingMappingError(
            f"the account mapped for {key!r} is not readable in this company —"
            " re-point the mapping"
        )
    return account


def mappings(session: Session, *, company_id: uuid.UUID) -> list[AccountMapping]:
    """Every key this company has mapped, in key order — what a settings screen reads."""
    return list(
        session.scalars(
            select(AccountMapping)
            .where(AccountMapping.company_id == company_id)
            .order_by(AccountMapping.key)
        )
    )
