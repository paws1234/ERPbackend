"""T-8.WORK.01 — `python -m app.worker`: the container that runs the scheduled work.

**Why this module exists.** `app/queue.py` says what a job is and what a run records;
`app/schedule.py` says what recurs; this is the process that holds them together. It is the
**fourth container** in `docker-compose.yml` — the same image as the backend with a different
command (`python -m app.worker`) — because background work has to survive the web process
being restarted mid-deploy, and because a job that ran inside a request would be a job whose
failure is a `500`.

**What it does, in order.** Read the validated configuration (`app/config.py` — a bad
`DATABASE_URL` stops this process too, naming the variable); configure logging so the log
lines of a job carry the same shape as a request's; register the outbound channels; connect
once (`pgqueuer`'s psycopg driver, autocommit, one connection); then run the queue manager and
the scheduler **together** in one event loop:

* the queue manager holds one worker per registered entrypoint and enforces each entrypoint's
  own concurrency limit in SQL (so two workers cannot run the same job twice at once),
* the scheduler fires each cron expression from `app/schedule.py` and the firing enqueues the
  job (so a firing is a queue job with a run row, not work done in the scheduler's process).

`SIGTERM` (what `docker compose stop` sends) sets the shared shutdown event, both loops
stop picking up new work, and the process exits after the jobs in flight finish — the same
`SIGTERM`-as-PID-1 behaviour `Dockerfile` gives uvicorn.

**Flags.**

* `--once` — drain the queue and exit instead of running continuously. What a smoke test and
  a CI job use, and what an operator runs to work through a backlog without leaving a
  worker running.
* `--check` — connect, report which jobs and schedules are wired, and exit non-zero if the
  queue's own tables are missing. A container that starts and immediately fails is worse than
  one that refuses to start, and this is the cheap way to tell the two apart.

**Ceilings, recorded rather than discovered later.**

* One process, one queue manager, `WORKER_CONCURRENCY` as the queue manager's own cap —
  pgqueuer already bounds per-entrypoint concurrency in SQL. Scaling out is more containers,
  which compose can do with `--scale worker=N`; there is no leader election because there is
  nothing that needs to be a singleton.
* No metrics *endpoint* on the worker: `app/queue.py` increments the same in-process registry
  the web process serves, but a worker has no HTTP server. Upgrade path: expose the registry
  on a small port when the host's scrape config can reach it, or push into the same
  Prometheus registry as the web process.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from datetime import timedelta

from app import config, delivery, observability, queue, schedule
from app.observability import log

# How long the queue manager waits for work before checking whether it should stop. Short
# enough that `--once` returns promptly on an empty queue, long enough not to poll hard.
DEQUEUE_TIMEOUT = timedelta(seconds=5)
# The most jobs one dequeue pass may take. pgqueuer requires its concurrency cap to be at
# least twice the batch (`max_concurrent_tasks >= 2 * batch_size`), so the batch is derived
# from WORKER_CONCURRENCY rather than fixed: a worker told to run 4 jobs at once takes them
# 2 at a time, and one told to run 64 takes 10.
BATCH_CEILING = 10


def _budget() -> tuple[int, int]:
    """``(concurrency, batch_size)`` as pgqueuer will accept them (§9's WORKER_CONCURRENCY)."""
    concurrency = max(2, config.SETTINGS.worker_concurrency)
    return concurrency, max(1, min(BATCH_CEILING, concurrency // 2))


def _wired() -> tuple[list[str], list[str]]:
    """The jobs and schedules this process carries — the report `--check` prints."""
    return sorted(queue.JOBS), sorted(schedule.SCHEDULES)


async def run(*, once: bool = False) -> int:
    """Run the worker (or drain the queue once) and return a process exit code."""
    jobs, schedules = _wired()
    log(
        "info",
        "the worker is starting",
        jobs=", ".join(jobs),
        schedules=", ".join(schedules),
        concurrency=config.SETTINGS.worker_concurrency,
    )
    if not jobs:
        log("error", "no job is registered, so this worker would do nothing")
        return 1

    from pgqueuer.db import PsycopgDriver
    from pgqueuer.types import QueueExecutionMode

    import psycopg

    concurrency, batch_size = _budget()
    connection = await psycopg.AsyncConnection.connect(
        config.SETTINGS.dsn, autocommit=True
    )
    try:
        app = queue.build(PsycopgDriver(connection))
        # The scheduler's crontab functions reach the queue through the context's
        # resources: they enqueue a job, they do not do the work themselves.
        app.resources["queries"] = app.queries
        if once:
            await app.qm.run(
                mode=QueueExecutionMode.drain,
                batch_size=batch_size,
                dequeue_timeout=DEQUEUE_TIMEOUT,
                max_concurrent_tasks=concurrency,
            )
            log("info", "the queue is drained, stopping")
            return 0

        _on_signal(app)
        await asyncio.gather(
            app.qm.run(
                mode=QueueExecutionMode.continuous,
                batch_size=batch_size,
                dequeue_timeout=DEQUEUE_TIMEOUT,
                max_concurrent_tasks=concurrency,
            ),
            app.sm.run(),
        )
        log("info", "the worker stopped")
        return 0
    finally:
        queue.close()
        await connection.close()


def _on_signal(app) -> None:
    """Stop the loops on SIGTERM/SIGINT — what the container runtime sends."""

    def request_stop(*_args) -> None:
        log("info", "shutdown requested; finishing the jobs in flight")
        app.shutdown.set()

    loop = asyncio.get_running_loop()
    for name in ("SIGTERM", "SIGINT"):
        try:
            loop.add_signal_handler(getattr(signal, name), request_stop)
        except (NotImplementedError, ValueError):  # pragma: no cover - not a POSIX loop
            signal.signal(getattr(signal, name), lambda *_a: request_stop())


async def check() -> int:
    """Report what is wired and whether the queue's tables are there; non-zero if not."""
    jobs, schedules = _wired()
    print(f"jobs: {', '.join(jobs) or '(none)'}")
    print(f"schedules: {', '.join(schedules) or '(none)'}")
    from sqlalchemy import inspect

    present = set(inspect(queue.engine()).get_table_names())
    missing = [name for name in queue.QUEUE_TABLES if name not in present]
    if missing:
        print(
            f"the queue's tables are missing ({', '.join(missing)}): run"
            " `alembic upgrade head` (the migration environment installs them)",
            file=sys.stderr,
        )
        return 1
    for job in queue.describe():
        print(f"  {job['job']}: {job['description']} (up to {job['attempts']} attempt(s))")
    for entry in schedule.describe():
        print(f"  {entry['job']}: {entry['cron']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.worker",
        description="Run the platform's background work: the queue's jobs and their schedules.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="drain the queue and exit instead of running continuously",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report the wired jobs and schedules and the queue's tables, then exit",
    )
    args = parser.parse_args(argv)

    observability.configure(config.SETTINGS)
    delivery.register_configured()
    if args.check:
        return asyncio.run(check())
    try:
        return asyncio.run(run(once=args.once))
    except KeyboardInterrupt:  # pragma: no cover - an operator's Ctrl-C
        return 0


if __name__ == "__main__":  # pragma: no cover - the entry point itself
    raise SystemExit(main())
