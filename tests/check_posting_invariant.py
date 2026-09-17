"""T-0.CORE.01 check — the posting invariant, against a scratch database.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/postgres \
        python tests/check_posting_invariant.py

It fails (non-zero exit) if any of these stops holding:

1. a balanced set persists, as one unit
2. an unbalanced set is refused with a clear error and nothing is persisted
3. a single-line set is refused
4. raw SQL that bypasses ``post_journal_entry`` is refused by the database
   itself — the invariant lives at the storage boundary, not only in the caller

**Scratch database only**: it drops and recreates the two ledger tables.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger.posting import (  # noqa: E402
    JournalEntry,
    JournalLine,
    UnbalancedEntryError,
    post_journal_entry,
)

COMPANY = uuid.uuid4()
DAY = date(2026, 9, 17)


def _lines(*spec: tuple[str, str, str]) -> list[dict[str, str]]:
    return [
        {"account": account, "debit": debit, "credit": credit}
        for account, debit, credit in spec
    ]


def _entry_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(JournalEntry)) or 0


def _refused(call, expected: str) -> str:
    """Return the error message if `call` raises it; fail the check otherwise."""
    try:
        call()
    except UnbalancedEntryError as exc:
        assert expected in str(exc), f"unclear error: {exc}"
        return str(exc)
    raise AssertionError(f"accepted what it must refuse (expected {expected!r})")


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
    # The ledger references the company master now (T-0.CORE.03), so this check
    # posts for a real company.
    with Session(engine) as session:
        session.add(
            Company(
                id=COMPANY,
                code="POSTING-CHECK",
                name="Posting invariant check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()

    with Session(engine) as session:
        # 1 — a balanced set persists
        entry = post_journal_entry(
            session,
            company_id=COMPANY,
            posting_date=DAY,
            currency="PHP",
            lines=_lines(("1000", "100.00", "0"), ("4000", "0", "100.00")),
        )
        session.commit()
        assert entry.id is not None and len(entry.lines) == 2, "balanced entry did not persist"
        assert _entry_count(session) == 1

        # 2 — an unbalanced set is refused and leaves nothing behind
        refusal = _refused(
            lambda: post_journal_entry(
                session,
                company_id=COMPANY,
                posting_date=DAY,
                currency="PHP",
                lines=_lines(("1000", "100.00", "0"), ("4000", "0", "90.00")),
            ),
            "does not balance",
        )
        session.rollback()
        assert _entry_count(session) == 1, "unbalanced entry left rows behind"

        # 3 — one line is not a double entry
        _refused(
            lambda: post_journal_entry(
                session,
                company_id=COMPANY,
                posting_date=DAY,
                currency="PHP",
                lines=_lines(("1000", "100.00", "0")),
            ),
            "at least 2 lines",
        )
        session.rollback()
        assert _entry_count(session) == 1

    # 4 — the storage boundary refuses a writer that bypasses the primitive
    for expected, rows in (
        ("does not balance", ((10, 0), (0, 9))),
        ("at least 2", ()),
    ):
        with engine.connect() as conn:
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
            try:
                # the triggers are deferred: the refusal arrives here, at COMMIT
                conn.commit()
            except DBAPIError as exc:
                db_message = str(exc.orig).strip()
            else:
                raise AssertionError(f"the database accepted {rows!r} written by raw SQL")
            conn.rollback()
            assert expected in db_message, f"unclear database error: {db_message}"
            left = conn.exec_driver_sql(
                "SELECT count(*) FROM journal_entry WHERE id = %s", (entry_id,)
            ).scalar()
            assert left == 0, "the refused direct write is still in the ledger"
            print(f"database refused a direct write: {db_message}")

    print(f"ok — the primitive refused the unbalanced set: {refusal}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
