# Operating the ERP stack

**Task**: `T-8.OPS.01` (Phase 8) — the operator-facing runbook for the stack `docker-compose.yml`
brings up: its containers, the environment each needs, the install, the worker, the health and
metrics endpoints, what to look at when something is wrong, and the limits this stack has today.

**The rule this document follows.** §4b principle 5 is *operational truth is tested, documentation
is not evidence*, so **every command below was run, as written, against the stack this repository's
compose file brings up** (2026-10-12, `docker compose` on the backend repository's own tree), and
every claim about behaviour names the check under `tests/` that holds it. Where a limit has no check
behind it, it is stated as a limit rather than as behaviour.

Not here on purpose: the backup and restore procedure (`T-9.BACKUP.02`), the deploy jobs and the
rollback (`T-9.DEPLOY.01`, `T-9.DEPLOY.02`), and the TLS proxy and rate limits (`T-9.EDGE.01`,
`T-9.EDGE.02`). Those are Phase 9's; §8 below says what that means for this stack today.

## 1. The four containers

| Service | Image | Runs | Networks | Health |
|---|---|---|---|---|
| `db` | `postgres:16` | the database, on the `db-data` volume | `data` | `pg_isready` |
| `backend` | `${BACKEND_IMAGE}` (default `erpv1-backend:local`) | `uvicorn app.api:app` | `data`, `edge` | `GET /api/v1/health` from inside the container |
| `worker` | the **same image** as `backend` | `python -m app.worker` | `data` | `python -m app.worker --check` |
| `frontend` | `${FRONTEND_IMAGE}` (default `erpv1-frontend:local`) | the Next.js standalone server | `edge` | `fetch('http://127.0.0.1:3000/')` |

- **The worker is a second container over one image, not a second application.** Background work has
  to survive a redeploy of the web process, and a job that ran inside a request would turn a job's
  failure into a `500` (`app/worker.py`). It sits on `data` alone: it reaches the database, and
  nothing reaches it.
- **The frontend is not on `data`.** It cannot reach the database at all — the API-first rule is
  enforced by the compose networks rather than by convention (`tests/check_compose_stack.py`, which
  attempts the connection from inside the container).
- **The `backend` publishes no port.** Only `frontend` publishes one (`${FRONTEND_PORT:-3000}`).
  Until `T-9.EDGE.01` puts the proxy in front, the API is reachable from the compose network and
  from inside its own container, which is how §5 asks it.
- **The whole stack is four services and one volume.** `docker compose down` removes the containers
  and keeps `db-data`; `docker compose down -v` **destroys the database** — it is the only copy
  (§8).

## 2. Installing it the first time

Two commands, in this order. The order matters and is not the obvious one: the worker cannot start
before the database has been installed, because the queue's own tables arrive with the migration
tree (`app/bootstrap.py`) — see §4 and §8.

```sh
# 1. migrate and seed — this also starts `db` and waits for it to answer
POSTGRES_PASSWORD=… docker compose run --rm backend python -m app.bootstrap \
    --company-code ACME --company-name "Acme Trading" --market philippines \
    --fiscal-year-start-month 1 --admin-subject you@example.com \
    --admin-password '…'

# 2. now bring the stack up; every service reaches healthy
POSTGRES_PASSWORD=… docker compose up -d --wait

POSTGRES_PASSWORD=… docker compose ps
```

```
company 102413f5-2da6-42be-b0de-2c1c036fdea7 installed for 'ACME'
administrator 'you@example.com' holds 32 capability(ies) in it
```

- **`POSTGRES_PASSWORD` is required by the compose file itself** — it has no default, and every
  `docker compose` command in this directory refuses without it (`:?POSTGRES_PASSWORD is required`),
  including the ones that do not start the database.
- **The install is one transaction, and the migration tree runs first.** A refusal — a company code
  already taken, a pack that leaves a section empty, a fiscal year start nobody has confirmed —
  leaves nothing behind instead of half an install.
- **Running it twice is refused by name**, and the exit code is non-zero:

  ```
  refused: company 'ACME' already exists ('Acme Trading') — this is an install, not a
  migration, and it will not touch an existing company
  ```
- The printed **company id** is what `COMPANY_ID` is given on the frontend service.
- `--market` decides the chart of accounts and the tax rules. `--fiscal-year-start-month` is
  required wherever the market's pack leaves the fiscal calendar open (plan §8, OQ-1) and the pack
  refuses to guess it.

For a stack built from this checkout rather than from published images:

```sh
POSTGRES_PASSWORD=… docker compose -f docker-compose.yml -f docker-compose.dev.yml build backend
POSTGRES_PASSWORD=… docker compose -f docker-compose.yml -f docker-compose.dev.yml build frontend
```

## 3. Migrations and upgrades

`python -m app.bootstrap` **is** the migration runner — it runs `alembic upgrade head` before it
seeds anything. An upgrade with no install is the same tree, driven directly:

```sh
POSTGRES_PASSWORD=… docker compose exec backend alembic current      # 0001_baseline (head)
POSTGRES_PASSWORD=… docker compose exec backend alembic upgrade head # a second time: no-op
```

- `alembic upgrade head` on an already-current schema **changes nothing** — the same no-op the
  install's second run relies on (`tests/check_migrations.py`).
- The migration tree also installs what is not a table: the company-scoping policies, the audit and
  ledger triggers, and the queue's own tables (`tests/check_migrated_scoping.py`).
- A model that arrives without a migration makes `alembic check` exit non-zero and name it, so the
  drift is a red build rather than a surprise in production (`tests/check_migrations.py`).
- **Readiness is what tells you the schema is wrong**, not the upgrade: `GET /api/v1/ready` reports
  `{"name": "migrations", "ok": false, "detail": "the schema is not at the revision this build
  expects"}` when the database is behind the image (§5).

## 4. The worker

```sh
POSTGRES_PASSWORD=… docker compose exec worker python -m app.worker --check
```

```
jobs: purge_expired_sessions, retry_outbound_deliveries
schedules: purge_expired_sessions, retry_outbound_deliveries
  purge_expired_sessions: delete user sessions that have lapsed (identity; unlogged-in) (up to 1 attempt(s))
  retry_outbound_deliveries: re-attempt outbound deliveries whose next attempt is due (up to 2 attempt(s))
  purge_expired_sessions: */15 * * * *
  retry_outbound_deliveries: */5 * * * *
```

- **`--check` is also the container's healthcheck**, so a worker that would run nothing says so
  rather than merely running. It exits non-zero when the queue's tables are missing.
- **`--once` drains the queue and exits** — what an operator runs to work through a backlog without
  leaving a worker running, and what a smoke test uses.
- **Two schedules recur**, both on `pgqueuer` (Postgres-backed, no Redis): the session purge every
  15 minutes, the outbound-delivery retry every 5. Every declared schedule has been observed to fire
  (`tests/check_schedules.py`), and a job run twice produces one effect while a failing job retries
  with its attempt and its error on the record (`tests/check_worker.py`, `tests/check_schedules.py`).
- **Scaling out is more containers**, not a bigger one:

  ```sh
  POSTGRES_PASSWORD=… docker compose up -d --scale worker=2 --wait
  ```

  `WORKER_CONCURRENCY` caps the queue manager; pgqueuer bounds each entrypoint's concurrency in SQL,
  so two workers cannot run the same job at once.
- **`docker compose logs worker`** is where a failing job's traceback appears. The worker has no
  HTTP endpoint and so **no `/metrics` of its own** (§8).

## 5. Health and metrics

The API serves three platform routes. They are asked from inside the backend container, because the
backend publishes no port yet:

```sh
ask() { docker compose exec -T backend python -c "import sys,urllib.request;print(urllib.request.urlopen(sys.argv[1],timeout=5).read().decode())" "$1"; }

ask http://127.0.0.1:8000/api/v1/health    # liveness
ask http://127.0.0.1:8000/api/v1/ready     # readiness
ask http://127.0.0.1:8000/api/v1/metrics   # Prometheus text
```

| Route | Answers | What it means |
|---|---|---|
| `GET /api/v1/health` | always `200` while the process runs | **liveness.** It touches no dependency, because a restart cannot fix a dependency and a probe that fails for one is a crash loop. |
| `GET /api/v1/ready` | `200`, or `503` when it cannot serve | **readiness.** The checks it made are named in the body: the database answering, and the schema at the revision this build expects. This is the one to put behind a load balancer. |
| `GET /api/v1/metrics` | Prometheus text, or `404` | the counters this process reports. `metrics_enabled=false` answers `404` rather than an empty document, so a scrape that is switched off looks switched off. |

Liveness and readiness are both held by `tests/check_health.py`, including the deadline: readiness
answers `503` **within `READINESS_TIMEOUT_SECONDS`** when the database stops, and `200` again when
it comes back. Verified on the running stack:

```sh
POSTGRES_PASSWORD=… docker compose stop db
# 503 {"status":"unavailable", …, "checks":[{"name":"database","ok":false,
#      "detail":"the database did not answer"}, {"name":"migrations","ok":false,
#      "detail":"not checked: the database did not answer"}]}
# and liveness is still 200 {"status":"ok","version":"v1"}
POSTGRES_PASSWORD=… docker compose start db
# 200 {"status":"ok", …, "checks":[{"name":"database","ok":true,"detail":"ok"},
#      {"name":"migrations","ok":true,"detail":"at head","revision":"0001_baseline"}]}
```

The frontend is the only published port:

```sh
curl -sS -o /dev/null -w '%{http_code}\n' http://localhost:3000/       # 307 -> the sign-in screen
curl -sS -o /dev/null -w '%{http_code}\n' http://localhost:3000/login  # 200
```

**The metrics registry is this process's own.** A scrape of `backend` reads the web container's
numbers and the worker's separately, and the worker is not scraped at all (§8).

## 6. When something is wrong

```sh
POSTGRES_PASSWORD=… docker compose ps                 # which service, and whether it is healthy
POSTGRES_PASSWORD=… docker compose logs backend       # and: worker, db, frontend
POSTGRES_PASSWORD=… docker compose exec -T db psql -U erpv1 -d erpv1 -tAc "select code, name from company order by code"
```

- **Read the startup line first.** Every process logs its whole validated configuration once, with
  secrets redacted — `smtp_password` and the like render as `******`, never as a value:

  ```json
  {"message": "the application is starting", "version": "v1", "database_url": "******db:5432/erpv1",
   "queue_library": "pgqueuer", "worker_concurrency": 4, "log_level": "info", "log_format": "json",
   "metrics_enabled": true, "smtp_host": null, "delivery_max_attempts": 5, …}
  ```

  A setting that is wrong stops the process here, naming the variable, rather than failing a request
  later (`tests/check_config.py` — a missing or malformed `DATABASE_URL` fails at start, before the
  process even logs that it is starting).
- **A failure a request cannot be answered for answers `500` with a correlation id**, and the same
  id is on the log line and the traceback. No exception class, message, path or SQL appears in the
  body (`tests/check_error_hygiene.py`). Every request leaves exactly one structured log line
  carrying that id, and no credential ever appears in it (`tests/check_request_log.py`).
- **A notification that never arrived** is in the delivery log, not in an inbox: one
  `outbound_delivery` row per message and one `delivery_attempt` row per attempt, each with its
  status, its error and — while attempts remain — the instant the next one is due. A retry continues
  the same delivery rather than making a second message, and an unconfigured channel is a recorded
  attempt rather than a failed request (`tests/check_delivery.py`). Nothing is sent until
  `smtp_host` and `smtp_from` are set (§7).
- **When a check fails**, the checks are the account of what the platform does:

  ```sh
  DATABASE_URL=postgresql+psycopg://… python tests/check_delivery.py   # one check, on its own
  ```

  The pipeline runs all of them, then the ledger gate last, which is the order `TESTING.md` states
  (a failure in a lower layer makes the later ones meaningless). `.github/workflows/backend.yml` is
  the list and the order: every `tests/check_*.py` except `check_backend_image.py` (the `publish` job
  builds the image for real) and `check_compose_stack.py` (it needs the frontend repository's image,
  and this pipeline must never need its sibling), then `tests/check_ledger_integrity.py`.

## 7. The variables

Every setting is declared, validated and read **once, at start**, by `app/config.py`
(`tests/check_config.py` holds all three). A name below is the environment's; the value in *Default*
is what an install that says nothing gets.

| Variable | Required? | Default | What it does |
|---|---|---|---|
| `DATABASE_URL` | **required** | — | the database. Missing or malformed fails at start, naming the variable. |
| `POSTGRES_PASSWORD` | **required by the compose file** | — | the database container's password; `POSTGRES_DB` / `POSTGRES_USER` default to `erpv1`. |
| `BACKEND_IMAGE` / `FRONTEND_IMAGE` | no | `erpv1-backend:local` / `erpv1-frontend:local` | which images the stack runs. |
| `FRONTEND_PORT` | no | `3000` | the one published port. |
| `COMPANY_ID` / `ACTOR` | no | empty / `shell` | what the frontend is given at start. |
| `QUEUE_LIBRARY` | no | `pgqueuer` | the background-work library. |
| `WORKER_CONCURRENCY` | no | `4` | the queue manager's concurrency cap (1–64). |
| `LOG_LEVEL` / `LOG_FORMAT` | no | `info` / `json` | how the log lines are written (`json` or `text`). |
| `METRICS_ENABLED` | no | `true` | whether `/metrics` answers; when `false` it is `404`. |
| `ERROR_TRACKING_DSN` | no | unset | an error tracker to report exceptions to. OQ-4 has not chosen one, so it is unset rather than guessed. |
| `READINESS_TIMEOUT_SECONDS` | no | `3` | the deadline readiness answers on (1–30). |
| `SMTP_HOST`, `SMTP_FROM` | **both, for email** | unset | the SMTP relay and the sender. One without the other is refused at start; unset means the channel is not registered and every attempt is recorded as failed rather than sent. |
| `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_STARTTLS` | no | `587`, unset, unset, `true` | the rest of the relay's settings. |
| `SMS_PROVIDER` and `SMS_GATEWAY_URL` / `_TOKEN` / `SMS_SENDER` | **only if a provider is named**; unset by default (OQ-11) | unset | the SMS gateway. Once `SMS_PROVIDER` is set, the other three are required. |
| `DELIVERY_MAX_ATTEMPTS` | no | `5` | how many attempts one delivery gets before it stops being retried (1–20). |
| `DELIVERY_BACKOFF_MINUTES` / `DELIVERY_BACKOFF_MAX_MINUTES` | no | `1` / `60` | the first retry delay and the cap on the exponential growth. |
| `PASSWORD_MIN_LENGTH`, `SESSION_TTL_MINUTES`, `SESSION_IDLE_TIMEOUT_MINUTES`, `LOCKOUT_THRESHOLD`, `LOCKOUT_WINDOW_MINUTES`, `MFA_REQUIRED_ROLES`, `RECOVERY_CODE_COUNT` | no | `12`, `720`, `60`, `5`, `15`, `administrator`, `10` | Phase 7's identity settings, declared in the same place so one module reads the environment. |

Rotating a secret is a restart, not a rebuild: the images copy no configuration in
(`Dockerfile`), so the same image runs against any environment.

## 8. What this stack does not do yet

Each of these is a limit to know about, not a defect to work around.

- **There is no backup.** The database is one volume on this host, and `docker compose down -v`
  destroys it. Off-host `pg_dump` with retention is `T-9.BACKUP.01` and the timed restore drill is
  `T-9.BACKUP.02`; until those land, the operator's only copy is the volume itself.
- **There is no TLS, no proxy and no rate or size limit.** The API is not reachable from outside the
  compose network. `T-9.EDGE.01` and `T-9.EDGE.02` add Caddy, the certificates and the limits.
- **There are no resource limits, no rolling deploy and no rollback.** `T-9.DEPLOY.01` and
  `T-9.DEPLOY.02` add the staging and production jobs with a rollback.
- **The worker cannot start before the database is installed.** Its queue tables come from the
  migration tree, so on a stack whose database is empty the worker exits with
  `RuntimeError: The required table 'pgqueuer' is missing` and `restart: unless-stopped` restarts it
  — which is what `docker compose up -d --wait` on a *fresh* stack reports as an unhealthy `worker`.
  Install first (§2). Once the database is installed the worker starts and stays healthy.
- **The worker is not scraped.** Its counters live in the worker process, which serves no HTTP; a
  scrape of `backend` does not include them.
- **Nothing is sent until a channel is configured.** With `SMTP_HOST` and `SMTP_FROM` unset a
  notification is a recorded failed attempt that is retried to the attempt limit — visible in the
  delivery log, and never sent, until the relay is configured and the stack restarted.
- **One host, one database, one of each service.** There is no replication and no failover; scaling
  out is `--scale worker=N` or a bigger host.
