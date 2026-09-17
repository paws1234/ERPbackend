"""T-0.DEPLOY.01 check — the backend image: builds, starts on env alone, stops on SIGTERM.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/postgres \
        uv run --with 'sqlalchemy>=2.0' --with 'psycopg[binary]' \
        python tests/check_backend_image.py

It fails (non-zero exit) if any of these stops holding:

1. the image builds from this directory's context
2. it runs as a non-root user, and its command is the process itself — no shell
   wrapper, so signals are not swallowed
3. **no secret is inside it**: a `.env` holding a sentinel value sits in the build
   context while it builds, and that value is nowhere in the image
4. it starts against a database given only environment variables, and answers —
   health, the published contract, and a real query through the database
5. it stops on SIGTERM promptly, without waiting to be killed
6. its dependency versions are exactly the pinned lock

**Scratch database only**: the seeding step resets the schema.
**Docker only**: it builds and runs the image; nothing is left behind.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger import posting  # noqa: E402,F401 — every check builds the one schema
from app.security import assign, define_role, grant  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
IMAGE = "erpv1-backend:deploy-check"
CONTAINER = "erpv1-backend-deploy-check"
SENTINEL = "sentinel-secret-not-in-the-image-4f2a"
HEALTH_TIMEOUT = 30


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=check
    )


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is required (a scratch Postgres)", file=sys.stderr)
        return 2

    # 4 — seed the database the container will read
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    Base.metadata.create_all(engine)
    company_id = uuid.uuid4()
    with Session(engine) as session:
        session.add(
            Company(
                id=company_id,
                code="IMAGE-CHECK",
                name="Image check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()
        role = define_role(session, company_id=company_id, code="checker", name="Checker")
        grant(session, role, "company.read")
        assign(session, company_id=company_id, subject="alice", role=role)
        session.commit()

    # 3 — a secret in the build context must not reach the image
    env_file = ROOT / ".env"
    env_file.write_text(f"DEPLOY_CHECK_SECRET={SENTINEL}\n")

    built = False
    try:
        # 1 — the image builds
        start = time.monotonic()
        result = docker("build", "-t", IMAGE, "-f", "Dockerfile", ".")
        print(f"the image builds in {time.monotonic() - start:.1f}s ({len(result.stdout.splitlines())} build steps)")
        built = True

        # 2 — non-root, and the process is the command
        config = json.loads(docker("image", "inspect", IMAGE).stdout)[0]["Config"]
        assert config["User"] not in ("", "root", "0"), f"the image runs as {config['User']!r}"
        assert isinstance(config["Cmd"], list) and config["Cmd"][0] == "uvicorn", config["Cmd"]
        assert not any(shell in config["Cmd"] for shell in ("sh", "bash", "-c")), config["Cmd"]
        print(f"runs as uid {config['User']}, command is exec form: {' '.join(config['Cmd'])}")

        # 3 — no secret inside it
        inside = docker(
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            IMAGE,
            "-c",
            "ls -a /app; grep -r 'sentinel-secret' /app /etc 2>/dev/null | head -3",
        ).stdout
        assert SENTINEL not in inside, "the build context's secret is in the image"
        assert ".env" not in inside.splitlines(), "a .env file is in the image"
        layers = docker("run", "--rm", "--entrypoint", "sh", IMAGE, "-c", "cat /etc/gai.conf | tail -1").stdout
        assert "precedence ::ffff:0:0/96" in layers, "the IPv4 preference is missing"
        print("no secret in the image, and the build context's .env stayed in the context")

        # 6 — the versions are the lock's
        locked = {
            line.split("==")[0].strip(): line.split("==")[1].strip()
            for line in (ROOT / "requirements.lock").read_text().splitlines()
            if "==" in line
        }
        probe = "import importlib.metadata as m, json; print(json.dumps({n: m.version(n) for n in %r}))" % (
            sorted(locked),
        )
        installed = json.loads(
            docker("run", "--rm", "--entrypoint", "python", IMAGE, "-c", probe).stdout
        )
        assert installed == locked, f"versions differ from the lock: {installed}"
        print(f"all {len(locked)} dependency versions are exactly the pinned lock")

        # 4 — it starts on environment variables alone and answers
        docker("rm", "-f", CONTAINER, check=False)
        docker(
            "run",
            "-d",
            "--name",
            CONTAINER,
            "--network",
            "host",
            "-e",
            f"DATABASE_URL={url}",
            IMAGE,
        )
        health = subprocess.run(
            [
                "curl",
                "-s",
                "--retry",
                str(HEALTH_TIMEOUT),
                "--retry-delay",
                "1",
                "--retry-connrefused",
                "-w",
                "\n%{http_code}",
                "http://127.0.0.1:8000/api/v1/health",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        body, _, status = health.stdout.rpartition("\n")
        assert status.strip() == "200", f"the container did not answer health: {health.stdout}"
        assert json.loads(body)["version"] == "v1"
        contract = docker(
            "exec",
            CONTAINER,
            "python",
            "-c",
            "import urllib.request;"
            "print(len(urllib.request.urlopen('http://127.0.0.1:8000/api/v1/openapi.json').read()))",
        ).stdout.strip()
        assert int(contract) > 5000, f"the contract is not served: {contract}"
        scoped = docker(
            "exec",
            CONTAINER,
            "python",
            "-c",
            "import json, urllib.request;"
            f"r = urllib.request.Request('http://127.0.0.1:8000/api/v1/companies/current',"
            f" headers={{'X-Company-Id': '{company_id}', 'X-Actor': 'alice'}});"
            "print(json.load(urllib.request.urlopen(r))['code'])",
        ).stdout.strip()
        assert scoped == "IMAGE-CHECK", f"the container did not read the database: {scoped}"
        print(f"the container answered health, served the contract ({contract} bytes) and read the database")

        # 2 and 5 — the process is PID 1 and stops on SIGTERM
        pid_one = docker("exec", CONTAINER, "cat", "/proc/1/cmdline").stdout.replace("\x00", " ")
        assert "uvicorn" in pid_one, f"PID 1 is not the app: {pid_one!r}"
        start = time.monotonic()
        docker("stop", "-t", "10", CONTAINER)
        elapsed = time.monotonic() - start
        state = json.loads(docker("inspect", CONTAINER).stdout)[0]["State"]
        assert elapsed < 5, f"the stop took {elapsed:.1f}s — SIGTERM was not handled"
        assert state["ExitCode"] == 0, f"the container exited {state['ExitCode']}"
        print(f"PID 1 is {' '.join(pid_one.split()[:3])}; stop took {elapsed:.2f}s with exit code 0")
    finally:
        docker("rm", "-f", CONTAINER, check=False)
        if built:
            docker("rmi", IMAGE, check=False)
        if env_file.exists():
            env_file.unlink()
        engine.dispose()
        assert not env_file.exists(), "the sentinel .env was left behind"

    print("ok — the image builds, carries no secret, runs non-root and stops on SIGTERM")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
