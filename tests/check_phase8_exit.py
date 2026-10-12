"""T-8.X.GATE — the Phase 8 exit criteria, each held to the check that owns it.

    DATABASE_URL=postgresql+psycopg://… python tests/check_phase8_exit.py

§4b Phase 8 states nine exit criteria. They are restated here as checks: this file adds no feature
and repairs no finding — it runs **the check that owns each criterion** and requires it green, then
prints the criterion with the line that check ends on, so a criterion whose verification has
disappeared, or whose figures have drifted, fails **by name** instead of passing by omission. §6's
four Phase 8 metrics (Schema, Background work, Observability, Delivery) are then re-measured
through the same checks, each with the line that states its figure.

The one half of a criterion that no check in this pipeline owns is *"the worker container is in the
stack"*: it is read out of `docker-compose.yml` here — the service, the command it runs and the
healthcheck that reports what it is wired to — and `tests/check_compose_stack.py` is the check that
brings the stack up for real, excluded from this repository's pipeline by name for the reason it
gives (it needs the frontend repository's image), exactly as `check_phase6_exit.py` handles a
sibling half.

**Scratch database only**: every check it runs drops and recreates the schema.
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"

sys.path.insert(0, str(ROOT))

# Each exit criterion with the check that owns it and the id that check verifies — so a criterion
# whose verification is deleted fails with the file it is missing.
CRITERIA: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "`alembic upgrade head` on an empty database yields a schema whose RLS policies match the"
        " `create_all` schema table by table; `alembic check` reports no drift",
        [("check_migrations.py", "T-8.MIG.01")],
    ),
    (
        "a second `upgrade head` is a no-op, and the install CLI runs on top of migrations,"
        " idempotently",
        [("check_migrated_scoping.py", "T-8.MIG.02")],
    ),
    (
        "the worker container is in the stack, and every schedule in `app/schedule.py` has been"
        " observed to fire in a check",
        [("check_schedules.py", "T-8.WORK.02")],
    ),
    (
        "a job run twice produces one effect; a failing job retries and records the attempt and"
        " its error",
        [("check_worker.py", "T-8.WORK.01"), ("check_schedules.py", "T-8.WORK.02")],
    ),
    (
        "readiness answers `503` within the timeout when the database is stopped and `200` when it"
        " returns; liveness stays `200` throughout",
        [("check_health.py", "T-8.OBS.02")],
    ),
    (
        "a forced unhandled exception answers `500` with `internal_error` and a correlation id, and"
        " no exception class, message, path or SQL appears in the body",
        [("check_error_hygiene.py", "T-8.OBS.03")],
    ),
    (
        "every request produces one structured log line carrying the correlation id, and a"
        " credential never appears in it",
        [("check_request_log.py", "T-8.OBS.01")],
    ),
    (
        "with SMTP configured a delivery reaches the sink; without it, the attempt is recorded —"
        " never a `500`",
        [("check_delivery.py", "T-8.DELIVERY.01")],
    ),
    (
        "a missing or malformed `DATABASE_URL` fails at startup with a named variable",
        [("check_config.py", "T-8.CFG.01")],
    ),
]

# §6's four metrics added with Phases 7–15, each with the check that measures it and the id it
# belongs to. The figure is the line that check ends on, so the gate prints the measurement rather
# than the word "green".
METRICS: list[tuple[str, str, str]] = [
    (
        "Schema — `alembic check` reports 0 drift from empty",
        "check_migrations.py",
        "T-8.MIG.01",
    ),
    (
        "Background work — 100 % of declared schedules observed firing, and no duplicate effect",
        "check_schedules.py",
        "T-8.WORK.02",
    ),
    (
        "Observability — every request logged with a correlation id, and readiness inside the"
        " deadline",
        "check_request_log.py",
        "T-8.OBS.01",
    ),
    (
        "Delivery — ≥ 99 % success over a 100-message run, and every failure with an attempt count",
        "check_delivery.py",
        "T-8.DELIVERY.01",
    ),
]

# The check that drives the stack for real, and why this pipeline does not run it.
STACK_CHECK = "check_compose_stack.py"

_RESULTS: dict[str, str] = {}


def _run(check: str, expected: str) -> str:
    """Run one check in its own process and return the line that states its result.

    A missing check, a check that no longer names the id it verifies, or a check that fails all
    end the gate — the criterion is not carried by a passing suite but by a verification that is
    still there. A check already run for an earlier criterion is not run twice.
    """
    if check in _RESULTS:
        return _RESULTS[check]
    path = TESTS / check
    assert path.exists(), f"the criterion's check is missing: tests/{check}"
    assert expected in path.read_text(), f"tests/{check} no longer names {expected}"
    result = subprocess.run(
        [sys.executable, str(path)], cwd=str(ROOT), env=os.environ, capture_output=True, text=True
    )
    assert result.returncode == 0, (
        f"tests/{check} failed:\n"
        + "\n".join((result.stdout + result.stderr).strip().splitlines()[-25:])
    )
    printed = [line for line in result.stdout.strip().splitlines() if line.strip()]
    _RESULTS[check] = printed[-1] if printed else "(no output)"
    return _RESULTS[check]


def _worker_service() -> None:
    """Criterion 3's stack half: the worker is a service, running the worker's own command.

    Read out of the compose file rather than taken on trust — the criterion is that the container
    is *in the stack*, and a worker nothing starts is a schedule nothing fires.
    """
    text = (ROOT / "docker-compose.yml").read_text()
    block = re.search(r"\n  worker:\n(.*?)(?=\n  [a-z-]+:\n|\nnetworks:)", text, re.DOTALL)
    assert block, "docker-compose.yml declares no `worker` service"
    service = block.group(1)
    assert '"python", "-m", "app.worker"' in service, (
        "the worker service does not run the worker's own command:"
        f"\n{service.strip()}"
    )
    assert '"python", "-m", "app.worker", "--check"' in service, (
        "the worker service has no healthcheck that reports what it is wired to:"
        f"\n{service.strip()}"
    )
    assert "db:" in service, "the worker service does not wait for the database"
    assert (TESTS / STACK_CHECK).exists(), f"the stack's own check is missing: tests/{STACK_CHECK}"


def main() -> int:
    if not os.environ.get("DATABASE_URL"):
        print("DATABASE_URL is required (a scratch Postgres)", file=sys.stderr)
        return 2

    # 1–9 — each criterion, held to its own check
    for criterion, owners in CRITERIA:
        if criterion.startswith("the worker container"):
            _worker_service()
        ends = [(_run(check, expected), check) for check, expected in owners]
        print(criterion)
        for line, check in ends:
            print(f"    {check}: {line[:150]}")
        if criterion.startswith("the worker container"):
            print(
                "    docker-compose.yml: the `worker` service runs `python -m app.worker` with a"
                f" `--check` healthcheck, and tests/{STACK_CHECK} drives the stack for real"
            )

    # §6's four Phase 8 metrics, re-measured through the checks that measure them
    print("\n§6 metrics, re-measured after Phase 8:")
    for metric, check, expected in METRICS:
        print(f"  {metric}")
        print(f"    {check}: {_run(check, expected)[:150]}")

    print(
        "\nPhase 8's nine exit criteria are held, and its four metrics are measured: 0 criteria"
        " carried by a check that is missing, and every figure printed rather than asserted."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
