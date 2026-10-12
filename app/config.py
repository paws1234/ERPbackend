"""T-8.CFG.01 — every setting the platform has, declared once, validated before it serves.

**Why this module exists.** Settings used to be read where they were used, so
``app/api.py`` reached for ``DATABASE_URL`` on the **first request** rather than at
start, and a container with a missing or malformed one came up "healthy" (its
liveness probe answered) and then failed every request with a `500` — finding 26 of
the 2026-10-10 audit. A setting that cannot be read correctly is a fact about the
*deployment*, and a deployment that cannot serve should say so at start, naming the
variable, rather than at request time with a traceback nobody attributes.

The rules this module is held to, from plan §4b's production principles:

* **Declared once.** Each variable in plan §9 appears here exactly once, with a
  type and a default. A module that needs a setting imports it from :data:`SETTINGS`
  (or calls :func:`load_settings` for its own), so "what is configurable" is one
  file, and a variable that is not in §9 is not read from the environment.
* **Validated at start**, with the variable named. Every problem is collected before
  anything is raised, so an operator with three misconfigured variables sees three,
  not the first and then the next. :data:`SETTINGS` is loaded at import, which is the
  start: importing the application with a broken environment raises, and the process
  dies — which is what a `restart: unless-stopped` container is supposed to do.
* **Types are real.** ``METRICS_ENABLED=false`` is the boolean ``False``, not the
  string ``"false"`` (which is truthy). ``LOG_LEVEL`` is one of five names, not
  whatever was typed.
* **Secrets have no default** (principle 7): ``DATABASE_URL``, ``SMTP_PASSWORD`` and
  the SMS token are read from the environment and never invented.

**What it is not.** Not a settings *service*: nothing reloads at runtime, because a
value that can change under a running process is a value two parts of the process can
disagree about. A change is a restart. T-9.SECRETS.01 adds the envelope encryption and
rotation story; this module only guarantees the values came from the environment.

**The two §9 values with no decided value.** ``fx_rate_source`` (§9: *"Not stated"* — the
provider adapter is Phase 11's) and ``integration_endpoints`` (§9: *"Configured per tenant"*)
are declared here, with the shape a stated value must have, and read once like every other
setting. Their *values* are left to the phases that adopt them, so an unset one is ``None``
and the module that needs it refuses where it is used rather than inventing a default.

**The identity settings live here too.** They were read at import in
:mod:`app.identity` with the note that "T-8.CFG.01 (app/config.py) is where they are
declared once and validated at startup" — so they moved here rather than being
declared a second time, and :mod:`app.identity` re-exports them under the names it
always used.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, fields

from sqlalchemy.engine import make_url

# The names of the variables this module reads. Stated as constants because each is
# named in an error message, and an error message that misspells the variable is worse
# than none at all.
DATABASE_URL = "DATABASE_URL"
QUEUE_LIBRARY = "QUEUE_LIBRARY"
WORKER_CONCURRENCY = "WORKER_CONCURRENCY"
LOG_LEVEL = "LOG_LEVEL"
LOG_FORMAT = "LOG_FORMAT"
METRICS_ENABLED = "METRICS_ENABLED"
ERROR_TRACKING_DSN = "ERROR_TRACKING_DSN"
READINESS_TIMEOUT_SECONDS = "READINESS_TIMEOUT_SECONDS"
SMTP_HOST = "SMTP_HOST"
SMTP_PORT = "SMTP_PORT"
SMTP_USER = "SMTP_USER"
SMTP_PASSWORD = "SMTP_PASSWORD"
SMTP_FROM = "SMTP_FROM"
SMTP_STARTTLS = "SMTP_STARTTLS"
SMS_PROVIDER = "SMS_PROVIDER"
SMS_GATEWAY_URL = "SMS_GATEWAY_URL"
SMS_GATEWAY_TOKEN = "SMS_GATEWAY_TOKEN"
SMS_SENDER = "SMS_SENDER"
DELIVERY_MAX_ATTEMPTS = "DELIVERY_MAX_ATTEMPTS"
DELIVERY_BACKOFF_MINUTES = "DELIVERY_BACKOFF_MINUTES"
DELIVERY_BACKOFF_MAX_MINUTES = "DELIVERY_BACKOFF_MAX_MINUTES"
PASSWORD_MIN_LENGTH = "PASSWORD_MIN_LENGTH"
SESSION_TTL_MINUTES = "SESSION_TTL_MINUTES"
SESSION_IDLE_TIMEOUT_MINUTES = "SESSION_IDLE_TIMEOUT_MINUTES"
LOCKOUT_THRESHOLD = "LOCKOUT_THRESHOLD"
LOCKOUT_WINDOW_MINUTES = "LOCKOUT_WINDOW_MINUTES"
MFA_REQUIRED_ROLES = "MFA_REQUIRED_ROLES"
RECOVERY_CODE_COUNT = "RECOVERY_CODE_COUNT"
# §9's two variables whose *value* the plan leaves to the phase that owns them — but which a
# module needs today, so they are declared here rather than read where they are used.
FX_RATE_SOURCE = "FX_RATE_SOURCE"
INTEGRATION_ENDPOINTS = "INTEGRATION_ENDPOINTS"

LOG_LEVELS = ("debug", "info", "warning", "error")
LOG_FORMATS = ("json", "text")
# The queue library is not a preference: postgres-backed, no Redis, decided with the
# plan (2026-10-10). The variable exists so a deployment *states* it, and a value this
# build cannot serve is refused rather than silently ignored.
QUEUE_LIBRARIES = ("pgqueuer",)
# The one SMS gateway shape this build speaks: an HTTP endpoint that takes the
# provider's own JSON. §9 leaves the provider open (OQ-11), so the enum has one member
# rather than a list of names nothing implements.
SMS_PROVIDERS = ("generic-http",)
# SQLAlchemy dialect names this platform serves. `postgresql` alone is accepted because
# that is what libpq tools and a copied `psql` string say; psycopg is the driver either
# way.
POSTGRES_SCHEMES = ("postgresql", "postgres", "postgresql+psycopg", "postgres+psycopg")


class ConfigError(RuntimeError):
    """One or more settings cannot be read. Nothing has been served."""


def _problems(environ: Mapping[str, str]) -> tuple[list[str], dict[str, object]]:
    """Every problem with the environment, and the values that parsed.

    Collected rather than raised one at a time: an operator fixing a deployment wants
    the whole list. Each problem names the variable it is about.

    The values are keyed by the **field** they fill, which is the variable's own name
    lowercased (``DATABASE_URL`` → ``database_url``): the two cannot drift, and a setting
    added to :class:`Settings` without being parsed below fails in :func:`load_settings`
    rather than defaulting to None somewhere it is used.
    """
    problems: list[str] = []
    values: dict[str, object] = {}

    def text(name: str, default: str | None = None, *, required: bool = False) -> None:
        raw = environ.get(name, default)
        if raw is None or str(raw).strip() == "":
            if required:
                problems.append(f"{name} is required and not set")
            else:
                values[name.lower()] = None
            return
        values[name.lower()] = str(raw).strip()

    def integer(name: str, default: int, low: int, high: int) -> None:
        raw = environ.get(name)
        if raw is None or str(raw).strip() == "":
            values[name.lower()] = default
            return
        try:
            parsed = int(str(raw).strip())
        except ValueError:
            problems.append(f"{name} is {raw!r}, which is not a whole number")
            return
        if not low <= parsed <= high:
            problems.append(f"{name} is {parsed}, which is outside {low}–{high}")
            return
        values[name.lower()] = parsed

    def boolean(name: str, default: bool) -> None:
        raw = environ.get(name)
        if raw is None or str(raw).strip() == "":
            values[name.lower()] = default
            return
        lowered = str(raw).strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            values[name.lower()] = True
        elif lowered in ("false", "0", "no", "off"):
            values[name.lower()] = False
        else:
            problems.append(f"{name} is {raw!r}, which is not true/false")

    def choice(name: str, default: str, allowed: tuple[str, ...]) -> None:
        raw = environ.get(name)
        value = default if raw is None or str(raw).strip() == "" else str(raw).strip().lower()
        if value not in allowed:
            problems.append(f"{name} is {value!r}, which is not one of {', '.join(allowed)}")
            return
        values[name.lower()] = value

    # --- the database ---------------------------------------------------------
    # Required and validated here rather than at the first request, which is the whole
    # point of this module: a URL this platform cannot dial stops the process.
    raw_url = environ.get(DATABASE_URL)
    if raw_url is None or str(raw_url).strip() == "":
        problems.append(f"{DATABASE_URL} is required and not set: there is no database to serve from")
    else:
        try:
            url = make_url(str(raw_url).strip())
        except Exception as exc:  # SQLAlchemy states why it cannot parse it
            problems.append(f"{DATABASE_URL} is not a usable URL: {exc}")
        else:
            if url.drivername not in POSTGRES_SCHEMES:
                problems.append(
                    f"{DATABASE_URL} names the {url.drivername!r} driver, and this platform"
                    f" serves Postgres ({', '.join(POSTGRES_SCHEMES)})"
                )
            elif not url.database:
                problems.append(f"{DATABASE_URL} names no database")
            elif not url.host and not url.query.get("host"):
                problems.append(f"{DATABASE_URL} names no host")
            else:
                values[DATABASE_URL.lower()] = str(raw_url).strip()

    # --- the platform's own settings -----------------------------------------
    choice(QUEUE_LIBRARY, "pgqueuer", QUEUE_LIBRARIES)
    integer(WORKER_CONCURRENCY, 4, 1, 64)
    choice(LOG_LEVEL, "info", LOG_LEVELS)
    choice(LOG_FORMAT, "json", LOG_FORMATS)
    boolean(METRICS_ENABLED, True)
    text(ERROR_TRACKING_DSN)
    integer(READINESS_TIMEOUT_SECONDS, 3, 1, 30)

    # --- delivery (§9) --------------------------------------------------------
    integer(DELIVERY_MAX_ATTEMPTS, 5, 1, 20)
    integer(DELIVERY_BACKOFF_MINUTES, 1, 1, 1440)
    integer(DELIVERY_BACKOFF_MAX_MINUTES, 60, 1, 1440)

    text(SMTP_HOST)
    text(SMTP_USER)
    text(SMTP_PASSWORD)
    text(SMTP_FROM)
    integer(SMTP_PORT, 587, 1, 65535)
    boolean(SMTP_STARTTLS, True)
    host, sender = values.get(SMTP_HOST.lower()), values.get(SMTP_FROM.lower())
    if host and not sender:
        problems.append(
            f"{SMTP_FROM} is required when {SMTP_HOST} is set: a message needs a sender"
        )
    if sender and not host:
        problems.append(f"{SMTP_HOST} is required when {SMTP_FROM} is set")

    # An unset SMS provider is the decided default (OQ-11 leaves the provider open), so
    # the variables below are only required once one is named.
    raw_provider = environ.get(SMS_PROVIDER)
    if raw_provider is None or str(raw_provider).strip() == "":
        for name in (SMS_PROVIDER, SMS_GATEWAY_URL, SMS_GATEWAY_TOKEN, SMS_SENDER):
            values[name.lower()] = None
    else:
        choice(SMS_PROVIDER, "", SMS_PROVIDERS)
        text(SMS_GATEWAY_URL, required=True)
        text(SMS_GATEWAY_TOKEN, required=True)
        text(SMS_SENDER, required=True)

    # --- identity (§9; T-7.AUTH.01's settings, declared here) ------------------
    integer(PASSWORD_MIN_LENGTH, 12, 8, 64)
    integer(SESSION_TTL_MINUTES, 720, 5, 1440)
    integer(SESSION_IDLE_TIMEOUT_MINUTES, 60, 5, 480)
    integer(LOCKOUT_THRESHOLD, 5, 1, 100)
    integer(LOCKOUT_WINDOW_MINUTES, 15, 1, 1440)
    integer(RECOVERY_CODE_COUNT, 10, 5, 20)
    raw_roles = environ.get(MFA_REQUIRED_ROLES, "administrator")
    roles = tuple(part.strip() for part in str(raw_roles).split(",") if part.strip())
    values[MFA_REQUIRED_ROLES.lower()] = roles

    # --- §9 values the phase that owns them has not decided yet ----------------
    # Both are read by the module that needs them, and both are declared here for the reason
    # every other setting is: "what is configurable" is one file. What is *not* decided here is
    # either value — §9 defers `fx_rate_source` to the provider adapter of Phase 11
    # (T-11.FX.01) and states `integration_endpoints` as per-tenant — so a deployment that has
    # not stated them gets None, and the module that needs one says so where it is used
    # (`app/ledger/currency.py` refuses a sync with no source; `app/integrations.py` refuses a
    # channel with no endpoint). What *is* decided here is the shape: stated but unreadable is a
    # misconfigured deployment, which refuses to start rather than at the first send.
    text(FX_RATE_SOURCE)

    # The shape is validated here, once, rather than at the first send: §9 leaves this one
    # "Configured per tenant" (it is not a platform setting), so what is decided here is that a
    # *stated* value must be an object of channels — a deployment that states nonsense refuses
    # to start rather than failing when a message is sent.
    # ponytail: per-tenant in the plan, read here as one deployment-wide value. Ceiling: one
    # installation, one boundary configuration. Upgrade path: the phase that owns tenant
    # configuration reads the per-tenant value and leaves only the shape described here.
    raw_endpoints = environ.get(INTEGRATION_ENDPOINTS)
    if raw_endpoints is None or str(raw_endpoints).strip() == "":
        values[INTEGRATION_ENDPOINTS.lower()] = None
    else:
        try:
            endpoints = json.loads(str(raw_endpoints))
        except json.JSONDecodeError as exc:
            problems.append(f"{INTEGRATION_ENDPOINTS} is not valid JSON: {exc}")
        else:
            if not isinstance(endpoints, dict):
                problems.append(
                    f"{INTEGRATION_ENDPOINTS} is a {type(endpoints).__name__}, not an object"
                    ' of channel to endpoint (for example {"email": {"url": "…"}})'
                )
            else:
                values[INTEGRATION_ENDPOINTS.lower()] = endpoints

    if (
        isinstance(values.get(DELIVERY_BACKOFF_MINUTES.lower()), int)
        and isinstance(values.get(DELIVERY_BACKOFF_MAX_MINUTES.lower()), int)
        and values[DELIVERY_BACKOFF_MAX_MINUTES.lower()]
        < values[DELIVERY_BACKOFF_MINUTES.lower()]
    ):
        problems.append(
            f"{DELIVERY_BACKOFF_MAX_MINUTES} is below {DELIVERY_BACKOFF_MINUTES},"
            " so the delay would shrink as attempts are made"
        )

    return problems, values


@dataclass(frozen=True)
class Settings:
    """The whole configuration of one running instance, read once."""

    database_url: str
    queue_library: str
    worker_concurrency: int
    log_level: str
    log_format: str
    metrics_enabled: bool
    error_tracking_dsn: str | None
    readiness_timeout_seconds: int
    delivery_max_attempts: int
    delivery_backoff_minutes: int
    delivery_backoff_max_minutes: int
    smtp_host: str | None
    smtp_port: int
    smtp_user: str | None
    smtp_password: str | None
    smtp_from: str | None
    smtp_starttls: bool
    sms_provider: str | None
    sms_gateway_url: str | None
    sms_gateway_token: str | None
    sms_sender: str | None
    password_min_length: int
    session_ttl_minutes: int
    session_idle_timeout_minutes: int
    lockout_threshold: int
    lockout_window_minutes: int
    mfa_required_roles: tuple[str, ...]
    recovery_code_count: int
    # §9's two not-yet-decided values (see `_problems`): read once, like everything else.
    fx_rate_source: str | None
    integration_endpoints: dict[str, dict] | None

    @property
    def dsn(self) -> str:
        """The URL in libpq form — what psycopg and the queue connect with.

        ``DATABASE_URL`` carries SQLAlchemy's own dialect name
        (``postgresql+psycopg://``); psycopg wants the plain scheme.
        """
        return make_url(self.database_url).set(drivername="postgresql").render_as_string(
            hide_password=False
        )

    @property
    def smtp_configured(self) -> bool:
        """Whether this instance can actually send mail."""
        return bool(self.smtp_host and self.smtp_from)

    @property
    def sms_configured(self) -> bool:
        """Whether this instance can actually send a text message."""
        return bool(self.sms_provider and self.sms_gateway_url and self.sms_sender)

    def delivery_backoff(self, attempt: int) -> int:
        """Minutes to wait before `attempt` (1-based) is made.

        Exponential, from ``DELIVERY_BACKOFF_MINUTES``, capped at
        ``DELIVERY_BACKOFF_MAX_MINUTES`` — §9's "5 / exponential to 60".
        """
        minutes = self.delivery_backoff_minutes * (2 ** max(attempt - 1, 0))
        return int(min(minutes, self.delivery_backoff_max_minutes))


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Read and validate the environment. Raises :class:`ConfigError` naming every problem.

    Callable more than once (a check that wants a second configuration loads one), but
    the instance loads it **once**, at import, in :data:`SETTINGS`.
    """
    source = os.environ if environ is None else environ
    problems, values = _problems(source)
    if problems:
        raise ConfigError(
            "the configuration is not valid, so this instance will not serve:\n  "
            + "\n  ".join(problems)
        )
    # `values` is built from this dataclass's own field names, so a new field that
    # nobody parsed fails here rather than defaulting to None at the point of use.
    return Settings(**{field.name: values[field.name] for field in fields(Settings)})


# Loaded at import: importing the application *is* the start, so a broken environment
# stops the process before a socket is opened.
SETTINGS = load_settings()


def describe() -> dict[str, object]:
    """The configuration as one line of startup log, with every secret replaced.

    An operator reading the log can see what the process is configured to do; a secret
    is not in the log, because a log is not a secret store (principle 7, and Phase 8's
    "a credential never appears in it").
    """
    redacted = {"smtp_password", "sms_gateway_token", "integration_endpoints"}
    described: dict[str, object] = {}
    for field in fields(Settings):
        value = getattr(SETTINGS, field.name)
        if field.name in redacted and value:
            described[field.name] = "***"
        elif field.name == "database_url":
            described[field.name] = _without_password(SETTINGS.database_url)
        else:
            described[field.name] = value
    return described


def _without_password(url: str) -> str:
    """The database URL with its password removed — the one credential the DSN carries."""
    try:
        parsed = make_url(url)
    except Exception:  # pragma: no cover - SETTINGS holds a URL that already parsed
        return "postgresql://(unparsed)"
    return parsed.set(password="***").render_as_string(hide_password=False)
