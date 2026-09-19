"""T-1.ACCT.04 — period locking: a closed month takes no more postings.

§2.1 asks for "Period locking and audit trail". `period_lock_granularity` is
**month** (decided 2026-09-17, plan §8), so a period is one calendar month of one
company and locking it means no posting may be dated inside it any more.

Three things this module is deliberate about:

* **The refusal lives in the posting primitive, not in a caller.** A lock that
  each module has to remember to check is a lock that one module will not check;
  :func:`app.ledger.posting.post_journal_entry` asks :func:`period_is_locked`
  before it writes anything, so every module — present and future — is covered by
  construction.
* **Locking changes no entry.** The ledger is append-only (T-0.AUDIT.01), and a
  lock is a row about a month, not a rewrite of what was posted in it. Closing a
  period says "nothing more", never "not what happened".
* **Unlocking is a permission, and both moves are attributed.** Unlocking calls
  T-0.SEC.01's `require` for `period.unlock` and needs a reason; the period row
  carries who changed it, when and why, and T-0.AUDIT.02's trigger keeps the
  before/after of every transition on the trail — so "who opened March again, and
  saying what" is answerable.

ponytail: month granularity only, the decided value. Ceiling: a company that wants
quarter or year locking. Upgrade path: read `period_lock_granularity` and widen
the key from (year, month) to the period the company configured.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db import Base
from app.security import require

# The states a period can be in.
OPEN, CLOSED = "open", "closed"

# The capability unlocking asks for (T-0.SEC.01) — locking is a normal accounting
# task, reopening a closed month is not.
UNLOCK_CAPABILITY = "period.unlock"


class PeriodError(ValueError):
    """The period could not be locked or unlocked as asked."""


class PeriodLockedError(PeriodError):
    """A posting was dated inside a closed period."""


class AccountingPeriod(Base):
    """One company's month, and whether it is open for posting.

    A row is created the first time a month is locked; a month nobody has locked
    has no row and is open. `changed_by`/`changed_at`/`reason` describe the last
    transition, and the audit trail (T-0.AUDIT.02) keeps every one before it.
    """

    __tablename__ = "accounting_period"
    __table_args__ = (
        UniqueConstraint("company_id", "year", "month", name="uq_accounting_period_month"),
        CheckConstraint("month BETWEEN 1 AND 12", name="ck_accounting_period_month"),
        CheckConstraint("state IN ('open', 'closed')", name="ck_accounting_period_state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    month: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(8), nullable=False)
    changed_by: Mapped[str] = mapped_column(String(64), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    reason: Mapped[str | None] = mapped_column(Text)


def _period(
    session: Session, *, company_id: uuid.UUID, year: int, month: int
) -> AccountingPeriod | None:
    return session.scalar(
        select(AccountingPeriod).where(
            AccountingPeriod.company_id == company_id,
            AccountingPeriod.year == year,
            AccountingPeriod.month == month,
        )
    )


def period_is_locked(
    session: Session, *, company_id: uuid.UUID, on: date
) -> bool:
    """Whether the month `on` falls in is closed for posting."""
    period = _period(session, company_id=company_id, year=on.year, month=on.month)
    return period is not None and period.state == CLOSED


def lock_period(
    session: Session,
    *,
    company_id: uuid.UUID,
    year: int,
    month: int,
    actor: str,
    reason: str | None = None,
) -> AccountingPeriod:
    """Close one month: no posting dated inside it is accepted any more.

    Existing entries are untouched — nothing here writes the ledger.
    """
    if not 1 <= month <= 12:
        raise PeriodError(f"{month!r} is not a month")
    period = _period(session, company_id=company_id, year=year, month=month)
    if period is None:
        period = AccountingPeriod(
            company_id=company_id,
            year=year,
            month=month,
            state=CLOSED,
            changed_by=str(actor),
            reason=reason,
        )
        session.add(period)
    else:
        period.state = CLOSED
        period.changed_by = str(actor)
        period.changed_at = datetime.now(timezone.utc)
        period.reason = reason
    session.flush()
    return period


def unlock_period(
    session: Session,
    *,
    company_id: uuid.UUID,
    year: int,
    month: int,
    actor: str,
    reason: str,
) -> AccountingPeriod:
    """Reopen one month — a permission (``period.unlock``) and a stated reason.

    Both are required: reopening a closed period is the kind of change somebody
    has to be able to explain afterwards, and the trail is where the explanation
    lives.
    """
    require(
        session,
        company_id=company_id,
        subject=actor,
        capability=UNLOCK_CAPABILITY,
        entity="accounting_period",
    )
    if not reason:
        raise PeriodError("unlocking a period needs a reason")
    period = _period(session, company_id=company_id, year=year, month=month)
    if period is None:
        raise PeriodError(f"{year}-{month:02d} is open; there is nothing to unlock")
    period.state = OPEN
    period.changed_by = str(actor)
    period.changed_at = datetime.now(timezone.utc)
    period.reason = reason
    session.flush()
    return period
