"""T-1.ACCT.03 check — one posting interface, its account mapping, its atomicity.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/erpv1 \
        python tests/check_posting_interface.py

Green on all seven:

1. **no other writer**: the tree is scanned for a statement that writes
   `journal_entry` / `journal_line` and the only file found is the ledger's own
   primitive — and the scanner is proved able to fail by scanning an injected
   writer
2. an unbalanced call and a single-line call are refused through the interface
3. a document that fails **after** posting leaves no journal entry (the posting
   is atomic with the caller's transaction)
4. an account mapping resolves a key to the company's account, and re-pointing a
   key does not create a second row
5. an unmapped key is refused by name, never defaulted to an account
6. a key mapped to an account the company does not have is refused at the mapping,
   not at the first posting
7. the T-0.CORE.02 ledger-integrity gate is green over what the interface wrote

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import os
import pathlib
import re
import sys
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger.accounts import UnknownAccountError  # noqa: E402
from app.ledger.mapping import (  # noqa: E402
    AccountMapping,
    MissingMappingError,
    mapped_account,
    mappings,
    set_mapping,
)
from app.ledger.posting import (  # noqa: E402
    JournalEntry,
    UnbalancedEntryError,
    post_journal_entry,
)
from tests.check_ledger_integrity import ledger_gate  # noqa: E402
from tests.seed import seed_accounts  # noqa: E402

APP = pathlib.Path(__file__).resolve().parent.parent / "app"
COMPANY = uuid.uuid4()
DAY = date(2026, 9, 17)

# What a writer of the two ledger tables looks like: constructing either row, or
# writing one with raw SQL. `select(JournalEntry)` and `JournalLineOut(...)` are
# reads and a response model, and match neither.
WRITER = re.compile(r"\b(JournalEntry|JournalLine)\(|INSERT\s+INTO\s+journal_(entry|line)")


def writers_in(source: str, name: str) -> list[str]:
    """The files in `{name: source}` that write the ledger tables."""
    return [path for path, text in source.items() if WRITER.search(text)]


def _refused(call, expected: type[Exception] | str) -> str:
    try:
        call()
    except Exception as exc:  # noqa: BLE001 — the message is what is being checked
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

    # 1 — the only writer of the two tables is the ledger's primitive
    real = {
        str(path.relative_to(APP.parent)): path.read_text()
        for path in sorted(APP.rglob("*.py"))
    }
    found = writers_in(real, "app")
    assert found == ["app/ledger/posting.py"], f"the ledger has another writer: {found}"
    injected = writers_in(
        {"app/inventory.py": "session.add(JournalLine(line_no=1))"}, "injected"
    )
    assert injected == ["app/inventory.py"], "the scanner cannot see a writer it must flag"
    print(f"only {found[0]} writes journal_entry / journal_line; the scanner sees an injected one")

    engine = create_engine(url)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(
            Company(
                id=COMPANY,
                code="INTERFACE-CHECK",
                name="Posting interface check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()
        seed_accounts(session, company_id=COMPANY)
        session.commit()

        # 2 — the interface refuses what is not a double entry
        assert "at least 2 lines" in _refused(
            lambda: post_journal_entry(
                session,
                company_id=COMPANY,
                posting_date=DAY,
                currency="PHP",
                lines=[{"account": "1000", "debit": Decimal("10")}],
            ),
            UnbalancedEntryError,
        )
        session.rollback()
        assert "does not balance" in _refused(
            lambda: post_journal_entry(
                session,
                company_id=COMPANY,
                posting_date=DAY,
                currency="PHP",
                lines=[
                    {"account": "1000", "debit": Decimal("10")},
                    {"account": "4000", "credit": Decimal("9")},
                ],
            ),
            UnbalancedEntryError,
        )
        session.rollback()
        print("the interface refused a single-line and an unbalanced call")

        # 3 — a document that fails after posting leaves nothing behind
        def failing_document() -> None:
            post_journal_entry(
                session,
                company_id=COMPANY,
                posting_date=DAY,
                currency="PHP",
                source_type="stock_movement",
                source_id=uuid.uuid4(),
                lines=[
                    {"account": "1200", "debit": Decimal("500")},
                    {"account": "2000", "credit": Decimal("500")},
                ],
            )
            raise RuntimeError("the document failed after its posting")

        try:
            failing_document()
        except RuntimeError:
            pass
        session.rollback()
        assert session.scalar(select(func.count()).select_from(JournalEntry)) == 0, (
            "a failed document left its posting in the ledger"
        )
        print("a document that failed after posting left no journal entry")

        # 4 — a key resolves to the company's account, and re-pointing is one row
        mapping = set_mapping(
            session, company_id=COMPANY, key="inventory", account_code="1200"
        )
        session.commit()
        assert mapped_account(session, company_id=COMPANY, key="inventory").code == "1200"
        set_mapping(session, company_id=COMPANY, key="inventory", account_code="1210")
        session.commit()
        assert mapped_account(session, company_id=COMPANY, key="inventory").code == "1210"
        assert (
            session.scalar(
                select(func.count())
                .select_from(AccountMapping)
                .where(AccountMapping.company_id == COMPANY, AccountMapping.key == "inventory")
            )
            == 1
        ), "re-pointing a key created a second mapping"
        assert [row.key for row in mappings(session, company_id=COMPANY)] == ["inventory"]
        print("a key maps to an account and re-pointing keeps one row")

        # 5 — an unmapped key is refused by name, never defaulted
        refusal = _refused(
            lambda: mapped_account(session, company_id=COMPANY, key="cogs"),
            MissingMappingError,
        )
        session.rollback()
        print(f"an unmapped key is refused: {refusal[:52]}…")

        # 6 — a mapping cannot point at an account that does not exist
        _refused(
            lambda: set_mapping(
                session, company_id=COMPANY, key="cogs", account_code="8888"
            ),
            UnknownAccountError,
        )
        session.rollback()

        # 7 — the gate is green over what the interface wrote, through a mapped key
        post_journal_entry(
            session,
            company_id=COMPANY,
            posting_date=DAY,
            currency="PHP",
            source_type="stock_receipt",
            source_id=uuid.uuid4(),
            lines=[
                {"account": mapped_account(session, company_id=COMPANY, key="inventory").code,
                 "debit": Decimal("500")},
                {"account": mapped_account(session, company_id=COMPANY, key="inventory").code,
                 "credit": Decimal("500")},
            ],
        )
        session.commit()
        with engine.connect() as connection:
            gate = ledger_gate(connection)
        assert gate == 0, "the ledger-integrity gate is not green over the interface's writes"
        print("the ledger-integrity gate is green over the interface's writes")

    engine.dispose()
    print("ok — one posting interface, one mapping, refusals at the interface")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
