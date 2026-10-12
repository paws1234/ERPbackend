"""T-8.WORK.01 — every recurring unit of work, stated once, on pgqueuer's own scheduler.

**Why this module exists.** Background work that nobody owns a schedule for is work that
does not happen: an expired session stayed live until a process restarted, and a delivery
that failed was retried by the request that sent it (or not at all). This module is the one
place that says *what recurs*, and it is deliberately small — a name, a cron expression and
which registered job the firing runs.

**How a firing becomes a run.** `pgqueuer`'s scheduler holds the cron expressions in its own
``pgqueuer_schedules`` table and owns the clock (`apply`); when one is due it calls the
function this module registers, which does one thing: **enqueue the job, stamped with the
firing instant**. Everything after that is `app/queue.py`'s: the queue carries the key, the
worker runs it once, and the run row records it. The split matters — the scheduler is a
clock, not a worker; it must not do the work itself, or the work would have no run row and
would retry in a process that is not supposed to be long-lived.

**The key is the firing instant, not the wall clock.** The job's idempotency key is derived
from the instant the schedule was due (``schedule.next_run``) and travels in the queue's own
dedupe key, so a firing that is redelivered — because the worker that picked it up died —
is recognised as the same firing rather than run a second time.

**Ceilings, recorded rather than discovered later.**

* **The biometric device pull is not scheduled here.** §10 lists it ("the pull is finally
  scheduled (Phase 8)"), and this module is where it belongs — but no module in this tree
  *fetches from a clock*: `app/hr/biometric.py` states that the device's address and
  credentials "are read by whoever schedules the delivery", and the device's own protocol
  is undecided (the neighbouring question to §12 OQ-8, "POS hardware … or whether adapters
  plus simulators are the whole deliverable"). Writing an invented pull here would be a
  protocol nothing implements pretending to be one; the schedule arrives with the device
  adapter, and the two jobs below are real in the meantime. This is a deviation from the
  phase's note, recorded rather than hidden.
* The expressions are **not** configurable (they are code, reviewed with the jobs). Upgrade
  path: a setting per schedule when an installation needs a different cadence — the natural
  one is the retention windows of §12's OQ-9, which are still open.
* No jitter and no catch-up: a worker that was down does not run the firings it missed
  (pgqueuer keeps one next_run per schedule). A job that must catch up reads the state it
  is responsible for rather than assuming it ran on time — both jobs here do exactly that.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.observability import log
from app.queue import enqueue, register_job

# The name of every recurring unit of work and the cron expression it fires on.
#
# Five-field cron, as pgqueuer's scheduler reads it. Both cadences are chosen against what
# the job has to guarantee rather than against a round number: a session purge is only
# interesting within a quarter of an hour of the expiry (sessions are refused by their own
# expiry check the moment they lapse — this only removes the rows), and a delivery retry had
# its next attempt written by the send that failed, so the sweep's cadence is the granularity
# of that delay.
SCHEDULES: dict[str, str] = {
    "purge_expired_sessions": "*/15 * * * *",
    "retry_outbound_deliveries": "*/5 * * * *",
}


def entrypoint_of(name: str) -> str:
    """The queue entrypoint a schedule fires into — the job's own name."""
    if name not in SCHEDULES:
        raise KeyError(f"{name!r} is not a schedule; schedules: {', '.join(sorted(SCHEDULES))}")
    return name


async def fire(schedule, context) -> None:
    """One firing: put the job on the queue under the instant it was due for.

    Registered with ``accepts_context=True``, so pgqueuer hands over the schedule row (and
    with it the instant) and the context carrying the queue's own queries.
    """
    queries = context.resources["queries"]
    due = _instant(schedule.next_run)
    key = f"{schedule.entrypoint}@{due}"
    await enqueue(queries, str(schedule.entrypoint), key=key, payload={"due": due})
    log("info", "schedule fired", job=str(schedule.entrypoint), due=due)


def _instant(value: Any) -> str:
    """A firing instant as a stable string, second precision (the cron's own resolution)."""
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    return str(value)


