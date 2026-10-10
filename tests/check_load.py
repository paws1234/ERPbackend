"""T-6.HARD.01 check — the load harness, the recorded figures, and how a miss is recorded.

    DATABASE_URL=postgresql+psycopg://erpv1:erpv1@localhost:5432/erpv1 \
        python tests/check_load.py

Green on all six:

1. **the five paths §4 names are the ones measured**, each with a stated target, and the POS
   target is the plan's own figure (§6 metric 6, < 2 s) rather than one this task invented
2. **a stated dataset and load produce a figure per path** — every path is measured
   `repeats × concurrency` times on separate connections, and the distribution (not one
   average) is what the report carries
3. **POS checkout is within the budget at that load** — measured, not assumed, at the
   concurrency the run states
4. **a path over its target is a finding with its cause** — a real path made genuinely slow
   is recorded with the path, the measured 95th percentile, the target and the cause; and a
   miss with no cause stated says exactly that rather than passing silently
5. **`LOAD-TEST.md` holds the figures** — the command, the dataset, the concurrency and one
   line per named path, so the numbers are reproducible and the report is not a memory
6. **the harness refuses what it cannot measure** — a path that is not one of the named five,
   and a run with no repeat or no worker, are refused by name; a worker's fault is raised
   rather than counted as a fast sample

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import Base  # noqa: E402
from app.ledger.posting import post_journal_entry  # noqa: E402
from tools.load_test import (  # noqa: E402
    COMPANY as LOAD_COMPANY,
)
from tools.load_test import (  # noqa: E402
    DESCRIPTIONS,
    POS_BUDGET_MS,
    TARGETS_MS,
    UNSTATED,
    build_dataset,
    findings,
    measure,
    paths_for,
    run_load,
)

RECORD = ROOT / "LOAD-TEST.md"
CHEAP = {"items": 12, "postings": 60}
REPEATS = 2
CONCURRENCY = 2
HEAVY_POSTINGS = 120


def _refused(call, expected: type[Exception]) -> str:
    try:
        call()
    except expected as exc:  # noqa: BLE001 — the type and the message are the point
        return str(exc)
    raise AssertionError(f"accepted what it must refuse ({expected.__name__})")


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is required (a scratch Postgres)", file=sys.stderr)
        return 2

    engine = create_engine(url)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        dataset = build_dataset(session, items=CHEAP["items"], postings=CHEAP["postings"])
        paths = paths_for(session)

    # 1 — the paths the plan names, and the one target the plan states
    assert set(paths) == set(TARGETS_MS), (sorted(paths), sorted(TARGETS_MS))
    assert set(DESCRIPTIONS) == set(TARGETS_MS), "a path has no description"
    assert POS_BUDGET_MS == 2000.0, POS_BUDGET_MS
    assert TARGETS_MS["pos_checkout"] == POS_BUDGET_MS, "the POS target is not §6's figure"
    print(
        f"1. the five paths §4 names are measured — {', '.join(sorted(paths))} — each with a"
        f" target in TARGETS_MS, and pos_checkout's is the plan's own"
        f" ({TARGETS_MS['pos_checkout']:.0f} ms, §6 metric 6)"
    )

    # 2 and 3 — a stated dataset and load, measured for real
    report = run_load(
        paths,
        engine=engine,
        dataset={**dataset, "concurrency": CONCURRENCY, "repeats": REPEATS},
        repeats=REPEATS,
        concurrency=CONCURRENCY,
    )
    assert len(report["paths"]) == len(paths), report["paths"]
    for row in report["paths"]:
        assert row["runs"] == REPEATS * CONCURRENCY, row
        assert row["min_ms"] <= row["median_ms"] <= row["max_ms"], row
        assert row["p95_ms"] <= row["max_ms"], row
        assert row["target_ms"] == TARGETS_MS[row["path"]], row
    checkout = [row for row in report["paths"] if row["path"] == "pos_checkout"][0]
    assert checkout["p95_ms"] <= POS_BUDGET_MS, checkout
    print(
        f"2. {CHEAP['items']} items and {CHEAP['postings']} postings in the ledger,"
        f" {CONCURRENCY} workers × {REPEATS} repeats: every path measured"
        f" {checkout['runs']} times with its own distribution (posting median"
        f" {[r for r in report['paths'] if r['path'] == 'posting'][0]['median_ms']:.0f} ms,"
        f" statement median"
        f" {[r for r in report['paths'] if r['path'] == 'statement_generation'][0]['median_ms']:.0f} ms)"
    )
    print(
        f"3. POS checkout at that load: p95 {checkout['p95_ms']:.0f} ms of the"
        f" {POS_BUDGET_MS:.0f} ms budget (max {checkout['max_ms']:.0f} ms over"
        f" {checkout['runs']} sales)"
    )

    # 4 — a miss is a recorded finding, with its cause or without one that was never stated
    def slow_posting(worker: Session) -> None:
        """A real path, made genuinely slow: the recorder has to catch what it does not like."""
        for index in range(HEAVY_POSTINGS):
            post_journal_entry(
                worker,
                company_id=LOAD_COMPANY,
                posting_date=date(2026, 6, 1),
                currency="PHP",
                memo=f"slow {index}",
                source_type="load_test",
                source_id=uuid.uuid4(),
                lines=[
                    {"account": "1000", "debit": Decimal("1.25")},
                    {"account": "4000", "credit": Decimal("1.25")},
                ],
            )
        worker.commit()

    slow = run_load(
        {"posting": slow_posting},
        engine=engine,
        dataset={**dataset, "concurrency": CONCURRENCY, "repeats": REPEATS},
        repeats=REPEATS,
        concurrency=CONCURRENCY,
        causes={"posting": f"{HEAVY_POSTINGS} postings in one sample, on purpose"},
    )
    row = slow["paths"][0]
    assert row["within_target"] is False, row
    assert row["p95_ms"] > TARGETS_MS["posting"], row
    found = slow["findings"]
    assert len(found) == 1 and found[0]["path"] == "posting", found
    assert found[0]["target_ms"] == TARGETS_MS["posting"], found[0]
    assert found[0]["measured_ms"] == row["p95_ms"], found[0]
    assert found[0]["cause"] == f"{HEAVY_POSTINGS} postings in one sample, on purpose", found[0]
    unstated = findings(slow["paths"], {})
    assert unstated[0]["cause"] == UNSTATED, unstated
    print(
        f"4. a path made genuinely slow is a finding with its figures"
        f" ({found[0]['measured_ms']:.0f} ms against {found[0]['target_ms']:.0f} ms) and the"
        f" cause it was recorded with; with nobody's cause written down it says"
        f" '{UNSTATED}' — recorded as open, never as a pass"
    )

    # 5 — the recorded report is the repository's own figures
    recorded = RECORD.read_text()
    assert "python tools/load_test.py" in recorded, "the report does not name its command"
    assert "concurrency" in recorded and "repeats per worker" in recorded, (
        "the load is not stated"
    )
    for name in TARGETS_MS:
        assert f"`{name}`" in recorded, f"{name} has no line in the recorded report"
    assert "ms" in recorded and "95th percentile" in recorded, "the figures are not the point"
    print(
        f"5. LOAD-TEST.md names the command, the dataset and the load, and gives each of the"
        f" {len(TARGETS_MS)} paths a line ({RECORD.stat().st_size} bytes)"
    )

    # 6 — the harness refuses what it cannot measure
    made_up = _refused(
        lambda: run_load(
            {"latency": lambda worker: None},
            engine=engine,
            dataset={},
            repeats=1,
            concurrency=1,
        ),
        ValueError,
    )
    assert "not one of the paths" in made_up, made_up
    no_repeat = _refused(
        lambda: measure(lambda worker: None, engine=engine, repeats=0, concurrency=1),
        ValueError,
    )
    assert "at least one repeat" in no_repeat, no_repeat
    no_worker = _refused(
        lambda: measure(lambda worker: None, engine=engine, repeats=1, concurrency=0),
        ValueError,
    )
    assert "one worker" in no_worker, no_worker

    boom = _refused(
        lambda: measure(
            lambda worker: (_ for _ in ()).throw(RuntimeError("the path is broken")),
            engine=engine,
            repeats=1,
            concurrency=1,
        ),
        RuntimeError,
    )
    assert "the path is broken" in boom, boom
    print(
        f"6. the harness refused an unnamed path ('{made_up}'), a run with no repeat"
        f" ('{no_repeat}') and one with no worker ('{no_worker}'), and a worker's own fault"
        f" came back as '{boom}' rather than as a fast sample"
    )

    print("\ncheck_load: all assertions green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
