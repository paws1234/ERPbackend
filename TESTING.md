# Testing strategy — unit / integration / ledger-integrity

- **Task**: `T-0.CORE.02` (Phase 0)
- **Repository**: backend (`ERPbackend`)
- **Serves**: §7.5 (delivery), §6 metric 1 (all financial postings balance), `T-0.CICD.01` (the pipeline that runs these layers)
- **Consumers**: every later task's own check

## The three layers

| Layer | What belongs in it | Runs against |
|---|---|---|
| **Unit** | A rule that lives inside one pure function: UOM conversion maths (`T-1.INV.01`), a valuation method's arithmetic (`T-1.INV.04`), FX conversion (`T-1.ACCT.05`), a payroll component's maths (`T-5.PAY.*`), the approval-threshold band that picks a level (`T-0.WF.01`). No database, no HTTP, no clock. | the function |
| **Integration** | Code against a real boundary: the posting primitive against PostgreSQL, a stock movement and its GL posting committing as one unit (`T-1.INV.07`), an endpoint through the app, a webhook replayed twice (`T-0.INT.01`). Real transaction, real constraints — never a mock of the store the rule depends on. | a scratch database (the compose PostgreSQL) |
| **Ledger integrity** | Assertions over the **stored** ledger: every persisted `journal_entry` balances and has at least two lines, read from `journal_entry` / `journal_line` themselves. This is metric 1 of §6 and applies to every posting regardless of which module, path or phase wrote it. | the ledger tables, read directly |

## What belongs in each

- **Rules that more than one module depends on belong in the integrity layer, once** — not copied into each module's tests. Balance is the obvious case; company scoping (`T-0.CORE.03`) and append-only-ness (`T-0.AUDIT.01`) join it as their tasks land.
- **Nothing asserts balance from an API response.** A response proves what the caller was told, not what was stored; the integrity layer reads the tables.
- **A test that needs the database is an integration test**, even when it looks like a unit test.
- Run order: unit (fast, always) → integration → integrity. A failure in a lower layer makes the later ones meaningless.

## How a check is written in this repository (no frameworks yet)

- **One check per task** — the rule from the workspace skills. A check is a plain script under `tests/`, assert-based, with no pytest, no fixtures, no factories.
- It is runnable directly and exits non-zero on failure:
  `DATABASE_URL=postgresql+psycopg://… python tests/check_<something>.py`
- It proves the **rule**, not the implementation: the smallest thing that goes red when the logic breaks.
- A task whose deliverable is trivial (a one-line mapping, a constant) adds no check.

## The pipeline gate

`tests/check_ledger_integrity.py` is the gate:

- `ledger_gate(connection)` scans the whole ledger and returns `1` after reporting every entry that does not balance or has fewer than two lines; `0` when the ledger is clean. A run that starts against a broken ledger fails — that is the CI gate.
- The same script proves it can fail: it injects a deliberately unbalanced entry, and an entry with no lines, with the tables' triggers disabled (the way a restored dump or a hand-edit arrives), asserts the gate reports each — and rolls both back, so the scratch ledger is left clean.
- Why it exists at all, given `T-0.CORE.01`'s deferred constraint triggers already refuse an unbalanced posting: the triggers are the guard, this is the **audit**. A ledger reached by a path the triggers did not cover — a restore, a manual `UPDATE`, a dropped trigger — is exactly what metric 1 needs to catch, and only a scan of the stored rows can.
- What it deliberately does **not** do: it does not check API responses, and it does not assert the balance of a document mid-transaction (the triggers are deferred to COMMIT by design).

## Wiring

- The pipeline that runs these layers on every change is `.github/workflows/backend.yml`
  (`T-0.CICD.01`). Its `checks` job starts a `postgres:16` service, installs
  `requirements-dev.lock` (this repository's app dependencies plus the `httpx` the
  API-level checks use), runs **every** `tests/check_*.py` and then the gate as its own
  final step — the run order above, enforced by the job's step order rather than by
  convention. The loop means a check that lands tomorrow is wired in without editing the
  workflow; two checks are excluded by name, each with its reason in the file
  (`check_backend_image.py` builds the image, which the `publish` job does for real;
  `check_compose_stack.py` needs the frontend repository's image too).
- A failing check fails the run, and the `publish` job needs that run, so no image is
  published from a red commit. The same applies to the on-demand `deploy` job.
- **Requiring the check on `main` is a repository setting, not a file.** Marking the
  `checks` job as a required status check in branch protection is what turns a red run
  into a blocked merge; the workflow can only make the run fail.
- Locally, the checks run by hand with the command above; run them against a scratch
  PostgreSQL, never against a database whose ledger matters.
