"""T-2.PROC.02 check — purchase requisitions through the configured approval chain.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/erpv1 \
        python tests/check_requisition_approval.py

Green on all eight:

1. a draft requisition with lines totals exactly `quantity × estimated unit price`,
   at money scale
2. an empty requisition cannot be submitted; a blank description, a zero quantity, a
   negative price, a repeated number and an unregistered currency are all refused
3. a requisition above the top threshold is routed to a **two-level** chain and
   cannot be sourced until the **last** level approves — blocked after the first
4. the engine's own rules still hold: a decision from the wrong role is refused, and a
   rejection without a reason is refused — while a rejection with one closes it
5. a requisition that reaches **no** configured level is approved on submission (the
   engine's answer, not an invented rule), and a document type with **no chain** is
   refused rather than waved through
6. an approved requisition is **frozen** — a line cannot be added — and revising it
   raises a new draft that carries the same lines while the original keeps its
   approval and its history
7. the approval history is complete and in order, read from the engine's own
   append-only decisions
8. a revision cannot be raised from a draft, and a document that already has a
   revision is not revised twice

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger.currency import UnknownCurrencyError, register_currency  # noqa: E402
from app.procurement.requisitions import (  # noqa: E402
    APPROVED,
    PENDING,
    REJECTED,
    RETURNED,
    DuplicateRequisitionError,
    IncompleteRequisitionError,
    NotApprovedError,
    Requisition,
    RequisitionLockedError,
    RequisitionStateError,
    add_line,
    approval_history,
    create_requisition,
    record_decision,
    require_sourceable,
    requisition_total,
    revise,
    submit,
)
from app.workflow import (  # noqa: E402
    APPROVE,
    NoWorkflowConfigured,
    REJECT,
    RETURN,
    WrongApprover,
    WorkflowError,
    configure,
)

COMPANY = uuid.uuid4()
UNCONFIGURED = uuid.uuid4()
NEEDED_BY = date(2026, 10, 31)
LEVELS = [(Decimal("1000"), "manager"), (Decimal("10000"), "director")]


def _refused(call, expected: type[Exception] | str) -> str:
    try:
        call()
    except Exception as exc:  # noqa: BLE001 — the type and the message are the point
        if isinstance(expected, str):
            assert expected in str(exc), f"unclear refusal: {exc}"
        else:
            assert isinstance(exc, expected), f"refused with {type(exc).__name__}: {exc}"
        return str(exc)
    raise AssertionError("accepted what it must refuse")


def _requisition(session, company_id, number, lines, *, needed_by=NEEDED_BY):
    return create_requisition(
        session,
        company_id=company_id,
        number=number,
        requested_by="rina.requester",
        needed_by=needed_by,
        currency="PHP",
        lines=lines,
    )


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
        for company_id, code in ((COMPANY, "REQ-CHECK"), (UNCONFIGURED, "NO-CHAIN")):
            session.add(
                Company(
                    id=company_id,
                    code=code,
                    name=f"{code} company",
                    base_currency="PHP",
                    fiscal_year_start_month=1,
                )
            )
            register_currency(session, company_id=company_id, code="PHP", name="Peso")
        session.commit()
        configure(
            session,
            company_id=COMPANY,
            doc_type="purchase_requisition",
            name="Purchase requisition",
            levels=LEVELS,
        )
        session.commit()

        # 1 — the lines and their exact total
        big = _requisition(
            session,
            COMPANY,
            "REQ-001",
            [
                {"description": "Laptops", "quantity": "25", "uom": "each",
                 "estimated_unit_price": "1000.00"},
                {"description": "Docking stations", "quantity": "3", "uom": "box",
                 "estimated_unit_price": "0.005"},
            ],
        )
        session.commit()
        assert requisition_total(big) == Decimal("25000.015000"), requisition_total(big)
        assert [line.line_no for line in big.lines] == [1, 2]
        print(f"1. REQ-001 totals {requisition_total(big)} from 25 × 1000.00 and 3 × 0.005")

        # 2 — nothing half-stated gets in
        said = _refused(
            lambda: _requisition(session, COMPANY, "REQ-001", []), DuplicateRequisitionError
        )
        session.rollback()
        said += " | " + _refused(
            lambda: add_line(session, big, description="  ", quantity="1", uom="each",
                             estimated_unit_price="1"),
            IncompleteRequisitionError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: add_line(session, big, description="Zero", quantity="0", uom="each",
                             estimated_unit_price="1"),
            IncompleteRequisitionError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: add_line(session, big, description="Negative", quantity="1", uom="each",
                             estimated_unit_price="-1"),
            IncompleteRequisitionError,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: create_requisition(
                session,
                company_id=COMPANY,
                number="REQ-BAD-CCY",
                requested_by="rina.requester",
                needed_by=NEEDED_BY,
                currency="GBP",
                lines=[],
            ),
            UnknownCurrencyError,
        )
        session.rollback()
        empty = _requisition(session, COMPANY, "REQ-EMPTY", [])
        session.commit()
        said += " | " + _refused(lambda: submit(session, empty, actor="rina.requester"),
                                 IncompleteRequisitionError)
        session.rollback()
        print(f"2. every half-stated requisition is refused: {said}")

        # 3 — a two-level chain, and sourcing blocked until the last level
        session.refresh(big)
        assert big.status == "draft", big.status
        submit(session, big, actor="rina.requester")
        session.commit()
        assert big.status == PENDING, big.status
        said = _refused(lambda: require_sourceable(session, big), NotApprovedError)
        session.rollback()
        record_decision(
            session, big, actor="mia.manager", action=APPROVE, role="manager"
        )
        session.commit()
        assert big.status == PENDING, f"level 1 approval finished the chain: {big.status}"
        still = _refused(lambda: require_sourceable(session, big), NotApprovedError)
        record_decision(
            session, big, actor="dan.director", action=APPROVE, role="director"
        )
        session.commit()
        assert big.status == APPROVED, big.status
        assert require_sourceable(session, big) is big
        print(f"3. REQ-001 blocked after level 1 ({said[:38]}… / {still[:30]}…) and sourceable"
              " only after level 2 approved")

        # 4 — the engine's own rules still apply
        noisy = _requisition(
            session, COMPANY, "REQ-002",
            [{"description": "Chairs", "quantity": "20", "uom": "each",
              "estimated_unit_price": "100"}],
        )
        submit(session, noisy, actor="rina.requester")
        session.commit()
        said = _refused(
            lambda: record_decision(session, noisy, actor="dan.director", action=APPROVE,
                                    role="director"),
            WrongApprover,
        )
        session.rollback()
        said += " | " + _refused(
            lambda: record_decision(session, noisy, actor="mia.manager", action=REJECT,
                                    role="manager"),
            WorkflowError,
        )
        session.rollback()
        record_decision(session, noisy, actor="mia.manager", action=REJECT, role="manager",
                        reason="budget freeze")
        session.commit()
        assert noisy.status == REJECTED, noisy.status
        said += " | " + _refused(lambda: require_sourceable(session, noisy), NotApprovedError)
        session.rollback()

        returned = _requisition(
            session, COMPANY, "REQ-003",
            [{"description": "Monitors", "quantity": "30", "uom": "each",
              "estimated_unit_price": "100"}],
        )
        submit(session, returned, actor="rina.requester")
        session.commit()
        record_decision(session, returned, actor="mia.manager", action=RETURN, role="manager",
                        reason="attach the vendor quote")
        session.commit()
        assert returned.status == RETURNED, returned.status
        print(f"4. wrong role and a reasonless rejection refused ({said[:60]}…); a rejection"
              " closes REQ-002 and a return leaves REQ-003 returned")

        # 5 — below every level, and with no chain at all
        small = _requisition(
            session, COMPANY, "REQ-004",
            [{"description": "Cable ties", "quantity": "500", "uom": "each",
              "estimated_unit_price": "1"}],
        )
        submit(session, small, actor="rina.requester")
        session.commit()
        assert small.status == APPROVED and small.approval_request_id is None, small.status
        assert require_sourceable(session, small) is small
        assert approval_history(session, small) == []
        unchained = _requisition(
            session, UNCONFIGURED, "REQ-005",
            [{"description": "Anything", "quantity": "1", "uom": "each",
              "estimated_unit_price": "50"}],
        )
        session.commit()
        said = _refused(lambda: submit(session, unchained, actor="rina.requester"),
                        NoWorkflowConfigured)
        session.rollback()
        print(f"5. REQ-004 (below every threshold) approved on submission; a requisition with"
              f" no configured chain is refused: {said[:60]}…")

        # 6 — frozen once approved; a revision instead
        said = _refused(
            lambda: add_line(session, big, description="Late addition", quantity="1",
                             uom="each", estimated_unit_price="1"),
            RequisitionLockedError,
        )
        session.rollback()
        revision = revise(session, big, number="REQ-001-R2", actor="rina.requester")
        session.commit()
        assert revision.status == "draft" and revision.revision_no == 2
        assert revision.revision_of_id == big.id
        assert [line.description for line in revision.lines] == [
            line.description for line in big.lines
        ]
        assert requisition_total(revision) == requisition_total(big)
        session.refresh(big)
        assert big.status == APPROVED, "the original lost its approval"
        assert len(approval_history(session, big)) == 2
        print(f"6. REQ-001 refused a new line ({said[:44]}…) and REQ-001-R2 was raised as a"
              " draft carrying the same lines, the original still approved")

        # 7 — the history is complete and in order
        history = approval_history(session, big)
        assert [(row.level_no, row.action, row.actor) for row in history] == [
            (1, APPROVE, "mia.manager"),
            (2, APPROVE, "dan.director"),
        ], [(row.level_no, row.action, row.actor) for row in history]
        rejected_history = approval_history(session, noisy)
        assert [(row.action, row.reason) for row in rejected_history] == [
            (REJECT, "budget freeze")
        ]
        print(f"7. REQ-001's history reads {[(r.level_no, r.actor) for r in history]}")

        # 8 — revisions are for finished documents, once
        draft_for_revision = _requisition(
            session, COMPANY, "REQ-006",
            [{"description": "Rags", "quantity": "1", "uom": "each",
              "estimated_unit_price": "5"}],
        )
        session.commit()
        said = _refused(lambda: revise(session, draft_for_revision, number="REQ-006-R2"),
                        RequisitionStateError)
        session.rollback()
        said += " | " + _refused(lambda: revise(session, big, number="REQ-001-R3"),
                                 RequisitionStateError)
        session.rollback()
        print(f"8. a draft is not revised and a second revision is refused: {said}")

        # and the database refuses a second revision of the same document too
        session.add(
            Requisition(
                company_id=COMPANY,
                number="REQ-001-R4",
                requested_by="rina.requester",
                needed_by=NEEDED_BY,
                currency="PHP",
                status="draft",
                revision_no=3,
                revision_of_id=big.id,
            )
        )
        try:
            session.commit()
        except Exception as exc:  # noqa: BLE001 — the constraint name is the point
            assert "uq_purchase_requisition_revision_of_id" in str(exc) or "revision_of_id" in str(exc), exc
            session.rollback()
        else:
            raise AssertionError("the database accepted a second revision of one requisition")
        print("   the database refuses a second revision of the same requisition")

        assert session.scalar(
            select(Requisition).where(Requisition.number == "REQ-001")
        ).status == APPROVED
        print("check_requisition_approval: all assertions green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
