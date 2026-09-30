"""T-3.SALES.01 check — the customer master on top of the shared party.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/erpv1 \
        python tests/check_customer_master.py

Green on all seven:

1. a customer is created from a party that does not exist yet, and that party holds
   the customer role — the role §5's Party model asks for, not a parallel master
2. a party that is already a **supplier** becomes a customer **without a second
   party record** — one identity, two roles, one tax number
3. a second customer profile for the same party is refused: one identity, one profile
4. contacts and addresses are validated **at entry** — a malformed e-mail, a blank
   address line, an unknown address kind, a country that is not two letters (including a
   well-formed-looking `12`) and a currency this company has not registered are all refused
5. the credit limit keeps **three** states, not two: `None` (no limit agreed), `0`
   (no credit at all) and a real ceiling are stored and read back differently, a
   negative one, a float one and a **non-finite** one (`Infinity`, `NaN`) are refused, and
   the database refuses a negative one written by hand
6. addresses are **reusable across documents** — two documents point at the *same*
   address row — and at most one primary stands per kind, with a new primary
   demoting the previous one while the database's partial index refuses a
   hand-written second
7. a customer a **document** still names is refused retirement by name, a free one
   is retired by marking (the row stays and `DELETE` is refused), and a party
   without the role is not found as a customer

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Uuid, create_engine, insert, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Mapped, Session, mapped_column

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger.currency import UnknownCurrencyError, register_currency  # noqa: E402
from app.party import Party, PartyRole  # noqa: E402
from app.procurement.suppliers import create_supplier  # noqa: E402
from app.sales.customers import (  # noqa: E402
    Customer,
    CustomerAddress,
    DuplicateCustomerError,
    InvalidCustomerDataError,
    CustomerInUseError,
    add_address,
    add_contact,
    address_for,
    create_customer,
    credit_limit_of,
    customer_by_code,
    primary_contact,
    retire_customer,
    set_credit_limit,
)
from app.sales.customers import (  # noqa: E402
    NotACustomerError as CustomerNotACustomerError,
)

COMPANY = uuid.uuid4()
OTHER = uuid.uuid4()
TERMS = 30


class ProbeSalesDocument(Base):
    """A stand-in for the documents a later task adds (T-3.SALES.03's quotation, T-3.AR.01's invoice).

    It exists only in this check, and it is deliberately the shape those documents
    will have: a customer, and an address **by row id** rather than by re-typed
    text. Two of these pointing at one address row is what "reusable across
    documents" means, and it also proves the "a customer a document names cannot be
    retired" rule against a real foreign key without this task inventing a document
    it does not own. Because :func:`documents_naming` scans the schema, a real
    document table starts being honoured the day it appears.
    """

    __tablename__ = "probe_sales_document"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customer.id"), nullable=False, index=True
    )
    address_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customer_address.id"))


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
        for company_id, code in ((COMPANY, "CUST-CHECK"), (OTHER, "OTHER")):
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

        # 1 — a customer from a party that does not exist yet
        acme = create_customer(
            session,
            company_id=COMPANY,
            party_code="ACME",
            name="Acme Retail",
            payment_terms_days=TERMS,
        )
        session.commit()
        assert acme.party.code == "ACME", acme.party.code
        assert acme.party.has_role("customer"), "the new party does not hold the customer role"
        assert acme.payment_terms_days == TERMS
        assert acme.transaction_currency is None, "the base currency is stated as null, not copied"
        print(f"1. party {acme.party.code} created holding the customer role")

        # 2 — one party, both roles, one record: a supplier becomes a customer
        supplier_first = create_supplier(
            session,
            company_id=COMPANY,
            party_code="BOTH",
            name="Both Ways Trading",
            payment_terms_days=0,
            tax_id="123-456-789",
        )
        session.commit()
        both = create_customer(
            session,
            company_id=COMPANY,
            party_code="BOTH",
            payment_terms_days=15,
        )
        session.commit()
        parties = list(
            session.scalars(select(Party).where(Party.company_id == COMPANY, Party.code == "BOTH"))
        )
        assert len(parties) == 1, f"one party became {len(parties)} records"
        assert parties[0].has_role("customer") and parties[0].has_role("supplier")
        assert parties[0].tax_id == "123-456-789", "the shared identity lost its tax number"
        assert supplier_first.party_id == both.party_id
        print("2. one party holds both roles — one identity, one tax number, no duplicate")

        # 3 — one profile per party
        said = _refused(
            lambda: create_customer(
                session,
                company_id=COMPANY,
                party_code="ACME",
                name="Acme Retail",
                payment_terms_days=45,
            ),
            DuplicateCustomerError,
        )
        session.rollback()
        print(f"3. a second profile for the same party is refused: {said}")

        # 4 — validation at entry
        said = _refused(
            lambda: add_contact(session, acme, name="Ana", email="ana@acme"),
            InvalidCustomerDataError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: add_address(session, acme, kind="billing", line1="   "),
            InvalidCustomerDataError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: add_address(session, acme, kind="head_office", line1="12 Rizal St"),
            InvalidCustomerDataError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: add_address(session, acme, kind="billing", line1="12 Rizal St", country="PHL"),
            InvalidCustomerDataError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: add_address(session, acme, kind="billing", line1="12 Rizal St", country="12"),
            InvalidCustomerDataError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: create_customer(
                session,
                company_id=COMPANY,
                party_code="XYZ",
                name="Wrong Currency",
                transaction_currency="XYZ",
            ),
            UnknownCurrencyError,
        )
        session.rollback()
        print(f"4. every malformed detail is refused at entry: {said}")

        # 4b — and the well-formed ones land
        contact = add_contact(
            session, acme, name="Ana Reyes", position="Buyer", email="ANA@acme.example", is_primary=True
        )
        billing = add_address(
            session,
            acme,
            kind="billing",
            line1="12 Rizal St",
            city="Makati",
            region="NCR",
            postal_code="1226",
            country="ph",
            is_primary=True,
        )
        shipping = add_address(
            session,
            acme,
            kind="shipping",
            line1="Warehouse 4, Laguna",
            city="Calamba",
            is_primary=True,
        )
        session.commit()
        assert contact.email == "ana@acme.example", contact.email
        assert billing.country == "PH", billing.country
        assert primary_contact(acme).name == "Ana Reyes"
        assert address_for(acme, "billing").id == billing.id
        assert address_for(acme, "shipping").id == shipping.id
        print("4b. a well-formed contact and billing/shipping addresses are stored; lookups resolve")

        # 5 — the credit limit keeps three states
        assert credit_limit_of(acme) is None, "a new customer invented a limit"
        assert acme.credit_limit is None
        set_credit_limit(session, acme, limit="0")
        session.commit()
        session.refresh(acme)
        assert credit_limit_of(acme) == Decimal("0"), credit_limit_of(acme)
        assert acme.credit_limit is not None, "zero was stored as 'no limit agreed'"
        set_credit_limit(session, acme, limit="25000.50")
        session.commit()
        session.refresh(acme)
        assert credit_limit_of(acme) == Decimal("25000.50"), credit_limit_of(acme)
        said = _refused(
            lambda: set_credit_limit(session, acme, limit="-1"), InvalidCustomerDataError
        )
        session.rollback()
        said += " | " + _refused(
            lambda: set_credit_limit(session, acme, limit=25000.5), InvalidCustomerDataError
        )
        session.rollback()
        # non-finite decimals are not one of the three documented states: Infinity
        # would pass the sign test, and NaN would raise out of it
        said += " | " + _refused(
            lambda: set_credit_limit(session, acme, limit=Decimal("Infinity")),
            InvalidCustomerDataError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: set_credit_limit(session, acme, limit=Decimal("NaN")),
            InvalidCustomerDataError,
        )
        session.rollback()
        # the database refuses a negative ceiling written by hand, past the function.
        # A *fresh* party, so the one-profile-per-party rule cannot be what fires.
        probe_party = Party(company_id=COMPANY, code="PROBE-NEG", name="Negative Probe")
        probe_party.roles = [PartyRole(role="customer")]
        session.add(probe_party)
        session.commit()
        try:
            session.execute(
                insert(Customer.__table__).values(
                    id=uuid.uuid4(),
                    company_id=COMPANY,
                    party_id=probe_party.id,
                    payment_terms_days=0,
                    credit_limit=Decimal("-5"),
                )
            )
            session.commit()
        except DBAPIError as exc:
            assert "ck_customer_credit_limit" in str(exc), exc
            session.rollback()
        else:
            raise AssertionError("the database accepted a negative credit limit")
        # ... and withdrawing the limit restores "no limit agreed", not zero
        set_credit_limit(session, acme, limit=None)
        session.commit()
        session.refresh(acme)
        assert credit_limit_of(acme) is None and acme.credit_limit is None
        set_credit_limit(session, acme, limit="25000.50")
        session.commit()
        print(
            "5. the limit reads as None (unset) · 0 (no credit) · 25000.50 (a ceiling); a negative"
            f" and a float one are refused ({said}), and the database refuses a negative by hand"
        )

        # 6 — addresses are reusable, and one primary per kind
        first_doc = ProbeSalesDocument(company_id=COMPANY, customer_id=acme.id, address_id=billing.id)
        second_doc = ProbeSalesDocument(
            company_id=COMPANY, customer_id=acme.id, address_id=billing.id
        )
        session.add_all([first_doc, second_doc])
        session.commit()
        assert first_doc.address_id == second_doc.address_id == billing.id
        assert session.get(CustomerAddress, first_doc.address_id).line1 == "12 Rizal St"
        # a new primary billing address demotes the previous one ...
        newer = add_address(session, acme, kind="billing", line1="99 Ayala Ave", is_primary=True)
        session.commit()
        session.refresh(acme)
        assert address_for(acme, "billing").id == newer.id, "the new primary did not take over"
        assert sum(1 for row in acme.addresses if row.is_primary and row.kind == "billing") == 1
        assert address_for(acme, "shipping").id == shipping.id, "shipping was demoted with billing"
        # ... and the database refuses a hand-written second primary of the same kind
        try:
            session.execute(
                insert(CustomerAddress.__table__).values(
                    id=uuid.uuid4(),
                    company_id=COMPANY,
                    customer_id=acme.id,
                    kind="billing",
                    line1="A third one",
                    is_primary=True,
                )
            )
            session.commit()
        except DBAPIError as exc:
            assert "uq_customer_primary_address_per_kind" in str(exc), exc
            session.rollback()
        else:
            raise AssertionError("the database accepted two primary billing addresses")
        print(
            "6. two documents point at the same address row; a new primary demotes the previous"
            " one, shipping keeps its own, and the database refuses a second primary of a kind"
        )

        # 7 — a document blocks retirement; marking is how a free customer is retired
        said = _refused(lambda: retire_customer(session, acme), CustomerInUseError)
        session.rollback()
        assert acme.deleted_at is None, "the refused retirement marked the customer anyway"

        spare = create_customer(
            session, company_id=COMPANY, party_code="SPARE", name="Spare Retail", payment_terms_days=7
        )
        session.commit()
        retire_customer(session, spare)
        session.commit()
        assert spare.deleted_at is not None, "retirement did not mark the customer"
        assert session.get(Customer, spare.id) is not None, "the row was removed, not marked"
        try:
            session.execute(Customer.__table__.delete().where(Customer.id == spare.id))
            session.commit()
        except DBAPIError as exc:
            assert "is a master" in str(exc), exc
            session.rollback()
        else:
            raise AssertionError("DELETE was allowed on a master")
        other_party = create_supplier(
            session,
            company_id=COMPANY,
            party_code="ONLYSUP",
            name="Supplies Only",
            payment_terms_days=10,
        )
        session.commit()
        said2 = _refused(
            lambda: customer_by_code(session, company_id=COMPANY, code="ONLYSUP"),
            CustomerNotACustomerError,
        )
        session.rollback()
        assert other_party is not None
        print(
            f"7. retirement refused while a document names it ({said}); a free customer is marked,"
            f" its row stays and DELETE is refused; a non-customer is refused ({said2})"
        )

        # 8 — two companies may each have an ACME, and neither sees the other's
        create_customer(
            session, company_id=OTHER, party_code="ACME", name="Other Acme", payment_terms_days=14
        )
        session.commit()
        mine = customer_by_code(session, company_id=COMPANY, code="ACME")
        assert mine.company_id == COMPANY, "the lookup crossed companies"
        print("8. the same party code exists in two companies, each resolving inside its own")

    print("check_customer_master: all assertions green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
