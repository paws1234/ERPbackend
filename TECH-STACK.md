# Technology Stack — Decision Record

- **Task**: `T-0.STACK.01` (Phase 0)
- **Decided**: 2026-09-17
- **Mirrored in**: `plan.md` §1 (Technology Stack) and §7.1; defaults in §8
- **Consumers**: `T-0.MODELS.01` (Phase 1 domain models and API contracts), `T-0.CICD.01` (backend pipeline), every later task's runtime
- **Repository**: backend (`ERPbackend`), per the ledger's `## Repositories` map

## Decisions

| Area | Choice |
|---|---|
| Backend | Python — FastAPI + SQLAlchemy |
| Frontend | Next.js |
| Database | PostgreSQL |
| Job queue | Postgres-backed, **no Redis** (library not pinned) |
| Search | PostgreSQL full-text |

## Reasons

### Backend — Python, FastAPI, SQLAlchemy

- FastAPI generates the OpenAPI contract **from the code**, which `T-0.API.01` requires ("generated from the code rather than hand-maintained") and which is the *only* coupling between the two application repositories.
- Declared request/response models give one type per field, so every documented field states its type and whether it is required, and a field removed from the contract breaks the frontend's generated client at build time rather than at runtime (`T-0.API.02`).
- SQLAlchemy makes the transaction boundary explicit, and the boundary is where `T-0.CORE.01` must enforce debit = credit ("at the storage boundary, not only in the caller").
- Exact decimal money arithmetic, mapped to `numeric`, so no floating-point rounding can enter a posting, an FX conversion or a valuation.

### Frontend — Next.js

- The frontend is a separate deployable that talks only to the published API and never to the database; Next.js runs as its own server process in its own container (`T-0.DEPLOY.02`), not embedded in the backend.
- TypeScript is what makes `T-0.API.02`'s typed client meaningful: a contract change that breaks the client fails a build (`T-0.CICD.02`).

### Database — PostgreSQL

- One durable store covers the three §3 needs that would otherwise each add a service: append-only ledgers, scheduled work, and search — so the compose stack stays at the containers the plan names (database, backend, frontend).
- `numeric` plus dated rates gives multi-currency amounts and FX rates an exact representation (`T-1.ACCT.05`).
- Transactions let a stock movement and its GL posting commit as one unit or not at all (`T-1.INV.07`, `T-1.ACCT.03`).
- Constraint support at the storage level is what the immutability and company-scoping rules are enforced with (`T-0.AUDIT.01`, `T-0.CORE.03`).

### Job queue — Postgres-backed, no Redis

- The scheduled work in §3 and the ledger — report runs, dunning, recurring billing, offline POS sync, biometric pulls — needs durable, retryable jobs, not a second infrastructure service to run and back up.
- Enqueueing a job in the same transaction as the business write is what makes "processed once" reachable for offline sync and for a re-run of a scheduled report, with no cross-store coordination (`T-6.OFFLINE.01`, `T-3.AR.03`, `T-0.REPORT.01`).
- **Still open** — and the only sub-choice this record leaves undecided: the library, `pgqueuer` or `procrastinate`. It is pinned when the first scheduled job is built (`T-0.REPORT.01`), and the decision travels with the worker container added at that point.

### Search — PostgreSQL full-text

- What needs searching is master data and documents already in the database (items, parties, documents); searching the same store keeps results consistent with the transaction that wrote the row.
- Avoids running and synchronising a second engine for requirements the database already meets.

## How the stack serves the §3 cross-cutting needs

| Need (§3) | How the stack serves it |
|---|---|
| Immutable append-only ledgers | Append-only tables written only through the posting primitive, with update/delete refused at the storage boundary rather than in each caller (`T-0.CORE.01`, `T-0.AUDIT.01`) |
| Multi-currency | Exact decimal money, rates held per currency pair per date and never silently overwritten, conversion through one central service (`T-1.ACCT.05`) |
| Offline POS sync | The client queues sales locally and replays them into the API, each with an idempotency key so a retry posts once (`T-6.OFFLINE.01`) |
| Scheduled reporting | A Postgres-backed worker reads the same database, so a run and its delivery record commit together (`T-0.REPORT.01`) |

## Consequences

- PostgreSQL is a dependency of the queue and of search, not only of the data: it cannot be swapped without revisiting this record.
- No Redis, no second search engine, no shared source tree or cross-repository build between the two applications.
- The worker is a fourth container, added only when the first scheduled job exists (`T-0.REPORT.01`).
