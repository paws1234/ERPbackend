"""T-0.CORE.03 — the company master and its fiscal calendar.

One row per legal entity.  ``company.id`` is the scoping dimension every other
master and posting table carries as ``company_id``; the convention that enforces
the dimension, and the database-level isolation that goes with it, live in
:mod:`app.db` — registered on the shared metadata, so no model module can build
a schema without them.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, CheckConstraint, SmallInteger, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.audit import SoftDeleteMixin, deny_hard_delete
from app.db import Base

# The costing methods §2.2 names. `costing_method` is decided **per company**
# (`valuation_scope`, decided 2026-09-17), which is why the column is here and not
# on the item (DOMAIN-MODELS.md §5.1).
COSTING_METHODS = ("fifo", "moving_average", "standard_cost")
DEFAULT_COSTING_METHOD = "moving_average"


# The credit-check policies a company may state, applied when a sales order is
# confirmed (T-3.SALES.04). Plan §8 lists the credit-check mode among what is **still
# undecided**, so the column has no default and `None` — unstated — is a real state,
# not a synonym for `off`.
CREDIT_CHECK_MODES = ("off", "warn", "block")


class UnknownCompanyError(ValueError):
    """A posting or lookup named a company that does not exist."""


class UnknownCreditCheckMode(ValueError):
    """A credit-check mode that is not one of the three the plan names."""


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
        CheckConstraint(
            "credit_check_mode IS NULL OR credit_check_mode IN ("
            + ", ".join(f"'{mode}'" for mode in CREDIT_CHECK_MODES)
            + ")",
            name="ck_company_credit_check_mode",
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
    # What this company does when a confirmed sales order breaches the customer's
    # credit limit (T-3.SALES.04). **No default, on purpose**: plan §8 leaves the
    # credit-check mode undecided, so a company states its own policy rather than
    # inheriting an invented one — the same reasoning as `fiscal_year_start_month`
    # above. `None` is *unstated*, which is not `off`: `off` is the policy "check
    # nothing", while unstated means no policy has been agreed, and the order-time
    # check refuses until one is.
    credit_check_mode: Mapped[str | None] = mapped_column(String(8))
    # T-3.POS.03's variable: whether a till has a cash drawer a shift has to be opened
    # for. Null means *not stated*, and unlike an unstated credit policy that is the
    # permissive state — a shop selling without drawer management is the ordinary case,
    # and requiring a shift is the deliberate act. A `False` here is therefore the same
    # policy as null; it is stored separately so the answer somebody gave is kept
    # rather than inferred from silence.
    cash_drawer_required: Mapped[bool | None] = mapped_column(Boolean)


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


def credit_check_mode_of(session, *, company_id: uuid.UUID) -> str | None:
    """This company's credit-check mode, or ``None`` when it has stated none.

    Returns the stored mode rather than a default, because there is no default to
    return: the caller has to decide what "unstated" means for it (T-3.SALES.04
    refuses the confirmation rather than inventing a policy).
    """
    company = session.get(Company, company_id)
    if company is None:
        raise UnknownCompanyError(f"no company {company_id}")
    return company.credit_check_mode


def cash_drawer_required_for(session, *, company_id: uuid.UUID) -> bool:
    """Whether this company's tills must trade inside an open shift (T-3.POS.03).

    Unstated reads as ``False``: a till with no drawer management is the common case,
    and the doubt an unstated *credit* policy creates — judging a customer — does not
    arise here. Stating it is what makes a shift mandatory.
    """
    company = session.get(Company, company_id)
    if company is None:
        raise UnknownCompanyError(f"no company {company_id}")
    return bool(company.cash_drawer_required)


def set_cash_drawer_required(
    session, company: Company, *, required: bool | None
) -> Company:
    """State, change or withdraw whether this company's tills need a shift.

    Withdrawing with ``None`` returns the company to *unstated*, which a till reads as
    "no shift required" — the same behaviour as ``False``, kept apart so the answer
    that was given is not confused with the answer nobody gave.
    """
    if required is None:
        company.cash_drawer_required = None
    else:
        company.cash_drawer_required = bool(required)
    session.flush()
    return company


def set_credit_check_mode(session, company: Company, *, mode: str | None) -> Company:
    """State, change or withdraw this company's credit-check mode.

    Withdrawing with ``None`` returns the company to *unstated*, which the order-time
    check treats differently from ``off``. Changing the mode never restates a decision
    already taken: each decision recorded its own mode (T-3.SALES.04).
    """
    if mode is None:
        company.credit_check_mode = None
        session.flush()
        return company
    wanted = str(mode).strip().lower()
    if wanted not in CREDIT_CHECK_MODES:
        raise UnknownCreditCheckMode(
            f"{mode!r} is not a credit-check mode; state one of"
            f" {', '.join(CREDIT_CHECK_MODES)}, or null for no policy agreed"
        )
    company.credit_check_mode = wanted
    session.flush()
    return company
