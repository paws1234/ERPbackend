"""T-8.OBS.03 check — a failure answers in one shape with nothing of itself in it, and it is
counted where an operator can scrape it.

    DATABASE_URL=postgresql+psycopg://… python tests/check_error_hygiene.py

An unhandled failure used to answer with the exception's own words — its class and its message,
which name a table, a column or the SQL it failed on — handed to whoever asked, and it was
counted nowhere an operator could see. Plan §4b's fourth principle is "nothing fails silently".

It fails (non-zero exit) if any of these stops holding:

1. **a forced failure answers `500` `internal_error`** with the correlation id, in the one
   documented error shape, and **none of the exception's own words appear in the body**: not
   its class, not the SQL or the identifiers its message named, not the path the caller asked
   for. Those words are read **out of the log**, where they belong, so "absent from the body"
   is measured against what actually leaked into the log rather than against a guess
2. **the failure is counted where an operator can see it** — `/metrics` carries
   `erp_errors_total{kind="unhandled"}` and the `500` on the request counter, so "the log says
   it failed" and "the counter says it failed" cannot disagree
3. **`/metrics` is reachable only as the plan intends, and leaks nothing** — Prometheus text
   when `metrics_enabled` is true and `404` when it is false; no caller-supplied value becomes
   a series (a path somebody typed is the `unmatched` label, never a label of its own); and no
   credential is in the exposition

**Scratch database only**: it drops and recreates the schema, and it ends with the schema
dropped, because the forced failure is a database that went away.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ALEMBIC = [sys.executable, "-m", "alembic", "-c", str(ROOT / "alembic.ini")]
FAILED_ID = "check-hygiene-failed"
# What a caller types that must never become a series of its own.
TYPED_PATH = "check-hygiene-typed"
# The words a body must not carry. The first three are read out of the log at run time; these
# are the ones a leaked exception answers with whatever the failure was.
NEVER_IN_BODY = ("Traceback", "psycopg", "sqlalchemy")
QUOTED = re.compile(r'"([^"\n]{4,60})"')


def child_env(url: str, **extra: str) -> dict[str, str]:
    """This process's environment pointed at `url`, without an inherited `PYTHONPATH`."""
    env = {name: value for name, value in os.environ.items() if name != "PYTHONPATH"}
    env["DATABASE_URL"] = url
    env.update(extra)
    return env


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

    from fastapi.testclient import TestClient

    from app import observability
    from app.api import BASE, app
    from app.company import Company
    from tests.identity import sign_in

    # The app configured logging at import; the stream is swapped afterwards so this check
    # reads the lines the running app emits.
    stream = io.StringIO()
    observability.configure(stream=stream)

    with Session(engine) as session:
        company = Company(
            code="HYGIENE", name="The error hygiene's check", base_currency="PHP",
            fiscal_year_start_month=1,
        )
        session.add(company)
        session.commit()
        company_id = company.id
    signed_in = sign_in(engine, company_id=company_id, actor="error-hygiene-check")
    token = signed_in["Cookie"].split("=", 1)[1]

    client = TestClient(app, raise_server_exceptions=False)
    # A path a caller typed, with something private in the query string, so the metric labels
    # can be shown not to carry it
    client.get(f"{BASE}/no-such-route?token={TYPED_PATH}", headers={"X-Request-Id": "check-typed"})

    # 1 — the forced failure: the database goes away under an authenticated caller
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    failed = client.get(f"{BASE}/currencies", headers={**signed_in, "X-Request-Id": FAILED_ID})
    body = failed.json()
    written = stream.getvalue()

    assert failed.status_code == 500, f"an unhandled exception answered {failed.status_code}"
    assert body["error"]["code"] == "internal_error", f"the failure answered {body['error']}"
    assert set(body) == {"error"}, f"the failure is not the one error shape: {sorted(body)}"
    assert set(body["error"]) <= {"code", "message", "details"}, (
        f"the error object carries more than one shape: {sorted(body['error'])}"
    )
    reported = body["error"]["details"][0]["request_id"]
    assert reported == FAILED_ID == failed.headers.get("X-Request-Id"), (
        f"the body says {reported!r} and the header {failed.headers.get('X-Request-Id')!r}, so a"
        " client cannot quote the id the failure was logged under"
    )

    # The exception's own words, read out of the log where they are supposed to be: its class
    # from the line that names it, and the identifiers its message quoted from the traceback.
    # Calibrating against the log rather than a fixed list is what makes "absent from the body"
    # a measurement of what actually leaked instead of a guess about what might have.
    records = [json.loads(line) for line in written.splitlines() if line.strip()]
    failures = [
        record
        for record in records
        if str(record.get("message", "")).startswith("unhandled exception")
    ]
    named = [
        match.group(1)
        for match in (re.search(r"unhandled exception: ([A-Za-z_]\w*)", str(r.get("message"))) for r in failures)
        if match
    ]
    assert named, f"the failure was logged without naming its class:\n{written[-800:]}"
    traceback_text = "\n".join(str(record.get("exception") or "") for record in failures)
    assert traceback_text.strip(), "the failure reached the log without a traceback to compare against"
    leaked = set(named) | set(QUOTED.findall(traceback_text))
    assert len(leaked) > 1, (
        f"the traceback named nothing but the class ({sorted(leaked)}), so this comparison would"
        " be about one word only"
    )

    serialised = json.dumps(body)
    for word in sorted(leaked) + list(NEVER_IN_BODY):
        assert word not in serialised, f"the body carries the exception's own {word!r}: {serialised}"
    for path in (f"{BASE}/currencies", TYPED_PATH):
        assert path not in serialised, f"the body carries the path the caller asked for: {path!r}"
    assert "select " not in serialised.lower(), f"the body carries SQL: {serialised}"
    assert FAILED_ID in serialised, "the body does not carry the correlation id at all"
    print(
        f"a forced {named[0]} answered one shape with {reported} and none of its own words"
        f" ({', '.join(sorted(leaked)[:4])}…), which are in the log"
    )

    # 2 and 3 — the counters, the label discipline and the credential
    metrics = client.get(f"{BASE}/metrics")
    assert metrics.status_code == 200, f"/metrics answered {metrics.status_code}"
    assert "text/plain" in metrics.headers.get("content-type", ""), metrics.headers.get("content-type")
    exposition = metrics.text
    assert 'erp_errors_total{kind="unhandled"}' in exposition, (
        "the failure is not counted anywhere an operator can scrape it:"
        f" {[line for line in exposition.splitlines() if line.startswith('erp_errors')]}"
    )
    errors = float(exposition.split('erp_errors_total{kind="unhandled"}')[1].split()[0])
    assert errors >= 1, f"the unhandled-error counter is {errors}"
    assert re.search(r'erp_requests_total\{[^}]*status="500"[^}]*\} \d', exposition), (
        f"the 500 is not on the request counter: {[l for l in exposition.splitlines() if 'erp_requests_total' in l]}"
    )
    assert 'route="unmatched"' in exposition, "the unmatched path is not labelled as such"
    assert TYPED_PATH not in exposition, (
        "a value the caller typed became a metric label — unbounded cardinality, and whatever"
        " they put in the URL"
    )
    assert token not in exposition, "the exposition carries the session token"
    print(
        f"the failure is on /metrics (erp_errors_total{{kind=\"unhandled\"}} = {errors:g}, and the"
        " 500 on the request counter), the typed path is one `unmatched` label, and no secret is"
        " in the exposition"
    )

    # The guard that keeps that from ever being true again: counting a name nobody declared is
    # a refusal rather than a series that is kept and never scraped, which is what this counter
    # used to be.
    fresh = observability.Registry()
    try:
        fresh.inc("erp_nobody_declared_this")
    except observability.UnknownMetric:
        pass
    else:
        raise AssertionError(
            "the registry counted a metric nobody declared — it would be counted and never"
            " scraped, and /metrics would silently omit it"
        )
    print("the registry refuses to count a metric nobody declared")

    # 3 — switched off, the surface is a 404 rather than an empty document
    off = subprocess.run(
        [
            sys.executable,
            "-c",
            "from fastapi.testclient import TestClient\n"
            "from app.api import app, BASE\n"
            "print(TestClient(app, raise_server_exceptions=False).get(f'{BASE}/metrics').status_code)\n",
        ],
        cwd=ROOT,
        env=child_env(url, METRICS_ENABLED="false"),
        capture_output=True,
        text=True,
    )
    answered = off.stdout.strip().splitlines()[-1] if off.stdout.strip() else off.stderr[-200:]
    assert answered == "404", f"with METRICS_ENABLED=false, /metrics answered {answered!r}"
    print("with metrics_enabled false, /metrics answers 404 rather than an empty document")

    engine.dispose()
    print("ok — a failure answers one shape with no internals, and is counted where it is scraped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
