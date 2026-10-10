"""Seed the stack's database from inside it, and print the company id.

Run as a one-off container on the stack's data network:

    docker run --rm --network erpv1_data -e DATABASE_URL=... -v $PWD/tests/compose_seed.py:/seed.py:ro \\
        erpv1-backend:local python /seed.py

Used by T-0.DEPLOY.03's check, and handy by hand when the stack is up.

It installs through :mod:`app.bootstrap` — the same `python -m app.bootstrap` an operator runs —
and then posts one entry, so the frontend has something to render through both hops.

That call is the point of this rewrite. This file used to create the schema, the company and a
role by hand, and it is not maintained as an entry point: when T-1.ACCT.01 made a posting line's
account resolve to a real account, its posting was refused with `no account '1000' in this
company`, and the stack check stayed red unnoticed because it needs Docker and the pipeline
excludes it. One install path, in `app/`, is the fix.
"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.audit import set_actor
from app.bootstrap import bootstrap
from app.db import scope_to_company
from app.ledger.posting import post_journal_entry

# The subject the stack's frontend states (compose's own `ACTOR` default), and the actor the
# install and the posting are recorded under.
ACTOR = "shell"

engine = create_engine(os.environ["DATABASE_URL"])

# A scratch database with nothing in it: this is an install, and an install refuses a company
# code that is already taken, so a schema left over from an earlier run would be refused rather
# than reused.
with engine.begin() as connection:
    connection.exec_driver_sql("DROP SCHEMA public CASCADE")
    connection.exec_driver_sql("CREATE SCHEMA public")

company_id = bootstrap(
    engine,
    company_code="STACK-CHECK",
    company_name="Stack Check Trading",
    market="philippines",
    # Plan §8 leaves the Philippines' fiscal year start open and the pack carries null for it,
    # so the install states the month rather than letting one be invented (T-0.LOC.01).
    fiscal_year_start_month=1,
    admin_subject=ACTOR,
    actor=ACTOR,
)

with Session(engine) as session:
    set_actor(session, ACTOR)
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

# The company id, and nothing else: the check passes it to the frontend as `COMPANY_ID`.
print(company_id)
