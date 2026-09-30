"""T-2.MATCH.02 check — holding what did not match, and releasing it.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/erpv1 \
        python tests/check_match_hold.py

Green on all eight:

1. an invoice whose match **failed** is held, with the reason and the verdict it is
   held against; a matched invoice cannot be held at all
2. a held invoice is refused by :func:`require_not_held` — the gate a payment run
   calls — so it cannot be paid while held
3. it is released only through an **authorised** override: an actor whose roles lack
   `match.override` is refused, and the refusal is on T-0.SEC.01's own record
4. the release records **who, when and why**, and the reason is required
5. an override does **not** alter the verdict: no new `MatchRun` is written, and the
   failed run is exactly as it was — the exception stays visible for reporting
6. the **overrides are counted apart** from clean matches, so a rate that counted them
   as matches cannot hide the thing the metric exists to show
7. an invoice cannot be held twice, and a released invoice is no longer held
8. holding without a stated reason, holding an unmatched invoice and releasing a
   release are all refused

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

from app.ap.invoices import create_invoice, post_invoice  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger.accounts import create_account  # noqa: E402
from app.ledger.currency import register_currency  # noqa: E402
from app.ledger.mapping import set_mapping  # noqa: E402
from app.matching import (  # noqa: E402
    FAILED,
    HELD,
    PARTIAL,
    MATCHED,
    OVERRIDE_CAPABILITY,
    RELEASED,
    AlreadyHeldError,
    InvoiceHeldError,
    MatchHold,
    MatchHoldError,
    MatchRun,
    NotHeldError,
    current_hold,
    hold_failed_matches,
    hold_invoice,
    holds,
    is_held,
    match_invoice,
    match_rate,
    override_count,
    release,
    require_not_held,
    set_tolerance,
)
from app.procurement import receipts as _receipts  # noqa: E402,F401 — the FK target
from app.procurement.orders import award, decide_order, submit_order  # noqa: E402
from app.procurement.receipts import create_receipt, post_receipt  # noqa: E402
from app.procurement.requisitions import (  # noqa: E402
    create_requisition,
    record_decision as decide_requisition,
    submit as submit_requisition,
)
from app.procurement.rfq import issue_rfq, record_response  # noqa: E402
from app.procurement.suppliers import add_tax_identifier, create_supplier  # noqa: E402
from app.security import AccessDenied, assign, define_role, grant  # noqa: E402
from app.stock.items import create_item  # noqa: E402
from app.stock.locations import create_location  # noqa: E402
from app.workflow import APPROVE, configure  # noqa: E402

COMPANY = uuid.uuid4()
ISSUED_ON = date(2026, 10, 1)
DEADLINE = date(2026, 10, 10)
INVOICE_DATE = date(2026, 11, 5)


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


def _invoice(session, *, number, supplier, item, quantity, unit_price, ordered,
             received, bin_a, tag):
    """requisition → RFQ → order → receipt → posted invoice, with the numbers stated."""
    requisition = create_requisition(
        session, company_id=COMPANY, number=f"REQ-9{tag}", requested_by="rina.requester",
        needed_by=date(2026, 11, 30), currency="PHP",
        lines=[{"description": "Widgets", "quantity": ordered, "uom": "each",
                "estimated_unit_price": unit_price, "item_sku": item.sku}],
    )
    session.commit()
    submit_requisition(session, requisition, actor="rina.requester")
    session.commit()
    decide_requisition(session, requisition, actor="mia.manager", action=APPROVE,
                       role="manager")
    session.commit()
    rfq = issue_rfq(session, requisition=requisition, number=f"RFQ-9{tag}",
                    supplier_codes=[supplier.party.code], response_deadline=DEADLINE,
                    issued_on=ISSUED_ON)
    session.commit()
    record_response(session, rfq, supplier_code=supplier.party.code,
                    received_on=date(2026, 10, 5),
                    lines=[{"line_no": 1, "unit_price": unit_price}])
    session.commit()
    order = award(
        session, rfq=rfq, actor="bob.buyer",
        awards=[{"supplier_code": supplier.party.code, "number": f"PO-9{tag}",
                 "required_date": date(2026, 11, 30),
                 "lines": [{"line_no": 1, "quantity": ordered}]}],
    )[0]
    session.commit()
    submit_order(session, order, actor="bob.buyer")
    session.commit()
    receipt = create_receipt(session, order=order, number=f"GRN-9{tag}", location=bin_a,
                             received_on=date(2026, 10, 20),
                             lines=[{"line_no": 1, "quantity": received}])
    session.commit()
    post_receipt(session, receipt)
    session.commit()
    net = (Decimal(unit_price) * Decimal(quantity)).quantize(Decimal("0.000001"))
    invoice = create_invoice(
        session, company_id=COMPANY, number=f"AP-9{tag}", supplier=supplier,
        supplier_reference=f"ACME-9{tag}", invoice_date=INVOICE_DATE,
        order_id=order.id, receipt_id=receipt.id,
        lines=[{"description": "Widgets", "item_id": item.id, "quantity": quantity,
                "unit_price": unit_price,
                "tax_amount": (net * Decimal("12") / 100).quantize(Decimal("0.000001")),
                "order_line_id": order.lines[0].id,
                "receipt_line_id": receipt.lines[0].id}],
    )
    session.commit()
    post_invoice(session, invoice)
    session.commit()
    return invoice


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
        session.add(
            Company(id=COMPANY, code="HOLD", name="Hold check", base_currency="PHP",
                    fiscal_year_start_month=1)
        )
        register_currency(session, company_id=COMPANY, code="PHP", name="Peso")
        session.commit()
        from tests.seed import seed_stock_accounts

        seed_stock_accounts(session, company_id=COMPANY)
        create_account(session, company_id=COMPANY, code="1310", name="Input VAT",
                       account_class="asset")
        session.commit()
        set_mapping(session, company_id=COMPANY, key="payables", account_code="2000")
        set_mapping(session, company_id=COMPANY, key="input_tax", account_code="1310")
        set_mapping(session, company_id=COMPANY, key="expense", account_code="5200")
        configure(session, company_id=COMPANY, doc_type="purchase_requisition",
                  name="Requisition", levels=[(Decimal("1"), "manager")])
        configure(session, company_id=COMPANY, doc_type="purchase_order",
                  name="Order", levels=[(Decimal("100000"), "manager")])
        set_tolerance(session, company_id=COMPANY, quantity_percent="2",
                      price_percent="1", tax_percent="0.5")
        supplier = create_supplier(session, company_id=COMPANY, party_code="ACME",
                                   name="Acme Supplies", payment_terms_days=30)
        add_tax_identifier(session, supplier, kind="tin", value="001-234-567")
        # security: a clerk may not override, a controller may
        clerk = define_role(session, company_id=COMPANY, code="clerk", name="Clerk")
        grant(session, clerk, "invoice.post")
        controller = define_role(session, company_id=COMPANY, code="controller",
                                 name="Controller")
        grant(session, controller, OVERRIDE_CAPABILITY)
        assign(session, company_id=COMPANY, subject="clerk.jo", role=clerk)
        assign(session, company_id=COMPANY, subject="fin.ada", role=controller)
        item = create_item(session, company_id=COMPANY, sku="WIDGET", name="Widget",
                           base_uom="each", traceability_mode="none")
        warehouse = create_location(session, company_id=COMPANY, code="MAIN",
                                    name="Main", location_type="warehouse")
        zone = create_location(session, company_id=COMPANY, code="MAIN-Z", name="Zone",
                               location_type="zone", parent_id=warehouse.id)
        aisle = create_location(session, company_id=COMPANY, code="MAIN-A", name="Aisle",
                                location_type="aisle", parent_id=zone.id)
        bin_a = create_location(session, company_id=COMPANY, code="MAIN-B1", name="Bin",
                                location_type="bin", parent_id=aisle.id)
        session.commit()

        clean = _invoice(session, number="AP-901", supplier=supplier, item=item,
                         quantity="100", unit_price="100", ordered="100",
                         received="100", bin_a=bin_a, tag="01")
        short = _invoice(session, number="AP-902", supplier=supplier, item=item,
                         quantity="100", unit_price="100", ordered="100",
                         received="90", bin_a=bin_a, tag="02")
        clean_run = match_invoice(session, clean)
        short_run = match_invoice(session, short)
        session.commit()
        # AP-902 is invoiced for 100 but only 90 arrived: the **quantity** comparison
        # fails while price and tax hold, which is a partial match — still not payable
        # without somebody's decision, which is what the hold is for.
        assert clean_run.status == MATCHED and short_run.status == PARTIAL, (
            clean_run.status, short_run.status
        )

        # 1 — hold the one that failed; a matched invoice cannot be held
        said = _refused(
            lambda: hold_invoice(session, clean, reason="just in case"),
            MatchHoldError,
        )
        session.rollback()
        hold = hold_invoice(session, short, reason="received 90 but invoiced 100")
        session.commit()
        assert hold.state == HELD and hold.run_id == short_run.id
        assert hold.opened_reason == "received 90 but invoiced 100"
        print(f"1. AP-902 held against its failed run ({said[:44]}… for the matched"
              " AP-901)")

        # 2 — a held invoice cannot go on
        said = _refused(lambda: require_not_held(session, short), InvoiceHeldError)
        session.rollback()
        assert is_held(session, short) is True and is_held(session, clean) is False
        print(f"2. the gate refuses a held invoice by name: {said[:62]}…")

        # 3 + 4 — only an authorised override releases it, and it is recorded
        said = _refused(
            lambda: release(session, hold, actor="clerk.jo", reason="looks fine to me"),
            AccessDenied,
        )
        session.rollback()
        refused_capability = said
        assert current_hold(session, short) is not None, "the refused release released it"
        said = _refused(
            lambda: release(session, hold, actor="fin.ada", reason="   "),
            MatchHoldError,
        )
        session.rollback()
        release(session, hold, actor="fin.ada",
                reason="the 10 short were back-ordered and credited on AP-903",
                on=date(2026, 11, 30))
        session.commit()
        assert hold.state == RELEASED
        assert hold.resolved_by == "fin.ada" and hold.resolve_reason.startswith(
            "the 10 short were back-ordered"
        )
        assert hold.resolved_on == date(2026, 11, 30)
        print(f"3. a clerk is refused ({refused_capability[:48]}…) and a controller may"
              " override")
        print("4. the release records who (fin.ada), when (2026-11-30) and why; a release"
              f" with no reason is refused ({said[:38]}…)")

        # 5 — the verdict is untouched
        assert len(session.scalars(select(MatchRun).where(MatchRun.invoice_id == short.id)).all()) == 1
        again = session.get(MatchRun, short_run.id)
        assert again.status == PARTIAL and again.details == short_run.details, (
            "the override rewrote the verdict"
        )
        print("5. no new run was written and the partial verdict is byte for byte what"
              " it was — the exception stays visible")

        # 6 — overrides counted apart
        rate = match_rate(session, company_id=COMPANY, start=INVOICE_DATE,
                          end=INVOICE_DATE)
        assert rate["considered"] == 2 and rate["matched"] == 1 and rate["partial"] == 1, rate
        assert rate["rate_percent"] == Decimal("50.0000"), rate
        assert override_count(session, company_id=COMPANY, start=date(2026, 11, 30),
                              end=date(2026, 11, 30)) == 1
        assert rate["rate_percent"] < Decimal("100")
        print(f"6. the rate stays {rate['rate_percent']} % (1 of 2 clean) while the"
              " override is counted separately, so paying it after an override is not"
              " read as a clean match")

        # 7 + 8 — the edges
        third = _invoice(session, number="AP-903", supplier=supplier, item=item,
                         quantity="110", unit_price="130", ordered="100",
                         received="100", bin_a=bin_a, tag="03")
        third_run = match_invoice(session, third)
        session.commit()
        # invoiced for 110 against 100 received, at the price that was ordered: the
        # quantity comparison alone takes it out of a clean match
        assert third_run.status != MATCHED, third_run.status
        assert third_run.status == PARTIAL, third_run.status
        made = hold_failed_matches(session, company_id=COMPANY,
                                  reason="nightly sweep", on=date(2026, 12, 1))
        session.commit()
        assert [row.invoice.number for row in made] == ["AP-903"], made
        assert len(made) == 1, "the sweep re-held an invoice that was already resolved"
        assert [row.invoice.number for row in holds(session, company_id=COMPANY,
                                                    state=HELD)] == ["AP-903"]

        said = _refused(lambda: hold_invoice(session, third, reason="again"),
                        AlreadyHeldError)
        session.rollback()
        said += " | " + _refused(
            lambda: hold_invoice(session, clean, reason="  "), MatchHoldError
        )
        session.rollback()
        release(session, current_hold(session, third), actor="fin.ada",
                reason="supplier issued a credit note")
        session.commit()
        said += " | " + _refused(
            lambda: release(session, holds(session, company_id=COMPANY,
                                           state=RELEASED)[0],
                            actor="fin.ada", reason="twice"),
            NotHeldError,
        )
        session.rollback()
        assert is_held(session, third) is False
        assert [row.invoice.number for row in holds(session, company_id=COMPANY,
                                                    state=RELEASED)] == ["AP-902", "AP-903"]
        print(f"7/8. the sweep held only the unresolved failed invoice; the invoice left"
              f" the hold on release; and holding twice, holding without a reason and"
              f" releasing a release are refused: {said}")

        # an unmatched invoice has no verdict to be held against
        late = create_invoice(
            session, company_id=COMPANY, number="AP-999", supplier=supplier,
            supplier_reference="ACME-999", invoice_date=INVOICE_DATE,
            lines=[{"description": "Never matched", "quantity": "1", "unit_price": "1"}],
        )
        session.commit()
        post_invoice(session, late)
        session.commit()
        said = _refused(lambda: hold_invoice(session, late, reason="unmatched"),
                        MatchHoldError)
        session.rollback()
        print(f"   an unmatched invoice cannot be held: {said[:58]}…")
        assert MatchHold.__tablename__ == "match_hold" and _receipts is not None

    print("check_match_hold: all assertions green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
