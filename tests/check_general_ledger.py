"""T-1.ACCT.02 check — the general ledger: source links, append-only, derivable balances.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/erpv1 \
        python tests/check_general_ledger.py

Green on all six:

1. an entry records the document that produced it, and the drill-down finds that
   document's postings — both of them, when a document posted twice
2. a manual entry stores no source, and half a pair is refused by the primitive
   and by the table's own constraint at COMMIT
3. a posted entry refuses UPDATE and DELETE at the storage boundary, with the
   ledger unchanged afterwards (T-0.AUDIT.01 on this tree)
4. an account's balance is the sum of its lines, read from the entries alone —
   checked against an independently computed sum, not against itself
5. a period filter narrows the balance to the entries inside it
6. `ledger_rows` carries posting date, account, debit, credit, party and the
   source document for each line

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger.gl import account_balance, entries_for_source, ledger_rows  # noqa: E402
from app.ledger.posting import (  # noqa: E402
    IncompleteSourceError,
    post_journal_entry,
)
from app.party import create_party  # noqa: E402
from tests.seed import seed_accounts  # noqa: E402

COMPANY = uuid.uuid4()
INVOICE = uuid.uuid4()
DAY = date(2026, 9, 17)
LATER = date(2026, 10, 2)


def _post(session, *, day, source=None, party=None, debit="100.00", credit="100.00"):
    lines = [
        {"account": "1000", "debit": Decimal(debit)},
        {"account": "4000", "credit": Decimal(credit)},
    ]
    if party is not None:
        lines[1]["party"] = party
    return post_journal_entry(
        session,
        company_id=COMPANY,
        posting_date=day,
        currency="PHP",
        lines=lines,
        source_type=source[0] if source else None,
        source_id=source[1] if source else None,
    )


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
        session.add(
            Company(
                id=COMPANY,
                code="GL-CHECK",
                name="General ledger check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()
        seed_accounts(session, company_id=COMPANY)
        create_party(
            session, company_id=COMPANY, code="ACME", name="Acme Trading", roles=["customer"]
        )
        session.commit()

        # 1 — a document's postings are found from the document
        _post(session, day=DAY, source=("sales_invoice", INVOICE), party="ACME")
        _post(session, day=LATER, source=("sales_invoice", INVOICE))
        manual = _post(session, day=LATER)
        session.commit()
        assert manual.source_type is None and manual.source_id is None, (
            "a manual entry invented a source document"
        )
        found = entries_for_source(
            session, company_id=COMPANY, source_type="sales_invoice", source_id=INVOICE
        )
        assert [entry.posting_date for entry in found] == [DAY, LATER], (
            f"the drill-down found {[entry.posting_date for entry in found]}"
        )
        other = entries_for_source(
            session, company_id=COMPANY, source_type="sales_invoice", source_id=uuid.uuid4()
        )
        assert other == [], "the drill-down found another document's postings"
        print(f"one document's two postings are found by its id ({len(found)}), a manual entry none")

        # 2 — half a source pair is refused, by the primitive and by the table
        try:
            _post(session, day=DAY, source=("sales_invoice", None))
        except IncompleteSourceError as exc:
            refusal = str(exc)
        else:
            raise AssertionError("the primitive accepted half a source pair")
        session.rollback()
        with engine.connect() as connection:
            try:
                connection.exec_driver_sql(
                    "INSERT INTO journal_entry (id, company_id, posting_date, currency,"
                    " exchange_rate, source_type)"
                    " VALUES (%s, %s, %s, 'PHP', 1, 'sales_invoice')",
                    (uuid.uuid4(), COMPANY, DAY),
                )
                connection.commit()
            except DBAPIError as exc:
                database_message = str(exc.orig).strip()
            else:
                raise AssertionError("the table accepted half a source pair")
            connection.rollback()
        assert "ck_journal_entry_source_pair" in database_message, database_message
        print(f"half a source pair refused: {refusal[:46]}… and by the table's own constraint")

        # 3 — a posted entry is append-only
        entry_id = found[0].id
        for statement in (
            "UPDATE journal_line SET debit = 1 WHERE entry_id = %s",
            "DELETE FROM journal_entry WHERE id = %s",
        ):
            with engine.connect() as connection:
                try:
                    connection.exec_driver_sql(statement, (entry_id,))
                    connection.commit()
                except DBAPIError as exc:
                    append_message = str(exc.orig).strip()
                else:
                    raise AssertionError(f"the ledger accepted: {statement}")
                connection.rollback()
            assert "append-only" in append_message, append_message
        print(f"a posted entry refused a rewrite ({append_message[:44]}…)")

        # 4 — the balance is derived from the entries, not stored
        balance = account_balance(session, company_id=COMPANY, account_code="1000")
        independent = Decimal(
            session.scalar(
                text(
                    "SELECT coalesce(sum(l.debit - l.credit), 0) FROM journal_line l"
                    " JOIN journal_entry e ON e.id = l.entry_id"
                    " WHERE l.account = '1000' AND e.company_id = :company"
                ).bindparams(company=COMPANY)
            )
        )
        assert balance == independent == Decimal(300), f"balance {balance} != {independent}"
        assert account_balance(session, company_id=COMPANY, account_code="4100") == Decimal(0), (
            "an untouched account did not answer zero"
        )

        # 5 — the period narrows it
        first_day = account_balance(
            session, company_id=COMPANY, account_code="1000", start=DAY, end=DAY
        )
        assert first_day == Decimal(100), f"the dated balance is {first_day}"
        print(f"balances are read from the entries: all {balance}, {DAY} alone {first_day}")

        # 6 — the rows carry what §2.1 names, plus the source
        rows = ledger_rows(session, company_id=COMPANY, account_code="4000")
        assert [row["credit"] for row in rows] == ["100.000000"] * 3, rows
        assert rows[0]["party"] == "ACME" and rows[0]["source_id"] == str(INVOICE), rows[0]
        assert rows[0]["posting_date"] == DAY.isoformat() and rows[0]["debit"] == "0.000000"
        print(f"ledger rows carry account, debit/credit, party and source ({len(rows)} rows)")

    engine.dispose()
    print("ok — the ledger names its documents, refuses rewrites and derives its balances")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
