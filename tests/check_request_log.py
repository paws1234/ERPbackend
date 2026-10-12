"""T-8.OBS.01 check — one structured line per request, carrying the correlation id the failure
is also answered with, and no credential in any line.

    DATABASE_URL=postgresql+psycopg://… python tests/check_request_log.py

Before Phase 8 nothing was logged at all, so an unexpected failure answered `500` with the
exception's own words and appeared nowhere else: finding 2 of the 2026-10-10 audit. This check
drives real requests through the real app and reads the **emitted stream** — not the logger's
configuration, which is a claim, but the lines that came out of it.

It fails (non-zero exit) if any of these stops holding:

1. **one line per request, and it is JSON** — the platform's request lines are counted against
   the requests driven, and every line the platform emits parses as a JSON object
2. **the line carries the correlation id, the route and the outcome** — the id on the line is
   the one the response was answered with (the caller's when it sent a well-formed
   `X-Request-Id`, the middleware's own when it did not), the route is the *template* rather
   than the raw path, and the status and the duration are on the line
3. **a failure is answered with the id it was logged under** — a request forced to fail answers
   `internal_error`, and the body *and* the header carry the same id as its request line and
   its traceback line, so the id a client quotes finds the line in the log
4. **no credential and nothing else the caller sent appears in a line the platform wrote** —
   not the session token, not the password, not a query string, not a header

**Scratch database only**: it drops and recreates the schema, and it ends with the schema
dropped, because the failing request is exactly "the database went away".
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ALEMBIC = [sys.executable, "-m", "alembic", "-c", str(ROOT / "alembic.ini")]
# Values a caller sends that must never come back out in a line: the second is in a query
# string and the third in a header, which are the two routes a raw path or a header dump
# would leak them through.
QUERY_SECRET = "check-query-secret"
HEADER_SECRET = "check-header-secret"


def child_env(url: str) -> dict[str, str]:
    """This process's environment pointed at `url`, without an inherited `PYTHONPATH`."""
    env = {name: value for name, value in os.environ.items() if name != "PYTHONPATH"}
    env["DATABASE_URL"] = url
    return env


