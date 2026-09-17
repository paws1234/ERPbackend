"""Seed the stack's database from inside it, and print the company id.

Run as a one-off container on the stack's data network:

    docker run --rm --network erpv1_data -e DATABASE_URL=... -v $PWD/tests/compose_seed.py:/seed.py:ro \\
        erpv1-backend:local python /seed.py

Used by T-0.DEPLOY.03's check, and handy by hand when the stack is up.
"""

from __future__ import annotations

import os
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.company import Company
from app.db import Base, scope_to_company
from app.ledger.posting import post_journal_entry
from app.security import assign, define_role, grant

engine = create_engine(os.environ["DATABASE_URL"])
with engine.begin() as connection:
    connection.exec_driver_sql("DROP SCHEMA public CASCADE")
    connection.exec_driver_sql("CREATE SCHEMA public")
Base.metadata.create_all(engine)

company_id = uuid.uuid4()
with Session(engine) as session:
    session.add(
        Company(
            id=company_id,
            code="STACK-CHECK",
            name="Stack Check Trading",
            base_currency="PHP",
            fiscal_year_start_month=1,
        )
    )
    session.commit()
    role = define_role(session, company_id=company_id, code="checker", name="Checker")
    grant(session, role, "company.read", "journal.read")
    assign(session, company_id=company_id, subject="shell", role=role)
    session.commit()
    scope_to_company(session, company_id)
    post_journal_entry(
        session,
        company_id=company_id,
        posting_date=date(2026, 9, 17),
        currency="PHP",
        memo="stack check",
        lines=[
            {"account": "1000", "debit": Decimal("1234.00")},
            {"account": "4000", "credit": Decimal("1234.00")},
        ],
    )
    session.commit()

print(company_id)
