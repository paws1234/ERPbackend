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

from app.audit import SoftDeleteMixin, deny_hard_delete
from app.db import Base

# The costing methods §2.2 names. `costing_method` is decided **per company**
# (`valuation_scope`, decided 2026-09-17), which is why the column is here and not
# on the item (DOMAIN-MODELS.md §5.1).
COSTING_METHODS = ("fifo", "moving_average", "standard_cost")
DEFAULT_COSTING_METHOD = "moving_average"


class UnknownCompanyError(ValueError):
    """A posting or lookup named a company that does not exist."""


class Company(SoftDeleteMixin, Base):
    """One legal entity: the company that owns every other row in the schema.

    A master, so it follows the T-0.AUDIT.01 convention: retiring it marks
    ``deleted_at`` and the row stays for the postings filed under it — the
    database refuses to delete it.
    """

    __tablename__ = "company"
    __table_args__ = (
        CheckConstraint(
            "fiscal_year_start_month BETWEEN 1 AND 12",
            name="ck_company_fiscal_year_start_month",
        ),
        CheckConstraint(
            "costing_method IN ("
            + ", ".join(f"'{method}'" for method in COSTING_METHODS)
            + ")",
            name="ck_company_costing_method",
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
    # How this company values stock (T-1.INV.04). Per company, the decided
    # `valuation_scope`; the ledger is append-only, so changing it never rewrites
    # what was already valued — it changes what the next valuation reads.
    costing_method: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DEFAULT_COSTING_METHOD
    )


# Part of the master convention (T-0.AUDIT.01), registered here because this
# module owns the table: a company is retired by marking it, never by deleting it.
deny_hard_delete(Company.__table__)


def company_base_currency(session, *, company_id: uuid.UUID) -> str:
    """The reporting currency of a company — what every posting is measured against.

    The posting primitive asks for it to decide whether an entry is in a foreign
    currency and therefore needs a rate (T-1.ACCT.05). A company that does not
    exist is refused here, by name, rather than by a foreign key at COMMIT.
    """
    company = session.get(Company, company_id)
    if company is None:
        raise UnknownCompanyError(f"no company {company_id}")
    return company.base_currency
