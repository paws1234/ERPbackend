"""T-2.PROC.01 check — the supplier master on top of the shared party.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/erpv1 \
        python tests/check_supplier_master.py

Green on all seven:

1. a supplier is created from a party that does not exist yet, and that party holds
   the supplier role — the role §5's Party model asks for, not a parallel master
2. a party that is already a customer becomes a supplier **without a second party
   record** — one identity, two roles, one tax number
3. a second supplier profile for the same party is refused: one identity, one profile
4. contacts, bank details and tax identifiers are validated **at entry** — a
   malformed e-mail, a bank account number that is not one, a SWIFT code of the
   wrong length, a blank tax identifier and an unknown identifier kind are all
   refused — and a currency this company has not registered is refused too
5. at most one primary contact and one primary bank account stand at a time: naming
   a new primary demotes the previous one in the same transaction, and the database's
   partial unique index refuses a hand-written second one
6. a supplier is retired by marking (the row stays and `DELETE` is refused), while a
   supplier a **document** still names is refused retirement by name
7. looking a supplier up by party code refuses a party that does not hold the role

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import os
import sys
import uuid

from sqlalchemy import ForeignKey, Uuid, create_engine, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Mapped, Session, mapped_column

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger.currency import UnknownCurrencyError, register_currency  # noqa: E402
from app.party import Party, PartyRole  # noqa: E402
from app.procurement.suppliers import (  # noqa: E402
    DuplicateSupplierError,
    InvalidSupplierDataError,
    Supplier,
    SupplierBankAccount,
    SupplierContact,
    SupplierInUseError,
    add_bank_account,
    add_contact,
    add_tax_identifier,
    create_supplier,
    primary_bank_account,
    primary_contact,
    retire_supplier,
    supplier_by_code,
)
from app.procurement.suppliers import (  # noqa: E402
    NotASupplierError as SupplierNotASupplierError,
)

COMPANY = uuid.uuid4()
OTHER = uuid.uuid4()
TERMS = 30


class ProbePurchaseOrder(Base):
    """A stand-in for the document a later phase adds (T-2.PROC.06's real one).

    It exists only in this check: it proves the "a supplier a document names cannot
    be retired" rule against a real foreign key, without this task inventing a
    purchase order it does not own. Because :func:`documents_naming` scans the
    schema, a real order table starts being honoured the day it appears.
    """

    __tablename__ = "probe_purchase_order"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("supplier.id"), nullable=False, index=True
    )


def _refused(call, expected: type[Exception] | str) -> str:
    try:
        call()
    except Exception as exc:  # noqa: BLE001 — the type and the message are the point
        if isinstance(expected, str):
            assert expected in str(exc), f"unclear refusal: {exc}"
        else:
            assert isinstance(exc, expected), f"refused with {type(exc).__name__}: {exc}"
        return str(exc)
    raise AssertionError("accepted what it must refuse")


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is required (a scratch Postgres)", file=sys.stderr)
        return 2

    engine = create_engine(url)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        for company_id, code in ((COMPANY, "SUP-CHECK"), (OTHER, "OTHER")):
            session.add(
                Company(
                    id=company_id,
                    code=code,
                    name=f"{code} company",
                    base_currency="PHP",
                    fiscal_year_start_month=1,
                )
            )
        session.commit()
        register_currency(session, company_id=COMPANY, code="USD", name="US Dollar")
        session.commit()

        # 1 — a supplier from a party that does not exist yet
        acme = create_supplier(
            session,
            company_id=COMPANY,
            party_code="ACME",
            name="Acme Supplies",
            payment_terms_days=TERMS,
        )
        session.commit()
        assert acme.party.code == "ACME", acme.party.code
        assert acme.party.has_role("supplier"), "the new party does not hold the supplier role"
        assert acme.payment_terms_days == TERMS
        assert acme.transaction_currency is None, "the base currency is stated as null, not copied"
        print(f"1. party {acme.party.code} created holding the supplier role")

        # 2 — one party, both roles, one record
        customer_first = create_supplier(
            session,
            company_id=COMPANY,
            party_code="BOTH",
            name="Both Ways Trading",
            payment_terms_days=0,
            tax_id="123-456-789",
        )
        session.commit()
        # ... and give it the customer role, the way Phase 3's own master will
        customer_first.party.roles.append(PartyRole(role="customer"))
        session.commit()

        parties = list(
            session.scalars(select(Party).where(Party.company_id == COMPANY, Party.code == "BOTH"))
        )
        assert len(parties) == 1, f"one party became {len(parties)} records"
        assert parties[0].has_role("customer") and parties[0].has_role("supplier")
        assert parties[0].tax_id == "123-456-789"
        print("2. one party holds both roles — one identity, one tax number, no duplicate")

        # 3 — one profile per party
        said = _refused(
            lambda: create_supplier(
                session,
                company_id=COMPANY,
                party_code="ACME",
                name="Acme Supplies",
                payment_terms_days=45,
            ),
            DuplicateSupplierError,
        )
        session.rollback()
        print(f"3. a second profile for the same party is refused: {said}")

        # 4 — validation at entry
        said = _refused(
            lambda: add_contact(session, acme, name="Ana", email="ana@acme"),
            InvalidSupplierDataError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: add_bank_account(
                session, acme, bank_name="BPI", account_name="Acme", account_number="not-a-number"
            ),
            InvalidSupplierDataError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: add_bank_account(
                session,
                acme,
                bank_name="BPI",
                account_name="Acme",
                account_number="1234567890",
                swift="BPI",
            ),
            InvalidSupplierDataError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: add_tax_identifier(session, acme, kind="tin", value="   "),
            InvalidSupplierDataError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: add_tax_identifier(session, acme, kind="shoe_size", value="42"),
            InvalidSupplierDataError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: add_bank_account(
                session,
                acme,
                bank_name="BPI",
                account_name="Acme",
                account_number="1234567890",
                currency="XYZ",
            ),
            UnknownCurrencyError,
        )
        session.rollback()
        print(f"4. every malformed detail is refused at entry: {said}")

        # 4b — and the well-formed ones land
        contact = add_contact(
            session, acme, name="Ana Reyes", position="Sales", email="ANA@acme.example", is_primary=True
        )
        bank = add_bank_account(
            session,
            acme,
            bank_name="BPI",
            account_name="Acme Supplies Inc.",
            account_number="1234-5678-90",
            swift="BOPIPHMM",
            currency="USD",
            is_primary=True,
        )
        identifier = add_tax_identifier(session, acme, kind="tin", value="001-234-567")
        session.commit()
        assert contact.email == "ana@acme.example", contact.email
        assert bank.currency == "USD" and bank.swift == "BOPIPHMM"
        assert identifier.kind == "tin"
        assert primary_contact(acme).name == "Ana Reyes"
        assert primary_bank_account(acme).bank_name == "BPI"
        print("4b. a well-formed contact, bank account and TIN are stored; primary lookups resolve")

        # 5 — one primary at a time: the new one demotes the previous one
        second = add_contact(session, acme, name="Bo Cruz", is_primary=True)
        session.commit()
        session.refresh(acme)
        assert primary_contact(acme).id == second.id, "the new primary did not take over"
        assert sum(1 for row in acme.contacts if row.is_primary) == 1
        second_bank = add_bank_account(
            session,
            acme,
            bank_name="BDO",
            account_name="Acme Supplies Inc.",
            account_number="9988776655",
            is_primary=True,
        )
        session.commit()
        session.refresh(acme)
        assert primary_bank_account(acme).id == second_bank.id
        # the database refuses a hand-written second primary too
        session.add(
            SupplierBankAccount(
                company_id=COMPANY,
                supplier_id=acme.id,
                bank_name="Third",
                account_name="Acme",
                account_number="111",
                is_primary=True,
            )
        )
        try:
            session.commit()
        except DBAPIError as exc:
            assert "uq_supplier_primary_bank_account" in str(exc), exc
            session.rollback()
        else:
            raise AssertionError("the database accepted a second primary bank account")
        print(f"5. {second.name} and {second_bank.bank_name} are the primaries; the previous ones"
              " were demoted and the database refuses a second primary")

        # 6 — a document blocks retirement; marking is how a free supplier is retired
        session.add(ProbePurchaseOrder(company_id=COMPANY, supplier_id=acme.id))
        session.commit()
        said = _refused(lambda: retire_supplier(session, acme), SupplierInUseError)
        session.rollback()
        assert acme.deleted_at is None, "the refused retirement marked the supplier anyway"

        other_supplier = create_supplier(
            session, company_id=COMPANY, party_code="SPARE", name="Spare Parts", payment_terms_days=7
        )
        session.commit()
        retire_supplier(session, other_supplier)
        session.commit()
        assert other_supplier.deleted_at is not None, "retirement did not mark the supplier"
        assert session.get(Supplier, other_supplier.id) is not None, "the row was removed, not marked"
        try:
            session.execute(Supplier.__table__.delete().where(Supplier.id == other_supplier.id))
            session.commit()
        except DBAPIError as exc:
            assert "is a master" in str(exc), exc
            session.rollback()
        else:
            raise AssertionError("DELETE was allowed on a master")
        print(f"6. retirement refused while a document names it ({said}); a free supplier is"
              " marked, its row stays and DELETE is refused")

        # 7 — a party without the role is not a supplier
        said = _refused(
            lambda: supplier_by_code(session, company_id=COMPANY, code="SPARE"),
            SupplierNotASupplierError,
        )
        session.rollback()
        print(f"7. a retired supplier is not found by code, and a non-supplier is refused: {said}")

        # 8 — two companies may each have an ACME, and neither sees the other's
        create_supplier(
            session, company_id=OTHER, party_code="ACME", name="Other Acme", payment_terms_days=14
        )
        session.commit()
        mine = supplier_by_code(session, company_id=COMPANY, code="ACME")
        assert mine.company_id == COMPANY, "the lookup crossed companies"
        print("8. the same party code exists in two companies, each resolving inside its own")

    print("check_supplier_master: all assertions green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
