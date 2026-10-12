"""T-8.WORK.01 check — the worker starts against the migrated schema, and a job enqueued
produces exactly one effect when a run is processed.

    DATABASE_URL=postgresql+psycopg://… python tests/check_worker.py

§4b's third principle is that every unit of background work is scheduled, idempotent,
observable and retryable, and §9 pinned the queue to Postgres — **`pgqueuer`, no Redis**.
`app/queue.py` holds the abstraction and the run record, `app/worker.py` is the process, and
`docker-compose.yml` gives that process a container. T-8.WORK.02's check drives the
*schedules*; this one drives `python -m app.worker` — the container's own command — and a job
**enqueued** onto the real queue, because "the worker runs the work" is a claim about the
process and the consumer rather than about the cron expressions.

It fails (non-zero exit) if any of these stops holding:

1. the schema the worker connects to is the **migrated** one — `alembic upgrade head` on an
   empty schema leaves the queue's own tables behind, and a worker without them refuses to
   start rather than idling a container that runs nothing
2. the worker's own process starts against it, run exactly as the container runs it, and
   reports the platform's real jobs and schedules
3. a job enqueued through the real queue and processed by the real consumer produces
   **exactly one effect** — one run row, `succeeded`, and the one lapsed session deleted
   while the live one survives
4. the queue library is `pgqueuer` and **no Redis** was introduced: not a setting, not a
   dependency, not an import

**Scratch database only**: it drops and recreates the schema, as the other checks do.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import identity, queue, schedule  # noqa: E402
from app.company import Company  # noqa: E402
from app.config import SETTINGS  # noqa: E402
from app.identity import UserSession  # noqa: E402

ALEMBIC = [sys.executable, "-m", "alembic", "-c", str(ROOT / "alembic.ini")]
# The job this check enqueues: a real registered unit of work with a countable effect — the
# sessions that lapsed are deleted, the ones that have not are left alone.
JOB = "purge_expired_sessions"
MANIFESTS = ("requirements.in", "requirements.lock", "requirements-dev.lock")
REDIS_IMPORT = re.compile(r"^\s*(?:import|from)\s+redis\b", re.MULTILINE)


def child_env(url: str) -> dict[str, str]:
    """This process's environment pointed at `url`, without an inherited `PYTHONPATH`.

    `alembic.ini` puts its own tree on the child's `sys.path`, and the application the child
    imports has to be this repository's — an inherited `PYTHONPATH` could shadow it.
    """
    env = {name: value for name, value in os.environ.items() if name != "PYTHONPATH"}
    env["DATABASE_URL"] = url
    return env


def run_in(url: str, args: list[str]) -> subprocess.CompletedProcess:
    """A command in its own process, against `url` — the operator's own invocation."""
    return subprocess.run(args, cwd=ROOT, env=child_env(url), capture_output=True, text=True)


def lapsed_and_live(engine) -> tuple[uuid.UUID, uuid.UUID]:
    """One session that lapsed more than a day ago, and one that has not. Their two ids.

    Two, not one, because "exactly one effect" is a claim about what the job did *and* what
    it left alone: a purge that deleted both would satisfy a count of one and still be wrong.
    """
    with Session(engine) as session:
        company = Company(
            code="WORKCHECK",
            name="The worker's check",
            base_currency="PHP",
            fiscal_year_start_month=1,
        )
        session.add(company)
        session.flush()
        user = identity.create_user(
            session, login="worker-check", display_name="The worker's check",
            companies=[company.id],
        )
        now = datetime.now(timezone.utc)
        rows = [
            UserSession(
                token_hash=uuid.uuid4().hex,
                user_id=user.id,
                active_company_id=company.id,
                expires_at=now - timedelta(days=2),
            ),
            UserSession(
                token_hash=uuid.uuid4().hex,
                user_id=user.id,
                active_company_id=company.id,
                expires_at=now + timedelta(hours=1),
            ),
        ]
        session.add_all(rows)
        session.commit()
        return rows[0].id, rows[1].id


