"""T-6.ANALYTICS.01 check — the dashboard: figures that open, reconcile, and are permissioned.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_dashboard.py

A dashboard is where a number gets believed, so this check is what makes believing it reasonable.
It fails (non-zero exit) if any of these stops holding:

1. **every figure opens onto the rows it was summed from** — asking for a tile's drill-down
   returns the open invoices, the items on hand or the accounts the profit is made of, and those
   rows add up to the figure beside them; the answer is bounded, and says how many rows stand
   behind it and whether they were cut
2. **every figure reconciles to something independent** — the profit and loss agrees with the
   trial balance's own arithmetic for the same window, receivables and payables agree with their
   subledgers' reconciliation against the control accounts, and inventory agrees with the
   inventory account, each recomputed here rather than trusted, and each difference **reported**
   rather than absorbed: an injected posting straight to the control account moves the tile to
   `balanced: false` with the difference stated, and the figure itself does not move, because
   the invoices did not
3. **a caller without a tile's capability sees neither the figure nor its aggregate** — the tile
   is absent from `tiles` and named in `withheld` with its capability and the refusal's own
   sentence, no figure of it appears anywhere in the answer, asking for its drill-down returns
   no rows, and the refusal is on the trail (T-0.SEC.01) so the attempt is attributable
4. **the dashboard is current and says when it was made** — a document posted after one call
   changes the next call's figure, and `generated_at` advances with it: there is no overnight
   snapshot to be stale
5. **the tiles a caller sees are the ones its capabilities name** — a role holding every tile's
   capability sees all of them, in registry order, each naming the capability that guards it
6. **the period is the caller's, and a window that cannot be read is refused** — an explicit
   window returns the same figures the statement layer produces for it, the default window is
   the month `as_of` is in (the window T-1.ACCT.07's own builders use), and a window ending
   before it starts is `422 invalid_window` rather than a set of figures nobody can interpret

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import analytics  # noqa: E402
from app.analytics import TILES  # noqa: E402
from app.ap.invoices import SupplierInvoice  # noqa: E402,F401 — for its table
from app.ap.invoices import create_invoice as create_supplier_invoice  # noqa: E402
from app.ap.invoices import post_invoice as post_supplier_invoice  # noqa: E402
from app.api import BASE, app  # noqa: E402
from app.ar.invoices import create_invoice as create_customer_invoice  # noqa: E402
from app.ar.invoices import post_invoice as post_customer_invoice  # noqa: E402
from app.audit import read_trail, set_actor  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base, scope_to_company  # noqa: E402
from app.ledger import posting  # noqa: E402,F401 — every check builds the one schema
from app.ledger.accounts import create_account  # noqa: E402
from app.ledger.currency import register_currency  # noqa: E402
from app.ledger.mapping import set_mapping  # noqa: E402
from app.ledger.posting import post_journal_entry  # noqa: E402
from app.ledger.statements import profit_and_loss, trial_balance  # noqa: E402
from app.procurement import receipts as _receipts  # noqa: E402,F401 — the FK target
from app.procurement.suppliers import add_tax_identifier, create_supplier  # noqa: E402
from app.sales.customers import Customer, create_customer  # noqa: E402
from app.security import Role, assign, define_role, grant  # noqa: E402
from app.stock.items import create_item  # noqa: E402
from app.stock.locations import create_location  # noqa: E402
from app.stock.transactions import receive  # noqa: E402
from tests.seed import seed_stock_accounts  # noqa: E402

DAY = date(2026, 9, 30)

TILE_CODES = [code for code, _label, _capability, _builder in TILES]
TILE_CAPABILITIES = {code: capability for code, _label, capability, _builder in TILES}

ALL_TILES = ["report.read", "invoice.read", "stock.read"]


def _tile(answer: dict, code: str) -> dict:
    for tile in answer["tiles"]:
        if tile["code"] == code:
            return tile
    raise AssertionError(f"{code} is not in the answer: {[t['code'] for t in answer['tiles']]}")


def _leaves(node):
    """Every scalar in a payload, so "this figure is not in the answer" can be checked
    without a substring test that '100.00' inside '1000.00' would fool."""
    if isinstance(node, dict):
        for value in node.values():
            yield from _leaves(value)
    elif isinstance(node, list):
        for item in node:
            yield from _leaves(item)
    else:
        yield node


def _get(client: TestClient, company_id: uuid.UUID, actor: str, **params) -> dict:
    query = "&".join(f"{key}={value}" for key, value in params.items() if value is not None)
    response = client.get(
        f"{BASE}/dashboard{f'?{query}' if query else ''}",
        headers={"X-Company-Id": str(company_id), "X-Actor": actor},
    )
    assert response.status_code == 200, response.text
    return response.json()


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
    with Session(engine) as session:
        session.add(
            Company(id=company_id, code="DASH", name="Dashboards", base_currency="PHP",
                    fiscal_year_start_month=1)
        )
        session.commit()
        scope_to_company(session, company_id)
        set_actor(session, "mia")
        seed_stock_accounts(session, company_id=company_id)
        register_currency(session, company_id=company_id, code="PHP", name="Philippine Peso")
        create_account(session, company_id=company_id, code="2200", name="Output VAT",
                       account_class="liability")
        create_account(session, company_id=company_id, code="1300", name="Input VAT",
                       account_class="asset")
        for key, code in (("receivables", "1100"), ("revenue", "4000"),
                          ("payables", "2000"), ("expense", "5200"),
                          ("input_tax", "1300"), ("output_tax", "2200")):
            set_mapping(session, company_id=company_id, key=key, account_code=code)
        reader = define_role(session, company_id=company_id, code="viewer", name="Viewer")
        grant(session, reader, "report.read")
        accountant = define_role(session, company_id=company_id, code="accountant",
                                 name="Accountant")
        for capability in ALL_TILES:
            grant(session, accountant, capability)
        assign(session, company_id=company_id, subject="vera", role=reader)
        assign(session, company_id=company_id, subject="tina", role=accountant)
        session.commit()

        # A receivable of 1 000.00, a payable of 400.00 and 100.00 of stock on the shelf: one
        # document each, so the figure and the rows behind it are checkable by eye as well as
        # by arithmetic.
        customer = create_customer(session, company_id=company_id, party_code="ACME",
                                   name="Acme Trading")
        supplier = create_supplier(session, company_id=company_id, party_code="BOREAL",
                                   name="Boreal Supplies", payment_terms_days=30)
        session.commit()
        receivable = create_customer_invoice(
            session, company_id=company_id, number="AR-1", customer=customer,
            invoice_date=DAY - timedelta(days=5), terms_days=30,
            lines=[{"description": "Goods", "quantity": "1", "unit_price": "1000.00"}],
        )
        session.commit()
        post_customer_invoice(session, receivable)
        session.commit()
        payable = create_supplier_invoice(
            session, company_id=company_id, number="AP-1", supplier=supplier,
            supplier_reference="AP-1", invoice_date=DAY - timedelta(days=5), terms_days=30,
            lines=[{"description": "Parts", "quantity": "1", "unit_price": "400.00"}],
        )
        session.commit()
        add_tax_identifier(session, supplier, kind="tin", value="009-876-543")
        session.commit()
        post_supplier_invoice(session, payable)
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
        customer_id = customer.id
        receive(session, item=bolt, location=bin_one, uom="each", quantity=10,
                value=Decimal("100.00"), currency="PHP", source_type="goods_receipt",
                source_id=uuid.uuid4(), posting_date=DAY)
        session.commit()

    client = TestClient(app, raise_server_exceptions=False)

    # 1 — every figure opens onto the rows it was summed from
    answer = _get(client, company_id, "tina", as_of=DAY.isoformat())
    assert [tile["code"] for tile in answer["tiles"]] == TILE_CODES, answer["tiles"]
    opened = _get(client, company_id, "tina", as_of=DAY.isoformat(),
                  drill_down="finance.receivables")
    receivables = _tile(opened, "finance.receivables")
    assert receivables["basis_rows"] == 1 and receivables["basis_truncated"] is False, receivables
    invoice = receivables["basis"][0]
    assert invoice["document"] == "AR-1" and invoice["party"] == "ACME", invoice
    assert invoice["open_amount"] == receivables["figures"]["currencies"]["PHP"]["outstanding"], (
        invoice,
        receivables["figures"],
    )
    stock = _tile(
        _get(client, company_id, "tina", as_of=DAY.isoformat(), drill_down="operations.stock"),
        "operations.stock",
    )
    assert [row["item"] for row in stock["basis"]] == ["BOLT"], stock["basis"]
    assert sum(Decimal(row["value"]) for row in stock["basis"]) == Decimal(
        stock["figures"]["value"]
    ), stock
    statement = _tile(
        _get(client, company_id, "tina", as_of=DAY.isoformat(),
             drill_down="finance.profit_and_loss"),
        "finance.profit_and_loss",
    )
    by_class = {
        side: sum(
            (Decimal(row["amount"]) for row in statement["basis"] if row["class"] == side),
            Decimal(0),
        )
        for side in ("income", "expense")
    }
    figures = statement["figures"]
    assert by_class["income"] == Decimal(figures["income"]), (by_class, figures)
    assert by_class["expense"] == Decimal(figures["expenses"]), (by_class, figures)
    assert by_class["income"] - by_class["expense"] == Decimal(figures["net_profit"]), (
        by_class,
        figures,
    )
    # The bound is stated, not silent: with the cap at one row, the answer says so and still
    # reports how many rows stand behind the figure.
    original = analytics.MAX_BASIS
    try:
        analytics.MAX_BASIS = 1
        session_bolt = stock["figures"]["value"]
        capped = _tile(
            _get(client, company_id, "tina", as_of=DAY.isoformat(),
                 drill_down="finance.profit_and_loss"),
            "finance.profit_and_loss",
        )
    finally:
        analytics.MAX_BASIS = original
    assert len(capped["basis"]) <= 1, capped["basis"]
    assert capped["basis_rows"] > 1 and capped["basis_truncated"] is True, capped
    print(
        f"1. {len(TILE_CODES)} tiles, each opening onto its own rows: receivables onto"
        f" {invoice['document']} ({invoice['open_amount']}), inventory onto"
        f" {len(stock['basis'])} item(s) worth {session_bolt}, and the profit and loss onto its"
        f" {capped['basis_rows']} accounts — cut to {len(capped['basis'])} row with"
        " `basis_truncated` saying so rather than returning a year of invoices"
    )

    # 2 — every figure reconciles to something independent, recomputed here
    with Session(engine) as session:
        scope_to_company(session, company_id)
        ledger = profit_and_loss(session, company_id=company_id,
                                 start=DAY.replace(day=1), end=DAY)
        trial = trial_balance(session, company_id=company_id,
                              start=DAY.replace(day=1), end=DAY)
        account = {row["account"]: row for row in trial["rows"]}
        from_ledger = (
            -Decimal(account.get("4000", {"balance": "0"})["balance"])
            - Decimal(account.get("4100", {"balance": "0"})["balance"])
            - (Decimal(account.get("5000", {"balance": "0"})["balance"])
               + Decimal(account.get("5200", {"balance": "0"})["balance"])
               + Decimal(account.get("5900", {"balance": "0"})["balance"]))
        ).quantize(analytics.MONEY)
    profit = _tile(answer, "finance.profit_and_loss")
    assert Decimal(profit["figures"]["net_profit"]) == Decimal(ledger["net_profit"]), profit
    assert Decimal(profit["reconciled_to"]["figure"]) == from_ledger, profit["reconciled_to"]
    assert Decimal(profit["reconciled_to"]["figure"]) == from_ledger.quantize(analytics.MONEY), (
        profit["reconciled_to"]
    )
    assert profit["reconciled_to"]["difference"] == "0.00", profit["reconciled_to"]
    assert profit["reconciled_to"]["balanced"] is True, profit["reconciled_to"]
    payables = _tile(answer, "finance.payables")
    assert payables["reconciled_to"]["balanced"] is True, payables["reconciled_to"]
    assert payables["figures"]["currencies"]["PHP"] == {
        "outstanding": "400.00",
        "control": "400.00",
        "difference": "0.00",
        "balanced": True,
    }, payables["figures"]
    receivables_tile = _tile(answer, "finance.receivables")
    assert receivables_tile["reconciled_to"]["balanced"] is True, receivables_tile["reconciled_to"]
    assert receivables_tile["reconciled_to"]["difference_total"] == "0.00", receivables_tile
    assert _tile(answer, "operations.stock")["reconciled_to"] == {
        "against": "the inventory control account 1200",
        "figure": "100.00",
        "difference": "0.00",
        "balanced": True,
    }, _tile(answer, "operations.stock")["reconciled_to"]

    # An injected posting straight to the receivables control account: the invoices did not
    # change, so the figure must not; the reconciliation must say what the difference is.
    with Session(engine) as session:
        scope_to_company(session, company_id)
        set_actor(session, "mia")
        drift = uuid.uuid4()
        post_journal_entry(
            session, company_id=company_id, posting_date=DAY, currency="PHP",
            memo="injected difference", source_type="manual", source_id=drift,
            lines=[{"account": "1100", "debit": Decimal("250.00")},
                   {"account": "3000", "credit": Decimal("250.00")}],
        )
        session.commit()
    drifted = _tile(_get(client, company_id, "tina", as_of=DAY.isoformat()),
                    "finance.receivables")
    assert drifted["figures"]["currencies"]["PHP"]["outstanding"] == "1120.00", drifted["figures"]
    # The control account holds more than the invoices say, so the difference is the account's
    # own: -250.00 from the subledger's side, and the tile states it rather than hiding it.
    assert drifted["figures"]["currencies"]["PHP"]["difference"] == "-250.00", drifted["figures"]
    assert drifted["reconciled_to"]["difference_total"] == "250.00", drifted["reconciled_to"]
    assert drifted["reconciled_to"]["balanced"] is False, drifted["reconciled_to"]
    with Session(engine) as session:
        scope_to_company(session, company_id)
        set_actor(session, "mia")
        post_journal_entry(
            session, company_id=company_id, posting_date=DAY, currency="PHP",
            memo="reversing it", source_type="manual", source_id=drift,
            lines=[{"account": "3000", "debit": Decimal("250.00")},
                   {"account": "1100", "credit": Decimal("250.00")}],
        )
        session.commit()
    assert _tile(
        _get(client, company_id, "tina", as_of=DAY.isoformat()), "finance.receivables"
    )["reconciled_to"]["balanced"] is True
    print(
        "2. every tile reconciles to its own independent source, recomputed here: the profit and"
        f" loss to the trial balance's {from_ledger}, the subledgers to their control accounts"
        " and inventory to account 1200 (all 0.00 difference); 250.00 posted straight to the"
        " control account made the tile report a 250.00 difference while the figure stayed"
        f" {drifted['figures']['currencies']['PHP']['outstanding']}, and reversing it balanced again"
    )

    # 3 — a caller without a tile's capability sees neither the figure nor its aggregate
    hidden = _get(client, company_id, "vera", as_of=DAY.isoformat(),
                  drill_down="finance.receivables")
    assert [tile["code"] for tile in hidden["tiles"]] == ["finance.profit_and_loss"], hidden
    assert [tile["code"] for tile in hidden["withheld"]] == [
        "finance.receivables",
        "finance.payables",
        "operations.stock",
    ], hidden["withheld"]
    assert all(
        tile["capability"] in tile["reason"] for tile in hidden["withheld"]
    ), hidden["withheld"]
    assert all(
        set(entry) == {"code", "label", "capability", "reason"} for entry in hidden["withheld"]
    ), hidden["withheld"]
    assert all(tile.get("basis") is None for tile in hidden["tiles"]), (
        "a tile returned rows the request did not ask for"
    )
    # The withheld entries carry nothing but the refusal, and no figure of a withheld tile is
    # anywhere in the answer — compared as whole payloads, because a value may legitimately
    # appear in a tile the caller *may* see.
    body = json.dumps(hidden, sort_keys=True)
    for tile in hidden["withheld"]:
        figures = json.dumps(_tile(answer, tile["code"])["figures"], sort_keys=True)
        assert figures not in body, f"{tile['code']}'s figures reached {body}"
    assert _tile(hidden, "finance.profit_and_loss")["figures"] == _tile(
        answer, "finance.profit_and_loss"
    )["figures"], "the tile a viewer sees is not the one a permitted caller sees"
    marks = {str(leaf) for leaf in _leaves(hidden)}
    for mark in ("AR-1", "AP-1", "1200", "outstanding"):
        assert mark not in marks, f"{mark} reached a caller who may not see that tile"
    with Session(engine) as session:
        scope_to_company(session, company_id)
        refusals = read_trail(session, entity="dashboard_tile")
        assert {row.entity_id for row in refusals} == {
            tile["code"] for tile in hidden["withheld"]
        }, refusals
        assert {row.actor for row in refusals} == {"vera"}, refusals
        assert all(row.action == "refused" for row in refusals), refusals
        for row in refusals:
            assert TILE_CAPABILITIES[row.entity_id] in row.after_values["attempted"], row
    print(
        f"3. a viewer holding only 'report.read' was shown {[t['code'] for t in hidden['tiles']]}"
        f" and told the other {len(hidden['withheld'])} tiles are withheld by capability"
        f" ({', '.join(tile['capability'] for tile in hidden['withheld'])}), with no figure, no"
        f" aggregate and no rows of them anywhere in the answer — and {len(refusals)} refusals"
        f" naming 'vera' are on the trail, one per tile"
    )

    # 4 — current, and said so
    before = _get(client, company_id, "tina", as_of=DAY.isoformat())
    with Session(engine) as session:
        scope_to_company(session, company_id)
        set_actor(session, "mia")
        extra = create_customer_invoice(
            session, company_id=company_id, number="AR-2",
            customer=session.get(Customer, customer_id),
            invoice_date=DAY, terms_days=30,
            lines=[{"description": "More goods", "quantity": "1", "unit_price": "50.00"}],
        )
        session.commit()
        post_customer_invoice(session, extra)
        session.commit()
    after = _get(client, company_id, "tina", as_of=DAY.isoformat())
    assert _tile(before, "finance.receivables")["figures"]["currencies"]["PHP"][
        "outstanding"
    ] == "1120.00"
    assert _tile(after, "finance.receivables")["figures"]["currencies"]["PHP"][
        "outstanding"
    ] == "1176.00", after
    assert after["generated_at"] > before["generated_at"], (before, after)
    stamped = datetime.fromisoformat(after["generated_at"])
    assert datetime.now(timezone.utc) - stamped < timedelta(minutes=1), stamped
    print(
        "4. a second invoice posted between two calls moved the tile from 1120.00 to"
        f" {after['tiles'][1]['figures']['currencies']['PHP']['outstanding']} and the answer"
        f" carries the instant it was read ({after['generated_at']}) — nothing overnight to go"
        " stale"
    )

    # 5 — the tiles a caller sees are the ones its capabilities name
    for actor, expected in (("vera", ["finance.profit_and_loss"]), ("tina", TILE_CODES)):
        seen = _get(client, company_id, actor, as_of=DAY.isoformat())
        assert [tile["code"] for tile in seen["tiles"]] == expected, (actor, seen["tiles"])
        for tile in seen["tiles"]:
            assert tile["capability"] == TILE_CAPABILITIES[tile["code"]], tile
        if actor == "tina":
            assert seen["withheld"] == [], seen
    print(
        "5. the tiles are the caller's capabilities, tile by tile: 'tina' holds all"
        f" {len(TILE_CODES)} and is withheld nothing, 'vera' holds 'report.read' and is shown"
        " exactly the profit and loss, each tile naming the capability that guards it"
    )

    # 6 — the period is the caller's window, and a window that cannot be read is refused
    with Session(engine) as session:
        scope_to_company(session, company_id)
        explicit = profit_and_loss(session, company_id=company_id,
                                   start=DAY - timedelta(days=90), end=DAY)
    windowed = _tile(
        _get(client, company_id, "tina", as_of=DAY.isoformat(),
             start=(DAY - timedelta(days=90)).isoformat()),
        "finance.profit_and_loss",
    )
    # The tile states money at the contract's own scale; the statement layer keeps six places,
    # so the two are compared as values rather than as strings.
    assert Decimal(windowed["figures"]["net_profit"]) == Decimal(explicit["net_profit"]), (
        windowed,
        explicit,
    )
    assert windowed["label"].endswith(str(DAY)), windowed["label"]
    default = _tile(_get(client, company_id, "tina", as_of=DAY.isoformat()),
                    "finance.profit_and_loss")
    assert "2026-09-01 to 2026-09-30" in default["label"], default["label"]
    backwards = client.get(
        f"{BASE}/dashboard?as_of=2026-09-30&start=2026-10-31",
        headers={"X-Company-Id": str(company_id), "X-Actor": "tina"},
    )
    assert backwards.status_code == 422, backwards.text
    assert backwards.json()["error"]["code"] == "invalid_window", backwards.json()
    missing = client.get(
        f"{BASE}/dashboard?drill_down=finance.nothing",
        headers={"X-Company-Id": str(company_id), "X-Actor": "tina"},
    )
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "no_dashboard_tile", (
        missing.text
    )
    print(
        f"6. the window is the caller's: 90 days back gives the statement's own"
        f" {explicit['net_profit']}, no window stated means the month ({default['label']}), a"
        f" window ending before it starts is {backwards.status_code}"
        f" {backwards.json()['error']['code']} and an unknown tile is {missing.status_code}"
        f" {missing.json()['error']['code']}"
    )

    print("\ncheck_dashboard: all assertions green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
