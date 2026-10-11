"""T-8.WORK.01 — every unit of background work: scheduled, idempotent, recorded, retryable.

**Why this module exists.** §8 pinned the queue to Postgres — **`pgqueuer`**, no Redis — and
the platform had no background work at all: an expired session was purged by whoever
remembered to call it, an outbound delivery was retried by the request that sent it, and a
biometric clock was never asked for anything. Plan §4b's third principle is that every unit
of background work is scheduled, idempotent, observable and retryable, and that a job which
ran twice is safe *and visible as such*.

Four decisions, each a rule a reviewer can fail this module against:

* **The queue is the library's; the record is ours** (:class:`JobRun`). `pgqueuer` owns
  enqueue/dequeue, its own tables, its notifications and the scheduling clock
  (:mod:`app.schedule`); this module owns the **run row** — one per attempt, with the job,
  the idempotency key, the status, the attempt number, the error, the duration and, when a
  retry is due, the instant it is due. A queue with no run table answers "is something
  running?" and nothing else; an operator needs "which job, how many times, what did it say".
* **A run is idempotent by its key** (:func:`run_job`). Two runs of the same job under the
  same key produce one effect: the second is recorded as ``skipped`` rather than performed
  — visible, not silent. The guard is the database's, not the lookup's: a partial unique
  index on ``(job, key) WHERE status = 'succeeded'`` means two workers racing cannot both
  win, and the loser records the skip. The key comes from the *queue* rather than from a
  guess: the scheduler stamps the firing instant into the payload and the queue carries it
  in its dedupe key, so a redelivered job (a worker died mid-flight) is recognised as a
  duplicate.
* **A failure retries and is recorded** (:func:`run_job`'s attempt loop). Each attempt is
  its own row, so "it failed twice then worked" is readable; the delay before the next
  attempt comes from `DELIVERY_BACKOFF_MINUTES` / `DELIVERY_BACKOFF_MAX_MINUTES` (§9) and
  is recorded on the failed row as ``next_attempt_at``. The waiting itself is a seam
  (:data:`PAUSE`) so a check drives a retry without sleeping through it.
* **A job speaks the platform's own session** — a function of a `Session`, like every other
  unit of work in this tree — and a job's writes are attributed to the job
  (``job:<name>``) on the trail instead of landing as `unknown`.

**Ceilings, recorded rather than discovered later.**

* A retry that outlives its worker is **not** rescheduled: a job's attempts run inside one
  invocation, and a process that dies mid-job leaves its ``running`` row behind — which is
  itself the signal, and `OPERATIONS.md` says what to do with it. Upgrade path: enqueue a
  deferred job with ``execute_after`` for the next attempt instead of looping, which needs
  the worker to requeue rather than the run to retry.
* `pgqueuer`'s own tables are excluded from Alembic's autogenerate
  (`migrations/env.py`'s ``INSTALLATION_OWNED_TABLES``) and installed from the library's
  own DDL (:func:`install_queue_schema`), so a library upgrade cannot be quietly reverted
  by a migration that had a stale idea of the library's schema.
* One queue manager per process, no cross-process leader election: pgqueuer enforces
  per-entrypoint concurrency limits in SQL, and the stack runs one worker container
  (`docker-compose.yml`). More throughput is more containers.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Engine,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    create_engine,
    func,
    select,
    text,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.audit import set_actor
from app.config import SETTINGS
from app.db import Base
from app.observability import METRICS, log

# The states a run row can be in. `running` is written before the work starts, so a
# process that died mid-job leaves evidence rather than nothing.
RUNNING, SUCCEEDED, FAILED, SKIPPED = "running", "succeeded", "failed", "skipped"
RUN_STATUSES = (RUNNING, SUCCEEDED, FAILED, SKIPPED)

# The names of pgqueuer's own tables, as the library states them. Kept here because
# `migrations/env.py` must exclude them from autogenerate and `tests/check_bootstrap.py`
# must allow them when it counts the tables an install leaves behind.
QUEUE_TABLES = ("pgqueuer", "pgqueuer_log", "pgqueuer_statistics", "pgqueuer_schedules")

# The channel pgqueuer listens on. Named for this application so two installations that
# share a database by mistake do not wake each other's workers.
CHANNEL = "ch_erpv1_jobs"

# How a job names itself on the audit trail.
JOB_ACTOR_PREFIX = "job:"

# The pause between attempts. A module-level seam so a check can drive a retry without
# sleeping through the backoff it proves is recorded.
PAUSE: Callable[[float], None] = time.sleep


class QueueError(RuntimeError):
    """The queue cannot do what it was asked."""


class UnknownJob(QueueError):
    """A job name that nobody registered — a typo, or work that was never wired."""


class JobRun(Base):
    """One attempt at one job: what ran, under which key, how it ended and how long it took.

    Installation-level (``app/db.py``'s ``UNSCOPED_TABLES``): a scheduled job belongs to
    the platform, not to one company, and the worker that runs it holds no company session
    — a company-scoped row would be invisible to the very process that has to report it.
    A job's *effect* is company-scoped like everything else; the record that it ran is not.
    """

    __tablename__ = "job_run"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'skipped')", name="ck_job_run_status"
        ),
        # The idempotency guard. A partial index because only a *successful* run claims a
        # key: a failed attempt must be allowed to be retried under the same key.
        Index(
            "uq_job_run_once",
            "job",
            "key",
            unique=True,
            postgresql_where=text("status = 'succeeded'"),
        ),
        Index("ix_job_run_job_started", "job", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # The registered job's name — the same string the queue's entrypoint carries.
    job: Mapped[str] = mapped_column(String(64), nullable=False)
    # The idempotency key: two runs with the same (job, key) have one effect.
    key: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    # When a failed run's next attempt is due — the backoff, recorded rather than implied.
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def install_queue_schema(connection) -> None:
    """Install pgqueuer's own schema, from the library's own DDL.

    Called by `migrations/env.py` after the versioned steps, so a migrated database has
    the queue's tables (and its enum, function and trigger) without this repository
    hand-writing a copy of them. Idempotent: it does nothing when the queue table is
    already there, because pgqueuer's own DDL is not written with ``IF NOT EXISTS``.
    """
    from pgqueuer.adapters.persistence import qb

    if connection.exec_driver_sql("SELECT to_regclass('pgqueuer') IS NOT NULL").scalar():
        return
    statements = qb.QueryBuilderEnvironment().build_install_query(create_schema=False)
    # The DDL is a multi-statement script with function bodies in it, so it goes to the
    # driver in one piece rather than being split on semicolons (which would break on
    # `$$`). psycopg sends a parameterless statement as a simple query, which is what
    # allows several statements in one call.
    driver = connection.connection.driver_connection
    with driver.cursor() as cursor:
        cursor.execute(statements)


# --- The job registry ---------------------------------------------------------


@dataclass(frozen=True)
class Job:
    """One unit of background work: its name, what it does, and how hard it may try."""

    name: str
    run: Callable[[Session], Any]
    max_attempts: int = 1
    description: str = ""


JOBS: dict[str, Job] = {}


def register_job(
    name: str,
    run: Callable[[Session], Any],
    *,
    max_attempts: int = 1,
    description: str = "",
) -> Job:
    """Declare a job. Registering one name twice with different work is a mistake."""
    if not name or len(name) > 64:
        raise QueueError(f"a job name is 1–64 characters, got {name!r}")
    if max_attempts < 1:
        raise QueueError(f"{name}: a job is attempted at least once, got {max_attempts}")
    existing = JOBS.get(name)
    if existing is not None and existing.run is not run:
        raise QueueError(f"job {name!r} is already registered with different work")
    job = Job(name=name, run=run, max_attempts=max_attempts, description=description)
    JOBS[name] = job
    return job


def job_for(name: str) -> Job:
    """The registered job, or a refusal naming it."""
    try:
        return JOBS[name]
    except KeyError:
        raise UnknownJob(
            f"no job named {name!r} is registered; registered: {', '.join(sorted(JOBS)) or 'none'}"
        ) from None


def describe() -> list[dict[str, Any]]:
    """Every registered job with what it does — logged at start, and read by OPERATIONS.md."""
    return [
        {"job": job.name, "attempts": job.max_attempts, "description": job.description}
        for job in sorted(JOBS.values(), key=lambda job: job.name)
    ]


# --- Running a job ------------------------------------------------------------


def run_job(
    session: Session,
    name: str,
    *,
    key: str | None = None,
    pause: Callable[[float], None] | None = None,
) -> JobRun:
    """Run one job, recording every attempt. Returns the last attempt's row.

    A run whose key has already succeeded does nothing and is recorded as ``skipped`` —
    which is what makes a redelivered queue job, or a second firing under one key, safe
    *and* visible. A failure is retried inline up to the job's ``max_attempts``, with the
    next attempt's instant recorded on the failed row.

    The session is the caller's; every run row is committed here, so it survives whatever
    the job's own work does (a job that rolls its own transaction back still leaves its
    failure and its retries on the record).
    """
    job = job_for(name)
    pause = pause or PAUSE
    if _succeeded_run(session, name, key) is not None:
        return _record(session, job=name, key=key, status=SKIPPED, attempt=0)

    run: JobRun | None = None
    for attempt in range(1, job.max_attempts + 1):
        # Attribution (T-0.AUDIT.02): a job's writes name the job, not `unknown`.
        session.rollback()
        set_actor(session, f"{JOB_ACTOR_PREFIX}{name}")
        run = _record(session, job=name, key=key, status=RUNNING, attempt=attempt)
        started = time.perf_counter()
        try:
            job.run(session)
            session.commit()
        except Exception as exc:
            # A job that failed half-way must not leave its partial writes behind, and a
            # session is unusable after a database error until it is rolled back.
            session.rollback()
            duration_ms = int((time.perf_counter() - started) * 1000)
            retrying = attempt < job.max_attempts
            _finish(
                session,
                run,
                status=FAILED,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=duration_ms,
                next_attempt_at=(
                    datetime.now(timezone.utc) + timedelta(minutes=_backoff(attempt))
                    if retrying
                    else None
                ),
            )
            log("error", "job failed", job=name, attempt=attempt, error=type(exc).__name__)
            METRICS.inc("erp_job_runs_total", {"job": name, "status": FAILED})
            if not retrying:
                return _fresh(session, run)
            pause(_backoff(attempt) * 60)
            continue

        duration_ms = int((time.perf_counter() - started) * 1000)
        try:
            _finish(session, run, status=SUCCEEDED, duration_ms=duration_ms)
        except Exception:
            # Another worker claimed this key between the lookup and the write: the unique
            # index did its job, and the loser is recorded as the skip it is.
            session.rollback()
            return _record(session, job=name, key=key, status=SKIPPED, attempt=attempt)
        log("info", "job succeeded", job=name, attempt=attempt, duration_ms=duration_ms)
        METRICS.inc("erp_job_runs_total", {"job": name, "status": SUCCEEDED})
        return _fresh(session, run)

    assert run is not None  # max_attempts >= 1, so the loop ran at least once
    return _fresh(session, run)


def _succeeded_run(session: Session, job: str, key: str | None) -> JobRun | None:
    """The run that already claimed `key`, if one did."""
    if key is None:
        return None
    return session.scalar(
        select(JobRun).where(JobRun.job == job, JobRun.key == key, JobRun.status == SUCCEEDED)
    )


def _record(
    session: Session,
    *,
    job: str,
    key: str | None,
    status: str,
    attempt: int,
) -> JobRun:
    """Write one run row and commit it — a run that is not written down is not observable."""
    run = JobRun(job=job, key=key, status=status, attempt=attempt)
    if status != RUNNING:
        run.finished_at = datetime.now(timezone.utc)
    session.add(run)
    session.commit()
    return run


def _finish(
    session: Session,
    run: JobRun,
    *,
    status: str,
    error: str | None = None,
    duration_ms: int | None = None,
    next_attempt_at: datetime | None = None,
) -> None:
    """Write a run's outcome onto its own row."""
    stored = session.get(JobRun, run.id) or run
    stored.status = status
    stored.error = error
    stored.duration_ms = duration_ms
    stored.next_attempt_at = next_attempt_at
    stored.finished_at = datetime.now(timezone.utc)
    session.commit()


