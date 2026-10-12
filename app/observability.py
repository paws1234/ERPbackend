"""T-8.OBS.01 — logs an operator can read, a correlation id they can follow, counters they can scrape.

**Why this module exists.** Before Phase 8 the platform had no logging at all — not one
``import logging`` under ``app/`` — so an unexpected failure answered `500` with
``f"{type(exc).__name__}: {exc}"`` in the body (leaking internals to whoever asked) and
appeared **nowhere else**: an operator had a stack trace in a client's browser tab and
nothing in the container's output. Plan §4b's fourth principle is "nothing fails
silently": every failure is logged with a correlation id, answers in the one documented
error shape without internals, and increments a counter an operator can see.

Three things, each with a job:

* **One structured line per request** (:class:`RequestContext`). It carries the
  correlation id, the method, the route *template* (never the raw path, so a scraper
  cannot be given unbounded labels by whoever types the URL), the status and how long
  the request took. It is written even when the request failed — a request that
  produced no line at all is the failure mode this exists to prevent — and it carries
  nothing a caller sent: no headers, no cookie, no body, which is why a credential
  cannot reach it.
* **A correlation id** (``X-Request-Id``). A client may send one; when it does not, this
  module mints one. Either way it is echoed on the response and is in every line the
  request produces, so a failure an operator sees in a response can be found in the log.
  A client-supplied id is accepted only in a restricted shape (letters, digits and
  ``._:-``, up to 128 characters), so a log line cannot be forged, split or padded by
  whoever sends it.
* **Counters in Prometheus text format** (:class:`Registry`, ``/api/v1/metrics``). The
  request counter, the request duration summary, the in-flight gauge and the readiness
  gauge. A counter is incremented in the same place the line is logged, so "the log says
  it failed" and "the counter says it failed" cannot disagree.

**Ceilings, recorded rather than discovered later.**

* The registry is this module's own, in-process and unlabelled-by-design: a counter is a
  name, a small fixed label set and a float. There is no histogram, no exemplar, no
  push gateway and no cross-process aggregation (a worker's counters are the worker's).
  Upgrade path: when a scrape needs quantiles or several processes, replace
  :class:`Registry` with ``prometheus-client``'s multiprocess registry — the endpoint and
  the call sites stay as they are, because the endpoint only renders what the registry
  holds.
* ``ERROR_TRACKING_DSN`` is declared (by :mod:`app.config`) and recorded here, but
  **nothing ships an exception to it**: OQ-4 (plan §12) has not decided the sink, and
  inventing one is not this task's. Until it is decided, the exception and its traceback
  go to the process's own log, which is where a container's operator reads them.
* Log lines go to a stream (stdout, as a container wants). Rotation, shipping and
  retention are the host's, not the application's — plan §12 rules out hosted log
  aggregation for now.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
import uuid
from collections.abc import Iterable, Sequence
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from app.config import SETTINGS, Settings

# Where the migration tree lives, relative to the repository root — the same file the
# `alembic` CLI is run with, so the CLI and readiness cannot disagree about "head".
MIGRATIONS_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

# Where the correlation id lives for the duration of one request. A ContextVar rather
# than a thread-local: the value has to be visible to the router, to the logging filter
# and to the exception handlers, and a ContextVar is what follows an async call graph.
REQUEST_ID: ContextVar[str] = ContextVar("erp_request_id", default="")

REQUEST_ID_HEADER = "X-Request-Id"

# Where the middleware parks the correlation id for the one handler that runs *outside*
# it (see `carried_request_id`).
REQUEST_ID_STATE = "erp_request_id"

# What a client may put in X-Request-Id: no whitespace, no control characters, no
# quotes, and short. Anything else is replaced rather than logged (see the module
# docstring: a log line must not be forgeable by whoever sends the header).
REQUEST_ID_SHAPE = re.compile(r"\A[A-Za-z0-9._:-]{1,128}\Z")

REQUESTS = "erp_requests_total"
REQUEST_DURATION = "erp_request_duration_seconds"
IN_FLIGHT = "erp_requests_in_flight"
READY = "erp_ready"
BUILD_INFO = "erp_build_info"

_logging_configured = False


# --- The registry -------------------------------------------------------------


class Registry:
    """The counters and gauges this process reports, in Prometheus text format.

    Deliberately small: a counter, a gauge and a summary are what the platform reports,
    and a name plus a label set identifies a series. Labels are always the same set for
    a name (Prometheus rejects a family whose members disagree), which the call sites
    keep true by construction — a route label is always the route template.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._types: dict[str, str] = {}
        self._help: dict[str, str] = {}
        self._series: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._counts: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}

    def declare(self, name: str, kind: str, help_text: str) -> None:
        """State a metric's type and meaning once, so the exposition can carry them."""
        self._types[name] = kind
        self._help[name] = help_text

    def inc(self, name: str, labels: dict[str, str] | None = None, value: float = 1.0) -> None:
        key = (name, _labels(labels))
        with self._lock:
            self._series[key] = self._series.get(key, 0.0) + value

    def set(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        """State a gauge's current value (a gauge is set, never accumulated)."""
        self._series[(name, _labels(labels))] = value

    def observe(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        """Add one observation to a summary: its total and its count."""
        key = (name, _labels(labels))
        with self._lock:
            self._series[(f"{name}_sum", key[1])] = self._series.get((f"{name}_sum", key[1]), 0.0) + value
            self._counts[key] = self._counts.get(key, 0.0) + 1.0

    def snapshot(self) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
        """Every series as it stands — what a check reads instead of parsing text."""
        with self._lock:
            return dict(self._series)

    def render(self) -> str:
        """The text exposition format, sorted so two scrapes of a quiet process agree."""
        with self._lock:
            series = dict(self._series)
            counts = dict(self._counts)
        lines: list[str] = []
        for name in sorted(self._types):
            lines.append(f"# HELP {name} {self._help[name]}")
            lines.append(f"# TYPE {name} {self._types[name]}")
            if self._types[name] == "summary":
                for (series_name, labels), value in sorted(series.items()):
                    if series_name.startswith(f"{name}_"):
                        lines.append(f"{series_name}{_render_labels(labels)} {_number(value)}")
                for (series_name, labels), value in sorted(counts.items()):
                    if series_name == name:
                        lines.append(f"{name}_count{_render_labels(labels)} {_number(value)}")
                continue
            for (series_name, labels), value in sorted(series.items()):
                if series_name == name:
                    lines.append(f"{name}{_render_labels(labels)} {_number(value)}")
        return "\n".join(lines) + "\n"


def _labels(labels: dict[str, str] | None) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((key, str(value)) for key, value in (labels or {}).items()))


def _render_labels(labels: Sequence[tuple[str, str]]) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{key}="{_escape(value)}"' for key, value in labels)
    return "{" + inner + "}"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _number(value: float) -> str:
    return f"{value:g}"


METRICS = Registry()
METRICS.declare(REQUESTS, "counter", "Requests answered, by method, route template and status.")
METRICS.declare(REQUEST_DURATION, "summary", "Time spent answering requests, in seconds.")
METRICS.declare(IN_FLIGHT, "gauge", "Requests being answered right now.")
METRICS.declare(READY, "gauge", "1 when the last readiness probe was ready, 0 otherwise.")
METRICS.declare(BUILD_INFO, "gauge", "One series naming the contract version this instance serves.")


# --- Logging ------------------------------------------------------------------


class _JsonFormatter(logging.Formatter):
    """One JSON object per line, with whatever the caller attached to the record."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key.startswith("erp_"):
                payload[key.removeprefix("erp_")] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class _TextFormatter(logging.Formatter):
    """The same fields for a human reading a terminal: ``key=value`` after the message."""

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        extras = {
            key.removeprefix("erp_"): value
            for key, value in record.__dict__.items()
            if key.startswith("erp_")
        }
        if extras:
            message += " " + " ".join(f"{key}={value}" for key, value in sorted(extras.items()))
        if record.exc_info:
            message += "\n" + self.formatException(record.exc_info)
        return message


class RedactFilter(logging.Filter):
    """Remove the configured secrets from every line that goes out.

    The structural guarantee is that the request log carries nothing a caller sent (see
    the module docstring). This is the belt to that pair of braces: the values that are
    secrets *by configuration* — the database password, the SMTP password and the SMS
    gateway token — are replaced wherever they appear, including inside an exception
    message from a transport that quoted them.
    """

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        self._secrets = [secret for secret in secrets if secret and len(secret) >= 4]

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        message = record.getMessage()
        redacted = message
        for secret in self._secrets:
            redacted = redacted.replace(secret, "***")
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True


def configured_secrets(settings: Settings) -> list[str]:
    """Every value in the configuration that must never appear in a log line."""
    secrets = [settings.smtp_password, settings.sms_gateway_token]
    try:
        from sqlalchemy.engine import make_url

        password = make_url(settings.database_url).password
    except Exception:  # pragma: no cover - the URL was validated at start
        password = None
    secrets.append(password)
    return [secret for secret in secrets if secret]


def configure(settings: Settings | None = None, *, stream: Any = None, level: str | None = None):
    """Install the log format, the level and the redaction. Idempotent.

    Called once when the application is imported; callable again (a check that wants to
    capture a line, or a worker that starts with a different level) because it replaces
    the handlers it installed rather than adding a second set.
    """
    global _logging_configured
    settings = settings or SETTINGS
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "erp_owned", False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.erp_owned = True  # type: ignore[attr-defined]
    handler.addFilter(RedactFilter(configured_secrets(settings)))
    if settings.log_format == "text":
        handler.setFormatter(_TextFormatter("%(asctime)s %(levelname)s %(message)s"))
    else:
        handler.setFormatter(_JsonFormatter())
    root.addHandler(handler)
    root.setLevel((level or settings.log_level).upper())
    # Alembic's own loggers narrate plugin loading at import (`app/observability.py` reads
    # the migration tree for readiness, which imports it). That is library chatter, not
    # application information, and at INFO it makes one startup line look like four.
    logging.getLogger("alembic").setLevel(logging.WARNING)
    _logging_configured = True
    return handler


def log(level: str, message: str, **fields: Any) -> None:
    """Write one line with the correlation id and whatever fields the caller names."""
    logging.getLogger("erp").log(
        getattr(logging, level.upper()),
        message,
        extra={"erp_request_id": REQUEST_ID.get() or "-", **{f"erp_{k}": v for k, v in fields.items()}},
    )


def request_id_of(request) -> str:
    """The correlation id for a request: the caller's when it is well-formed, else a new one."""
    supplied = request.headers.get(REQUEST_ID_HEADER, "")
    if REQUEST_ID_SHAPE.match(supplied):
        return supplied
    return uuid.uuid4().hex


# --- The request middleware ---------------------------------------------------


class RequestContext:
    """One request's log line, timing and counters — the ASGI middleware itself.

    Plain ASGI rather than ``BaseHTTPMiddleware``: this has to run in the same context as
    the application so the correlation id is visible to the exception handlers (which
    write it into the `500` body), and it has to see the response *start* message to set
    the header before the body is sent.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        from starlette.requests import Request

        request = Request(scope, receive)
        request_id = request_id_of(request)
        token = REQUEST_ID.set(request_id)
        started = time.perf_counter()
        status = 500
        failed = False
def carried_request_id(request) -> str:
    """The correlation id a request is already logged under, for a handler outside the middleware.

    The handler for an *unhandled* exception is registered on Starlette's
    `ServerErrorMiddleware`, which wraps the user middleware stack — so it runs after
    :class:`RequestContext` has reset the ContextVar on its way out, and the id would read as
    empty by the time the body is built. The middleware therefore parks the id on the request's
    scope as well, and this reads it back: the id in the body is the id in the log line, which
    is the whole point of having one.

    Tolerates no request at all, because the handler is also called directly (a check driving
    the shape).
    """
    state = (getattr(request, "scope", None) or {}).get("state") or {}
    return state.get(REQUEST_ID_STATE) or REQUEST_ID.get() or "-"



        async def send_with_id(message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message["headers"] = [
                    (name, value)
                    for name, value in message["headers"]
                    if name.lower() != REQUEST_ID_HEADER.lower().encode()
                ]
                message["headers"].append((REQUEST_ID_HEADER.lower().encode(), request_id.encode()))
            await send(message)

        METRICS.inc(IN_FLIGHT)
        try:
            await self.app(scope, receive, send_with_id)
        except Exception:
            # The body is the exception handler's job (one error shape, no internals);
            # the log and the counter are this one's, and the re-raise is what lets the
            # handler answer at all.
            failed = True
            log("error", "request failed with an unhandled exception", method=request.method)
            # The traceback line carries the id too — it is the line an operator actually
            # needs to find, and "the id is in every line the request produces" is otherwise
            # untrue for this one. The id comes from the local rather than the ContextVar:
            # this block runs before the `finally` that resets it, but the local is the one
            # the response and the request line are already using.
            logging.getLogger("erp").exception(
                "unhandled exception serving %s",
                _route_label(scope),
                extra={"erp_request_id": request_id},
            )
            raise
        # Parked on the scope as well as in the ContextVar: the handler for an unhandled
        # exception runs outside this middleware and cannot see a ContextVar this resets.
        scope.setdefault("state", {})[REQUEST_ID_STATE] = request_id
        finally:
            duration = time.perf_counter() - started
            METRICS.inc(IN_FLIGHT, value=-1)
            route = _route_label(scope)
            labels = {"method": request.method, "route": route, "status": str(status)}
            METRICS.inc(REQUESTS, labels)
            METRICS.observe(REQUEST_DURATION, duration, {"method": request.method, "route": route})
            log(
                "warning" if failed or status >= 500 else "info",
                "request",
                method=request.method,
                route=route,
                status=status,
                duration_ms=round(duration * 1000, 3),
            )
            REQUEST_ID.reset(token)


def _route_label(scope) -> str:
    """The route *template* (``/api/v1/widgets/{id}``), never the raw path.

    A raw path in a log line is a credential waiting to happen (query strings, ids) and
    a raw path in a metric label is unbounded cardinality with whoever types the URL in
    control. An unmatched path is one fixed label for the same reason.
    """
    route = scope.get("route")
    path = getattr(route, "path", None)
    return str(path) if path else "unmatched"


# --- Readiness ----------------------------------------------------------------


def head_revision() -> str | None:
    """The revision this build's migration tree ends at, or None if it has no tree.

    Read from the migration scripts rather than the database: "at head" is a comparison,
    and the head is a property of the code that is running, not of the database it runs
    against.
    """
    ini = MIGRATIONS_INI
    if not ini.exists():
        return None
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        return ScriptDirectory.from_config(Config(str(ini))).get_current_head()
    except Exception:
        logging.getLogger("erp").exception("the migration tree could not be read")
        return None


def database_reachable(engine) -> tuple[bool, str]:
    """Whether the database answers a trivial statement. The detail is safe to publish."""
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
    except Exception as exc:
        logging.getLogger("erp").warning(
            "the readiness probe could not reach the database: %s: %s",
            type(exc).__name__,
            exc,
        )
        return False, "the database did not answer"
    return True, "ok"


def applied_revision(engine) -> str | None:
    """The revision the database says it is at (``alembic_version``), or None."""
    try:
        with engine.connect() as connection:
            return connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
    except Exception:
        # No table means no migration has ever been applied — which is exactly what a
        # schema built by `create_all` looks like, and readiness has to say so rather
        # than reporting the database as ready.
        return None


def readiness(engine, *, head: str | None = None) -> tuple[bool, list[dict[str, object]]]:
    """Whether this instance can serve: the database answers and the schema is at head.

    Returns ``(ready, checks)`` where each check names itself and states its outcome in
    words safe to publish — a readiness answer is a public probe, so it says *what* is
    not ready and never *why* (the why, with the exception, is in the log).
    """
    checks: list[dict[str, object]] = []
    reachable, detail = database_reachable(engine)
    checks.append({"name": "database", "ok": reachable, "detail": detail})
    at_head = False
    revision: str | None = None
    if reachable:
        revision = applied_revision(engine)
        expected = head if head is not None else head_revision()
        if expected is None:
            checks.append(
                {
                    "name": "migrations",
                    "ok": False,
                    "revision": revision,
                    "detail": "this build carries no migration tree, so its schema cannot be checked",
                }
            )
        elif revision is None:
            checks.append(
                {
                    "name": "migrations",
                    "ok": False,
                    "revision": None,
                    "detail": "no migration has been applied to this database",
                }
            )
        else:
            at_head = revision == expected
            checks.append(
                {
                    "name": "migrations",
                    "ok": at_head,
                    "revision": revision,
                    "detail": "at head" if at_head else "the schema is not at the revision this build expects",
                }
            )
    else:
        checks.append(
            {
                "name": "migrations",
                "ok": False,
                "revision": None,
                "detail": "not checked: the database did not answer",
            }
        )
    return (reachable and at_head), checks
