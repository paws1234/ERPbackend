"""T-8.WORK.02 check — every declared schedule fires, a duplicate run is safe and visible as
a duplicate, and a failing job retries with its attempt and its error on the record.

    DATABASE_URL=postgresql+psycopg://… python tests/check_schedules.py

`app/schedule.py` declares what recurs; `app/queue.py`'s `run_job` is the run record and the
retry contract. T-8.WORK.01's check drives the queue's *plumbing* — a job enqueued, the
consumer, exactly one effect. This one drives the **clock**: the real scheduler and the real
consumer are started together, exactly as `app/worker.py` starts them, with each declared
schedule forced due — a cron expression coming round is the one thing a check cannot wait for
(`schedule.make_due` exists for that) — so what is observed is the production path from a due
row to a run row rather than a loop this check wrote.

It fails (non-zero exit) if any of these stops holding:

1. **every declared schedule has fired** — 100 % of `SCHEDULES`, each leaving a run row whose
   key is the firing instant the scheduler stamped. Declared and observed are compared as
   sets, so a schedule added without a proof is red rather than absorbed
2. **a job run twice produces one effect, and the second run is visible as such** — the second
   is recorded `skipped`, and it demonstrably did nothing: a session that lapsed between the
   two runs is still there afterwards
3. **a failing job retries** — each attempt is its own row with its attempt number and its
   error, the delay before the next attempt is recorded on the failed row, and the retry
   reaches success
4. **100 % of runs are recorded with an outcome** — no run is left `running`: every row has a
   terminal status and a `finished_at`

**Scratch database only**: it drops and recreates the schema, as the other checks do.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import identity, queue, schedule  # noqa: E402
from app.company import Company  # noqa: E402
from app.config import SETTINGS  # noqa: E402
from app.identity import UserSession  # noqa: E402
from app.queue import JobRun  # noqa: E402

ALEMBIC = [sys.executable, "-m", "alembic", "-c", str(ROOT / "alembic.ini")]
PURGE = "purge_expired_sessions"
# The job this check registers to drive the retry contract: no real job can be made to fail on
# demand, and "a failing job retries" has to be observed rather than asserted.
FLAKY = "check_flaky_job"
FLAKY_ATTEMPTS = 3
# How long a firing may take to reach a run row before the check calls it a failure. Generous:
# it is a scheduler poll plus one queue pass, not a wait for anything external.
FIRING_TIMEOUT_SECONDS = 60


def child_env(url: str) -> dict[str, str]:
    """This process's environment pointed at `url`, without an inherited `PYTHONPATH`."""
    env = {name: value for name, value in os.environ.items() if name != "PYTHONPATH"}
    env["DATABASE_URL"] = url
    return env


def migrate(url: str) -> None:
    """The operator's own command, in its own process — the schema the clock runs against."""
    done = subprocess.run(
        [*ALEMBIC, "upgrade", "head"], cwd=ROOT, env=child_env(url), capture_output=True, text=True
    )
    assert done.returncode == 0, (
        f"alembic upgrade head failed on an empty schema:\n{done.stdout}{done.stderr}"
    )


