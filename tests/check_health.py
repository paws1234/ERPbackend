"""T-8.OBS.02 check — liveness never fails, and readiness answers `503` on a deadline when the
database stops answering, then `200` when it comes back.

    DATABASE_URL=postgresql+psycopg://… python tests/check_health.py

One route used to answer both questions, so a probe that failed for a dependency reason — a
database that had gone away — was indistinguishable from a process that needed restarting, and
a deployment could only answer by doing the wrong thing to one of them. This check drives the
split and the deadline the readiness half promises.

It fails (non-zero exit) if any of these stops holding:

1. **liveness is `200` throughout** — before the outage, *during* it and after it, because a
   restart cannot fix a dependency and a probe that fails for one is a crash loop
2. **readiness is `200` while the database answers and the schema is at head**, and the body
   names the checks it made
3. **readiness is `503` when the database stops, within `READINESS_TIMEOUT_SECONDS`** — driven
   both ways the database can "stop": the address refusing (nothing listening) and, which is
   the case the deadline exists for, a connection that is **accepted and then never answered**
4. **readiness is `200` again when it returns** — the same address answering again, not a
   second database
5. **`/api/v1/health` and `/api/v1/ready` are the only health routes, and the served contract
   names both**

**The outage is this check's own.** The application is pointed at a small TCP door in front of
Postgres, so this check can close it (nothing listening), hold it open without answering (a
database that accepts and hangs) and put it back on the same port. No privilege is needed, no
container is stopped, and no other check on the same scratch server is affected.

**Scratch database only**: it drops and recreates the schema.
"""

from __future__ import annotations

import os
import select
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# The real database, as the pipeline passes it. It has to be read *before* the application is
# imported, because the application reads its configuration — and therefore `DATABASE_URL` —
# once, at import.
REAL_URL = os.environ.get("DATABASE_URL", "")
# Route names that answer "is this alive / is it ready". Nothing else may be a health route.
# `status` is deliberately not here: this API has domain routes that end in it
# (`/companies/{code}/status`), and a health check that matched those would be about names
# rather than about the probe surface.
HEALTH_NAMES = ("health", "healthz", "live", "liveness", "livez", "ping", "ready", "readyz",
                "readiness")
# What the deadline may overshoot by before this check calls it a failure: the answer is timed
# on the far side of a socket, so the assertion is "inside the deadline plus a little", not
# "inside the deadline to the millisecond".
SLACK_SECONDS = 2.5


class Door:
    """A TCP door in front of the database, so this check can make it stop answering.

    ``mode`` is what the door does with a connection: ``open`` forwards it (packets are relayed
    both ways), ``hang`` accepts it and never answers — the case the readiness deadline exists
    for — and a closed door is simply nothing listening, which is what "the database is
    stopped" looks like to a client. :meth:`listen` binds the port that was bound before, so
    "it came back" is the same address answering rather than a second database.
    """

    def __init__(self, host: str, port: int) -> None:
        self.target = (host, port)
        self.port = 0
        self.mode = "open"
        self._server: socket.socket | None = None
        self._generation = 0
        self._held: list[socket.socket] = []

    def listen(self) -> int:
        self._generation += 1
        generation = self._generation
        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", self.port))
        self.port = server.getsockname()[1]
        server.listen(64)
        server.settimeout(0.2)
        self._server = server
        threading.Thread(target=self._accept, args=(server, generation), daemon=True).start()
        return self.port

    def close(self) -> None:
        # Bumping the generation is what stops the accept loop that owns this socket; the
        # socket then goes, so the port has nothing listening on it at all.
        self._generation += 1
        server, self._server = self._server, None
        if server is not None:
            server.close()

    def _accept(self, server: socket.socket, generation: int) -> None:
        while self._generation == generation:
            try:
                client, _ = server.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            if self.mode == "hang":
                self._held.append(client)  # accepted, deliberately never answered
                continue
            threading.Thread(
                target=self._relay, args=(client, generation), daemon=True
            ).start()

    def _relay(self, client: socket.socket, generation: int) -> None:
        try:
            backend = socket.create_connection(self.target, timeout=5)
        except OSError:
            client.close()
            return
        try:
            while self._generation == generation:
                readable, _, _ = select.select([client, backend], [], [], 0.2)
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    (backend if source is client else client).sendall(data)
        except OSError:
            return
        finally:
            client.close()
            backend.close()


def child_env(url: str) -> dict[str, str]:
    """This process's environment pointed at `url`, without an inherited `PYTHONPATH`."""
    env = {name: value for name, value in os.environ.items() if name != "PYTHONPATH"}
    env["DATABASE_URL"] = url
    return env


