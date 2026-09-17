"""T-0.CORE.02 check — the ledger-integrity harness (metric 1 of §6).

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/erpv1 \
        python tests/check_ledger_integrity.py

:func:`ledger_gate` is the harness: it scans the **stored** ledger — not API
responses — and reports every entry whose debits do not equal its credits, or
that has fewer than two lines. This script proves both directions:

1. **green on a clean ledger** — a real posting through ``post_journal_entry``
   produces no findings. A finding at this point fails the run, which is what
   makes this usable as a gate against a real database.
2. **red on a deliberately unbalanced entry** — one is injected with the
   tables' triggers off, the way a restored dump or a hand-edit arrives, and the
   check fails unless the gate reports it. The injection is rolled back, so the
   disabled triggers and the bad rows both disappear.

**Scratch database only**: it drops and recreates the two ledger tables.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date
from decimal import Decimal
from typing import Iterable

from sqlalchemy import create_engine
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger.posting import (  # noqa: E402
    JournalEntry,
    JournalLine,
    post_journal_entry,
)

COMPANY = uuid.uuid4()
DAY = date(2026, 9, 17)

# Metric 1 of §6, read from the tables rather than from API responses.
#
# ponytail: the gate aggregates every entry and every line in the ledger, so a
# run costs a full scan. Ceiling: fine at foundation volumes. Upgrade path when
# the ledger is large — scope the scan by company and posting period (the shape
# T-1.INV.07's reconciliation already needs) and run it as a scheduled job
# rather than on every CI run.
IMBALANCES = """
SELECT e.id, count(l.id) AS lines,
       coalesce(sum(l.debit), 0) AS debits,
       coalesce(sum(l.credit), 0) AS credits
  FROM journal_entry e
  LEFT JOIN journal_line l ON l.entry_id = e.id
 GROUP BY e.id
HAVING count(l.id) < 2
    OR coalesce(sum(l.debit), 0) <> coalesce(sum(l.credit), 0)
 ORDER BY e.id
"""


def ledger_gate(conn: Connection) -> int:
    """0 when every stored entry balances, else 1 after reporting each offender."""
    imbalances = conn.exec_driver_sql(IMBALANCES).all()
    for row in imbalances:
        print(
            f"  entry {row.id}: {row.lines} line(s), debit {row.debits}"
            f" <> credit {row.credits}",
            file=sys.stderr,
        )
    return 1 if imbalances else 0


def _inject(conn: Connection, rows: Iterable[tuple[int, int]]) -> uuid.UUID:
    """Write one entry straight into the tables, bypassing the primitive."""
    entry_id = uuid.uuid4()
    conn.exec_driver_sql(
        "INSERT INTO journal_entry (id, company_id, posting_date, currency)"
        " VALUES (%s, %s, %s, 'PHP')",
        (entry_id, COMPANY, DAY),
    )
    for line_no, (debit, credit) in enumerate(rows, start=1):
        conn.exec_driver_sql(
            "INSERT INTO journal_line (id, entry_id, line_no, account, debit, credit)"
            " VALUES (%s, %s, %s, '1000', %s, %s)",
            (uuid.uuid4(), entry_id, line_no, debit, credit),
        )
    return entry_id


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is required (a scratch Postgres)", file=sys.stderr)
        return 2

    engine = create_engine(url)
    # The checks share one scratch database, so reset the schema rather than only
    # the tables this file imports: a table another module added keeps a foreign
    # key on `company` and would block the rebuild.
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    Base.metadata.create_all(engine)
    # The ledger references the company master now (T-0.CORE.03), so the ledger
    # this gate scans belongs to a real company.
    with Session(engine) as session:
        session.add(
            Company(
                id=COMPANY,
                code="INTEGRITY-CHECK",
                name="Ledger integrity check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()

    # 1 — a clean ledger: a real posting goes in, the gate stays quiet. A finding
    # here means a real database is broken, and the run must fail.
    with Session(engine) as session:
        post_journal_entry(
            session,
            company_id=COMPANY,
            posting_date=DAY,
            currency="PHP",
            lines=[
                {"account": "1000", "debit": Decimal("100.00")},
                {"account": "4000", "credit": Decimal("100.00")},
            ],
        )
        session.commit()

    with engine.connect() as conn:
        assert ledger_gate(conn) == 0, "the gate reported a clean ledger as broken"
        print("clean ledger: 1 posting, gate green")

    # 2 — the gate is red on entries the guards never saw. Both injections are
    # rolled back, so nothing is left behind.
    for label, rows in (("does not balance", ((10, 0), (0, 9))), ("has no line", ())):
        with engine.connect() as conn:
            trans = conn.begin()
            conn.exec_driver_sql("ALTER TABLE journal_entry DISABLE TRIGGER USER")
            conn.exec_driver_sql("ALTER TABLE journal_line DISABLE TRIGGER USER")
            entry_id = _inject(conn, rows)
            assert [row.id for row in conn.exec_driver_sql(IMBALANCES).all()] == [entry_id], (
                f"the gate did not report the entry that {label}"
            )
            assert ledger_gate(conn) == 1, f"the gate stayed green on an entry that {label}"
            print(f"injected an entry that {label}: gate red")
            trans.rollback()
            assert ledger_gate(conn) == 0, "the injection outlived its transaction"

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
