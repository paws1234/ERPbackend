"""T-1.ACCT.01 check — the chart of accounts tree and its rules.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/erpv1 \
        python tests/check_chart_of_accounts.py

Green on all seven:

1. the Philippines pack's 58-account template imports in one call, parents before
   children, with the pack's classes — no manual fix-up
2. the same tree comes back as a nested tree, each node under its parent
3. a class-mixing child is refused by the helper, and by the database at COMMIT
   when written straight into the table (the storage boundary, not only the caller)
4. a cycle is refused — an account cannot be moved under its own descendant
5. an account a posting references cannot be moved into another class
6. a parent with live children cannot be retired, by the helper or by raw SQL
7. a leaf retires by marking: the row stays, reads stop returning it, and DELETE
   is refused (the T-0.AUDIT.01 master convention)

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger.accounts import (  # noqa: E402
    ACCOUNT_CLASSES,
    Account,
    AccountHasChildrenError,
    ClassMismatchError,
    DuplicateAccountCode,
    UnknownAccountError,
    account_by_code,
    create_account,
    import_coa_template,
    reparent,
    retire_account,
    tree,
)
from app.ledger.posting import post_journal_entry  # noqa: E402

COMPANY = uuid.uuid4()
DAY = date(2026, 9, 17)


def _refused(call, expected: type[Exception] | str) -> str:
    """Run `call`, require it to refuse, and return what it said."""
    try:
        call()
    except Exception as exc:  # noqa: BLE001 — the message is what is being checked
        if isinstance(expected, str):
            assert expected in str(exc), f"unclear refusal: {exc}"
        else:
            assert isinstance(exc, expected), f"refused with {type(exc).__name__}: {exc}"
        return str(exc)
    raise AssertionError("accepted what it must refuse")


def _schema_fixture(engine) -> None:
    """A company to hang the chart of accounts on."""
    with Session(engine) as session:
        session.add(
            Company(
                id=COMPANY,
                code="COA-CHECK",
                name="Chart of accounts check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()


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
    _schema_fixture(engine)

    with Session(engine) as session:
        # 1 — the pack's template imports as it stands
        created = import_coa_template(session, company_id=COMPANY, market="philippines")
        session.commit()
        assert len(created) == 58, f"the template imported {len(created)} accounts"
        classes = {account.account_class for account in created}
        assert classes <= set(ACCOUNT_CLASSES), f"unknown class imported: {classes}"
        assert classes == set(ACCOUNT_CLASSES), f"a class is missing from the tree: {classes}"
        import json

        from app.localization import coa_template

        template = {row["code"]: row for row in coa_template("philippines")}
        by_code = {
            account.code: account
            for account in session.scalars(select(Account).where(Account.company_id == COMPANY))
        }
        for code, row in template.items():
            assert code in by_code, f"{code} did not import"
            assert by_code[code].account_class == row["class"], f"{code} changed class"
            parent = row.get("parent")
            if parent:
                assert by_code[code].parent_id == by_code[parent].id, (
                    f"{code} is not under {parent}"
                )
        print(f"the pack template imported: {len(created)} accounts, parents linked, no fix-up")

        # 2 — the tree reads back nested
        nested = tree(session, company_id=COMPANY)
        assert len(nested) >= 5, f"the tree has only {len(nested)} roots"

        def width(nodes):
            return sum(1 + width(node["children"]) for node in nodes)

        def depth(nodes):
            if not nodes:
                return 0
            return 1 + max(depth(node["children"]) for node in nodes)

        assert width(nested) == 58, f"the tree lost accounts: {width(nested)}"
        assert depth(nested) >= 2, "the template imported flat, with no children at all"
        children = {node["code"] for root in nested for node in root["children"]}
        assert {"1020", "1210", "2010"} <= children, "the pack's children are not nested"
        print(f"the chart reads back as a nested tree of {width(nested)} nodes, {depth(nested)} levels")

        # 3 — a class-mixing child is refused, by the helper and by the database
        refusal = _refused(
            lambda: create_account(
                session,
                company_id=COMPANY,
                code="9999",
                name="An asset under income",
                account_class="asset",
                parent_id=by_code["4000"].id,
            ),
            ClassMismatchError,
        )
        session.rollback()
        with engine.connect() as connection:
            connection.exec_driver_sql(
                "INSERT INTO account (id, company_id, code, name, class, parent_id)"
                " VALUES (%s, %s, '9998', 'Raw insert under income', 'asset', %s)",
                (uuid.uuid4(), COMPANY, by_code["4000"].id),
            )
            try:
                connection.commit()
            except DBAPIError as exc:
                database_message = str(exc.orig).strip()
            else:
                raise AssertionError("the database accepted a class-mixing child")
            connection.rollback()
        assert "cannot mix classes" in database_message, database_message
        print(f"class mixing refused by the helper ({refusal[:48]}…) and at the boundary")

        # 4 — a cycle is refused: the tree rule is a deferred constraint trigger,
        # so the refusal for a raw move arrives at COMMIT
        reparent(session, by_code["1010"], parent_id=by_code["1020"].id)
        try:
            session.commit()
        except DBAPIError as exc:
            cycle_message = str(exc.orig).strip()
        else:
            raise AssertionError("the database accepted a cycle in the chart of accounts")
        session.rollback()
        assert "inside its own subtree" in cycle_message, cycle_message
        print(f"moving an account under its own descendant was refused ({cycle_message[:40]}…)")

        # 5 — an account a posting references cannot change class
        post_journal_entry(
            session,
            company_id=COMPANY,
            posting_date=DAY,
            currency="PHP",
            lines=[
                {"account": "1000", "debit": Decimal("250.00")},
                {"account": "4000", "credit": Decimal("250.00")},
            ],
        )
        session.commit()
        _refused(
            lambda: reparent(session, by_code["1000"], parent_id=by_code["4100"].id),
            ClassMismatchError,
        )
        session.rollback()
        posted = account_by_code(session, company_id=COMPANY, code="1000")
        assert posted.parent_id is None, "the refusal still moved the account"
        print("a posted account was refused a move into another class")

        # 6 — a parent with live children cannot be retired, helper or raw SQL
        _refused(
            lambda: retire_account(session, by_code["1010"]),
            AccountHasChildrenError,
        )
        session.rollback()
        with engine.connect() as connection:
            connection.exec_driver_sql(
                "UPDATE account SET deleted_at = now() WHERE id = %s", (by_code["1010"].id,)
            )
            try:
                connection.commit()
            except DBAPIError as exc:
                retired_message = str(exc.orig).strip()
            else:
                raise AssertionError("the database retired a parent with live children")
            connection.rollback()
        assert "still has live children" in retired_message, retired_message
        print(f"a parent with live children was refused retirement ({retired_message[:44]}…)")

        # 7 — a leaf retires by marking, and hard delete is refused
        leaf = by_code["1020"]
        leaf_id = leaf.id
        retire_account(session, leaf)
        session.commit()
        # A fresh session for the reads: the retired row is still in this one's
        # identity map, and `Session.get` would answer from there.
        with Session(engine) as fresh:
            assert fresh.get(Account, leaf_id) is None, "a retired account is still read"
            still_there = fresh.scalar(
                select(Account.code)
                .where(Account.id == leaf_id)
                .execution_options(include_soft_deleted=True)
            )
            assert still_there == "1020", "the retired row is gone from the table"
        with engine.connect() as connection:
            try:
                connection.exec_driver_sql("DELETE FROM account WHERE id = %s", (leaf.id,))
                connection.commit()
            except DBAPIError as exc:
                delete_message = str(exc.orig).strip()
            else:
                raise AssertionError("the database deleted a master")
            connection.rollback()
        assert "is a master" in delete_message, delete_message
        print(f"a leaf retired by marking; DELETE refused ({delete_message[:44]}…)")

        # the code is the link: a code nobody created is refused
        _refused(
            lambda: account_by_code(session, company_id=COMPANY, code="0000"), UnknownAccountError
        )
        _refused(
            lambda: create_account(
                session,
                company_id=COMPANY,
                code="1000",
                name="Duplicate",
                account_class="asset",
            ),
            DuplicateAccountCode,
        )
        session.rollback()

    engine.dispose()
    print("ok — the chart of accounts is a five-class tree with its rules enforced")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
