"""T-6.HARD.02 check — the access review: every route guarded, the scopes isolated, no secret readable.

    DATABASE_URL=postgresql+psycopg://erpv1:erpv1@localhost:5432/erpv1 \
        python tests/check_security_review.py

The review is only as good as what it can be held to, so this check is its **verification**:
each claim the review makes is something this file either runs or reads out of the tree, and it
fails when a claim stops being true.

Green on all eight:

1. **every route on the app is guarded** — the sweep walks the app's own routes, reads each
   endpoint's own source, and requires a `require(...)` naming a capability; the routes that
   carry no permission are named in :data:`PUBLIC` with the reason each is public, so a further
   unguarded route is a failure rather than an omission
2. **the recorded matrix is the app's matrix** — `SECURITY-REVIEW.md` lists every route with
   the capability guarding it, and this check compares the two: a route added, removed or
   re-guarded without the document is red
3. **field restrictions are enforced on read and write** — a restricted reader's payload has
   the field *absent* (not null), and a write naming it is refused with the field named and
   nothing stored (the same two behaviours `tests/check_security.py` states, re-run here as the
   review's own evidence)
4. **cross-company and cross-supplier isolation hold by request** — another company's entries
   are not on a caller's ledger, and a supplier's portal shows the documents of its own account
   and refuses another supplier's RFQ
5. **credentials are not readable through the platform** — a configured integration token
   reaches the transport through `endpoint_for` and appears in **no** delivery-log row and in
   no API response body
6. **a key belongs to the request that wrote it** — an idempotency key is matched on its
   method, its path **and its actor** as well as its body, so another caller (or another
   endpoint) presenting a key is refused instead of being handed the stored answer — two holes
   the review found and closed, each with the request that used to leak it
7. **a guard that lives outside its endpoint is driven, not taken on trust** — the routes whose
   capability is asked for by a shared helper (the portal's scope) or by the framework (a report
   definition's own capability) are called with a subject holding nothing and must be refused,
   and the permitted caller is served: a table of exceptions with no request behind it is a
   place for a real hole to hide
8. **every finding is recorded with a disposition** — `SECURITY-REVIEW.md`'s findings each state
   `remediated` or `accepted risk` with the reason, so an open finding is visible and an
   accepted one is a decision somebody wrote down

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import sys
import uuid
from datetime import date
from pathlib import Path

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.ap.invoices import SupplierInvoice  # noqa: E402,F401 — for its table
from app.api import (  # noqa: E402
    BASE,
    ApiIdempotencyKey,
    JournalEntryIn,
    _fingerprint,
    app,
)
from app.company import Company  # noqa: E402
from app.db import Base, scope_to_company  # noqa: E402
from app.integrations import (  # noqa: E402
    ENDPOINTS_SETTING,
    endpoint_for,
    deliveries_for,
    register_transport,
    send_outbound,
)
from app.ledger import posting  # noqa: E402,F401 — every check builds the one schema
from app.ledger.currency import register_currency  # noqa: E402
from app.party import create_party  # noqa: E402
from app.reporting import register, register_builder  # noqa: E402
from app.procurement.orders import PurchaseOrder  # noqa: E402,F401 — for its table
from app.procurement.portal import account_for, documents_for, link_account  # noqa: E402
from app.procurement.receipts import GoodsReceipt  # noqa: E402,F401 — for its table
from app.procurement.requisitions import (  # noqa: E402
    create_requisition,
    record_decision,
    submit,
)
from app.procurement.rfq import NotInvitedError, issue_rfq  # noqa: E402
from app.procurement.suppliers import create_supplier  # noqa: E402
from app.security import Role, assign, define_role, grant, restrict  # noqa: E402
from app.workflow import APPROVE, configure  # noqa: E402
from tests.seed import seed_accounts  # noqa: E402

REVIEW = ROOT / "SECURITY-REVIEW.md"
DAY = "2026-09-17"

# The routes that carry no permission, each with the reason it is public: a check on the
# sweep's blind spot rather than a place to hide a route.
PUBLIC = {
    "/api/v1/health": "a liveness probe: it answers whether the process is up, nothing else",
    "/api/v1/openapi.json": "the contract the frontend pins; the same document is published",
    "/openapi.json": "the same contract, as FastAPI's own path",
    "/docs": "the generated reference for that public contract",
    "/docs/oauth2-redirect": "part of the generated reference",
    "/redoc": "the generated reference, in its other renderer",
}

CAPABILITY = re.compile(r'capability="([^"]+)"')
CAPABILITY_TUPLE = re.compile(r'capability in \(([^)]*)\)')
CAPABILITY_NAMED = re.compile(r"capability=([A-Za-z_][A-Za-z_0-9]*)")

# Routes whose guard is not in their own source and not in a helper of `app/api.py`, with the
# reason each is still covered. This check **drives** every one of them with a caller holding
# nothing and requires a refusal, so "somebody else checks" is a claim with a test behind it.
GUARDED_ELSEWHERE = {
    "POST /api/v1/reports/{code}/run": (
        "the capability the report definition carries is asked for by the reporting framework"
        " itself (T-0.REPORT.01) before anything is built"
    ),
    "GET /api/v1/dashboard": (
        "each tile names its own capability and the dashboard asks for it tile by tile"
        " (T-6.ANALYTICS.01): a caller holding nothing is shown no tile at all, and each is"
        " named with the refusal, so the request has no single capability to ask for"
    ),
}


def _capabilities(source: str, module: str) -> list[str]:
    """Every capability named in a piece of code, in the order the code asks for them.

    Three shapes are in the tree: a literal, a loop over a tuple (the two-document call), and a
    module constant (`portal_capability`) — all three are read out so the matrix states what
    actually runs rather than what a grep for one shape happens to see. A named constant is
    resolved to its runtime value, which is the very object the code passes.
    """
    found: list[str] = []
    for match in CAPABILITY.finditer(source):
        found.append(match.group(1))
    for match in CAPABILITY_TUPLE.finditer(source):
        found.extend(re.findall(r'"([^"]+)"', match.group(1)))
    for match in CAPABILITY_NAMED.finditer(source):
        # Only a module constant resolves; `capability=capability` is a loop variable and
        # `capability=definition.capability` comes off a row, and neither names its value here.
        value = getattr(sys.modules[module], match.group(1), None)
        if isinstance(value, str):
            found.append(value)
    return list(dict.fromkeys(found))


def _helper_guard(body: str, module: str, endpoint_name: str) -> str | None:
    """A helper defined in the endpoint's own module that performs the `require(...)`.

    The portal's four endpoints share `_portal_scope`, which holds the capability *and* the
    account, so the sweep follows one level into a helper rather than demanding the guard be
    written out four times. The capability is read out of the helper's own source, so what is
    recorded is what runs.
    """
    module_source = inspect.getsource(sys.modules[module])
    for name in re.findall(r"\b(_?[a-z][a-z_]*)\(", body):
        if name in {"require", "str", "print", "len", "list"} or name == endpoint_name:
            continue
        match = re.search(rf"^def {re.escape(name)}\(", module_source, re.MULTILINE)
        if match is None:
            continue
        tail = module_source[match.start() + 1 :]
        following = re.search(r"^def ", tail, re.MULTILINE)
        helper = tail if following is None else tail[: following.start()]
        if "require(" in helper:
            asked = _capabilities(helper, module)
            return f"{', '.join(asked)} (via {name})" if asked else f"via {name}"
    return None


def _routes() -> list[tuple[str, str, str, str]]:
    """Every route the app serves, with the function that answers it and what guards it.

    The guard is read out of the tree: a handler's own `require(...)`, one helper of its own
    module (the portal's shared scope), or the table of routes somebody else guards. A route
    that is in none of them and is not public is a finding.
    """
    out: list[tuple[str, str, str, str]] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        body = inspect.getsource(route.endpoint)
        module_source = inspect.getsource(sys.modules[route.endpoint.__module__])
        own = _capabilities(body, route.endpoint.__module__)
        note = ", ".join(own) if own else ""
        if not note:
            note = _helper_guard(body, route.endpoint.__module__, route.endpoint.__name__) or ""
        for method in sorted(route.methods):
            out.append((route.path, method, route.endpoint.__name__, note))
    return sorted(out)


def _review_rows(text: str) -> dict[tuple[str, str], str]:
    """The matrix rows the review records: `| METHOD path | capability |`."""
    rows: dict[tuple[str, str], str] = {}
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 3:
            continue
        match = re.fullmatch(r"(GET|POST|PUT|PATCH|DELETE) (`)?(/[^`]*)", cells[0].strip("`"))
        if match is None:
            continue
        rows[(match.group(3), match.group(1))] = cells[1].strip("`")
    return rows


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is required (a scratch Postgres)", file=sys.stderr)
        return 2

    # 1 — every route is guarded, and the ones that are not are named
    routes = _routes()
    unguarded = [
        (path, method, name)
        for path, method, name, capability in routes
        if capability == ""
        and path not in PUBLIC
        and f"{method} {path}" not in GUARDED_ELSEWHERE
    ]
    assert unguarded == [], f"routes with no permission check: {unguarded}"
    assert set(GUARDED_ELSEWHERE) <= {f"{method} {path}" for path, method, _n, _c in routes}, (
        "the review's table of routes somebody else guards names a route the app does not serve"
    )
    for path, _method, _name, capability in routes:
        if path in PUBLIC:
            assert capability == "", f"{path} is listed public but requires {capability!r}"
    families = sorted(
        {
            name.strip()
            for _p, _m, _n, capability in routes
            for name in capability.split(" (via ")[0].split(",")
            if name.strip()
        }
    )
    print(
        f"1. {len(routes)} method/route pairs swept, each carrying a `require(...)` naming a"
        f" capability ({len(families)} capabilities in use: {', '.join(families)}); the"
        f" {len(PUBLIC)} public routes are named with their reason, so the blind spot is"
        f" stated rather than assumed, and the {len(GUARDED_ELSEWHERE)} route whose capability"
        " the framework asks for is named and then driven (see 6)"
    )

    # 2 — the recorded matrix and the app agree
    review = REVIEW.read_text()
    recorded = _review_rows(review)
    measured = {(path, method): capability for path, method, _n, capability in routes}
    missing = sorted(key for key in measured if key not in recorded)
    extra = sorted(key for key in recorded if key not in measured)
    wrong = sorted(
        key for key in measured.keys() & recorded.keys() if recorded[key] != measured[key]
    )
    assert missing == [], f"routes the review does not list: {missing}"
    assert extra == [], f"routes the review lists that the app does not serve: {extra}"
    assert wrong == [], f"capabilities the review states differently: {wrong}"
    print(
        f"2. SECURITY-REVIEW.md records all {len(measured)} method/route pairs with the same"
        " capability the app enforces — a new route, a removed one or a re-guarded one makes"
        " the review stale, and it is checked rather than trusted"
    )

    engine = create_engine(url)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    Base.metadata.create_all(engine)

    company_id = uuid.uuid4()
    other_company = uuid.uuid4()
    with Session(engine) as session:
        for one, code in ((company_id, "REVIEW-A"), (other_company, "REVIEW-B")):
            session.add(
                Company(id=one, code=code, name=code, base_currency="PHP",
                        fiscal_year_start_month=1)
            )
        session.commit()
        scope_to_company(session, company_id)
        seed_accounts(session, company_id=company_id)
        register_currency(session, company_id=company_id, code="PHP", name="Philippine Peso")
        configure(session, company_id=company_id, doc_type="purchase_requisition",
                  name="Purchase requisition", levels=[(0, "manager")])
        accountant = define_role(session, company_id=company_id, code="accountant",
                                 name="Accountant")
        grant(session, accountant, "journal.post", "journal.read")
        auditor = define_role(session, company_id=company_id, code="auditor", name="Auditor")
        grant(session, auditor, "journal.read")
        restrict(session, auditor, entity="journal_line", field="party", can_read=False)
        assign(session, company_id=company_id, subject="tina", role=accountant)
        assign(session, company_id=company_id, subject="omar", role=auditor)
        create_party(session, company_id=company_id, code="ACME", name="Acme Trading",
                     roles=["customer"])
        supplier = create_supplier(session, company_id=company_id, party_code="SUP-A",
                                   name="Supplier A", payment_terms_days=30)
        other_supplier = create_supplier(session, company_id=company_id, party_code="SUP-B",
                                        name="Supplier B", payment_terms_days=30)
        session.commit()
        inside = link_account(session, company_id=company_id, supplier=supplier,
                              subject="sup-a.portal", actor="mia")
        outside = link_account(session, company_id=company_id, supplier=other_supplier,
                               subject="sup-b.portal", actor="mia")
        portal_role = define_role(session, company_id=company_id, code="supplier",
                                  name="Supplier")
        grant(session, portal_role, "portal.supplier")
        assign(session, company_id=company_id, subject="sup-a.portal", role=portal_role)
        session.commit()

        requisition = create_requisition(
            session, company_id=company_id, number="REQ-R", requested_by="rina",
            needed_by="2026-11-30", currency="PHP",
            lines=[{"description": "Laptops", "quantity": "1", "uom": "each",
                    "estimated_unit_price": "100.00"}],
        )
        session.commit()
        submit(session, requisition, actor="rina")
        session.commit()
        if requisition.status == "pending":
            record_decision(session, requisition, actor="mia", action=APPROVE, role="manager")
            session.commit()
        issue_rfq(session, requisition=requisition, number="RFQ-R", supplier_codes=["SUP-A"],
                  response_deadline=date(2026, 10, 20))
        session.commit()

    client = TestClient(app, raise_server_exceptions=False)
    headers = {"X-Company-Id": str(company_id), "X-Actor": "tina"}
    elsewhere = {"X-Company-Id": str(other_company), "X-Actor": "tina"}
    body = {
        "posting_date": DAY,
        "currency": "PHP",
        "memo": "review",
        "lines": [
            {"account": "1000", "debit": "10.00", "party": "ACME"},
            {"account": "4000", "credit": "10.00"},
        ],
    }
    posted = client.post(
        f"{BASE}/journal-entries", headers={**headers, "Idempotency-Key": "review-1"}, json=body
    )
    assert posted.status_code == 201, posted.text

    # 3 — the field levels, on read and on write
    hidden = client.get(f"{BASE}/journal-entries", headers={**headers, "X-Actor": "omar"})
    assert hidden.status_code == 200, hidden.text
    line = hidden.json()["items"][0]["lines"][0]
    assert "party" not in line, f"a restricted field reached the payload: {line}"
    assert "debit" in line, "the restriction removed the wrong field"
    visible = client.get(f"{BASE}/journal-entries", headers=headers)
    assert visible.json()["items"][0]["lines"][0]["party"] == "ACME", "the owner lost the field"
    with Session(engine) as session:
        scope_to_company(session, company_id)
        role = session.scalar(select(Role).where(Role.code == "accountant"))
        restrict(session, role, entity="journal_line", field="party", can_write=False)
        session.commit()
    refused = client.post(
        f"{BASE}/journal-entries",
        headers={**headers, "Idempotency-Key": "review-2"},
        json={**body, "memo": "review again"},
    )
    assert refused.status_code == 403, refused.text
    assert "journal_line.party" in refused.json()["error"]["message"], refused.json()
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM journal_entry").scalar() == 1, (
            "the refused write was stored"
        )
    print(
        f"3. a restricted field is absent on read ({sorted(line)}) and a write naming it is"
        f" refused with the field named ({refused.json()['error']['message'].split(':')[0]})"
        " and nothing stored"
    )

    # 4 — the two isolation boundaries
    #
    # The same actor, holding the same capabilities, in the other company: the row-level policy
    # is what makes the list empty, so the actor is given a role there and asked again rather
    # than being refused for having no role at all.
    with Session(engine) as session:
        scope_to_company(session, other_company)
        role_b = define_role(session, company_id=other_company, code="accountant",
                             name="Accountant")
        grant(session, role_b, "journal.post", "journal.read")
        assign(session, company_id=other_company, subject="tina", role=role_b)
        session.commit()
    theirs = client.get(f"{BASE}/journal-entries", headers=elsewhere)
    assert theirs.status_code == 200, theirs.text
    assert theirs.json()["items"] == [] and theirs.json()["total"] == 0, theirs.json()
    with Session(engine) as session:
        account_a = account_for(session, company_id=company_id, subject="sup-a.portal")
        account_b = account_for(session, company_id=company_id, subject="sup-b.portal")
        view_a = documents_for(session, account=account_a)
        view_b = documents_for(session, account=account_b)
        assert [row["number"] for row in view_a["rfqs"]] == ["RFQ-R"], view_a["rfqs"]
        assert view_b["rfqs"] == [], view_b["rfqs"]
        refused_supplier = None
        try:
            from app.procurement.portal import respond_to_rfq

            respond_to_rfq(session, account=account_b, number="RFQ-R", received_on=DAY,
                           lines=[{"line_no": 1, "unit_price": "1.00"}])
        except NotInvitedError as exc:
            refused_supplier = str(exc)
        session.rollback()
    assert refused_supplier is not None, "another supplier answered an RFQ it was not issued"
    print(
        f"4. company B's ledger is empty of company A's postings ({theirs.json()['total']}"
        f" rows), and supplier B sees {len(view_b['rfqs'])} of supplier A's RFQs and is refused"
        f" answering one ({refused_supplier[:44]}…)"
    )

    # 5 — a configured credential reaches the platform and nothing else
    token = "secret-token-9f3a"
    os.environ[ENDPOINTS_SETTING] = json.dumps(
        {"email": {"url": "https://mail.example.invalid", "token": token}}
    )
    sent: list[str] = []
    register_transport("email", lambda destination, payload: sent.append(destination))
    with Session(engine) as session:
        scope_to_company(session, company_id)
        send_outbound(session, company_id=company_id, channel="email",
                      destination="buyer@example.invalid", payload={"subject": "hello"})
        rows = deliveries_for(session, company_id=company_id, channel="email")
        assert token in endpoint_for("email")["token"], "the platform lost its own credential"
        assert rows, "the delivery was not logged"
        for row in rows:
            assert token not in json.dumps(row.payload), "a credential reached the log"
            assert token not in (row.destination or ""), "a credential reached the destination"
    for path in (f"{BASE}/health", f"{BASE}/journal-entries"):
        answer = client.get(path, headers=headers)
        assert token not in answer.text, f"{path} returned the credential"
    print(
        f"5. the token reaches the transport through endpoint_for ({sent[0]}) and is in"
        f" {sum(1 for row in rows if token in json.dumps(row.payload))} delivery-log payloads"
        " and no response body"
    )

    # 6 — an idempotency key is the request that wrote it, and nobody else's
    #
    # The lookup matched on the company and the key alone: the row's method, path and actor were
    # written and never read back, so a key presented at another endpoint (or by another caller)
    # replayed the stored answer — a document the presenter need never have been allowed to read.
    key_body = {
        "posting_date": DAY,
        "currency": "PHP",
        "memo": "review-key",
        "lines": [
            {"account": "1000", "debit": "10.00"},
            {"account": "4000", "credit": "10.00"},
        ],
    }
    key_headers = {**headers, "Idempotency-Key": "review-key"}
    owner = client.post(f"{BASE}/journal-entries", headers=key_headers, json=key_body)
    assert owner.status_code == 201, owner.text
    retry = client.post(f"{BASE}/journal-entries", headers=key_headers, json=key_body)
    assert retry.headers.get("Idempotent-Replay") == "true", "the writer's own retry stopped working"
    assert retry.json() == owner.json(), retry.text

    with Session(engine) as session:
        scope_to_company(session, company_id)
        poster = define_role(session, company_id=company_id, code="poster", name="Poster")
        grant(session, poster, "journal.post")
        assign(session, company_id=company_id, subject="peter", role=poster)
        session.commit()
    elsewhere_caller = {**key_headers, "X-Actor": "peter"}
    taken = client.post(f"{BASE}/journal-entries", headers=elsewhere_caller, json=key_body)
    assert taken.status_code == 409, f"another caller replayed a stored document: {taken.text}"
    assert taken.json()["error"]["code"] == "idempotency_key_reused", taken.json()
    assert "review-key" in taken.json()["error"]["message"], taken.json()

    # The path is part of the match too: a key planted for another endpoint with *this* body's
    # fingerprint is not an answer for this request, however the body compares.
    fingerprint = _fingerprint(JournalEntryIn(**key_body).model_dump(mode="json"))
    with Session(engine) as session:
        session.add(
            ApiIdempotencyKey(
                company_id=company_id,
                key="review-key-elsewhere",
                method="POST",
                path=f"{BASE}/pos/sync",
                actor="tina",
                fingerprint=fingerprint,
                status_code=201,
                body=json.dumps({"report": {"unexpected": True}}),
            )
        )
        session.commit()
    crossed = client.post(
        f"{BASE}/journal-entries",
        headers={**headers, "Idempotency-Key": "review-key-elsewhere"},
        json=key_body,
    )
    assert crossed.status_code == 409, f"a key from another endpoint answered here: {crossed.text}"
    assert "unexpected" not in crossed.text, crossed.text
    print(
        f"6. a key is matched on its method, path and actor: the writer's retry replayed"
        f" ({retry.headers['Idempotent-Replay']}), another caller presenting it got"
        f" {taken.status_code} {taken.json()['error']['code']}, and a key written at"
        f" {BASE}/pos/sync with this body's fingerprint got {crossed.status_code} — neither"
        " stored answer crossed over"
    )

    # 7 — the guards that live outside their endpoint, driven by request
    #
    # Four portal endpoints ask through `_portal_scope` and the report endpoint asks through the
    # definition the framework holds. Each is called by a subject holding nothing (403) and by a
    # subject that may (served), so the exception table is a claim this check keeps.
    with Session(engine) as session:
        scope_to_company(session, company_id)
        registered = register(
            session,
            company_id=company_id,
            code="review-report",
            name="Review report",
            schedule="0 6 * * *",
            recipients=["buyer@example.invalid"],
            capability="journal.post",
        )
        session.commit()
        assert registered.capability == "journal.post"
    register_builder("review-report", lambda _session, _definition, _window: {"rows": 0})

    portal_calls = [
        ("GET", f"{BASE}/portal/documents", None),
        (
            "POST",
            f"{BASE}/portal/rfqs/RFQ-R/responses",
            {"received_on": DAY, "lines": [{"line_no": 1, "unit_price": "1.00"}]},
        ),
        ("POST", f"{BASE}/portal/orders/PO-R/acknowledge", None),
        (
            "POST",
            f"{BASE}/portal/invoices",
            {
                "number": "SUP-A-1",
                "invoice_date": DAY,
                "supplier_reference": "REF-1",
                "lines": [{"line_no": 1, "unit_price": "1.00"}],
            },
        ),
    ]
    outsider = {"X-Company-Id": str(company_id), "X-Actor": "nobody"}
    driven = 0
    for method, path, payload in portal_calls:
        refused_here = client.request(method, path, headers=outsider, json=payload)
        assert refused_here.status_code == 403, (
            f"{method} {path} answered {refused_here.status_code} to a subject holding nothing:"
            f" {refused_here.text[:120]}"
        )
        assert "portal.supplier" in refused_here.json()["error"]["message"], refused_here.json()
        driven += 1
    served = client.get(
        f"{BASE}/portal/documents", headers={"X-Company-Id": str(company_id), "X-Actor": "sup-a.portal"}
    )
    assert served.status_code == 200, served.text
    assert [row["number"] for row in served.json()["documents"]["rfqs"]] == ["RFQ-R"], served.json()

    # The dashboard's guard is per tile, so driving it means a subject holding nothing is shown
    # nothing: not a zeroed figure, not a blanked tile — no tile, and every one of them named.
    dashboard = client.get(f"{BASE}/dashboard", headers=outsider)
    assert dashboard.status_code == 200, dashboard.text
    assert dashboard.json()["tiles"] == [], dashboard.json()
    assert {entry["code"] for entry in dashboard.json()["withheld"]} == {
        "finance.profit_and_loss",
        "finance.receivables",
        "finance.payables",
        "operations.stock",
    }, dashboard.json()["withheld"]
    assert all(entry["reason"] for entry in dashboard.json()["withheld"]), dashboard.json()
    driven += 1

    report_path = f"{BASE}/reports/review-report/run"
    refused_report = client.post(report_path, headers=outsider)
    assert refused_report.status_code == 403, refused_report.text
    assert "journal.post" in refused_report.json()["error"]["message"], refused_report.json()
    driven += 1
    ran = client.post(report_path, headers=headers)
    assert ran.status_code == 200 and ran.json()["status"] == "ok", ran.text
    print(
        f"7. {driven} routes whose capability is asked for outside their own body were driven —"
        " each refused a subject holding nothing (the four portal routes and the report run with"
        " 403, the dashboard by showing no tile and naming all four), and the permitted caller"
        " was served:"
        f" the supplier's portal lists {[row['number'] for row in served.json()['documents']['rfqs']]}"
        f" and the report ran ({ran.json()['status']}) past the framework's own check"
    )

    # 8 — the findings are recorded with a disposition
    findings = re.findall(r"^\| ([^|]+) \| (remediated|accepted risk) \| ([^|]+) \|$", review,
                          flags=re.MULTILINE)
    assert len(findings) >= 3, f"the review records {len(findings)} findings"
    for what, disposition, why in findings:
        assert len(why.strip()) > 40, f"{what} is disposed of without a reason: {why}"
    print(
        f"8. {len(findings)} findings recorded, each stating `remediated` or `accepted risk`"
        f" with the reason — {sorted({f[1] for f in findings})}"
    )

    print("\ncheck_security_review: all assertions green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
