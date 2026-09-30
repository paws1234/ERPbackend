"""T-3.SALES.01 — the customer master: a role on the shared party, plus its own data.

§5 puts **Party (Customer / Supplier / Employee)** first: a counterparty is created
once and given the roles it holds. T-0.PARTY.01 owns that identity, so nothing here
duplicates a name, a tax number or a role. This module owns the *selling-side* half
of a customer — the data a quotation, an order, a credit check and a dunning run
read, and which Phase 2's supplier master deliberately kept off the party for the
same reason it keeps this off it:

* **payment terms and the transaction currency** the customer is dealt with in;
* **a credit limit**, in the one shape that can tell "no limit agreed" from
  "agreed: no credit";
* **contacts** — who a quotation and a dunning reminder go to;
* **addresses** — a small library the customer owns, so a document points at an
  address row instead of re-typing one (and two documents can point at the same
  one).

Three rules the module is built around:

* **One profile per party.** A customer is a *view* of a party, so a party that is
  both a customer and a supplier is one identity with two roles and one tax number
  — never two records that drift apart. Creating a second profile for the same
  party is refused, exactly as T-2.PROC.01 refuses a second supplier profile.
* **The limit has three states, not two.** ``NULL`` is *no limit agreed*, ``0`` is
  *no credit at all*, and a positive amount is a real ceiling. Collapsing the first
  two into zero is how an un-agreed account silently gets refused a sale, so the
  column is nullable and :func:`credit_limit_of` reports the difference rather than
  a number the caller has to interpret.
* **Retired by marking, and only when nothing points at it.** A customer follows the
  T-0.AUDIT.01 master convention (`soft_delete`, no `DELETE`), and
  :func:`retire_customer` refuses while a document still names it — the row has to
  stay for the quotations, orders and invoices filed under it.

Enforcement of the limit is **not** here: T-3.AR.06 owns the exposure calculation
and T-3.SALES.04 the order-time decision. This module stores the limit and the data
those two read.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    func,
    inspect,
    select,
    text,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.audit import SoftDeleteMixin, deny_hard_delete, soft_delete
from app.db import Base
from app.ledger.currency import currency_by_code
from app.party import Party, PartyRole, party_by_code

# The role a customer's party must hold — T-0.PARTY.01's own vocabulary.
CUSTOMER_ROLE = "customer"

# The address kinds a document can ask for. "Reusable across documents" needs a
# kind: a quotation states a billing address and a delivery note a shipping one,
# and they are routinely different. A kind outside this list is refused rather
# than stored, so a typo cannot become a fourth category nobody reports on.
ADDRESS_KINDS = ("billing", "shipping")

# Exact decimals, like every amount in the platform (DOMAIN-MODELS.md §2).
MONEY = Numeric(20, 6)

# Loose but real: something@something.tld, and nothing with whitespace in it.
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# ISO 3166-1 alpha-2 is two letters. The *assigned* set is not enumerated here.
# ponytail: a well-formed but unassigned code (`ZZ`) still passes. Ceiling: the
# format guard only. Upgrade path: validate against the localization pack's own
# country list once it carries one, which is where a market's codes belong.
_ALPHA2 = re.compile(r"^[A-Z]{2}$")


class CustomerError(ValueError):
    """The customer master refused what was asked of it."""


class InvalidCustomerDataError(CustomerError):
    """A contact, address or limit failed validation at entry."""


class UnknownCustomerError(CustomerError):
    """A lookup named a customer this company does not have."""


class NotACustomerError(CustomerError):
    """A party without the customer role was treated as one."""


class DuplicateCustomerError(CustomerError):
    """That party already has a customer profile — one identity, one profile."""


class CustomerInUseError(CustomerError):
    """A customer a document still names cannot be retired."""


class Customer(SoftDeleteMixin, Base):
    """The selling-side half of one customer party."""

    __tablename__ = "customer"
    __table_args__ = (
        # One profile per party: a second one is how one customer becomes two.
        UniqueConstraint("company_id", "party_id", name="uq_customer_company_party"),
        CheckConstraint("payment_terms_days >= 0", name="ck_customer_payment_terms"),
        CheckConstraint(
            "transaction_currency IS NULL OR char_length(transaction_currency) = 3",
            name="ck_customer_currency_length",
        ),
        # A negative ceiling is not a limit. NULL stays allowed: it is the third
        # state, and a CHECK passes on NULL by definition.
        CheckConstraint("credit_limit IS NULL OR credit_limit >= 0", name="ck_customer_credit_limit"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    party_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("party.id"), nullable=False, index=True
    )
    # Days from the invoice date until payment is due — what AR aging buckets, the
    # dunning run and the credit exposure all read. Stated per customer, with no
    # default: the plan leaves terms to the user (`payment_terms`), and "0" is a
    # real answer meaning due on receipt, so a default would be indistinguishable
    # from an unstated one.
    payment_terms_days: Mapped[int] = mapped_column(Integer, nullable=False)
    # The currency this customer is invoiced in. Null means the company's base
    # currency — the honest way to say "no foreign currency", rather than storing a
    # copy of the base currency that a base-currency change would silently date.
    transaction_currency: Mapped[str | None] = mapped_column(String(3))
    # The agreed ceiling, as an exact decimal.
    #   NULL          no limit agreed (the account has not been through credit review)
    #   0             no credit at all (prepayment or cash only)
    #   > 0           a real ceiling; T-3.AR.06 pauses new commitments above it
    credit_limit: Mapped[Decimal | None] = mapped_column(MONEY)

    party: Mapped[Party] = relationship()

    contacts: Mapped[list[CustomerContact]] = relationship(
        back_populates="customer", order_by="CustomerContact.name"
    )
    addresses: Mapped[list[CustomerAddress]] = relationship(
        back_populates="customer", order_by="CustomerAddress.line1"
    )


class CustomerContact(Base):
    """One person at a customer — who a quotation and a reminder go to."""

    __tablename__ = "customer_contact"
    __table_args__ = (
        # At most one primary contact per customer, enforced by the database rather
        # than by hoping two writers agree.
        Index(
            "uq_customer_primary_contact",
            "customer_id",
            unique=True,
            postgresql_where=text("is_primary"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customer.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    position: Mapped[str | None] = mapped_column(String(80))
    email: Mapped[str | None] = mapped_column(String(160))
    phone: Mapped[str | None] = mapped_column(String(40))
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    customer: Mapped[Customer] = relationship(back_populates="contacts")


class CustomerAddress(Base):
    """One address the customer owns, reusable by every document filed under it.

    The point of a row rather than free text on each document: an order, its
    delivery note and its invoice all point at the *same* address, so correcting a
    street name is one edit and not three, and a report can ask "what did we ship to"
    without parsing anything.
    """

    __tablename__ = "customer_address"
    __table_args__ = (
        CheckConstraint(
            "kind IN (" + ", ".join(f"'{kind}'" for kind in ADDRESS_KINDS) + ")",
            name="ck_customer_address_kind",
        ),
        # One primary per kind: a customer may have a primary billing address and a
        # primary shipping address at the same time, but not two of either.
        Index(
            "uq_customer_primary_address_per_kind",
            "customer_id",
            "kind",
            unique=True,
            postgresql_where=text("is_primary"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customer.id"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    line1: Mapped[str] = mapped_column(String(160), nullable=False)
    line2: Mapped[str | None] = mapped_column(String(160))
    city: Mapped[str | None] = mapped_column(String(80))
    region: Mapped[str | None] = mapped_column(String(80))
    postal_code: Mapped[str | None] = mapped_column(String(16))
    # ISO 3166-1 alpha-2. No default: the pack says which country this installation
    # is in (`philippines`), but stamping a country onto an address nobody stated is
    # how a cross-border delivery silently becomes domestic.
    country: Mapped[str | None] = mapped_column(String(2))
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    customer: Mapped[Customer] = relationship(back_populates="addresses")


# A customer is a master: retired by marking, never removed (T-0.AUDIT.01).
deny_hard_delete(Customer.__table__)

# The customer's own detail tables: they belong to the profile, so they are not
# "documents that reference it" when it is retired.
_OWN_DETAIL_TABLES = (
    CustomerContact.__tablename__,
    CustomerAddress.__tablename__,
)


def _required(value: Any, what: str) -> str:
    text_value = "" if value is None else str(value).strip()
    if not text_value:
        raise InvalidCustomerDataError(f"{what} is required")
    return text_value


def _currency_or_base(session: Session, company_id: uuid.UUID, code: str | None) -> str | None:
    """The stated currency, or a refusal — never a guessed one.

    Reuses T-1.ACCT.05's master, so a customer can only be dealt with in a currency
    this company has registered. ``None`` is "the company's base currency", which is
    a real answer and is stored as such.
    """
    if code is None:
        return None
    registered = currency_by_code(session, company_id=company_id, code=code)
    return registered.code


def _limit_value(limit: Any) -> Decimal | None:
    """The stated ceiling as an exact decimal, or ``None`` when none is agreed.

    Accepts the decimal string the API boundary carries and the ``Decimal`` a caller
    already holds, and refuses anything else — a float ceiling is refused outright
    rather than silently converted, because that is the one conversion DOMAIN-MODELS
    §2 forbids for money.
    """
    if limit is None:
        return None
    if isinstance(limit, float):
        raise InvalidCustomerDataError(
            f"a credit limit is an exact decimal or a string, not the float {limit!r}"
        )
    try:
        value = limit if isinstance(limit, Decimal) else Decimal(str(limit).strip())
    except InvalidOperation as exc:
        raise InvalidCustomerDataError(
            f"not a credit limit: {limit!r}; give an exact decimal, or null for no limit"
        ) from exc
    if not value.is_finite():
        # Infinity would pass the sign test and NaN would raise out of the comparison
        # itself; neither is one of the three states the column is documented to hold.
        raise InvalidCustomerDataError(
            f"a credit limit must be a finite amount, not {value}; the states are null"
            " (no limit agreed), 0 (no credit) and a positive ceiling"
        )
    if value < 0:
        raise InvalidCustomerDataError(
            f"a credit limit is not negative: {value}; use null for no limit agreed,"
            " or 0 for no credit at all"
        )
    return value


def create_customer(
    session: Session,
    *,
    company_id: uuid.UUID,
    party_code: str,
    payment_terms_days: int = 0,
    name: str | None = None,
    tax_id: str | None = None,
    transaction_currency: str | None = None,
    credit_limit: Any = None,
) -> Customer:
    """Give a party the customer role and its selling profile.

    The party is created if this company does not have it yet (with the customer
    role), or — when it exists — simply given the role, so a party that is already a
    supplier becomes a customer **without a second record**. Either way there is one
    profile per party, and a second attempt for the same party is refused.
    """
    terms = int(payment_terms_days)
    if terms < 0:
        raise InvalidCustomerDataError(
            f"payment terms are a number of days, not {payment_terms_days!r}"
        )
    currency = _currency_or_base(
        session, company_id, None if transaction_currency is None else str(transaction_currency)
    )
    limit = _limit_value(credit_limit)

    party = session.scalar(
        select(Party).where(Party.company_id == company_id, Party.code == str(party_code))
    )
    if party is None:
        party = Party(
            company_id=company_id,
            code=str(party_code),
            name=_required(name, "a new party's name"),
            tax_id=None if tax_id is None else str(tax_id).strip(),
        )
        party.roles = [PartyRole(role=CUSTOMER_ROLE)]
        session.add(party)
        session.flush()
    elif not party.has_role(CUSTOMER_ROLE):
        # One more role on the same identity — the whole point of §5's Party.
        party.roles.append(PartyRole(role=CUSTOMER_ROLE))
        session.flush()
    elif tax_id is not None and party.tax_id is None:
        party.tax_id = str(tax_id).strip()

    if session.scalar(select(Customer).where(Customer.party_id == party.id)) is not None:
        raise DuplicateCustomerError(
            f"party {party.code!r} already has a customer profile; one party, one profile"
            " — edit that one instead (T-3.SALES.01)"
        )

    customer = Customer(
        company_id=company_id,
        party_id=party.id,
        payment_terms_days=terms,
        transaction_currency=currency,
        credit_limit=limit,
    )
    session.add(customer)
    session.flush()
    return customer


def customer_by_code(session: Session, *, company_id: uuid.UUID, code: str) -> Customer:
    """The live customer a document names by its party code, or a refusal."""
    party = party_by_code(session, company_id=company_id, code=code)
    customer = session.scalar(
        select(Customer).where(
            Customer.company_id == company_id, Customer.party_id == party.id
        )
    )
    if customer is None:
        raise NotACustomerError(
            f"party {code!r} is not a customer in this company; give it the customer role"
            " first (T-3.SALES.01)"
        )
    return customer


def credit_limit_of(customer: Customer) -> Decimal | None:
    """The agreed ceiling, or ``None`` when none has been agreed.

    The distinction the column exists for, stated once rather than left to every
    caller: ``None`` means the account has never been through credit review, and a
    caller that treats it as zero will refuse sales nobody agreed to refuse.
    """
    return None if customer.credit_limit is None else Decimal(customer.credit_limit)


def set_credit_limit(session: Session, customer: Customer, *, limit: Any) -> Customer:
    """Agree, change or withdraw the ceiling — validated exactly like the first.

    Passing ``None`` withdraws the limit (back to "no limit agreed"); passing ``0``
    is a decision, not a clearing, and the two are stored differently on purpose.
    """
    customer.credit_limit = _limit_value(limit)
    session.flush()
    return customer


def set_transaction_currency(
    session: Session, customer: Customer, *, currency: str | None
) -> Customer:
    """Change the currency this customer is invoiced in, validated like the first."""
    customer.transaction_currency = _currency_or_base(session, customer.company_id, currency)
    session.flush()
    return customer


def add_contact(
    session: Session,
    customer: Customer,
    *,
    name: str,
    position: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    is_primary: bool = False,
) -> CustomerContact:
    """Add one contact, with its e-mail validated at entry.

    A new primary demotes the previous one in the same transaction, so the partial
    unique index can never be violated by a legitimate change of contact.
    """
    contact = CustomerContact(
        company_id=customer.company_id,
        customer_id=customer.id,
        name=_required(name, "a contact's name"),
        position=None if position is None else _required(position, "a position"),
        email=None if email is None else _required(email, "an e-mail").lower(),
        phone=None if phone is None else _required(phone, "a phone number"),
        is_primary=bool(is_primary),
    )
    if contact.email is not None and not _EMAIL.match(contact.email):
        raise InvalidCustomerDataError(f"not an e-mail address: {contact.email!r}")
    if contact.is_primary:
        _demote(session, CustomerContact, customer.id, kind=None)
    session.add(contact)
    session.flush()
    return contact


def add_address(
    session: Session,
    customer: Customer,
    *,
    kind: str,
    line1: str,
    line2: str | None = None,
    city: str | None = None,
    region: str | None = None,
    postal_code: str | None = None,
    country: str | None = None,
    is_primary: bool = False,
) -> CustomerAddress:
    """Add one address the customer's documents may point at, validated at entry."""
    wanted = _required(kind, "an address kind").lower()
    if wanted not in ADDRESS_KINDS:
        raise InvalidCustomerDataError(
            f"unknown address kind {kind!r}; a customer holds"
            f" {', '.join(ADDRESS_KINDS)}"
        )
    address = CustomerAddress(
        company_id=customer.company_id,
        customer_id=customer.id,
        kind=wanted,
        line1=_required(line1, "the first address line"),
        line2=None if line2 is None else _required(line2, "the second address line"),
        city=None if city is None else _required(city, "a city"),
        region=None if region is None else _required(region, "a region"),
        postal_code=None if postal_code is None else _required(postal_code, "a postal code"),
        country=None if country is None else _required(country, "a country").upper(),
        is_primary=bool(is_primary),
    )
    if address.country is not None and not _ALPHA2.match(address.country):
        raise InvalidCustomerDataError(
            f"a country is an ISO 3166-1 alpha-2 code — two letters — not"
            f" {address.country!r}"
        )
    if address.is_primary:
        _demote(session, CustomerAddress, customer.id, kind=wanted)
    session.add(address)
    session.flush()
    return address


