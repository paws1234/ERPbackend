"""T-0.API.01 check — the conventions hold, and the published contract is current.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/postgres \
        uv run --with fastapi --with httpx --with 'sqlalchemy>=2.0' \
        --with 'psycopg[binary]' python tests/check_api_conventions.py

It fails (non-zero exit) if any of these stops holding:

1. the contract is generated from the app and **current**: what a running
   instance serves at ``/api/v1/openapi.json`` equals ``contract/v1/openapi.json``
   byte-for-byte as JSON, and the artifact path carries the version, so a change
   cannot be published inside a version the frontend has already pinned
2. every refusal answers in the one error shape — a domain refusal, a validation
   failure, a bad header and a missing route alike
3. every documented field states its type, and every field that is not required
   is nullable, so "optional" is never implicit
4. a posting endpoint is retry-safe: the same idempotency key and body posts
   once and replays the first answer, while the same key with a different body
   is refused
5. lists are paginated with limit/offset and report the total

**Scratch database only**: it drops and recreates the schema.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api import API_VERSION, BASE, app  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger import posting  # noqa: E402,F401 — every check builds the one schema
from app.security import assign, define_role, grant  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
CURRENT_DAY = "2026-09-17"
BALANCED = {
    "posting_date": CURRENT_DAY,
    "currency": "PHP",
    "memo": "api check",
    "lines": [
        {"account": "1000", "debit": "100.00"},
        {"account": "4000", "credit": "100.00"},
    ],
}
TYPE_KEYS = ("type", "anyOf", "allOf", "oneOf", "$ref")


def _shape_error(response) -> dict:
    """The one error shape, or a failure that says what arrived instead."""
    body = response.json()
    assert isinstance(body, dict) and set(body) == {"error"}, f"not the one error shape: {body}"
    error = body["error"]
    assert set(error) == {"code", "message", "details"}, f"unclear error shape: {error}"
    assert isinstance(error["code"], str) and isinstance(error["message"], str)
    return error


def _check_documented_fields(contract: dict) -> int:
    """Every property is typed, and every optional one says so."""
    checked = 0
    for name, schema in contract.get("components", {}).get("schemas", {}).items():
        required = set(schema.get("required", []))
        for field, definition in schema.get("properties", {}).items():
            assert any(key in definition for key in TYPE_KEYS), (
                f"{name}.{field} states no type: {definition}"
            )
            if field not in required:
                nullable = definition.get("type") == "null" or any(
                    option.get("type") == "null"
                    for option in definition.get("anyOf", definition.get("oneOf", []))
                )
                assert nullable, f"{name}.{field} is optional but not nullable: {definition}"
            checked += 1
    return checked


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
                code="API-CHECK",
                name="API check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()
        # The caller needs the capabilities T-0.SEC.01 enforces at the boundary;
        # without them every request in this check would be refused as forbidden.
        role = define_role(session, company_id=company_id, code="api-check", name="API check")
        grant(session, role, "company.read", "journal.read", "journal.post")
        assign(session, company_id=company_id, subject="alice", role=role)
        session.commit()

    client = TestClient(app, raise_server_exceptions=False)
    headers = {"X-Company-Id": str(company_id), "X-Actor": "alice"}

    # 1 — the contract is generated, served, and matches what is published
    health = client.get(f"{BASE}/health")
    assert health.status_code == 200 and health.json()["version"] == API_VERSION
    served = client.get(f"{BASE}/openapi.json")
    assert served.status_code == 200, "a running instance does not serve its contract"
    artifact = ROOT / "contract" / API_VERSION / "openapi.json"
    assert artifact.exists(), f"the contract was never published to {artifact.name}"
    committed = json.loads(artifact.read_text())
    assert served.json() == committed, (
        "the published contract differs from the app: publish it again"
        " (tools/publish_contract.py) before the frontend builds against v1"
    )
    assert artifact.parent.name == API_VERSION, "the artifact path does not carry the version"
    print(f"the contract the app serves equals contract/{API_VERSION}/openapi.json")

    # 3 — every documented field states its type and its optionality
    checked = _check_documented_fields(committed)
    print(f"every one of the {checked} documented fields is typed; optional fields are nullable")

    # 2 — one error shape, whatever went wrong
    cases = {
        "unbalanced_entry": client.post(
            f"{BASE}/journal-entries",
            headers={**headers, "Idempotency-Key": "unbalanced"},
            json={**BALANCED, "lines": [{"account": "1000", "debit": "100.00"},
                                        {"account": "4000", "credit": "90.00"}]},
        ),
        "invalid_request": client.post(
            f"{BASE}/journal-entries", headers={**headers, "Idempotency-Key": "invalid"}, json={}
        ),
        "invalid_company": client.get(f"{BASE}/companies/current", headers={"X-Company-Id": "nope"}),
        "not_found": client.get(f"{BASE}/no-such-endpoint", headers=headers),
    }
    for expected, response in cases.items():
        error = _shape_error(response)
        assert error["code"] == expected, f"{expected}: got {error}"
        print(f"{response.status_code} {error['code']}: {error['message'][:60]}")

    # 4 — the posting endpoint is retry-safe
    key = {"Idempotency-Key": "po-2026-0001"}
    first = client.post(f"{BASE}/journal-entries", headers={**headers, **key}, json=BALANCED)
    assert first.status_code == 201, first.text
    assert "Idempotent-Replay" not in first.headers
    again = client.post(f"{BASE}/journal-entries", headers={**headers, **key}, json=BALANCED)
    assert again.status_code == 201 and again.json() == first.json(), "the retry posted again"
    assert again.headers.get("Idempotent-Replay") == "true", "the retry is not marked as a replay"
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM journal_entry").scalar() == 1, (
            "the retried request posted twice"
        )
    clash = client.post(
        f"{BASE}/journal-entries",
        headers={**headers, **key},
        json={**BALANCED, "memo": "a different body"},
    )
    assert _shape_error(clash)["code"] == "idempotency_key_reused", clash.text
    print("the same key and body posted once and replayed; a different body is refused")

    # 5 — lists are paginated and report the total
    client.post(
        f"{BASE}/journal-entries",
        headers={**headers, "Idempotency-Key": "second"},
        json={**BALANCED, "memo": "second posting"},
    )
    page = client.get(f"{BASE}/journal-entries", headers=headers, params={"limit": 1, "offset": 0})
    assert page.status_code == 200, page.text
    body = page.json()
    assert (body["limit"], body["offset"], body["total"], len(body["items"])) == (1, 0, 2, 1), body
    second = client.get(f"{BASE}/journal-entries", headers=headers, params={"limit": 1, "offset": 1})
    assert second.json()["items"][0]["id"] != body["items"][0]["id"], "offset did not advance"
    print("a list page answers items/limit/offset/total and advances by offset")

    # the company endpoint answers what the shell reads first
    company = client.get(f"{BASE}/companies/current", headers=headers)
    assert company.status_code == 200 and company.json()["code"] == "API-CHECK"
    assert company.json()["fiscal_year_start_month"] == 1

    engine.dispose()
    print("ok — the conventions hold and the published contract is what the app serves")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
