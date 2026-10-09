"""T-5.EMP.02 check — the reporting hierarchy: a dated tree with one root, and the chart it renders.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_org_chart.py

It fails (non-zero exit) if any of these stops holding:

1. **a placement is history, not a column** — moving somebody appends a row and rewrites
   nothing: the database refuses an `UPDATE` to a recorded placement, and the structure of
   a past date is still answered with the managers of that date after a reorganisation
2. **the structure is a tree** — a placement that would put an employee inside their own
   chain of command is refused, whether it names themselves or somebody below them
3. **one root per company** — a second employee asking to top the tree is refused by name,
   while an employee who has never been placed is reported as *unplaced* rather than drawn
   as a second root
4. **the chart renders the hierarchy** — a three-level structure comes back in chart order
   with each line's depth, its direct reports, its department and cost centre, plus the
   employees who are not in the tree at all
5. a manager from another company is refused, and a placement that does not follow the one
   already recorded — or that starts before the employee was hired — is refused

**Scratch database only**: it drops and recreates the schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date

from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api import BASE, app  # noqa: E402
from app.audit import soft_delete  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.hr.employees import create_employee  # noqa: E402
from app.hr.org import (  # noqa: E402
    PlacementSequenceError,
    ReportingCycleError,
    SecondRootError,
    UnknownManagerError,
    chain_of_command,
    manager_in_force,
    org_chart,
    place_employee,
    placement_in_force,
)
from app.ledger import posting  # noqa: E402,F401 — every check builds the one schema
from app.security import Role, assign, define_role, grant, restrict  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _refused(call, expected: str) -> str:
    """The database's message if `call` is refused; fail the check otherwise."""
    try:
        call()
    except DBAPIError as exc:
        message = str(exc.orig).strip()
        assert expected in message, f"unclear database error: {message}"
        return message
    raise AssertionError(f"the database accepted what it must refuse ({expected!r})")


