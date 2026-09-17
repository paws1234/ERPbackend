"""T-0.WF.01 check — a document follows exactly the chain it was given.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/postgres \
        python tests/check_approval_engine.py

It fails (non-zero exit) if any of these stops holding:

1. the chain is configuration, not code: two document types — one of them a name
   no module mentions — are configured and driven end to end without a code
   change
2. a document follows exactly the configured chain: a large document waits for
   both levels and cannot progress on the first approval alone, and the second
   level's role cannot decide the first
3. an amount that reaches no level needs no approval
4. a document type with no configured chain is refused, never waved through
5. rejection and return are recorded with their actor and reason, and both end
   the request — a finished request takes no further decision
6. the decision history is immutable: the database refuses to change or remove it

**Scratch database only**: it drops and recreates the schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from decimal import Decimal

from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger import posting  # noqa: E402,F401 — every check builds the one schema
from app.workflow import (  # noqa: E402
    APPROVED,
    PENDING,
    REJECTED,
    RETURNED,
    NoWorkflowConfigured,
    RequestNotPending,
    WorkflowAlreadyConfigured,
    WorkflowError,
    WrongApprover,
    configure,
    decide,
    start_approval,
)

# Two chains, both pure data. "leave_request" is deliberately a document type no
# code in this repository mentions: configuring it proves the engine does not
# enumerate document types in code.
PO_LEVELS = [(Decimal("1000"), "manager"), (Decimal("10000"), "director")]
LEAVE_LEVELS = [(Decimal("0"), "team_lead")]


def _refused(call, expected, kind=WorkflowError) -> str:
    """The message if `call` is refused as expected; fail the check otherwise."""
    try:
        call()
    except kind as exc:
        assert expected in str(exc), f"unclear error: {exc}"
        return str(exc)
    raise AssertionError(f"accepted what it must refuse ({expected!r})")


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

    company_id = uuid.uuid4()
    with Session(engine) as session:
        session.add(
            Company(
                id=company_id,
                code="WF-CHECK",
                name="Workflow check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()

        # 1 — the chain is configuration
        configure(
            session,
            company_id=company_id,
            doc_type="purchase_order",
            name="Purchase orders",
            levels=PO_LEVELS,
        )
        configure(
            session,
            company_id=company_id,
            doc_type="leave_request",
            name="Leave requests",
            levels=LEAVE_LEVELS,
        )
        _refused(
            lambda: configure(
                session,
                company_id=company_id,
                doc_type="purchase_order",
                name="A second chain",
                levels=PO_LEVELS,
            ),
            "already has an approval chain",
            WorkflowAlreadyConfigured,
        )
        session.commit()
        print("two document types configured as data, one of which no module mentions")

        # 4 — an unconfigured document type is refused, not waved through
        message = _refused(
            lambda: start_approval(
                session,
                company_id=company_id,
                doc_type="payment_batch",
                document_id="PB-1",
                amount=Decimal("5000"),
            ),
            "no approval chain is configured",
            NoWorkflowConfigured,
        )
        print(f"an unconfigured document type is refused: {message}")

        # 3 — below every threshold, nothing to approve
        assert (
            start_approval(
                session,
                company_id=company_id,
                doc_type="purchase_order",
                document_id="PO-SMALL",
                amount=Decimal("500"),
            )
            is None
        ), "a document below every threshold was routed for approval"
        print("an amount that reaches no level needs no approval")

        # 2 — the large document waits for both levels, in order
        po = start_approval(
            session,
            company_id=company_id,
            doc_type="purchase_order",
            document_id="PO-2026-0001",
            amount=Decimal("50000"),
        )
        session.commit()
        assert po is not None and po.state == PENDING and po.current_level == 1
        _refused(
            lambda: decide(session, po, actor="dana", action="approve", role="director"),
            "is decided by 'manager', not 'director'",
            WrongApprover,
        )
        session.rollback()
        po = session.get(type(po), po.id)
        decide(session, po, actor="mara", action="approve", role="manager")
        session.commit()
        assert po.state == PENDING, "the first approval released the document"
        assert po.current_level == 2, "the document did not advance to the second level"
        print("after level 1 the document is still waiting, now at level 2")

        decide(session, po, actor="dana", action="approve", role="director")
        session.commit()
        assert po.state == APPROVED, "the chain did not finish at its last level"
        assert [(d.level_no, d.action, d.actor) for d in po.decisions] == [
            (1, "approve", "mara"),
            (2, "approve", "dana"),
        ], "the decisions do not follow the configured chain"
        print("both levels decided in order — the document is approved")

        # 5 — rejection and return carry their actor and reason, and end the run
        rejected = start_approval(
            session,
            company_id=company_id,
            doc_type="purchase_order",
            document_id="PO-2026-0002",
            amount=Decimal("2000"),
        )
        session.commit()
        assert rejected.current_level == 1
        _refused(
            lambda: decide(session, rejected, actor="mara", action="reject", role="manager"),
            "needs a reason",
        )
        decide(
            session,
            rejected,
            actor="mara",
            action="reject",
            role="manager",
            reason="no budget this quarter",
        )
        session.commit()
        assert rejected.state == REJECTED
        refusal = rejected.decisions[-1]
        assert (refusal.actor, refusal.reason) == ("mara", "no budget this quarter")
        _refused(
            lambda: decide(session, rejected, actor="dana", action="approve", role="director"),
            "takes no further decisions",
            RequestNotPending,
        )
        session.rollback()
        print(f"rejected by {refusal.actor}: {refusal.reason!r}")

        leave = start_approval(
            session,
            company_id=company_id,
            doc_type="leave_request",
            document_id="LV-7",
            amount=Decimal("0"),
        )
        session.commit()
        decide(session, leave, actor="lito", action="return", role="team_lead", reason="dates clash")
        session.commit()
        assert leave.state == RETURNED and leave.decisions[-1].reason == "dates clash"
        print("a returned request is recorded with its reason")

    # 6 — the history is immutable
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT count(*) FROM approval_decision").scalar() > 0
        for statement in (
            "UPDATE approval_decision SET actor = 'somebody.else'",
            "DELETE FROM approval_decision",
        ):
            try:
                result = conn.exec_driver_sql(statement)
                conn.commit()
            except DBAPIError as exc:
                message = str(exc.orig).strip()
                assert "append-only" in message, f"unclear database error: {message}"
                conn.rollback()
            else:
                raise AssertionError(
                    f"the history accepted {statement!r} ({result.rowcount} rows matched)"
                )
        print("the decision history refuses UPDATE and DELETE")

    engine.dispose()
    print("ok — the chain is configuration, a document follows it exactly, and its history stands")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