def a_lapsed_session(engine, *, company_code: str, expired: bool) -> uuid.UUID:
    """A company with one session, lapsed when `expired` — the purge's own subject.

    Returns the session's id. The company is created on first use so a second call adds only
    a session, which is what the duplicate case needs: a row that appears *between* two runs
    and must survive the second.
    """
    with Session(engine) as session:
        company = session.scalar(select(Company).where(Company.code == company_code))
        if company is None:
            company = Company(
                code=company_code,
                name="The schedules' check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
            session.add(company)
            session.flush()
        user = identity.user_by_login(session, "schedules-check")
        if user is None:
            user = identity.create_user(
                session, login="schedules-check", display_name="The schedules' check",
                companies=[company.id],
            )
        now = datetime.now(timezone.utc)
        row = UserSession(
            token_hash=uuid.uuid4().hex,
            user_id=user.id,
            active_company_id=company.id,
            expires_at=now - timedelta(days=2) if expired else now + timedelta(hours=1),
        )
        session.add(row)
        session.commit()
        return row.id


def terminal_jobs(engine, names: set[str]) -> dict[str, str]:
    """Which of `names` have a run row that reached an outcome, and what it was."""
    with Session(engine) as session:
        rows = session.execute(
            select(JobRun.job, JobRun.status).where(
                JobRun.job.in_(names), JobRun.status != queue.RUNNING
            )
        ).all()
    return {job: status for job, status in rows}


async def fire_every_declared_schedule(engine, dsn: str) -> dict[str, str]:
    """Start the real clock and the real consumer, force every schedule due, and wait.

    This is `app/worker.py`'s own arrangement — the queue manager and the scheduler in one
    event loop, with the resources the scheduler's firing function reads — ended by setting
    the same shutdown event the container's `SIGTERM` handler sets.
    """
    from pgqueuer.types import QueueExecutionMode

    async with queue.connected(dsn) as app:
        app.resources["queries"] = app.queries
        due = await schedule.make_due(app.queries)
        assert set(due) == set(schedule.SCHEDULES), (
            f"the check forced {sorted(due)} due and the module declares"
            f" {sorted(schedule.SCHEDULES)}"
        )
        loops = [
            asyncio.create_task(app.sm.run()),
            asyncio.create_task(
                app.qm.run(
                    mode=QueueExecutionMode.continuous,
                    batch_size=2,
                    dequeue_timeout=timedelta(seconds=1),
                    max_concurrent_tasks=4,
                )
            ),
        ]
        try:
            deadline = time.monotonic() + FIRING_TIMEOUT_SECONDS
            while True:
                observed = await asyncio.to_thread(terminal_jobs, engine, set(due))
                if set(due) <= set(observed):
                    return observed
                if time.monotonic() >= deadline:
                    missing = sorted(set(due) - set(observed))
                    raise AssertionError(
                        f"{missing} was forced due and did not reach a run row in"
                        f" {FIRING_TIMEOUT_SECONDS}s — the schedule is declared and never fires"
                    )
                await asyncio.sleep(0.25)
        finally:
            app.shutdown.set()
            await asyncio.gather(*loops, return_exceptions=True)


def a_job_that_fails_then_succeeds() -> list[int]:
    """Register the flaky job and return the list its attempts are recorded in."""
    attempts: list[int] = []

    def work(session: Session) -> dict:
        attempts.append(len(attempts) + 1)
        if len(attempts) < FLAKY_ATTEMPTS:
            raise RuntimeError(f"the check's own job failed on attempt {len(attempts)}")
        return {"attempts": len(attempts)}

    queue.register_job(
        FLAKY,
        work,
        max_attempts=FLAKY_ATTEMPTS,
        description="the check's own job: fails until its last attempt",
    )
    return attempts


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is required (a scratch Postgres)", file=sys.stderr)
        return 2

    engine = create_engine(url)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    migrate(url)

    # The retry seam `app/queue.py` states for exactly this: a check drives a retry without
    # sleeping through the backoff it proves is recorded. The recorded delay is unaffected —
    # it comes from configuration, not from the pause.
    queue.PAUSE = lambda _seconds: None

    # 1 — every declared schedule, on the real clock, through the real consumer
    observed = asyncio.run(fire_every_declared_schedule(engine, SETTINGS.dsn))
    print(
        f"all {len(observed)} declared schedule(s) fired and ran: "
        + ", ".join(f"{name} ({status})" for name, status in sorted(observed.items()))
    )
    with Session(engine) as session:
        keys = {
            job: session.scalar(
                select(JobRun.key).where(JobRun.job == job, JobRun.status == queue.SUCCEEDED)
            )
            for job in observed
        }
    stampless = [job for job, key in keys.items() if not (key or "").startswith(f"{job}@")]
    assert not stampless, (
        f"{stampless} ran without the firing instant as its key, so the run cannot be traced"
        f" to the firing it answers: {keys}"
    )

    # 2 — a job run twice: one effect, and the second run visible as the skip it is
    lapsed = a_lapsed_session(engine, company_code="SCHEDULES", expired=True)
    with Session(engine) as session:
        first = queue.run_job(session, PURGE, key="check-duplicate")
        assert first.status == queue.SUCCEEDED, f"the first run is {first.status!r}"
        assert session.get(UserSession, lapsed) is None, (
            "the purge did not delete the lapsed session, so there was no effect to duplicate"
        )
        # A row that lapses *between* the two runs: a second run that did anything at all
        # would delete it, so its survival is what proves the skip had no effect.
        between = a_lapsed_session(engine, company_code="SCHEDULES", expired=True)
        second = queue.run_job(session, PURGE, key="check-duplicate")
        assert second.status == queue.SKIPPED, (
            f"the second run under the same key is {second.status!r}, not skipped"
        )
        assert session.get(UserSession, between) is not None, (
            "the skipped run did something — the session that lapsed between the two runs is gone"
        )
        duplicated = queue.runs_for(session, job=PURGE)
        same_key = [run for run in duplicated if run.key == "check-duplicate"]
        assert sorted(run.status for run in same_key) == [queue.SKIPPED, queue.SUCCEEDED], (
            f"the two runs are not one success and one visible skip:"
            f" {[(run.status, run.attempt) for run in same_key]}"
        )
    print(
        "the same job under one key ran once and was skipped once — the skip had no effect:"
        " the session that lapsed between the runs survived"
    )

    # 3 — a failing job retries, and every attempt is on the record
    attempts = a_job_that_fails_then_succeeds()
    with Session(engine) as session:
        final = queue.run_job(session, FLAKY)
        rows = sorted(
            (run for run in queue.runs_for(session, job=FLAKY)), key=lambda run: run.attempt
        )
    assert attempts == [1, 2, 3], f"the job was run {attempts!r} times, not up to its limit"
    assert final.status == queue.SUCCEEDED, f"the retry never reached success: {final.status!r}"
    assert [run.status for run in rows] == [queue.FAILED, queue.FAILED, queue.SUCCEEDED], (
        f"the attempts are not two failures then a success:"
        f" {[(run.attempt, run.status) for run in rows]}"
    )
    for run in rows[:2]:
        assert run.error and "RuntimeError" in run.error, (
            f"attempt {run.attempt} failed without its error on the row: {run.error!r}"
        )
        assert run.next_attempt_at is not None, (
            f"attempt {run.attempt} is a failure with no next attempt due, so the retry is"
            " implied rather than recorded"
        )
    assert rows[-1].next_attempt_at is None and rows[-1].error is None, (
        "the successful attempt carries the previous attempt's failure state"
    )
    print(
        f"a job that fails {FLAKY_ATTEMPTS - 1} time(s) retried {len(rows)} time(s): each attempt"
        " its own row, with the error and the instant the next one was due"
    )

    # 4 — every run this check produced reached an outcome
    with Session(engine) as session:
        every = list(session.scalars(select(JobRun)))
    running = [run.job for run in every if run.status == queue.RUNNING]
    assert not running, f"a run was left without an outcome: {running}"
    unfinished = [run.job for run in every if run.finished_at is None]
    assert not unfinished, f"a run reached an outcome with no finish instant: {unfinished}"
    print(f"all {len(every)} run(s) recorded with an outcome and a finish instant")

    engine.dispose()
    print("ok — every declared schedule fires, a duplicate is safe and visible, and a job retries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