def timed_ready(client, base: str) -> tuple[int, float, dict]:
    """One readiness call, timed — the deadline is half of what this route promises."""
    started = time.monotonic()
    answer = client.get(f"{base}/ready")
    return answer.status_code, time.monotonic() - started, answer.json()


def main() -> int:
    if not REAL_URL:
        print("DATABASE_URL is required (a scratch Postgres)", file=sys.stderr)
        return 2

    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    real = make_url(REAL_URL)
    engine = create_engine(REAL_URL)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")

    door = Door(real.host or "127.0.0.1", real.port or 5432)
    door.listen()

    # The application is pointed at the door rather than at Postgres, and only then imported —
    # the configuration is read once, at import, so this is the one moment it can be told.
    os.environ["DATABASE_URL"] = real.set(
        host="127.0.0.1", port=door.port
    ).render_as_string(hide_password=False)

    from fastapi.testclient import TestClient

    from app import config
    from app.api import BASE, app

    timeout = config.SETTINGS.readiness_timeout_seconds
    client = TestClient(app, raise_server_exceptions=False)

    migrated = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ROOT / "alembic.ini"), "upgrade", "head"],
        cwd=ROOT,
        env=child_env(REAL_URL),
        capture_output=True,
        text=True,
    )
    assert migrated.returncode == 0, (
        f"alembic upgrade head failed against the real database:\n"
        f"{migrated.stdout}{migrated.stderr}"
    )

    # 2 and 5 — the routes are the two the split promises, and the contract names them
    paths = sorted({route.path for route in app.routes if getattr(route, "path", None)})
    healthish = [path for path in paths if path.rsplit("/", 1)[-1] in HEALTH_NAMES]
    assert healthish == [f"{BASE}/health", f"{BASE}/ready"], (
        f"health is answered by {healthish} — the split is exactly two routes"
    )
    contract = client.get(f"{BASE}/openapi.json").json()["paths"]
    assert f"{BASE}/health" in contract and f"{BASE}/ready" in contract, (
        f"the served contract does not name both health routes: {sorted(contract)[:8]}…"
    )
    print(f"health is exactly {healthish[0]} and {healthish[1]}, and the contract names both")

    # 1 and 2 — the database answers and the schema is at head
    assert client.get(f"{BASE}/health").status_code == 200, "liveness was not 200 to begin with"
    status, took, body = timed_ready(client, BASE)
    assert status == 200, f"readiness is {status} against a migrated database: {body}"
    assert [check["name"] for check in body["checks"]] == ["database", "migrations"], (
        f"readiness does not name what it checked: {body['checks']}"
    )
    assert all(check["ok"] for check in body["checks"]), body["checks"]
    print(f"readiness 200 in {took * 1000:.0f} ms, naming {[c['name'] for c in body['checks']]}")

    # 3 — the address refuses: nothing is listening
    door.close()
    refused_status, refused_took, refused_body = timed_ready(client, BASE)
    assert refused_status == 503, f"readiness is {refused_status} with nothing listening"
    assert refused_took <= timeout + SLACK_SECONDS, (
        f"readiness took {refused_took:.1f}s with the database gone, and its deadline is {timeout}s"
    )
    assert client.get(f"{BASE}/health").status_code == 200, (
        "liveness failed while the database was down — it must answer for a reason a restart fixes"
    )
    print(
        f"the database refused: readiness 503 in {refused_took * 1000:.0f} ms, liveness still 200"
    )

    # 3 — the address accepts and never answers: the case the deadline exists for
    door.mode = "hang"
    door.listen()
    hung_status, hung_took, hung_body = timed_ready(client, BASE)
    assert hung_status == 503, f"readiness is {hung_status} against a database that hangs"
    assert hung_took >= timeout * 0.5, (
        f"readiness gave up in {hung_took:.1f}s, so the connection was not actually hanging and"
        " this case proved nothing about the deadline"
    )
    assert hung_took <= timeout + SLACK_SECONDS, (
        f"readiness took {hung_took:.1f}s against a hanging database, and its deadline is"
        f" {timeout}s — the probe would outlive the caller's patience"
    )
    assert client.get(f"{BASE}/health").status_code == 200, (
        "liveness failed while the database hung"
    )
    print(
        f"the database hung: readiness 503 in {hung_took:.1f}s (deadline {timeout}s),"
        " liveness still 200"
    )

    # 4 — it returns, on the same port
    door.mode = "open"
    status, took, body = timed_ready(client, BASE)
    assert status == 200, f"readiness is {status} after the database came back: {body}"
    assert all(check["ok"] for check in body["checks"]), body["checks"]
    assert client.get(f"{BASE}/health").status_code == 200
    print(f"the database returned: readiness 200 again in {took * 1000:.0f} ms, liveness 200")

    door.close()
    print("ok — liveness never failed, and readiness answered 503 on a deadline and 200 after")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
