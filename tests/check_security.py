"""T-0.SEC.01 check — roles, field restrictions, and refusals on the record.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/postgres \
        uv run --with fastapi --with httpx --with 'sqlalchemy>=2.0' \
        --with 'psycopg[binary]' python tests/check_security.py

It fails (non-zero exit) if any of these stops holding:

1. a caller whose roles do not hold the capability is refused **at the API
   boundary** — the request never reaches the data, whatever the UI shows
2. a field marked unreadable is absent from the read payload (not null, absent),
   for that caller only
3. a field marked unwritable is refused on the write path, so it is not accepted
   and not stored
4. a permission change takes effect on the next request, with nothing restarted
   and nothing deployed
5. every refusal is attributable: the trail names the actor, what was attempted
   and the roles the actor held

**Scratch database only**: it drops and recreates the schema.
"""

from __future__ import annotations

import os
import sys
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api import BASE, app  # noqa: E402
from app.audit import AuditLog, read_trail  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base, scope_to_company  # noqa: E402
from app.ledger import posting  # noqa: E402,F401 — every check builds the one schema
from app.ledger.posting import JournalEntry  # noqa: E402
from app.party import create_party  # noqa: E402
from app.security import REFUSED, Role, assign, define_role, grant, restrict  # noqa: E402
from tests.seed import seed_accounts  # noqa: E402

DAY = "2026-09-17"
ALICE, BOB, CAROL = "alice", "bob", "carol"


def _body(memo: str, party: str | None = None) -> dict:
    line = {"account": "1000", "debit": "100.00"}
    other = {"account": "4000", "credit": "100.00"}
    if party is not None:
        other["party"] = party
    return {"posting_date": DAY, "currency": "PHP", "memo": memo, "lines": [line, other]}


def _post(client: TestClient, company_id: uuid.UUID, actor: str, key: str, body: dict):
    return client.post(
        f"{BASE}/journal-entries",
        headers={"X-Company-Id": str(company_id), "X-Actor": actor, "Idempotency-Key": key},
        json=body,
    )


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

    company_id = uuid.uuid4()
    with Session(engine) as session:
        session.add(
            Company(
                id=company_id,
                code="SEC-CHECK",
                name="Security check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()
        # The posts below state account codes (T-1.ACCT.01) and link a party by
        # its code (T-0.PARTY.01), so both masters exist before the first request.
        seed_accounts(session, company_id=company_id)
        create_party(
            session, company_id=company_id, code="ACME", name="Acme Trading", roles=["customer"]
        )
        session.commit()

        accountant = define_role(
            session, company_id=company_id, code="accountant", name="Accountant"
        )
        grant(session, accountant, "journal.post", "journal.read", "company.read")
        auditor = define_role(session, company_id=company_id, code="auditor", name="Auditor")
        grant(session, auditor, "journal.read", "company.read")
        # The auditor may read the ledger but not who it was for.
        restrict(session, auditor, entity="journal_line", field="party", can_read=False)
        assign(session, company_id=company_id, subject=ALICE, role=accountant)
        assign(session, company_id=company_id, subject=BOB, role=auditor)
        session.commit()

    client = TestClient(app, raise_server_exceptions=False)
    headers = lambda actor: {"X-Company-Id": str(company_id), "X-Actor": actor}  # noqa: E731

    # 1 — no role, no posting: refused at the boundary
    carol = _post(client, company_id, CAROL, "carol-1", _body("carol"))
    assert carol.status_code == 403, carol.text
    assert carol.json()["error"]["code"] == "forbidden", carol.text
    assert "no roles" in carol.json()["error"]["message"]
    print(f"carol, holding no role, is refused: {carol.json()['error']['message']}")

    # ... including a caller whose role exists but does not carry the capability
    bob_posting = _post(client, company_id, BOB, "bob-1", _body("bob"))
    assert bob_posting.status_code == 403 and "auditor" in bob_posting.json()["error"]["message"]
    print(f"bob, an auditor, cannot post: {bob_posting.json()['error']['message']}")
    carol_read = client.get(f"{BASE}/journal-entries", headers=headers(CAROL))
    assert carol_read.status_code == 403, carol_read.text

    # 2 — a permitted caller posts, and reads the whole record back
    allowed = _post(client, company_id, ALICE, "alice-1", _body("alice", party="ACME"))
    assert allowed.status_code == 201, allowed.text
    assert allowed.json()["lines"][1]["party"] == "ACME"
    alice_read = client.get(f"{BASE}/journal-entries", headers=headers(ALICE)).json()
    assert alice_read["items"][0]["lines"][1]["party"] == "ACME", "a permitted reader lost the field"
    print("alice posts and reads the party link back")

    # ... while a restricted reader does not see the field at all
    bob_read = client.get(f"{BASE}/journal-entries", headers=headers(BOB))
    assert bob_read.status_code == 200, bob_read.text
    line = bob_read.json()["items"][0]["lines"][1]
    assert "party" not in line, f"a restricted field reached the payload: {line}"
    assert "account" in line and "debit" in line, "the restriction removed the wrong fields"
    print(f"bob reads the same line without the restricted field: {sorted(line)}")

    # 3 — a field marked unwritable is refused, and nothing is stored
    with Session(engine) as session:
        scope_to_company(session, company_id)
        accountant = session.scalar(select(Role).where(Role.code == "accountant"))
        restrict(session, accountant, entity="journal_line", field="party", can_write=False)
        session.commit()

    refused_write = _post(client, company_id, ALICE, "alice-2", _body("alice again", party="ACME"))
    assert refused_write.status_code == 403, refused_write.text
    assert "journal_line.party" in refused_write.json()["error"]["message"]
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM journal_entry").scalar() == 1, (
            "the refused write was stored anyway"
        )
    print(f"a write to a restricted field is refused: {refused_write.json()['error']['message']}")

    # 4 — the permission change is live: no restart, no deploy
    with Session(engine) as session:
        scope_to_company(session, company_id)
        auditor = session.scalar(select(Role).where(Role.code == "auditor"))
        grant(session, auditor, "journal.post")
        session.commit()
    bob_posts_now = _post(client, company_id, BOB, "bob-2", _body("bob posts now"))
    assert bob_posts_now.status_code == 201, bob_posts_now.text
    print("granting the capability takes effect on the very next request")

    # 5 — every refusal is on the record
    with Session(engine) as session:
        scope_to_company(session, company_id)
        refusals = [row for row in read_trail(session) if row.action == REFUSED]
        assert len(refusals) == session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == REFUSED)
        ), "the trail is missing a refusal"
        attempted = sorted(row.after_values["attempted"] for row in refusals)
        assert attempted == [
            "journal.post",
            "journal.post",
            "journal.read",
            "write journal_line.party",
        ], attempted
        assert {row.actor for row in refusals} == {CAROL, BOB, ALICE}, "refusals are unattributed"
        carol_row = next(row for row in refusals if row.actor == CAROL)
        assert carol_row.after_values["held_roles"] == [], "the trail omits the roles held"
        bob_row = next(
            row for row in refusals if row.actor == BOB and row.after_values["attempted"] == "journal.post"
        )
        assert bob_row.after_values["held_roles"] == ["auditor"]
        print(f"{len(refusals)} refusals recorded, all attributable: {attempted}")

    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM journal_entry").scalar() == 2, (
            "the ledger does not hold exactly the two authorised postings"
        )

    engine.dispose()
    print("ok — permissions are enforced at the boundary, field by field, and refusals are recorded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