def read_stream(stream: str) -> list[dict]:
    """Every line the platform emitted, parsed — and a refusal for a line that is not JSON.

    Parsing the whole stream, not just the lines this check looks for: "the lines are JSON" is
    only true if *every* one is, and a traceback written as several raw lines would show up
    here rather than in an operator's terminal.
    """
    records = []
    for number, line in enumerate(stream.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise AssertionError(f"line {number} of the log stream is not JSON: {line!r}") from exc
    return records


def request_lines(records: list[dict], logger_name: str = "erp") -> list[dict]:
    """The platform's one-line-per-request records, in the order the requests were served."""
    return [
        record
        for record in records
        if record.get("logger") == logger_name and record.get("message") == "request"
    ]


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is required (a scratch Postgres)", file=sys.stderr)
        return 2

    engine = create_engine(url)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    migrated = subprocess.run(
        [*ALEMBIC, "upgrade", "head"], cwd=ROOT, env=child_env(url), capture_output=True, text=True
    )
    assert migrated.returncode == 0, (
        f"alembic upgrade head failed on an empty schema:\n{migrated.stdout}{migrated.stderr}"
    )

    # The app configures logging at import (its start). The stream is swapped afterwards, so
    # what this check reads is the lines the running app emits rather than the startup's.
    from fastapi.testclient import TestClient

    from app import observability
    from app.api import BASE, app
    from app.company import Company
    from tests.identity import CHECK_PASSWORD, sign_in

    stream = io.StringIO()
    observability.configure(stream=stream)

    with Session(engine) as session:
        company = Company(
            code="LOGCHECK", name="The request log's check", base_currency="PHP",
            fiscal_year_start_month=1,
        )
        session.add(company)
        session.commit()
        company_id = company.id
    signed_in = sign_in(engine, company_id=company_id, actor="request-log-check")
    token = signed_in["Cookie"].split("=", 1)[1]

    client = TestClient(app, raise_server_exceptions=False)
    answered: list[tuple[object, str | None]] = []

    # 1 — a request that names its own correlation id, and one that does not
    supplied = client.get(f"{BASE}/health", headers={"X-Request-Id": "check-log-supplied"})
    # 2 — a refusal a stranger gets, and a path that matches no route, each carrying something
    # the caller sent that must not come back out
    refused = client.get(
        f"{BASE}/companies/current",
        headers={"Authorization": f"Bearer {HEADER_SECRET}"},
    )
    missing = client.get(
        f"{BASE}/no-such-route?token={QUERY_SECRET}",
        headers={"X-Request-Id": "check-log-missing"},
    )
    minted = client.get(f"{BASE}/health")
    answered += [supplied, refused, missing, minted]

    # 3 — a request forced to fail: the database goes away under an authenticated caller, which
    # is an unhandled exception rather than a refusal the platform has a shape for
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    failed = client.get(
        f"{BASE}/currencies", headers={**signed_in, "X-Request-Id": "check-log-failed"}
    )
    answered.append(failed)

    records = read_stream(stream.getvalue())
    lines = request_lines(records)
    assert len(lines) == len(answered), (
        f"{len(answered)} request(s) produced {len(lines)} request line(s): the platform logs"
        f" one per request — {[line.get('route') for line in lines]}"
    )

    # 2 — each line names the id the response came back with, and the outcome it had
    for response, line in zip(answered, lines, strict=True):
        header = response.headers.get("X-Request-Id")
        assert header, f"the response to {line.get('route')} carried no correlation id"
        assert line.get("request_id") == header, (
            f"the line says {line.get('request_id')!r} and the response said {header!r}"
        )
        assert line.get("status") == response.status_code, (
            f"the line says {line.get('status')} and the response was {response.status_code}"
        )
        assert line.get("method") and line.get("route"), f"a line is missing its route: {line}"
        assert isinstance(line.get("duration_ms"), (int, float)), (
            f"the line carries no duration: {line}"
        )
    assert lines[0]["request_id"] == "check-log-supplied", (
        f"a caller's well-formed X-Request-Id was not the id used: {lines[0]['request_id']!r}"
    )
    assert observability.REQUEST_ID_SHAPE.match(lines[3]["request_id"]), (
        f"a request with no header was not given a shaped id: {lines[3]['request_id']!r}"
    )
    assert lines[2]["route"] == "unmatched", (
        f"the raw path was logged instead of the route template: {lines[2]['route']!r}"
    )
    print(
        "5 requests, 5 JSON lines, each carrying the id its response was answered with"
        f" ({lines[0]['request_id']}, {lines[3]['request_id']}, …), status, duration and the"
        " route template"
    )

    # 3 — the id in a failure's body and header is the id on its line
    body = failed.json()
    reported = body["error"]["details"][0]["request_id"]
    assert body["error"]["code"] == "internal_error", f"the failure answered {body['error']}"
    assert reported == "check-log-failed" == lines[4]["request_id"], (
        f"the body says {reported!r} and the line says {lines[4]['request_id']!r} — the id a"
        " client quotes would not find the line"
    )
    tracebacks = [
        record
        for record in records
        if str(record.get("message", "")).startswith("unhandled exception")
        and record.get("logger") == "erp"
    ]
    assert tracebacks, "the failure was answered but the traceback reached no line"
    assert all(record.get("request_id") == "check-log-failed" for record in tracebacks), (
        "a line about the failure does not carry the id the client was given:"
        f" {[record.get('request_id') for record in tracebacks]}"
    )
    print(
        f"a forced failure answered internal_error with {reported} in the body, the header and"
        f" all {len(tracebacks)} line(s) about it"
    )

    # 4 — nothing the caller sent, and no credential, came back out in a line the platform wrote
    written = "\n".join(
        json.dumps(record) for record in records if record.get("logger") == "erp"
    )
    for what, value in (
        ("the session token", token),
        ("the check password", CHECK_PASSWORD),
        ("an Authorization header", HEADER_SECRET),
        ("a query string", QUERY_SECRET),
    ):
        assert value not in written, f"{what} appears in a line the platform wrote"
    print("no session token, password, header or query string appears in a line the platform wrote")

    engine.dispose()
    print("ok — one JSON line per request, carrying the id the failure is answered with")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
