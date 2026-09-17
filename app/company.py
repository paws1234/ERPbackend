"""T-0.CORE.03 — the company master and its fiscal calendar.

One row per legal entity.  ``company.id`` is the scoping dimension every other
master and posting table carries as ``company_id``; the convention that enforces
the dimension, and the database-level isolation that goes with it, live in
:mod:`app.db` — registered on the shared metadata, so no model module can build
a schema without them.
"""

from __future__ import annotations

import uuid

from sqlalchemy import CheckConstraint, SmallInteger, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Company(Base):
    """One legal entity: the company that owns every other row in the schema."""

    __tablename__ = "company"
    __table_args__ = (
        CheckConstraint(
            "fiscal_year_start_month BETWEEN 1 AND 12",
            name="ck_company_fiscal_year_start_month",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Short human key, unique across the installation — what a person types.
    code: Mapped[str] = mapped_column(String(16), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # Reporting currency of this company (variable `base_currency`).
    base_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    # Fiscal calendar, per company: the month its fiscal year opens in. The 1st is
    # implied. Deliberately **no default** — plan §8 leaves the Philippines' fiscal
    # year start undecided, so creating a company has to state it rather than
    # inherit an invented January.
    # ponytail: no fiscal-period rows. Ceiling: period locking and statements need
    # per-company periods. Upgrade path: derive them from this month when
    # T-1.ACCT.* builds the monthly period lock.
    fiscal_year_start_month: Mapped[int] = mapped_column(SmallInteger, nullable=False)