def primary_contact(customer: Customer) -> CustomerContact | None:
    """The contact a quotation and a dunning reminder go to, or ``None``."""
    return next((contact for contact in customer.contacts if contact.is_primary), None)


def address_for(customer: Customer, kind: str) -> CustomerAddress | None:
    """The customer's primary address of `kind`, or ``None`` — never a guess.

    What a document calls when it needs "the billing address": it stores the row's
    id, so the same address can back the order, the delivery note and the invoice
    without being re-typed into any of them.
    """
    return next(
        (
            address
            for address in customer.addresses
            if address.kind == str(kind).lower() and address.is_primary
        ),
        None,
    )


def _demote(
    session: Session, model: Any, customer_id: uuid.UUID, *, kind: str | None
) -> None:
    """Clear the current primary, so a new one can take the flag.

    `kind` narrows the demotion to one address kind; contacts pass ``None`` and are
    demoted across the board, because a customer has one primary contact and not one
    per kind.
    """
    statement = select(model).where(model.customer_id == customer_id, model.is_primary)
    if kind is not None:
        statement = statement.where(model.kind == kind)
    for row in session.scalars(statement):
        row.is_primary = False
    session.flush()


def documents_naming(session: Session, customer: Customer) -> list[str]:
    """The tables holding a foreign key to this customer and at least one row for it.

    Scanned from the schema rather than from a list kept by hand: the moment
    T-3.SALES.03 adds a quotation or T-3.AR.01 a customer invoice, retiring a
    customer they name starts being refused without anyone remembering to register
    the new table here.
    """
    engine = session.get_bind()
    scanning = inspect(engine)
    referencing: list[str] = []
    for table_name in scanning.get_table_names():
        if table_name in _OWN_DETAIL_TABLES or table_name == Customer.__tablename__:
            continue
        for foreign_key in scanning.get_foreign_keys(table_name):
            if foreign_key["referred_table"] != Customer.__tablename__:
                continue
            column = foreign_key["constrained_columns"][0]
            table = Base.metadata.tables.get(table_name)
            if table is None:  # pragma: no cover — a table outside this metadata
                continue
            found = session.scalar(
                select(func.count()).select_from(table).where(table.c[column] == customer.id)
            )
            if found:
                referencing.append(table_name)
            break
    return sorted(referencing)


def retire_customer(
    session: Session, customer: Customer, *, at: datetime | None = None
) -> Customer:
    """Retire a customer by marking it — refused while a document still names it.

    The row has to stay for the quotations, orders and invoices filed under it, so
    removal is not an option at all; what this function decides is whether the
    customer has *finished*.
    """
    naming = documents_naming(session, customer)
    if naming:
        raise CustomerInUseError(
            f"customer {customer.party.code!r} is named by {', '.join(naming)}; it cannot"
            " be retired while those documents stand"
        )
    soft_delete(session, customer, at=at)
    session.flush()
    return customer