async def enqueue_then_consume(dsn: str) -> int:
    """Enqueue the job on the real queue, then let the real consumer drain it.

    `queue.connected` and `queue.drain` are the worker's own path (`app/worker.py` drives the
    same manager in the same mode), so this is the consumer the container runs rather than a
    loop the check wrote.
    """
    async with queue.connected(dsn) as app:
        await queue.enqueue(app.queries, JOB)
    return await queue.drain(dsn, timeout=timedelta(seconds=5))


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is required (a scratch Postgres)", file=sys.stderr)
        return 2

    engine = create_engine(url)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")

    # 1 — the migrated schema, by the operator's own command, in its own process
    upgraded = run_in(url, [*ALEMBIC, "upgrade", "head"])
    assert upgraded.returncode == 0, (
        f"alembic upgrade head failed on an empty schema:\n{upgraded.stdout}{upgraded.stderr}"
    )
    with engine.connect() as connection:
        present = set(
            connection.exec_driver_sql(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            ).scalars()
        )
    missing = [name for name in queue.QUEUE_TABLES if name not in present]
    assert not missing, (
        f"the migrated schema has no queue tables ({missing}), so a worker has nothing to"
        " consume from — the migration environment installs them"
    )
    print(f"the migrated schema carries the queue's own tables: {', '.join(queue.QUEUE_TABLES)}")

    # 2 — the container's own command, in its own process, against that schema
    reported = run_in(url, [sys.executable, "-m", "app.worker", "--check"])
    assert reported.returncode == 0, (
        "`python -m app.worker --check` refused the migrated schema:\n"
        f"{reported.stdout}{reported.stderr}"
    )
    unwired = [name for name in sorted(schedule.SCHEDULES) if name not in reported.stdout]
    assert not unwired, (
        f"the worker does not report the platform's own work ({unwired}):\n{reported.stdout}"
    )
    print(
        "`python -m app.worker --check` starts against the migrated schema and reports"
        f" {', '.join(sorted(queue.JOBS))}"
    )

    # 3 — a job enqueued, processed by the real consumer, with exactly one effect
    lapsed_id, live_id = lapsed_and_live(engine)
    logged = asyncio.run(enqueue_then_consume(SETTINGS.dsn))

    with Session(engine) as session:
        assert session.get(UserSession, lapsed_id) is None, (
            "the lapsed session is still there, so the job did not run"
        )
        assert session.get(UserSession, live_id) is not None, (
            "the job deleted a session that had not lapsed — the effect is not the purge"
        )
        runs = queue.runs_for(session, job=JOB)
        assert len(runs) == 1, (
            f"one enqueued job left {len(runs)} run row(s):"
            f" {[(str(run.status), run.attempt) for run in runs]}"
        )
        assert runs[0].status == queue.SUCCEEDED, (
            f"the run is {runs[0].status!r}, not succeeded: {runs[0].error}"
        )
        record = (runs[0].attempt, runs[0].duration_ms)
    print(
        f"one job enqueued, one run processed ({logged} logged by the queue): the lapsed"
        f" session deleted, the live one kept; run attempt {record[0]} in {record[1]} ms"
    )

    # 4 — the library is pgqueuer, and no Redis arrived with it
    assert SETTINGS.queue_library == "pgqueuer", (
        f"the queue library is {SETTINGS.queue_library!r}, and §9 pinned it to pgqueuer"
    )
    for name in MANIFESTS:
        manifest = (ROOT / name).read_text()
        assert "pgqueuer" in manifest, f"{name} does not carry the queue library"
        assert not re.search(r"^\s*redis", manifest, re.MULTILINE), (
            f"{name} asks for Redis, which §8 made a non-goal"
        )
    importing = [
        str(path.relative_to(ROOT))
        for path in (ROOT / "app").rglob("*.py")
        if REDIS_IMPORT.search(path.read_text())
    ]
    assert not importing, f"a module imports Redis: {importing}"
    print("the queue is pgqueuer, pinned in all three manifests, and nothing imports Redis")

    engine.dispose()
    print("ok — the worker starts against the migrated schema and runs an enqueued job once")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