def apply(scheduler) -> None:
    """Register every schedule on pgqueuer's scheduler.

    Called from `app/queue.py`'s `wire`, so a `PgQueuer` application always carries the
    schedules as well as the entrypoints — a worker with the jobs but not the clock would
    run work nobody schedules.

    ``clean_old`` stays off, deliberately: the stale *expressions* a later release leaves
    behind are never picked up anyway (pgqueuer matches a due row on its entrypoint **and**
    its expression, and this registry only asks for the expressions in this file), while
    turning it on would delete the schedule rows of the entrypoints it is about to run —
    including the one `make_due` writes for a check or a smoke test.
    """
    for name, expression in SCHEDULES.items():
        # The job has to exist: a schedule that fires an entrypoint nobody registered is
        # work that silently does nothing whenever its cron comes round.
        from app.queue import job_for

        job_for(name)
        scheduler.schedule(name, expression, accepts_context=True)(fire)


async def make_due(queries, names: list[str] | None = None) -> list[str]:
    """Mark schedules due **now**, for a check or a smoke test.

    Runtime firing is pgqueuer's own clock; the one thing a check cannot do is wait for a
    cron expression to come round. This creates the schedule row the clock would have been
    waiting for and **forces its `next_run` to this instant** (the insert alone is not
    enough: the row is keyed on ``(entrypoint, expression)`` and a worker that has already
    run once has left a real next-run on it). After that everything is the production path:
    the real scheduler picks the row up, the real queue carries the job, the real consumer
    runs it. Returns the names marked.
    """
    from pgqueuer.domain.models import CronExpressionEntrypoint

    wanted = list(names or SCHEDULES)
    entries: dict[CronExpressionEntrypoint, timedelta] = {}
    for name in wanted:
        entrypoint_of(name)
        entries[CronExpressionEntrypoint(name, SCHEDULES[name])] = timedelta(0)
    await queries.insert_schedule(entries)
    schedule = queries.qbe.qualified.schedules_table
    await queries.driver.execute(
        f"""
        UPDATE {schedule}
           SET next_run = date_trunc('seconds', NOW()), status = 'queued', updated = NOW()
         WHERE entrypoint = ANY($1::text[]) AND expression = ANY($2::text[])
        """,
        [name for name in wanted],
        [SCHEDULES[name] for name in wanted],
    )
    return wanted


def describe() -> list[dict[str, str]]:
    """Every schedule with its cadence — logged at start, and read by OPERATIONS.md."""
    return [{"job": name, "cron": expression} for name, expression in sorted(SCHEDULES.items())]


# --- The jobs themselves ------------------------------------------------------
# Registered here rather than in `app/queue.py` so "what recurs" and "what it does" are in
# one file: a schedule whose job is defined somewhere else is a schedule nobody can read
# without two files open.


def purge_expired_sessions(session) -> dict:
    """Remove the sessions that have lapsed (T-7.AUTH.02's own `purge_expired_sessions`).

    Idempotent by construction: it deletes rows whose expiry has passed, so a second run in
    the same window deletes nothing. Identity is installation-level, so this job runs
    unscoped, exactly as the module that owns those tables does.
    """
    from app.identity import purge_expired_sessions as purge

    removed = purge(session)
    return {"removed": removed}


def retry_outbound_deliveries(session) -> dict:
    """Re-attempt every delivery whose next attempt is due (T-8.DELIVERY.01).

    Runs per company, because the delivery log is company-scoped and row-level security is
    the database's (T-0.CORE.03): the job reads the company list from the unscoped company
    master and scopes a fresh transaction per company, rather than reading around the policy.
    """
    from app.delivery import retry_due

    return retry_due(session)


register_job(
    "purge_expired_sessions",
    purge_expired_sessions,
    description="delete user sessions that have lapsed (identity; unlogged-in)",
)
register_job(
    "retry_outbound_deliveries",
    retry_outbound_deliveries,
    max_attempts=2,
    description="re-attempt outbound deliveries whose next attempt is due",
)
