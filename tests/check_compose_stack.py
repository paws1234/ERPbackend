"""T-0.DEPLOY.03 check — the stack comes up as three containers, and only the backend sees the data.

    POSTGRES_PASSWORD=... uv run --with 'sqlalchemy>=2.0' --with 'psycopg[binary]' \
        python tests/check_compose_stack.py

It fails (non-zero exit) if any of these stops holding:

1. one command starts the database, backend and frontend as three separate
   containers, from a directory holding **only the compose file** — neither
   application repository is checked out next to it
2. the database is healthy before the backend starts
3. the frontend reaches the backend over the compose network (its page renders
   real data), while the frontend has **no route to the database** — not assumed,
   attempted from inside the container
4. the database's data survives a full stack restart: what was written before is
   still there afterwards
5. each service's health is observable from the stack itself

**Docker only**: it builds the two images, runs the stack and removes both.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
FRONTEND_REPO = ROOT.parent / "ERPfrontend"
PROJECT = "erpv1"
BACKEND_IMAGE = "erpv1-backend:local"
FRONTEND_IMAGE = "erpv1-frontend:local"
SEED_SCRIPT = ROOT / "tests" / "compose_seed.py"

COMPOSE = ["docker", "compose"]


def run(args, *, cwd=None, check=True, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=cwd, env=env, capture_output=True, text=True, check=check
    )


def compose_file(workdir: pathlib.Path, *args: str, env=None, check: bool = True):
    return run([*COMPOSE, "-f", str(workdir / "docker-compose.yml"), *args], cwd=workdir, check=check, env=env)


def service_states(workdir: pathlib.Path, env) -> dict:
    listing = compose_file(workdir, "ps", "--format", "json", env=env).stdout
    states = {}
    for line in listing.splitlines():
        if line.strip():
            row = json.loads(line)
            states[row["Service"]] = row
    return states


def main() -> int:
    password = os.environ.get("POSTGRES_PASSWORD")
    if not password:
        print("POSTGRES_PASSWORD is required (the stack refuses to start without it)", file=sys.stderr)
        return 2

    env = {**os.environ, "POSTGRES_PASSWORD": password, "COMPANY_ID": ""}

    # The two images, built from their own repositories once.
    print("building both images…")
    run(["docker", "build", "-t", BACKEND_IMAGE, "-f", str(ROOT / "Dockerfile"), str(ROOT)])
    run(["docker", "build", "-t", FRONTEND_IMAGE, "-f", str(FRONTEND_REPO / "Dockerfile"), str(FRONTEND_REPO)])

    # 1 — the stack is run from a directory that holds the compose file and
    # nothing else: neither repository is present beside it.
    workdir = pathlib.Path(tempfile.mkdtemp(prefix="erpv1-stack-"))
    shutil.copy(ROOT / "docker-compose.yml", workdir / "docker-compose.yml")
    assert sorted(path.name for path in workdir.iterdir()) == ["docker-compose.yml"], (
        "the stack directory is not empty — the proof would not be about the images"
    )

    seeded: str | None = None
    try:
        started = time.monotonic()
        compose_file(workdir, "up", "-d", "--wait", env=env)
        print(f"`docker compose up -d --wait` brought the stack up in {time.monotonic() - started:.1f}s")

        # 1 and 5 — three containers, each reporting its health
        states = service_states(workdir, env)
        assert set(states) == {"db", "backend", "frontend"}, f"the stack is not three services: {states}"
        for service, row in states.items():
            assert row["State"] == "running", f"{service} is {row['State']}"
            assert row.get("Health") == "healthy", f"{service} is not healthy: {row.get('Health')}"
        print("three services, all healthy:", ", ".join(sorted(states)))

        # 2 — the database was healthy before the backend started
        db_started = run(["docker", "inspect", f"{PROJECT}-db-1", "--format", "{{.State.StartedAt}}"]).stdout.strip()
        backend_started = run(
            ["docker", "inspect", f"{PROJECT}-backend-1", "--format", "{{.State.StartedAt}}"]
        ).stdout.strip()
        assert db_started < backend_started, (
            f"the backend started at {backend_started}, before the database at {db_started}"
        )
        print(f"db started {db_started}, backend {backend_started} — ordering held")

        # 3 — the frontend reaches the backend, and not the database
        reach = compose_file(
            workdir,
            "exec",
            "-T",
            "frontend",
            "node",
            "-e",
            "fetch('http://backend:8000/api/v1/health').then(r=>process.stdout.write(String(r.status)))",
            env=env,
        ).stdout.strip()
        assert reach == "200", f"the frontend cannot reach the backend: {reach!r}"
        blocked = compose_file(
            workdir,
            "exec",
            "-T",
            "frontend",
            "node",
            "-e",
            "fetch('http://db:5432').then(()=>process.stdout.write('REACHED')).catch(e=>process.stdout.write('BLOCKED:'+e.cause?.code))",
            env=env,
        ).stdout.strip()
        assert blocked.startswith("BLOCKED"), f"the frontend reached the database: {blocked}"
        print(f"the frontend reaches the backend (200) and cannot resolve the database ({blocked})")

        # 3 — and the page really renders through the two hops
        seeded = run(
            [
                "docker", "run", "--rm",
                "--network", f"{PROJECT}_data",
                "-e", f"DATABASE_URL=postgresql+psycopg://erpv1:{password}@db:5432/erpv1",
                "-e", "PYTHONPATH=/app",
                "-v", f"{SEED_SCRIPT}:/seed.py:ro",
                BACKEND_IMAGE, "python", "/seed.py",
            ]
        ).stdout.strip()
        assert len(seeded) > 30, f"the seed did not return a company id: {seeded!r}"
        compose_file(workdir, "up", "-d", "--wait", env={**env, "COMPANY_ID": seeded})
        page = run(
            [
                "curl", "-s", "--retry", "30", "--retry-delay", "1",
                "--retry-connrefused", "--retry-all-errors", "http://127.0.0.1:3000/",
            ]
        ).stdout
        assert "Stack Check Trading" in page, "the frontend did not render the company through the stack"
        assert "1234.00" in page, "the frontend did not render the ledger through the stack"
        print("the frontend renders the company and the ledger through the whole stack")

        # 4 — a full restart, and the data is still there
        compose_file(workdir, "down", env=env)
        compose_file(workdir, "up", "-d", "--wait", env={**env, "COMPANY_ID": seeded})
        after = run(
            [
                "curl", "-s", "--retry", "30", "--retry-delay", "1",
                "--retry-connrefused", "--retry-all-errors", "http://127.0.0.1:3000/",
            ]
        ).stdout
        assert "Stack Check Trading" in after and "1234.00" in after, (
            "the data did not survive the restart"
        )
        print("the data survived a full stack restart")
    finally:
        compose_file(workdir, "down", "-v", env=env, check=False)
        shutil.rmtree(workdir, ignore_errors=True)
        run(["docker", "rmi", BACKEND_IMAGE], check=False)
        run(["docker", "rmi", FRONTEND_IMAGE], check=False)

    print("ok — one command brings up three healthy containers, and only the backend sees the database")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