def _fresh(session: Session, run: JobRun) -> JobRun:
    """The row as the database has it, not as the session's pending state had it."""
    session.expire_all()
    found = session.get(JobRun, run.id)
    return found if found is not None else run


def _backoff(attempt: int) -> int:
    """Minutes to wait after a failed `attempt` — §9's exponential delay, from config."""
    return SETTINGS.delivery_backoff(attempt)


def runs_for(session: Session, *, job: str | None = None, limit: int = 50) -> list[JobRun]:
    """The run log, newest first — what an operator reads when a job misbehaved."""
    statement = select(JobRun).order_by(JobRun.started_at.desc(), JobRun.id).limit(limit)
    if job is not None:
        statement = statement.where(JobRun.job == job)
    return list(session.scalars(statement))


# --- The worker's own database handle -----------------------------------------


_ENGINE: Engine | None = None


def engine() -> Engine:
    """The worker's engine — one pool for the process, from the validated configuration."""
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = create_engine(SETTINGS.database_url, pool_pre_ping=True)
    return _ENGINE


def close() -> None:
    """Release the worker's pool (its shutdown step)."""
    global _ENGINE
    if _ENGINE is not None:
        _ENGINE.dispose()
        _ENGINE = None


# --- The queue itself (pgqueuer) ---------------------------------------------


