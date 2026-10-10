"""T-6.X.GATE — the Phase 6 exit criteria, each held to the check that owns it.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_phase6_exit.py

§4 Phase 6 stated no exit criteria until 2026-09-17. The eleven that were added are restated
here as checks: this file adds no feature, it runs **the check that owns each criterion** and
requires it green, then prints the criterion with the line that check ends on — so a criterion
whose verification has disappeared, or whose figures have drifted, fails **by name** instead of
passing by omission. §6 metrics 1–5 are then re-measured after Phase 6's changes, and two
artifacts the criteria produce are read back against the live system rather than trusted:

1. advanced MRP/capacity is capacity-respecting and deterministic → `check_advanced_planning.py`
2. forward and backward trace crosses a production step → `check_trace.py`
3. a supplier transacts through the portal, isolated from other suppliers → `check_supplier_portal.py`
4. offline POS sells without connectivity, synchronises exactly once, reports its differences →
   `check_pos_offline.py`
5. biometric events ingest without duplication and report unmapped users → `check_biometric.py`
6. dashboards reconcile with one-step drill-down and the catalogue reports on schedule →
   `check_dashboard.py`, `check_catalogue.py`
7. performance is measured on the named paths and POS is inside the < 2 s budget at load →
   `check_load.py`, `check_pos_latency.py`, and `LOAD-TEST.md` read back against `tools/load_test.py`'s
   own targets
8. the security review covers every module with no unguarded endpoint →
   `check_security_review.py`, and `SECURITY-REVIEW.md`'s matrix read back against the live app's
   routes one for one, with every finding stating `remediated` or `accepted risk`
9. every confirmed market's pack is complete and imports cleanly → `check_localization_packs.py`
10. audit coverage is verified for every master and transaction → `check_audit_coverage.py`
11. §6 metrics 1–5 re-measured and still met → the checks that measure them (the ledger scan,
    stock-to-GL, the Phase 2 exit gate's match rate, the MRP hand calculation, the payroll error
    rate), each printed with the figure it measured rather than with "green"

**What it deliberately does not do when the frontend repository is absent.** Four criteria have a
frontend half (the portal client, the offline POS queue, the dashboard and its drill-down). When
the sibling checkout is present those files are required and the frontend repository's own
`npm run check` is run; when it is not — as in the backend pipeline — the frontend halves are
that repository's own pipeline, exactly as `check_compose_stack.py` is excluded there for the
same reason.

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
FRONTEND = ROOT.parent / "ERPfrontend"

sys.path.insert(0, str(ROOT))

# The plan's POS latency budget (§6 metric 6, `pos_latency_budget`).
POS_LATENCY_BUDGET_MS = 2000.0

# Each exit criterion with the check that owns it and the id that check verifies — so a
# criterion whose verification is deleted fails with the file it is missing.
CRITERIA: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "advanced MRP/capacity is capacity-respecting and deterministic",
        [("check_advanced_planning.py", "T-6.ADV.01")],
    ),
    (
        "forward and backward trace crosses a production step",
        [("check_trace.py", "T-6.TRACE.03")],
    ),
    (
        "a supplier transacts through the portal, isolated from other suppliers",
        [("check_supplier_portal.py", "T-6.PORTAL.01")],
    ),
    (
        "offline POS sells without connectivity, synchronises exactly once and reports its"
        " differences",
        [("check_pos_offline.py", "T-6.OFFLINE.01")],
    ),
    (
        "biometric events ingest without duplication and report unmapped users",
        [("check_biometric.py", "T-6.OFFLINE.02")],
    ),
    (
        "dashboards reconcile with one-step drill-down and the catalogue reports on schedule",
        [
            ("check_dashboard.py", "T-6.ANALYTICS.01"),
            ("check_catalogue.py", "T-6.ANALYTICS.02"),
        ],
    ),
    (
        "performance is measured on the named paths and POS is inside its budget at load",
        [("check_load.py", "T-6.HARD.01"), ("check_pos_latency.py", "T-3.POS.06")],
    ),
    (
        "the security review covers every module with no unguarded endpoint",
        [("check_security_review.py", "T-6.HARD.02")],
    ),
    (
        "every confirmed market's pack is complete and imports cleanly",
        [("check_localization_packs.py", "T-6.HARD.03")],
    ),
    (
        "audit coverage is verified for every master and transaction with no gap",
        [("check_audit_coverage.py", "T-6.HARD.04")],
    ),
]

# §6 metrics 1–5, each with the check that measures it and the line that states the figure
# (metric 6 is the POS budget above).
METRICS: list[tuple[str, str, str, str]] = [
    (
        "1 — every stored posting balances and has at least two lines",
        "check_ledger_integrity.py",
        "T-0.CORE.02",
        "clean ledger",
    ),
    (
        "2 — the stock valuation reconciles to the general ledger",
        "check_stock_to_gl.py",
        "T-1.INV.07",
        "= GL",
    ),
    (
        "3 — the three-way match rate is above the 95 % target",
        "check_phase2_exit.py",
        "T-2.X.GATE",
        "the rate is measured",
    ),
    (
        "4 — the MRP plan is accurate on the verification dataset",
        "check_mrp_accuracy.py",
        "T-4.MRP.03",
        "the plan matches the hand calculation",
    ),
    (
        "5 — the payroll error rate is inside the < 0.1 % target",
        "check_payroll_accuracy.py",
        "T-5.PAY.08",
        "measured error rate",
    ),
]

# The frontend halves of the criteria that span both repositories, and the pipeline that
# verifies them in their own repository.
FRONTEND_HALVES = (
    "app/portal/page.tsx",
    "lib/offline.ts",
    "app/pos/offline.tsx",
    "lib/dashboard.ts",
    "app/dashboard/page.tsx",
)


def _run(check: str, expected: str, marker: str | None = None) -> str:
    """Run one check in its own process and return the line that states its result.

    A missing check, a check that no longer names the id it verifies, or a check that fails all
    end the gate — the criterion is not carried by a passing suite but by a verification that
    is still there. `marker` picks the line to report when a check prints its own falsification
    after its result (the ledger gate ends on an injected failure by design).
    """
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
    # The check's own conclusion, not the interpreter's warning summary it may end on.
    printed = [line for line in result.stdout.strip().splitlines() if line.strip()]
    if marker is not None:
        marked = [line for line in printed if marker in line]
        assert marked, f"tests/{check} printed no line with {marker!r}"
        return marked[-1]
    return printed[-1] if printed else "(no output)"


def _load_report() -> tuple[dict[str, float], str]:
    """The p95 each named path is recorded at in `LOAD-TEST.md`, and the file's text."""
    text = (ROOT / "LOAD-TEST.md").read_text()
    recorded: dict[str, float] = {}
    for line in text.splitlines():
        if not line.startswith("| `"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        recorded[cells[0].strip("`")] = float(cells[5].split()[0])
    return recorded, text


def _tool_targets() -> dict[str, float]:
    """The targets the load tool itself states, so the report and the tool are one statement."""
    source = (ROOT / "tools" / "load_test.py").read_text()
    block = source.split("TARGETS_MS: dict[str, float] = {", 1)[1].split("}", 1)[0]
    return {
        name: float(value)
        for name, value in re.findall(r'"([a-z_]+)": ([\d.]+)', block)
    }


def _reviewed_routes() -> set[str]:
    """Every `METHOD /path` the security review's access matrix states."""
    text = (ROOT / "SECURITY-REVIEW.md").read_text()
    routes = set()
    for line in text.splitlines():
        if not line.startswith("| `"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        routes.add(cells[0].strip("`"))
    return routes


def _live_routes() -> set[str]:
    """Every `METHOD /path` the app actually serves."""
    from fastapi.routing import APIRoute

    from app.api import app

    return {
        f"{method} {route.path}"
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }


def _findings() -> list[str]:
    """The disposition each finding in the review states (`Finding | Disposition | Why`)."""
    text = (ROOT / "SECURITY-REVIEW.md").read_text()
    body = text.split("## Findings", 1)[1].split("\n## ", 1)[0]
    rows = [
        [cell.strip() for cell in line.strip("|").split("|")]
        for line in body.splitlines()
        if line.startswith("| ") and not line.startswith("| Finding ")
    ]
    return [row[1] for row in rows if len(row) >= 3 and set(row[1]) != {"-"}]


def main() -> int:
    if not os.environ.get("DATABASE_URL"):
        print("DATABASE_URL is required (a scratch Postgres)", file=sys.stderr)
        return 2

    # 1–10 — each criterion, held to its own check
    for criterion, owners in CRITERIA:
        ends = [(_run(check, expected), check) for check, expected in owners]
        print(f"{criterion}")
        for line, check in ends:
            print(f"    {check}: {line[:150]}")
    # 7 — the load report read back against the tool's own targets and the plan's POS budget
    recorded, report = _load_report()
    targets = _tool_targets()
    assert set(recorded) == set(targets), (
        f"the report names {sorted(recorded)} and the tool states {sorted(targets)}"
    )
    assert "items in the catalogue" in report and "postings already in the ledger" in report, (
        "the report does not state the dataset its figures belong to"
    )
    over = {
        path: (recorded[path], targets[path])
        for path in targets
        if recorded[path] > targets[path]
    }
    assert not over, f"named paths outside their target: {over}"
    assert recorded["pos_checkout"] < POS_LATENCY_BUDGET_MS, recorded["pos_checkout"]
    print(
        "the load report, measured by tools/load_test.py and read back against its own targets:"
    )
    for path, target in targets.items():
        print(f"    {path:20} p95 {recorded[path]:>8.0f} ms of {target:>8.0f} ms")

    # 8 — the access matrix read back against the app's own routes, and every finding verdict
    reviewed, live = _reviewed_routes(), _live_routes()
    assert reviewed == live, (
        f"the review does not cover the app's routes: unreviewed {sorted(live - reviewed)},"
        f" stated but absent {sorted(reviewed - live)}"
    )
    verdicts = _findings()
    assert verdicts, "the review states no findings"
    unstated = [verdict for verdict in verdicts if verdict not in ("remediated", "accepted risk")]
    assert not unstated, f"findings with no verdict: {unstated}"
    print(
        f"the access matrix covers all {len(live)} routes the app serves, one for one, and all"
        f" {len(verdicts)} findings state a verdict ({verdicts.count('remediated')} remediated,"
        f" {verdicts.count('accepted risk')} accepted risk)"
    )

    # the frontend halves of the criteria that span both repositories
    if FRONTEND.is_dir():
        missing = [half for half in FRONTEND_HALVES if not (FRONTEND / half).exists()]
        assert not missing, f"the frontend half of a criterion is missing: {missing}"
        assert (FRONTEND / "node_modules").is_dir(), (
            "the frontend repository is present but not installed; run its own `npm run check`"
        )
        result = subprocess.run(
            ["npm", "run", "check"],
            cwd=str(FRONTEND),
            env=os.environ,
            capture_output=True,
            text=True,
        )
        lines = (result.stdout + result.stderr).strip().splitlines()
        assert result.returncode == 0, "the frontend's own check failed:\n" + "\n".join(
            lines[-25:]
        )
        print(
            f"the frontend half of the portal, offline POS and dashboard criteria:"
            f" {len(FRONTEND_HALVES)} artifacts present and `npm run check` green"
        )
    else:
        print(
            f"the frontend repository is not checked out here ({FRONTEND}); the portal client,"
            " the offline POS queue and the dashboard are that repository's own pipeline"
            " (`npm run check`), as check_compose_stack.py states for the stack"
        )

    # 11 — §6 metrics 1–5, re-measured after Phase 6
    print("§6 metrics 1–5, re-measured after Phase 6 (metric 3's line states both the measured")
    print("month and the deliberate miss its check drives to prove the rate can fail):")
    for metric, check, expected, marker in METRICS:
        print(
            f"    metric {metric}\n        {check}: {_run(check, expected, marker)[:150]}"
        )

    print(
        "ok — Phase 6's exit criteria: every one held to the check that owns it, the metrics"
        " re-measured, and the load report and access matrix read back against the system"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