def _refuses(call, expected: type, what: str) -> Exception:
    """The refusal `call` raises; fail the check if it does not refuse."""
    try:
        call()
    except expected as exc:
        return exc
    raise AssertionError(f"{what} was accepted")


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

    company_id = uuid.uuid4()
    other_company = uuid.uuid4()
    with Session(engine) as session:
        session.add_all(
            [
                Company(
                    id=company_id,
                    code="ORG-CHECK",
                    name="Org chart check",
                    base_currency="PHP",
                    fiscal_year_start_month=1,
                ),
                Company(
                    id=other_company,
                    code="ORG-OTHER",
                    name="Another company",
                    base_currency="PHP",
                    fiscal_year_start_month=1,
                ),
            ]
        )
        session.commit()

        people = {}
        for code, number, hired in (
            ("ANA", "E-001", "2026-01-05"),
            ("BEN", "E-002", "2026-02-01"),
            ("CARL", "E-003", "2026-02-15"),
            ("DIVA", "E-004", "2026-03-01"),
            ("ELI", "E-005", "2026-03-15"),
        ):
            people[code] = create_employee(
                session,
                company_id=company_id,
                party_code=code,
                number=number,
                hire_date=hired,
                subject="hr",
                name=f"{code.title()} Reyes",
            )
        zoe = create_employee(
            session,
            company_id=other_company,
            party_code="ZOE",
            number="E-900",
            hire_date="2026-01-05",
            subject="hr",
            name="Zoe Cruz",
        )
        session.commit()
        ana, ben, carl, diva, eli = (
            people["ANA"],
            people["BEN"],
            people["CARL"],
            people["DIVA"],
            people["ELI"],
        )

        # 4 (first half) — a three-level hierarchy, each level dated from the day it took effect
        place_employee(session, ana, effective_from="2026-01-05")
        place_employee(
            session,
            ben,
            effective_from="2026-02-01",
            manager=ana,
            department="Finance",
            cost_centre="CC-1",
        )
        place_employee(
            session,
            carl,
            effective_from="2026-02-15",
            manager=ben,
            department="Finance",
            cost_centre="CC-1",
        )
        place_employee(
            session,
            diva,
            effective_from="2026-03-01",
            manager=ben,
            department="Sales",
            cost_centre="CC-2",
        )
        session.commit()

        # 3 — ELI has never been placed: not a root, not in the tree, and still on the chart
        chart = org_chart(session, company_id=company_id, on=date(2026, 3, 31))
        assert chart["root_number"] == "E-001", f"the root is {chart['root_number']!r}"
        assert [entry["number"] for entry in chart["entries"]] == [
            "E-001",
            "E-002",
            "E-003",
            "E-004",
        ], f"the chart order is {[entry['number'] for entry in chart['entries']]}"
        assert [entry["depth"] for entry in chart["entries"]] == [1, 2, 3, 3], (
            f"the depths are {[entry['depth'] for entry in chart['entries']]}"
        )
        assert [entry["reports"] for entry in chart["entries"]] == [1, 2, 0, 0], (
            f"the direct-report counts are {[entry['reports'] for entry in chart['entries']]}"
        )
        by_number = {entry["number"]: entry for entry in chart["entries"]}
        assert by_number["E-003"]["manager_number"] == "E-002"
        assert by_number["E-002"]["department"] == "Finance"
        assert by_number["E-004"]["cost_centre"] == "CC-2"
        assert chart["as_of"] == "2026-03-31"
        assert [row["number"] for row in chart["unplaced"]] == ["E-005"], (
            "an employee who was never placed is not reported as unplaced"
        )
        print(
            "the chart renders ANA → BEN → (CARL, DIVA) at depths 1/2/3 with 1/2/0/0 direct"
            " reports, and ELI unplaced rather than drawn as a second root"
        )

        # 1 — the structure of a past date is the structure of that date
        earlier = org_chart(session, company_id=company_id, on=date(2026, 2, 10))
        assert [entry["number"] for entry in earlier["entries"]] == ["E-001", "E-002"], (
            f"the February chart is {[entry['number'] for entry in earlier['entries']]}"
        )
        assert [row["number"] for row in earlier["unplaced"]] == ["E-003", "E-004", "E-005"]
        assert manager_in_force(session, diva, on=date(2026, 3, 31)).number == "E-002"
        assert manager_in_force(session, diva, on=date(2026, 2, 10)) is None, (
            "a date before the placement answered with a manager"
        )
        assert [row.number for row in chain_of_command(session, diva, on=date(2026, 3, 31))] == [
            "E-002",
            "E-001",
        ], "the escalation path is not the chain of that date"
        assert chain_of_command(session, ana, on=date(2026, 3, 31)) == [], (
            "the root has somebody above them"
        )

        # A reorganisation: CARL moves to ANA from May. Nothing already recorded changes —
        # the new row is appended and the old placement closes by derivation.
        march = placement_in_force(session, carl, on=date(2026, 3, 31))
        place_employee(
            session,
            carl,
            effective_from="2026-05-01",
            manager=ana,
            department="Finance",
            cost_centre="CC-1",
        )
        session.commit()
        assert placement_in_force(session, carl, on=date(2026, 3, 31)).id == march.id, (
            "the reorganisation rewrote the placement that applied in March"
        )
        assert manager_in_force(session, carl, on=date(2026, 3, 31)).number == "E-002"
        assert manager_in_force(session, carl, on=date(2026, 6, 1)).number == "E-001"
        may = org_chart(session, company_id=company_id, on=date(2026, 5, 31))
        assert [entry["number"] for entry in may["entries"]] == [
            "E-001",
            "E-002",
            "E-004",
            "E-003",
        ], f"the May chart is {[entry['number'] for entry in may['entries']]}"
        message = _refused(
            lambda: (
                session.execute(
                    text("UPDATE org_placement SET department = 'Ops' WHERE id = :id"),
                    {"id": march.id},
                ),
                session.commit(),
            ),
            "is append-only",
        )
        session.rollback()
        print(
            "March still answers BEN→CARL after the May move, and the database refuses to"
            f" rewrite the recorded placement ({message.splitlines()[0]})"
        )

        # 2 — the cycle, refused from both directions
        _refuses(
            lambda: place_employee(session, ana, effective_from="2026-06-01", manager=ana),
            ReportingCycleError,
            "an employee managing themselves",
        )
        session.rollback()
        # BEN manages DIVA, so DIVA cannot manage BEN — and the check finds it through two
        # links rather than only the direct one.
        _refuses(
            lambda: place_employee(session, ben, effective_from="2026-04-01", manager=diva),
            ReportingCycleError,
            "a manager reporting to somebody in their own chain",
        )
        session.rollback()
        print("naming themselves, or a descendant, as manager is refused as a cycle")

        # 3 — and the second root, refused by name
        denied = _refuses(
            lambda: place_employee(session, eli, effective_from="2026-06-01"),
            SecondRootError,
            "a second employee topping the tree",
        )
        session.rollback()
        assert "E-001" in str(denied), f"the refusal does not name the root: {denied}"
        assert [entry["number"] for entry in org_chart(
            session, company_id=company_id, on=date(2026, 6, 1)
        )["entries"]][0] == "E-001"
        print(f"a second root is refused by name: {denied}")

        # 5 — a manager from another company, a placement out of sequence, one before the hire
        _refuses(
            lambda: place_employee(session, zoe, effective_from="2026-06-01", manager=ana),
            UnknownManagerError,
            "a manager from another company",
        )
        session.rollback()
        _refuses(
            lambda: place_employee(session, diva, effective_from="2026-03-01", manager=ana),
            PlacementSequenceError,
            "a placement that does not follow the one already recorded",
        )
        session.rollback()
        _refuses(
            lambda: place_employee(session, eli, effective_from="2026-01-01", manager=ana),
            PlacementSequenceError,
            "a placement starting before the employee was hired",
        )
        session.rollback()
        # A manager who has left: the chart could not draw their reports, so the placement
        # that would create the state is refused rather than stored.
        gone = create_employee(
            session,
            company_id=company_id,
            party_code="FRAN",
            number="E-006",
            hire_date="2026-03-01",
            subject="hr",
            name="Fran Lim",
        )
        session.commit()
        soft_delete(session, gone)
        session.commit()
        _refuses(
            lambda: place_employee(session, eli, effective_from="2026-06-01", manager=gone),
            UnknownManagerError,
            "a placement under an employee who has left",
        )
        session.rollback()
        print(
            "another company's manager, a retired one, an overlapping date and a pre-hire"
            " date are all refused"
        )

        # 4 (second half) — a manager who is no longer a live employee leaves nobody drawn
        # under them: the chain cannot reach the root, so they are unplaced, not a second root.
        soft_delete(session, ben)
        session.commit()
        after = org_chart(session, company_id=company_id, on=date(2026, 6, 1))
        assert after["root_number"] == "E-001", f"the root became {after['root_number']!r}"
        # CARL moved to ANA in May and is still drawn; DIVA still reports to BEN, who is no
        # longer among the company's live employees, so her chain reaches no root.
        assert [entry["number"] for entry in after["entries"]] == ["E-001", "E-003"], (
            f"the chart draws {[entry['number'] for entry in after['entries']]}"
        )
        assert after["entries"][0]["reports"] == 1
        assert [row["number"] for row in after["unplaced"]] == ["E-004", "E-005"], (
            f"the unplaced list is {[row['number'] for row in after['unplaced']]}"
        )
        print("a retired manager's reports are reported as unplaced, and the chart still has one root")

        # 6 — the chart the frontend draws comes over the published API
        viewer = define_role(session, company_id=company_id, code="viewer", name="Org viewer")
        assign(session, company_id=company_id, subject="olive", role=viewer)
        session.commit()

    client = TestClient(app, raise_server_exceptions=False)
    headers = {"X-Company-Id": str(company_id), "X-Actor": "olive"}
    chart_url = f"{BASE}/org-chart"

    # Without the capability the boundary refuses before any data is read, in the one shape.
    refused = client.get(chart_url, headers=headers, params={"on": "2026-06-01"})
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "forbidden", refused.json()

    with Session(engine) as session:
        viewer = session.scalar(
            select(Role).where(Role.company_id == company_id, Role.code == "viewer")
        )
        grant(session, viewer, "employee.read")
        session.commit()

    answered = client.get(chart_url, headers=headers, params={"on": "2026-06-01"})
    assert answered.status_code == 200, answered.text
    chart = answered.json()
    assert chart["as_of"] == "2026-06-01"
    assert chart["root_number"] == "E-001"
    assert [entry["number"] for entry in chart["entries"]] == ["E-001", "E-003"], chart
    assert [row["number"] for row in chart["unplaced"]] == ["E-004", "E-005"], chart
    assert chart["entries"][1]["manager_number"] == "E-001"

    # The date is stated, never assumed: without it the request is refused like any other
    # malformed one, rather than silently answering about today.
    assert client.get(chart_url, headers=headers).status_code in (400, 422)

    # A restricted field is **absent** from the response, not nulled — the chart is drawn,
    # it just does not carry what the viewer's role may not read.
    with Session(engine) as session:
        viewer = session.scalar(
            select(Role).where(Role.company_id == company_id, Role.code == "viewer")
        )
        restrict(session, viewer, entity="employee", field="name", can_read=False)
        session.commit()

    withheld = client.get(chart_url, headers=headers, params={"on": "2026-06-01"}).json()
    assert "name" not in withheld["entries"][0], withheld["entries"][0]
    assert withheld["entries"][0]["number"] == "E-001", withheld["entries"][0]
    assert "name" not in withheld["unplaced"][0], withheld["unplaced"][0]
    print(
        "the chart is served over POST-free GET /api/v1/org-chart: refused without"
        " employee.read, dated by the request, and a restricted name is absent rather than null"
    )

    engine.dispose()
    print("ok — the reporting hierarchy is a dated tree with one root, and the chart renders it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