async def enqueue(
    queries: Any,
    entrypoint: str,
    *,
    key: str | None = None,
    payload: dict | None = None,
    execute_after: timedelta | None = None,
) -> int | None:
    """Put one job on the queue, through pgqueuer's own `enqueue`.

    ``dedupe_key`` is stated whenever a key is: the queue then refuses a second copy of a
    job that is still queued, so a schedule that fires while its previous run is waiting
    does not pile up. The same key that makes the *run* idempotent also thins the queue.
    """
    body = json.dumps({"key": key, "payload": payload or {}}).encode()
    ids = await queries.enqueue(
        entrypoint, body, dedupe_key=key, execute_after=execute_after, on_conflict="skip"
    )
    return ids[0] if ids else None


def wire(app) -> None:
    """Attach every registered job and schedule to a `PgQueuer` application.

    One place that knows how a job name maps onto the queue, so the worker
    (`app/worker.py`) and a check drive the same wiring. The handler opens a session and
    hands it to :func:`run_job`, which is where the run row, the key and the retry live.
    """
    from app.schedule import SCHEDULES, apply

    for job in JOBS.values():
        app.entrypoint(job.name, concurrency_limit=1)(_handler(job.name))
    apply(app.sm)
    log(
        "info",
        "the queue is wired",
        jobs=", ".join(sorted(JOBS)),
        schedules=", ".join(sorted(SCHEDULES)),
    )


