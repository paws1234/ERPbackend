"""T-2.PROC.01 — the supplier master: a role on the shared party, plus its own data.

§5 puts **Party (Customer / Supplier / Employee)** first: a counterparty is created
once and given the roles it holds. T-0.PARTY.01 owns that identity, so nothing here
duplicates a name, a tax number or an address. What this module owns is the
procurement-specific half of a supplier — the terms and details a purchase order,
a payment and a tax check need:

* **payment terms and the transaction currency** a supplier is dealt with in;
* **contacts** — who to send the RFQ to and who confirms a delivery;
* **bank details** — where a payment run pays, validated at entry rather than when
  a payment is attempted;
* **tax identifiers** — the registration kinds `tax_pack` says a document needs
  (T-2.PROC.08 applies the pack's rules; this table only stores the values).

Three rules the module is built around:

* **One profile per party.** A supplier is a *view* of a party, so a party that is
  both a customer and a supplier has one identity, one tax number and two roles —
  never two records that drift apart. Creating a second profile for the same party
  is refused.
* **Validated at entry.** A malformed e-mail, SWIFT code, bank account number or
  blank tax identifier is refused where it is written, so a payment run never meets
  one. A currency the company has not registered (T-1.ACCT.05) is refused too — an
  unlisted currency is a typo, not a currency.
* **Retired by marking, and only when nothing points at it.** A supplier follows the
  T-0.AUDIT.01 master convention (`soft_delete`, no `DELETE`), and
  :func:`retire_supplier` refuses while a document still names it — the row has to
  stay for the orders and invoices filed under it.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
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

# The role a supplier's party must hold — T-0.PARTY.01's own vocabulary.
SUPPLIER_ROLE = "supplier"

# The tax registrations a supplier may carry. The *kinds* a document needs are the
# pack's business (T-2.PROC.08); these are the values a Philippine installation
# actually holds, and a kind outside the list is refused rather than stored.
TAX_IDENTIFIER_KINDS = ("tin", "vat", "branch_code", "withholding")

# Loose but real: something@something.tld, and nothing with whitespace in it.
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# ISO 9362: 8 or 11 characters, letters and digits, no separators.
_SWIFT = re.compile(r"^[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?$")
# A bank account number: digits, with the separators banks print but never store
# semantics in. Four characters is the shortest real one this platform has met.
_ACCOUNT = re.compile(r"^[0-9][0-9\- ]{3,33}[0-9]$")


class SupplierError(ValueError):
    """The supplier master refused what was asked of it."""


class InvalidSupplierDataError(SupplierError):
    """A contact, bank or tax detail failed validation at entry."""


class UnknownSupplierError(SupplierError):
    """A lookup named a supplier this company does not have."""


class NotASupplierError(SupplierError):
    """A party without the supplier role was treated as one."""


class DuplicateSupplierError(SupplierError):
    """That party already has a supplier profile — one identity, one profile."""


class SupplierInUseError(SupplierError):
    """A supplier a document still names cannot be retired."""


class Supplier(SoftDeleteMixin, Base):
    """The procurement half of one supplier party."""

    __tablename__ = "supplier"
    __table_args__ = (
        # One profile per party: a second one is how one supplier becomes two.
        UniqueConstraint("company_id", "party_id", name="uq_supplier_company_party"),
        CheckConstraint("payment_terms_days >= 0", name="ck_supplier_payment_terms"),
        CheckConstraint(
            "transaction_currency IS NULL OR char_length(transaction_currency) = 3",
            name="ck_supplier_currency_length",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    party_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("party.id"), nullable=False, index=True
    )
    # Days from the invoice date until payment is due — what AP aging buckets and a
    # payment run read. Stated per supplier, with no default: the plan leaves
    # supplier terms to the user (`payment_terms`), and "0" is a real answer meaning
    # due on receipt, so a default would be indistinguishable from an unstated one.
    payment_terms_days: Mapped[int] = mapped_column(Integer, nullable=False)
    # The currency this supplier invoices in. Null means the company's base
    # currency — the honest way to say "no foreign currency", rather than storing a
    # copy of the base currency that a base-currency change would silently date.
    transaction_currency: Mapped[str | None] = mapped_column(String(3))

    party: Mapped[Party] = relationship()

    contacts: Mapped[list[SupplierContact]] = relationship(
        back_populates="supplier", order_by="SupplierContact.name"
    )
    bank_accounts: Mapped[list[SupplierBankAccount]] = relationship(
        back_populates="supplier", order_by="SupplierBankAccount.bank_name"
    )
    tax_identifiers: Mapped[list[SupplierTaxIdentifier]] = relationship(
        back_populates="supplier", order_by="SupplierTaxIdentifier.kind"
    )


class SupplierContact(Base):
    """One person at a supplier — who an RFQ goes to, who confirms a receipt."""

    __tablename__ = "supplier_contact"
    __table_args__ = (
        # At most one primary contact per supplier, enforced by the database rather
        # than by hoping two writers agree.
        Index(
            "uq_supplier_primary_contact",
            "supplier_id",
            unique=True,
            postgresql_where=text("is_primary"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("supplier.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    position: Mapped[str | None] = mapped_column(String(80))
    email: Mapped[str | None] = mapped_column(String(160))
    phone: Mapped[str | None] = mapped_column(String(40))
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    supplier: Mapped[Supplier] = relationship(back_populates="contacts")


class SupplierBankAccount(Base):
    """Where a payment run pays: one supplier's account at one bank."""

    __tablename__ = "supplier_bank_account"
    __table_args__ = (
        Index(
            "uq_supplier_primary_bank_account",
            "supplier_id",
            unique=True,
            postgresql_where=text("is_primary"),
        ),
        UniqueConstraint(
            "supplier_id", "bank_name", "account_number", name="uq_supplier_bank_once"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("supplier.id"), nullable=False, index=True
    )
    bank_name: Mapped[str] = mapped_column(String(120), nullable=False)
    account_name: Mapped[str] = mapped_column(String(160), nullable=False)
    account_number: Mapped[str] = mapped_column(String(40), nullable=False)
    # BIC/SWIFT, where the payment crosses a border. The pack's `bank_file_format`
    # says what a payment file needs (T-2.AP.04); this column only stores it.
    swift: Mapped[str | None] = mapped_column(String(11))
    currency: Mapped[str | None] = mapped_column(String(3))
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    supplier: Mapped[Supplier] = relationship(back_populates="bank_accounts")


class SupplierTaxIdentifier(Base):
    """One tax registration of a supplier — a TIN, a VAT registration, a branch code."""

    __tablename__ = "supplier_tax_identifier"
    __table_args__ = (
        UniqueConstraint(
            "supplier_id", "kind", "value", name="uq_supplier_tax_identifier_once"
        ),
        CheckConstraint(
            "kind IN (" + ", ".join(f"'{kind}'" for kind in TAX_IDENTIFIER_KINDS) + ")",
            name="ck_supplier_tax_identifier_kind",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("supplier.id"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    value: Mapped[str] = mapped_column(String(32), nullable=False)

    supplier: Mapped[Supplier] = relationship(back_populates="tax_identifiers")


# A supplier is a master: retired by marking, never removed (T-0.AUDIT.01).
deny_hard_delete(Supplier.__table__)

# The supplier's own detail tables: they belong to the profile, so they are not
# "documents that reference it" when it is retired.
_OWN_DETAIL_TABLES = (
    SupplierContact.__tablename__,
    SupplierBankAccount.__tablename__,
    SupplierTaxIdentifier.__tablename__,
)


def _required(value: Any, what: str) -> str:
    text_value = "" if value is None else str(value).strip()
    if not text_value:
        raise InvalidSupplierDataError(f"{what} is required")
    return text_value


def _currency_or_base(session: Session, company_id: uuid.UUID, code: str | None) -> str | None:
    """The stated currency, or a refusal — never a guessed one.

    Reuses T-1.ACCT.05's master, so a supplier can only be dealt with in a currency
    this company has registered. ``None`` is "the company's base currency", which is
    a real answer and is stored as such.
    """
    if code is None:
        return None
    registered = currency_by_code(session, company_id=company_id, code=code)
    return registered.code


def create_supplier(
    session: Session,
    *,
    company_id: uuid.UUID,
    party_code: str,
    payment_terms_days: int,
    name: str | None = None,
    tax_id: str | None = None,
    transaction_currency: str | None = None,
) -> Supplier:
    """Give a party the supplier role and its procurement profile.

    The party is created if this company does not have it yet (with the supplier
    role), or — when it exists — simply given the role, so a party that is already a
    customer becomes a supplier **without a second record**. Either way there is one
    profile per party, and a second attempt for the same party is refused.
    """
    terms = int(payment_terms_days)
    if terms < 0:
        raise InvalidSupplierDataError(
            f"payment terms are a number of days, not {payment_terms_days!r}"
        )
    currency = _currency_or_base(
        session, company_id, None if transaction_currency is None else str(transaction_currency)
    )

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
        party.roles = [PartyRole(role=SUPPLIER_ROLE)]
        session.add(party)
        session.flush()
    elif not party.has_role(SUPPLIER_ROLE):
        # One more role on the same identity — the whole point of §5's Party.
        party.roles.append(PartyRole(role=SUPPLIER_ROLE))
        session.flush()
    elif tax_id is not None and party.tax_id is None:
        party.tax_id = str(tax_id).strip()

    if session.scalar(select(Supplier).where(Supplier.party_id == party.id)) is not None:
        raise DuplicateSupplierError(
            f"party {party.code!r} already has a supplier profile; one party, one profile"
            " — edit that one instead (T-2.PROC.01)"
        )

    supplier = Supplier(
        company_id=company_id,
        party_id=party.id,
        payment_terms_days=terms,
        transaction_currency=currency,
    )
    session.add(supplier)
    session.flush()
    return supplier


def supplier_by_code(session: Session, *, company_id: uuid.UUID, code: str) -> Supplier:
    """The live supplier a document names by its party code, or a refusal."""
    party = party_by_code(session, company_id=company_id, code=code)
    supplier = session.scalar(
        select(Supplier).where(
            Supplier.company_id == company_id, Supplier.party_id == party.id
        )
    )
    if supplier is None:
        raise NotASupplierError(
            f"party {code!r} is not a supplier in this company; give it the supplier role"
            " first (T-2.PROC.01)"
        )
    return supplier


def set_transaction_currency(
    session: Session, supplier: Supplier, *, currency: str | None
) -> Supplier:
    """Change the currency this supplier is dealt with in, validated like the first."""
    supplier.transaction_currency = _currency_or_base(session, supplier.company_id, currency)
    session.flush()
    return supplier


def add_contact(
    session: Session,
    supplier: Supplier,
    *,
    name: str,
    position: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    is_primary: bool = False,
) -> SupplierContact:
    """Add one contact, with its e-mail validated at entry.

    A new primary demotes the previous one in the same transaction, so the partial
    unique index can never be violated by a legitimate change of contact.
    """
    contact = SupplierContact(
        company_id=supplier.company_id,
        supplier_id=supplier.id,
        name=_required(name, "a contact's name"),
        position=None if position is None else _required(position, "a position"),
        email=None if email is None else _required(email, "an e-mail").lower(),
        phone=None if phone is None else _required(phone, "a phone number"),
        is_primary=bool(is_primary),
    )
    if contact.email is not None and not _EMAIL.match(contact.email):
        raise InvalidSupplierDataError(f"not an e-mail address: {contact.email!r}")
    if contact.is_primary:
        _demote(session, SupplierContact, supplier.id)
    session.add(contact)
    session.flush()
    return contact


def add_bank_account(
    session: Session,
    supplier: Supplier,
    *,
    bank_name: str,
    account_name: str,
    account_number: str,
    swift: str | None = None,
    currency: str | None = None,
    is_primary: bool = False,
) -> SupplierBankAccount:
    """Add one bank account, validated where it is written.

    An account number that is not an account number, or a SWIFT code that is not
    eight or eleven characters, is refused **here** — the alternative is discovering
    it when a payment run reaches the bank.
    """
    account = SupplierBankAccount(
        company_id=supplier.company_id,
        supplier_id=supplier.id,
        bank_name=_required(bank_name, "a bank name"),
        account_name=_required(account_name, "an account name"),
        account_number=_required(account_number, "an account number"),
        swift=None if swift is None else _required(swift, "a SWIFT code").upper(),
        currency=_currency_or_base(session, supplier.company_id, currency),
        is_primary=bool(is_primary),
    )
    if not _ACCOUNT.match(account.account_number):
        raise InvalidSupplierDataError(
            f"not a bank account number: {account.account_number!r}"
        )
    if account.swift is not None and not _SWIFT.match(account.swift):
        raise InvalidSupplierDataError(
            f"not a BIC/SWIFT code (8 or 11 characters): {account.swift!r}"
        )
    if account.is_primary:
        _demote(session, SupplierBankAccount, supplier.id)
    session.add(account)
    session.flush()
    return account


def add_tax_identifier(
    session: Session, supplier: Supplier, *, kind: str, value: str
) -> SupplierTaxIdentifier:
    """Record one tax registration, refusing an unknown kind or a blank value."""
    wanted = _required(kind, "a tax identifier kind").lower()
    if wanted not in TAX_IDENTIFIER_KINDS:
        raise InvalidSupplierDataError(
            f"unknown tax identifier kind {kind!r}; a supplier holds"
            f" {', '.join(TAX_IDENTIFIER_KINDS)}"
        )
    identifier = SupplierTaxIdentifier(
        company_id=supplier.company_id,
        supplier_id=supplier.id,
        kind=wanted,
        value=_required(value, "a tax identifier value"),
    )
    session.add(identifier)
    session.flush()
    return identifier


def primary_contact(supplier: Supplier) -> SupplierContact | None:
    """The contact an RFQ goes to, or ``None`` when the supplier has not named one."""
    return next((contact for contact in supplier.contacts if contact.is_primary), None)


def primary_bank_account(supplier: Supplier) -> SupplierBankAccount | None:
    """The account a payment run pays, or ``None`` — never a guess among several."""
    return next(
        (account for account in supplier.bank_accounts if account.is_primary), None
    )


def _demote(session: Session, model: Any, supplier_id: uuid.UUID) -> None:
    """Clear the current primary of its flag, so a new one can take it."""
    for row in session.scalars(
        select(model).where(model.supplier_id == supplier_id, model.is_primary)
    ):
        row.is_primary = False
    session.flush()


def documents_naming(session: Session, supplier: Supplier) -> list[str]:
    """The tables holding a foreign key to this supplier and at least one row for it.

    Scanned from the schema rather than from a list kept by hand: the moment
    T-2.PROC.06 adds a purchase order, or T-2.AP.01 a supplier invoice, retiring a
    supplier they name starts being refused without anyone remembering to register
    the new table here.
    """
    engine = session.get_bind()
    scanning = inspect(engine)
    referencing: list[str] = []
    for table_name in scanning.get_table_names():
        if table_name in _OWN_DETAIL_TABLES or table_name == Supplier.__tablename__:
            continue
        for foreign_key in scanning.get_foreign_keys(table_name):
            if foreign_key["referred_table"] != Supplier.__tablename__:
                continue
            column = foreign_key["constrained_columns"][0]
            table = Base.metadata.tables.get(table_name)
            if table is None:  # pragma: no cover — a table outside this metadata
                continue
            found = session.scalar(
                select(func.count()).select_from(table).where(table.c[column] == supplier.id)
            )
            if found:
                referencing.append(table_name)
            break
    return sorted(referencing)


def retire_supplier(
    session: Session, supplier: Supplier, *, at: datetime | None = None
) -> Supplier:
    """Retire a supplier by marking it — refused while a document still names it.

    The row has to stay for the orders and invoices filed under it, so removal is
    not an option at all; what this function decides is whether the supplier has
    *finished*. A supplier that a purchase order or an invoice still points at is
    not finished, and marking it retired would make those documents read as if they
    were filed against a supplier that had never existed.
    """
    naming = documents_naming(session, supplier)
    if naming:
        raise SupplierInUseError(
            f"supplier {supplier.party.code!r} is named by {', '.join(naming)}; it cannot"
            " be retired while those documents stand"
        )
    soft_delete(session, supplier, at=at)
    session.flush()
    return supplier
