"""T-6.HARD.04 check — every master and every transaction change is on the trail.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_audit_coverage.py

T-0.AUDIT.02 built the trail; this is the **coverage** of it, verified by enumerating the
schema rather than by sampling it, and the one place where the enumeration found a gap. It
fails (non-zero exit) if any of these stops holding:

1. **every table is either audited or named as excluded** — the check reads the tables out of
   `information_schema` and the triggers out of `pg_trigger`, not out of the Python that
   installs them, and requires an enabled `<table>_audited` trigger running
   `audit_row_change()` for all of them but the four named in :data:`EXCLUDED`, each with the
   reason it is out. A table added next phase without the trail fails **by name**, which is
   what "any gap is named" means
2. **each exclusion is safe for the reason given, not by comment** — a child row written only
   with its append-only parent (`journal_line`, `approval_decision`) is covered by that guard
   and the parent's row, and an `approval_level` cannot change after the fact because its
   workflow refuses a second configuration: both are driven here, and the append-only side is
   read out of `pg_trigger`
3. **the child rows that do change on their own are on the trail** — the gap this verification
   found and closed: granting a capability, restricting a field, giving an existing party one
   more role and counting an item each wrote a row whose *parent* did not change, so nothing
   was recorded; each now leaves a trail row with the actor, the action and the values, under
   the company its parent carries
4. **attribution survives a commit inside one request** — the actor and the origin are
   transaction-scoped settings, and the platform commits half-way through a request in several
   places (an inbound event, a dunning run, an outbound delivery), so everything written after
   such a commit used to land on the trail as `unknown`: the second finding. The values stated
   are held on the session and re-applied to each transaction it opens, and this check writes,
   commits and writes again without stating them twice
5. **a create, an edit and a soft delete are all captured** with the actor, the timestamp and
   the values before and after — and the retirement is named a retirement rather than a
   nameless update
6. **the trail cannot be altered through the application's interfaces** — no module of the
   platform writes it but the trail's own two writers (checked by reading every source file),
   an UPDATE and a DELETE against it are refused by the database, and the refusal is reported
   to the caller in the platform's one error shape rather than swallowed
7. **the coverage matrix is printed** — table → the module that owns it → audited or the
   reason it is not, ordered by the module that owns it, so the shape of the coverage is
   readable rather than counted

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.api import _unexpected  # noqa: E402
from app.audit import (  # noqa: E402
    UNKNOWN_ACTOR,
    read_trail,
    set_actor,
    set_origin,
    soft_delete,
)
from app.company import Company  # noqa: E402
from app.db import CHILD_TABLES, Base, scope_to_company  # noqa: E402
from app.ledger import posting  # noqa: E402,F401 — every check builds the one schema
from app.ledger.currency import register_currency  # noqa: E402
from app.ledger.mapping import set_mapping  # noqa: E402
from app.ledger.posting import post_journal_entry  # noqa: E402
from app.party import Party, create_party  # noqa: E402
from app.procurement.suppliers import create_supplier  # noqa: E402
from app.sales.customers import create_customer  # noqa: E402
from app.security import Role, define_role, grant, restrict  # noqa: E402
from app.stock.counts import PhysicalCount, record_count, start_count  # noqa: E402
from app.stock.transactions import receive  # noqa: E402
from app.stock.items import Item, create_item  # noqa: E402
from app.stock.locations import create_location  # noqa: E402
from app.workflow import WorkflowAlreadyConfigured, configure  # noqa: E402
from tests.seed import seed_accounts  # noqa: E402

DAY = date(2026, 9, 17)

# The tables with no trail of their own, each with the reason — the same four the framework's
# docstring gives, now checked rather than asserted in prose.
EXCLUDED = {
    "audit_log": "the trail itself: a row describing a write to the trail would recurse",
    "journal_line": (
        "written only with its append-only parent, whose own row names the change"
    ),
    "approval_decision": (
        "appended, never changed (append_only), and it carries its own actor and instant"
    ),
    "approval_level": (
        "written with its workflow, which refuses a second configuration, so a level cannot"
        " change after the fact"
    ),
}

AUDIT_TRIGGER = "audit_row_change"


def _tables_and_triggers(engine) -> list[tuple[str, str | None]]:
    """Every base table in the schema with the function its audit trigger runs, or None.

    Read from the catalogue rather than from the Python that installs the triggers: a table
    that exists without a trigger is exactly what this check is for.
    """
    with engine.connect() as connection:
        return [
            (name, function)
            for name, function in connection.exec_driver_sql(
                """
                SELECT t.table_name,
                       (SELECT p.proname FROM pg_trigger g
                          JOIN pg_class c ON c.oid = g.tgrelid
                          JOIN pg_proc p ON p.oid = g.tgfoid
                         WHERE c.relname = t.table_name
                           AND g.tgname = t.table_name || '_audited'
                           AND NOT g.tgisinternal AND g.tgenabled <> 'D')
                  FROM information_schema.tables t
                 WHERE t.table_schema = 'public' AND t.table_type = 'BASE TABLE'
                 ORDER BY t.table_name
                """
            ).fetchall()
        ]


def _has_trigger(engine, table: str, suffix: str) -> bool:
    with engine.connect() as connection:
        return (
            connection.exec_driver_sql(
                """
                SELECT count(*) FROM pg_trigger g JOIN pg_class c ON c.oid = g.tgrelid
                 WHERE c.relname = %s AND g.tgname = %s AND NOT g.tgisinternal
                """,
                (table, f"{table}_{suffix}"),
            ).scalar()
            == 1
        )


def _owning_modules() -> dict[str, set[str]]:
    """Which module declares each table — the matrix's own column."""
    modules: dict[str, set[str]] = {}
    for mapper in Base.registry.mappers:
        modules.setdefault(mapper.class_.__module__, set()).add(mapper.persist_selectable.name)
    return modules


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

    # 1 — every table is audited, or named as excluded
    found = _tables_and_triggers(engine)
    unaudited = [name for name, function in found if function is None]
    gaps = [name for name in unaudited if name not in EXCLUDED]
    assert gaps == [], f"tables with no trail and no reason: {gaps}"
    assert sorted(unaudited) == sorted(EXCLUDED), (
        f"the exclusions are stale: {sorted(set(EXCLUDED) - set(unaudited))} are audited now"
    )
    wrong_function = [
        (name, function) for name, function in found if function not in (None, AUDIT_TRIGGER)
    ]
    assert wrong_function == [], f"a trail trigger runs something else: {wrong_function}"
    print(
        f"1. {len(found)} tables read out of the catalogue: {len(found) - len(EXCLUDED)} carry an"
        f" enabled `<table>_audited` trigger running {AUDIT_TRIGGER}(), and the"
        f" {len(EXCLUDED)} without one are named with the reason each is out — a table added"
        " without the trail fails by name"
    )

    # 2 — each exclusion is safe for the reason given
    for child in ("journal_line", "approval_decision"):
        assert _has_trigger(engine, child, "append_only"), (
            f"{child} is excluded because it cannot change, but it is not append-only"
        )

    company_id = uuid.uuid4()
    with Session(engine) as session:
        session.add(
            Company(id=company_id, code="TRAIL", name="Trail", base_currency="PHP",
                    fiscal_year_start_month=1)
        )
        session.commit()
        scope_to_company(session, company_id)
        seed_accounts(session, company_id=company_id)
        register_currency(session, company_id=company_id, code="PHP", name="Philippine Peso")
        for key, account_code in (("inventory", "1200"), ("stock_receipt", "2000"),
                                  ("stock_issue", "5000"), ("stock_adjustment", "5900")):
            set_mapping(session, company_id=company_id, key=key, account_code=account_code)
        set_actor(session, "tina")
        configure(session, company_id=company_id, doc_type="inventory_adjustment",
                  name="Inventory adjustments", levels=[(Decimal("50"), "controller")])
        session.commit()
        try:
            configure(session, company_id=company_id, doc_type="inventory_adjustment",
                      name="Inventory adjustments again", levels=[(Decimal("1"), "controller")])
        except WorkflowAlreadyConfigured as exc:
            reconfigured = str(exc)
            session.rollback()
        else:
            raise AssertionError("a workflow was configured twice; its levels could be changed")
        org = define_role(session, company_id=company_id, code="accountant", name="Accountant")
        grant(session, org, "journal.post")
        party = create_party(session, company_id=company_id, code="ACME", name="Acme Trading",
                             roles=["customer"])
        session.commit()
        bolt = create_item(session, company_id=company_id, sku="BOLT", name="Bolt",
                           base_uom="each", traceability_mode="none")
        warehouse = create_location(session, company_id=company_id, code="WH1", name="Main",
                                    location_type="warehouse")
        zone = create_location(session, company_id=company_id, code="WH1-Z", name="Zone",
                               location_type="zone", parent_id=warehouse.id)
        aisle = create_location(session, company_id=company_id, code="WH1-Z-A", name="Aisle",
                                location_type="aisle", parent_id=zone.id)
        bin_one = create_location(session, company_id=company_id, code="B1", name="Bin 1",
                                  location_type="bin", parent_id=aisle.id)
        session.commit()
        receive(session, item=bolt, location=bin_one, uom="each", quantity=10,
                value=Decimal("100.00"), currency="PHP", source_type="goods_receipt",
                source_id=uuid.uuid4(), posting_date=date(2026, 9, 17))
        session.commit()
        count = start_count(session, company_id=company_id, location=bin_one, actor="tina")
        session.commit()
        entry = post_journal_entry(
            session, company_id=company_id, posting_date=DAY, currency="PHP", memo="trail",
            lines=[{"account": "1000", "debit": Decimal("10.00")},
                   {"account": "4000", "credit": Decimal("10.00")}],
        )
        session.commit()
        assert entry.lines, "the posting has no lines to prove the exclusion against"
        # Plain ids: the objects belong to this session, which closes before the trail is read.
        role_id, party_id, count_id, entry_id = org.id, party.id, count.id, entry.id
        bolt_id = bolt.id
    assert _has_trigger(engine, "journal_line", "append_only"), "a line could be changed"
    print(
        f"2. the exclusions hold for their reasons: `journal_line` and `approval_decision` carry"
        f" an append-only trigger and their parents are audited; the workflow refuses a second"
        f" configuration ({reconfigured[:48]}…), so no level is written twice"
    )

    # 3 — the child rows that change on their own are on the trail
    #
    # Before this task each of these four left *no* row: the change is a child insert or update
    # and the parent it hangs from is untouched, so a trigger on the parent cannot see it.
    with Session(engine) as session:
        scope_to_company(session, company_id)
        set_actor(session, "tina")
        set_origin(session, "journal_entry", entry_id)
        role = session.get(Role, role_id)
        count = session.get(PhysicalCount, count_id)
        bolt = session.get(Item, bolt_id)
        grant(session, role, "journal.read")
        restrict(session, role, entity="journal_line", field="party", can_read=False)
        session.commit()
        set_origin(session, "physical_count", count_id)
        supplier = create_supplier(session, company_id=company_id, party_code="ACME",
                                  name="Acme Trading", payment_terms_days=30)
        session.commit()
        record_count(session, count, item=bolt, counted_quantity=9)
        session.commit()
        record_count(session, count, item=bolt, counted_quantity=7)
        session.commit()

        def rows(entity: str, entity_id) -> list:
            return read_trail(session, entity=entity, entity_id=entity_id)

        granted = rows("permission", role.id)
        assert granted, "granting a capability to an existing role left no trail row"
        restricted = rows("field_permission", role.id)
        assert restricted, "restricting a field left no trail row"
        roles = rows("party_role", party_id)
        assert roles, "giving an existing party one more role left no trail row"
        lines = rows("physical_count_line", count_id)
        counts = [row.action for row in lines]
        assert counts == ["insert", "update", "update"], counts
        assert supplier.party_id == party_id, "the supplier is not the party that gained a role"
        for row in granted + restricted + roles + lines:
            assert row.actor == "tina", row.actor
            assert row.occurred_at is not None, row
        snapped, counted, counted_again = lines
        assert snapped.before_values is None and snapped.after_values is not None, snapped
        assert counted.before_values["counted_quantity"] is None, counted.before_values
        moved = (
            Decimal(str(counted_again.before_values["counted_quantity"])),
            Decimal(str(counted_again.after_values["counted_quantity"])),
        )
        assert moved == (Decimal("9"), Decimal("7")), (counted_again.before_values,
                                                      counted_again.after_values)
        granted_capabilities = [row.after_values["capability"] for row in granted]
        assert "journal.read" in granted_capabilities, granted_capabilities
        assert "party" in restricted[0].after_values["field"], restricted[0].after_values
        assert "supplier" in roles[-1].after_values["role"], roles[-1].after_values
    with engine.connect() as connection:
        orphans = connection.exec_driver_sql(
            "SELECT count(*) FROM audit_log WHERE company_id IS NULL"
        ).scalar()
        child_rows = connection.exec_driver_sql(
            "SELECT count(*) FROM audit_log WHERE entity IN"
            " ('permission', 'field_permission', 'party_role', 'physical_count_line')"
            f" AND company_id = '{company_id}'"
        ).scalar()
        assert orphans == 0, f"{orphans} trail rows belong to no company"
        expected = len(granted) + len(restricted) + len(roles) + len(lines)
        assert child_rows == expected, (
            f"{child_rows} child-table trail rows in the database for {expected} recorded"
            " changes — the trail holds something this check did not make"
        )
    print(
        f"3. the four child rows that change on their own are on the trail under the company"
        f" their parent carries: {len(granted)} capabilities granted on one role,"
        f" {len(restricted)} field restricted, {len(roles)} role given to a party that already"
        f" existed and {len(lines)} changes to one count line ({', '.join(counts)}), the last"
        f" naming what it moved ({moved[0]} → {moved[1]}) against the snapshot the count"
        f" left ({snapped.after_values['system_quantity']}), {orphans} rows without a company"
    )

    # 4 — attribution survives a commit inside one request
    #
    # The actor is a transaction-scoped setting and the platform commits mid-request (the
    # inbound integration handler, a dunning run, an outbound delivery), so everything written
    # after such a commit landed on the trail as `unknown`. This writes, commits and writes
    # again *without* stating the actor a second time.
    receipt = uuid.uuid4()
    with Session(engine) as session:
        scope_to_company(session, company_id)
        set_actor(session, "dana")
        set_origin(session, "goods_receipt", receipt)
        create_location(session, company_id=company_id, code="WH2", name="Overflow",
                        location_type="warehouse")
        session.commit()
        later = create_location(session, company_id=company_id, code="WH3", name="Returns",
                                location_type="warehouse")
        session.commit()
        after_commit = read_trail(session, entity="location", entity_id=later.id)
        assert [row.actor for row in after_commit] == ["dana"], after_commit
        assert after_commit[0].origin_type == "goods_receipt", after_commit[0]
        assert after_commit[0].origin_id == str(receipt), after_commit[0]
    print(
        "4. the actor and the origin stated once are re-applied to every transaction the"
        f" session opens: a warehouse created after a commit is still '{after_commit[0].actor}'s,"
        f" against '{after_commit[0].origin_type}' {after_commit[0].origin_id[:8]}…, not"
        f" '{UNKNOWN_ACTOR}'"
    )

    # 5 — a create, an edit and a soft delete, with before and after
    with Session(engine) as session:
        scope_to_company(session, company_id)
        set_actor(session, "mia")
        boreal = create_party(session, company_id=company_id, code="BOREAL",
                              name="Boreal Ltd", roles=["employee"])
        session.commit()
        create_customer(session, company_id=company_id, party_code="BOREAL",
                        credit_limit="1000.00")
        session.commit()
        boreal.name = "Boreal Limited"
        session.commit()
        soft_delete(session, boreal)
        session.commit()
        party_trail = read_trail(session, entity="party", entity_id=boreal.id)
        actions = [row.action for row in party_trail]
        assert actions == ["insert", "update", "soft_delete"], actions
        created, edited, retired = party_trail
        assert {row.actor for row in party_trail} == {"mia"}, actions
        assert edited.before_values["name"] == "Boreal Ltd", edited.before_values
        assert edited.after_values["name"] == "Boreal Limited", edited.after_values
        assert created.before_values is None and created.after_values is not None, created
        assert retired.before_values["deleted_at"] is None, retired.before_values
        assert retired.after_values["deleted_at"] is not None, retired.after_values
        assert all(row.occurred_at is not None for row in party_trail), actions
    print(
        f"5. one master's whole life is attributable ({', '.join(actions)}), the edit carrying"
        f" '{edited.before_values['name']}' → '{edited.after_values['name']}' and the retirement"
        " named a retirement rather than an update"
    )

    # 6 — the trail cannot be altered through the application's interfaces
    # The trail has exactly two writers by design: the framework itself (`app/audit.py`, which
    # installs the trigger and holds the table) and an authorisation refusal being recorded
    # (`app/security.py`, `record_refusal`). Any third is a module that could rewrite history.
    writers = []
    for path in sorted(ROOT.glob("app/**/*.py")):
        source = path.read_text()
        relative = str(path.relative_to(ROOT))
        if re.search(r"AuditLog\(|audit_log", source) and relative not in (
            "app/audit.py",
            "app/security.py",
        ):
            writers.append(relative)
    assert writers == [], f"something besides the trail's own two writers touches it: {writers}"
    with Session(engine) as session:
        scope_to_company(session, company_id)
        set_actor(session, "nobody")
        refusal = None
        try:
            session.connection().exec_driver_sql(
                "UPDATE audit_log SET actor = 'somebody else' WHERE entity = 'party'"
            )
        except DBAPIError as exc:
            refusal = exc
            session.rollback()
        assert refusal is not None, "the trail accepted an UPDATE"
        reported = asyncio.run(_unexpected(None, refusal)).body.decode()
        assert "append-only" in str(refusal), refusal
        assert "is append-only" in reported and "internal_error" in reported, reported
        deleted = None
        try:
            session.connection().exec_driver_sql("DELETE FROM audit_log WHERE entity = 'party'")
        except DBAPIError as exc:
            deleted = exc
            session.rollback()
        assert deleted is not None, "the trail accepted a DELETE"
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM audit_log").scalar() > 0, (
            "the trail was emptied"
        )
    print(
        f"6. nothing writes the trail but its own two writers — the framework and a recorded"
        f" refusal — read out of {len(list(ROOT.glob('app/**/*.py')))} source files; an UPDATE"
        f" and a DELETE against it are refused"
        f" ('{str(refusal).splitlines()[0][:44]}…') and the refusal reaches the caller in the"
        " platform's one error shape"
    )

    # 7 — the matrix, printed
    modules = _owning_modules()
    audited = {name for name, function in found if function is not None}
    for module in sorted(modules):
        tables = sorted(modules[module])
        covered = sum(1 for name in tables if name in audited)
        missing = [name for name in tables if name not in audited]
        note = f" — out: {', '.join(missing)}" if missing else ""
        print(f"     {module}: {covered}/{len(tables)} tables on the trail{note}")
    print(
        f"7. the matrix above is the coverage: {len(found)} tables over {len(modules)} modules,"
        f" {len(audited)} audited, {len(EXCLUDED)} excluded with a reason and no table"
        " unaccounted for"
    )

    print("\ncheck_audit_coverage: all assertions green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