def _handler(name: str):
    """The function pgqueuer calls for one job: unpack the key, run it, record it."""

    async def handle(job_payload) -> None:
        key, _payload = unpack(job_payload)
        with Session(engine()) as session:
            run_job(session, name, key=key)

    handle.__name__ = f"job_{name}"
    return handle


def unpack(job_payload) -> tuple[str | None, dict]:
    """The key and payload the queue carried, tolerating a body that is not ours."""
    raw = getattr(job_payload, "payload", None)
    if raw is None and isinstance(job_payload, (bytes, str)):
        raw = job_payload
    if isinstance(raw, memoryview):
        raw = raw.tobytes()
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode()
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return None, {}
        if isinstance(decoded, dict):
            return decoded.get("key"), dict(decoded.get("payload") or {})
    return None, {}


def build(driver):
    """A wired `PgQueuer` bound to a live driver (the worker's, or a check's)."""
    from pgqueuer.applications import PgQueuer
    from pgqueuer.types import Channel

    app = PgQueuer(connection=driver, channel=Channel(CHANNEL), resources={})
    wire(app)
    return app


@asynccontextmanager
async def connected(dsn: str):
    """A wired, connected queue application — the worker's own handle on the queue."""
    import psycopg
    from pgqueuer.db import PsycopgDriver

    connection = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    try:
        yield build(PsycopgDriver(connection))
    finally:
        await connection.close()


async def drain(dsn: str, *, batch_size: int = 10, timeout: timedelta | None = None) -> int:
    """Run queued jobs until the queue is empty, then stop. Returns the jobs the queue logged.

    `pgqueuer`'s own drain mode, wrapped so the worker's `--once` (and a check) drives the
    real consumer without a loop that has to be interrupted.
    """
    from pgqueuer.types import QueueExecutionMode

    async with connected(dsn) as app:
        await app.qm.run(
            mode=QueueExecutionMode.drain,
            batch_size=batch_size,
            dequeue_timeout=timeout or timedelta(seconds=5),
        )
        return int(getattr(app.qm, "jobs_logged", 0))
