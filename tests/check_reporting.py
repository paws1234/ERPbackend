"""T-0.REPORT.01 check — a scheduled report runs for the right caller and failure shows.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/postgres \
        python tests/check_reporting.py

It fails (non-zero exit) if any of these stops holding:

1. a registered report is selected when its schedule fires — at 06:30 for
   ``30 6 * * *`` and not a minute later
2. it is produced and delivered: the run is recorded and every recipient's send
   is in T-0.INT.01's delivery log
3. a run is scoped: a caller without the report's capability is refused and the
   refusal is on the trail, and another company's reports are never selected
4. a failure is visible rather than silent — a builder that raises, and a report
   with no builder at all, both leave a `failed` run carrying the reason

**Scratch database only**: it drops and recreates the schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.audit import read_trail  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base, scope_to_company  # noqa: E402
from app.integrations import SENT, deliveries_for, register_transport  # noqa: E402
from app.ledger import posting  # noqa: E402,F401 — every check builds the one schema
from app.reporting import (  # noqa: E402
    FAILED,
    OK,
    ScheduleError,
    due,
    register,
    register_builder,
    run,
    runs_for,
)
from app.security import REFUSED, PermissionDenied, assign, define_role, grant  # noqa: E402

WHEN = datetime(2026, 9, 17, 6, 30, tzinfo=timezone.utc)
ALICE, BOB = "alice", "bob"
RECIPIENTS = ["cfo@example.com", "controller@example.com"]


class BuilderFailed(RuntimeError):
    """What a report does when the data underneath it is not ready."""


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is required (a scratch Postgres)", file=sys.stderr)
        return 2

    engine = create_engine(url)
    # The checks share one scratch database, so reset the schema rather than only
    # the tables this file imports: a table another module added keeps a foreign
    # key on `company` and would block the rebuild.
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    Base.metadata.create_all(engine)

    delivered: list[tuple[str, str]] = []
    register_transport("email", lambda destination, payload: delivered.append((destination, payload["report"])))

    alpha, beta = uuid.uuid4(), uuid.uuid4()
    with Session(engine) as session:
        for company_id, code in ((alpha, "REPORT-ALPHA"), (beta, "REPORT-BETA")):
            session.add(
                Company(
                    id=company_id,
                    code=code,
                    name=f"{code} check",
                    base_currency="PHP",
                    fiscal_year_start_month=1,
                )
            )
        session.commit()

        role = define_role(session, company_id=alpha, code="finance", name="Finance")
        grant(session, role, "report.read")
        assign(session, company_id=alpha, subject=ALICE, role=role)
        assign(session, company_id=alpha, subject=BOB, role=role)
        session.commit()

        # A malformed schedule is refused at registration, not at 6:30 in the morning.
        try:
            register(
                session,
                company_id=alpha,
                code="trial_balance",
                name="Trial Balance",
                schedule="30 6 * *",
                recipients=RECIPIENTS,
            )
        except ScheduleError as exc:
            assert "five-field" in str(exc), f"unclear error: {exc}"
        else:
            raise AssertionError("a four-field schedule was accepted")

        balance = register(
            session,
            company_id=alpha,
            code="trial_balance",
            name="Trial Balance",
            schedule="30 6 * * *",
            recipients=RECIPIENTS,
        )
        broken = register(
            session,
            company_id=alpha,
            code="garble_report",
            name="A report nobody built",
            schedule="30 6 * * *",
            recipients=RECIPIENTS,
        )
        register(
            session,
            company_id=beta,
            code="trial_balance",
            name="Trial Balance (beta)",
            schedule="30 6 * * *",
            recipients=["beta@example.com"],
        )
        register_builder(
            "trial_balance",
            lambda session, definition: {"rows": 3, "debits": "100.00", "credits": "100.00"},
        )
        session.commit()

        # 1 — the schedule selects the report at its minute, and only then
        assert [d.code for d in due(session, company_id=alpha, now=WHEN)] == [
            "trial_balance",
            "garble_report",
        ]
        later = WHEN.replace(minute=31)
        assert [d.code for d in due(session, company_id=alpha, now=later)] == [], (
            "the report fired off its minute"
        )
        # 3 — another company's report is never selected for this one
        assert all(d.company_id == alpha for d in due(session, company_id=alpha, now=WHEN))
        print("the scheduled report is selected at 06:30, not at 06:31, and only for its company")

        # 2 — produced and delivered
        run_row = run(session, balance, actor=ALICE, now=WHEN)
        session.commit()
        assert run_row.status == OK and run_row.produced["rows"] == 3, run_row.error
        assert run_row.delivered_to == RECIPIENTS
        log = deliveries_for(session, company_id=alpha, channel="email")
        assert sorted(row.destination for row in log) == sorted(RECIPIENTS), (
            "a recipient's delivery is not in the boundary's log"
        )
        assert all(row.status == SENT for row in log)
        print(f"the run delivered to {len(log)} recipients, each send logged")

        # 3 — a caller without the capability is refused, and it is on the trail
        ops = define_role(session, company_id=alpha, code="ops", name="Operations")
        assign(session, company_id=alpha, subject="carol", role=ops)
        session.commit()
        with Session(engine) as outsider:
            scope_to_company(outsider, alpha)
            try:
                run(outsider, balance, actor="carol", now=WHEN)
            except PermissionDenied as exc:
                assert "report.read" in str(exc), f"unclear refusal: {exc}"
            else:
                raise AssertionError("a caller with no capability ran the report")
        with Session(engine) as session4:
            scope_to_company(session4, alpha)
            refusals = [row for row in read_trail(session4) if row.action == REFUSED]
            assert [row.actor for row in refusals] == ["carol"], "the refusal is not attributable"
            assert refusals[0].after_values["attempted"] == "report.read"
        print("a caller without the capability is refused, and the refusal is on the trail")

        # 4 — both kinds of failure leave a visible run
        unbuilt = run(session, broken, actor=ALICE, now=WHEN)
        session.commit()
        assert unbuilt.status == FAILED and "no builder is registered" in unbuilt.error

        def explode(session: Session, definition) -> dict:
            raise BuilderFailed("the ledger is empty")

        register_builder("explodes", explode)
        failing = register(
            session,
            company_id=alpha,
            code="explodes",
            name="A report that fails",
            schedule="0 7 * * *",
            recipients=RECIPIENTS,
        )
        session.commit()
        failed = run(session, failing, actor=ALICE, now=WHEN)
        session.commit()
        assert failed.status == FAILED and "the ledger is empty" in failed.error
        assert failed.finished_at is not None and failed.requested_by == ALICE
        statuses = [row.status for row in runs_for(session, company_id=alpha, definition=failing)]
        assert statuses == [FAILED], statuses
        print(f"a failing builder leaves a visible run: {failed.error}")

    engine.dispose()
    print("ok — reports are scheduled, scoped and delivered, and a failure is a row")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
