"""T-8.CFG.01 check — the configuration is read once, at start, and a bad one stops the start.

    DATABASE_URL=postgresql+psycopg://… python tests/check_config.py

Settings used to be read where they were used: `app/api.py` reached for `DATABASE_URL` on the
**first request**, so a container with a missing or malformed one came up "healthy" and then
failed every request with a `500` — finding 26 of the 2026-10-10 audit, and principle 6
("configuration is validated at startup and read once"). `app/config.py` is where that is
fixed, and this check is the evidence that it holds rather than the claim that it does.

Every refusal is driven the way it reaches an operator: **in its own process**, importing the
application — which is the start, because `app/config.py` loads `SETTINGS` at import — with an
environment that can be wrong. A refusal this check cannot produce in a fresh process is a
refusal a deployment cannot rely on.

It fails (non-zero exit) if any of these stops holding:

1. a missing or malformed `DATABASE_URL` fails at start, naming the variable, before the
   process logs that it is starting
2. an out-of-range or misspelled value fails at start too — and every problem is named at
   once, not the first and then the next
3. the environment is read for a setting in **one** module (`app/config.py`), so "what is
   configurable" is one file and no module can quietly re-read a value
4. settings are read **once**: an environment that changes under the running process changes
   nothing it uses, and a second read is what would see it

No database is touched: this check drives the *start*, not the application.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402
from app.config import load_settings  # noqa: E402

# The one read of the environment outside `app/config.py`, named with its reason so that a
# second one is red rather than absorbed. It configures nothing about the running platform: it
# is the input of a one-shot command, which `app/bootstrap.py` also states as `--admin-password`.
COMMAND_INPUTS = {"app/bootstrap.py": {"ERP_ADMIN_PASSWORD"}}

ENVIRON = re.compile(r"os\.(?:environ|getenv)")
ENVIRONMENT_NAME = re.compile(r'os\.environ(?:\.get)?[\(\[]\s*"?([A-Z_0-9]+)"?')

# Every variable `app/config.py` declares, read out of the module rather than listed here: a
# setting added tomorrow is covered by this check without editing it.
DECLARED = {
    name
    for name in vars(config)
    if name.isupper() and isinstance(getattr(config, name), str)
}


def start(environ: dict[str, str]) -> subprocess.CompletedProcess:
    """Import the application — the start — in its own process, with `environ`."""
    return subprocess.run(
        [sys.executable, "-c", "import app.api"],
        cwd=ROOT,
        env={**environ, "PYTHONPATH": ""},
        capture_output=True,
        text=True,
    )


def environment_reads() -> dict[str, set[str]]:
    """Every module under `app/` that reads the environment, and the variables it names."""
    found: dict[str, set[str]] = {}
    for path in sorted((ROOT / "app").rglob("*.py")):
        source = path.read_text()
        if ENVIRON.search(source):
            found[str(path.relative_to(ROOT))] = set(ENVIRONMENT_NAME.findall(source))
    return found


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is required (a scratch Postgres, for the valid start)", file=sys.stderr)
        return 2

    # The environment this check states for itself: everything this process has, minus every
    # variable the configuration declares. A child then starts from a configuration that is
    # *known*, rather than from whatever the machine running the check happens to export.
    clean = {name: value for name, value in os.environ.items() if name not in DECLARED}

    # 1 and 2 — the refusals, each naming the variable it is about
    refusals = [
        ("DATABASE_URL is not set", {"DATABASE_URL": None}, "DATABASE_URL"),
        ("DATABASE_URL is not a URL", {"DATABASE_URL": "not-a-url"}, "DATABASE_URL"),
        (
            "DATABASE_URL names another database engine",
            {"DATABASE_URL": "sqlite:///tmp/erpv1-config-check.db"},
            "DATABASE_URL",
        ),
        (
            "DATABASE_URL names no database",
            {"DATABASE_URL": "postgresql+psycopg://postgres@127.0.0.1:5432/"},
            "DATABASE_URL",
        ),
        ("WORKER_CONCURRENCY is out of range", {"WORKER_CONCURRENCY": "0"}, "WORKER_CONCURRENCY"),
        ("LOG_LEVEL is misspelled", {"LOG_LEVEL": "verbose"}, "LOG_LEVEL"),
        ("METRICS_ENABLED is not true or false", {"METRICS_ENABLED": "maybe"}, "METRICS_ENABLED"),
        ("SMTP_PORT is not a number", {"SMTP_PORT": "smtp"}, "SMTP_PORT"),
        (
            "INTEGRATION_ENDPOINTS is not JSON",
            {"INTEGRATION_ENDPOINTS": "{not json"},
            "INTEGRATION_ENDPOINTS",
        ),
        (
            "INTEGRATION_ENDPOINTS is not an object of channels",
            {"INTEGRATION_ENDPOINTS": "[]"},
            "INTEGRATION_ENDPOINTS",
        ),
    ]
    for label, overrides, expected in refusals:
        environ = {**clean, "DATABASE_URL": url}
        for name, value in overrides.items():
            # None means "not set at all", which is a different deployment from an empty value.
            if value is None:
                environ.pop(name, None)
            else:
                environ[name] = value
        refused = start(environ)
        assert refused.returncode != 0, (
            f"{label}: the application started anyway, so a deployment would serve on it"
        )
        assert expected in refused.stderr, (
            f"{label}: the refusal does not name {expected}, so an operator cannot tell what"
            f" to fix:\n{refused.stderr[-600:]}"
        )
        assert "the application is starting" not in refused.stdout, (
            f"{label}: the process logged that it is starting before refusing the"
            " configuration, so a container would look healthy"
        )
        print(f"refused at start, naming {expected}: {label}")

    # 2 — every problem is collected, so an operator with three sees three
    many = start(
        {**clean, "DATABASE_URL": "not-a-url", "LOG_LEVEL": "verbose", "WORKER_CONCURRENCY": "0"}
    )
    assert many.returncode != 0, "three broken settings were accepted"
    for name in ("DATABASE_URL", "LOG_LEVEL", "WORKER_CONCURRENCY"):
        assert name in many.stderr, f"the refusal reported one problem, not all three: {name}"
    print("three broken settings are reported together, not one at a time")

    # the other half of a refusal: a valid configuration does start
    started = start({**clean, "DATABASE_URL": url})
    assert started.returncode == 0, (
        f"the application refused a valid configuration, so the refusals above prove nothing:"
        f"\n{started.stderr[-600:]}"
    )
    assert "the application is starting" in started.stdout, (
        "a valid configuration started without saying so"
    )
    print("a valid configuration starts, and says so")

    # the other half of principle 7: what the start tells the log, and what it must not
    token = "endpoint-token-7c1d"
    logged = start(
        {
            **clean,
            "DATABASE_URL": url,
            "INTEGRATION_ENDPOINTS": json.dumps(
                {"email": {"url": "https://mail.example.invalid", "token": token}}
            ),
        }
    )
    assert logged.returncode == 0, f"a configured boundary stopped the start:\n{logged.stderr[-600:]}"
    assert token not in logged.stdout + logged.stderr, (
        "a credential reached the startup log, which is not a secret store"
    )
    assert "integration_endpoints" in logged.stdout, (
        "the boundary configuration is not reported at all, so an operator cannot see that it"
        " is set"
    )
    print("the start reports the boundary configuration with its credential removed")

    # 3 — the environment is read in one place
    reads = environment_reads()
    assert set(reads) == {"app/config.py", *COMMAND_INPUTS}, (
        "a module outside app/config.py reads the environment for a setting:"
        f" {sorted(set(reads) - {'app/config.py', *COMMAND_INPUTS})}"
    )
    stray = set(reads["app/bootstrap.py"]) - COMMAND_INPUTS["app/bootstrap.py"]
    assert not stray, f"app/bootstrap.py reads the environment for {sorted(stray)}"
    print(
        f"the environment is read in one module (app/config.py, {len(DECLARED)} variables);"
        " the only other read is the install CLI's own input"
    )

    # 4 — read once: changing the environment under the running process changes nothing
    before = config.describe()
    original = {name: os.environ.get(name) for name in ("LOG_LEVEL", "DATABASE_URL")}
    os.environ["LOG_LEVEL"] = "debug"
    os.environ["DATABASE_URL"] = "postgresql+psycopg://elsewhere.invalid:5432/other"
    try:
        assert config.SETTINGS.log_level == before["log_level"] != "debug", (
            "a setting changed under the running process, so two parts of it can disagree"
        )
        assert config.describe() == before, "a value changed under the running process"
        assert load_settings().log_level == "debug", (
            "the environment really did change and the configuration missed it, so the"
            " assertion above proves nothing"
        )
        assert load_settings().database_url != config.SETTINGS.database_url, (
            "the second read saw the old DATABASE_URL, so this check is not testing what it"
            " means to"
        )
    finally:
        for name, value in original.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    print("settings are read once: the environment changed under the process and nothing did")
    print("ok — the configuration is read once, at start, and a misconfigured instance refuses")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
