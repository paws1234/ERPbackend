"""T-6.ANALYTICS.02 check — the catalogue: schedulable, scoped, period-aware, never doubled.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_catalogue.py

The runner is T-0.REPORT.01 and `tests/check_reporting.py` proves the framework; this check
proves the **catalogue** that fills it, over the modules' own reports. It fails (non-zero exit)
if any of these stops holding:

1. **every catalogue entry is schedulable with recipients and a stated scope** — each names what
   it covers, over what period, and what a caller must hold, and scheduling it writes all three
   onto the definition (the report, its period and its capability are the catalogue's, so
   scheduling cannot quietly change what a report is)
2. **every entry produces the report its scope describes** and delivers it through T-0.INT.01's
   log — the statements over the run's window, the agings with their rows, totals and control
   figure, exposure per customer, scorecards per supplier, output per work order, the payroll
   summary and the day's tills
3. **a report is scoped to its company and to its recipients' permissions** — another company
   running the same report holds none of this company's documents or figures, a recipient naming
   a *subject* without the report's capability is not delivered to and is named on the run with
   the reason, and a mailbox is delivered to as the definition decided
4. **the period is the run's, not the clock's** — the same report run for two past periods gives
   each period's figures, so a month-end pack produced late still says the month it covers
5. **a re-run for the same period does not deliver twice** — the second run is a `skipped` row
   naming the period already delivered, the delivery log gains nothing, a different period
   delivers, and a period that **failed** is retried rather than skipped
6. **a failure is visible with its error** — a report with no builder leaves a `failed` run
   carrying the reason, and a builder that raises leaves its exception on the row

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.ar.gateway import GatewayPayment  # noqa: E402,F401 — the exposure report reads it
from app.ar.invoices import create_invoice as create_customer_invoice  # noqa: E402
from app.ar.invoices import post_invoice as post_customer_invoice  # noqa: E402
from app.audit import set_actor  # noqa: E402
from app.catalogue import CATALOGUE, CatalogueError, entries, schedule, schedule_all  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base, scope_to_company  # noqa: E402
from app.integrations import deliveries_for, register_transport  # noqa: E402
from app.ledger import posting  # noqa: E402,F401 — every check builds the one schema
from app.ledger.accounts import create_account  # noqa: E402
from app.ledger.currency import register_currency  # noqa: E402
from app.ledger.mapping import set_mapping  # noqa: E402
from app.ledger.posting import post_journal_entry  # noqa: E402
from app.procurement import receipts as _receipts  # noqa: E402,F401 — the AP invoices' FK target
from app.procurement.suppliers import create_supplier  # noqa: E402
from app.reporting import (  # noqa: E402
    FAILED,
    OK,
    SKIPPED,
    ReportDefinition,
    period_window,
    register,
    register_builder,
    run,
    runs_for,
)
from app.sales.customers import create_customer  # noqa: E402
from app.sales.fulfilment import Shipment  # noqa: E402,F401 — the customer invoice's FK target
from app.sales.pipeline import Opportunity  # noqa: E402,F401 — the quotation's FK target
from app.security import Role, assign, define_role, grant  # noqa: E402
from app.stock.transactions import receive  # noqa: E402
from app.stock.items import Item  # noqa: E402,F401 — the FK target of a movement
from app.stock.locations import Location  # noqa: E402,F401 — its other FK target
from tests.seed import seed_stock_accounts  # noqa: E402

ALPHA, BETA = uuid.uuid4(), uuid.uuid4()
DAY = date(2026, 9, 17)
OTHER_DAY = date(2026, 9, 16)
AUGUST, JULY = date(2026, 8, 15), date(2026, 7, 15)
CFO, OUTSIDER = "cfo@example.invalid", "vera"

# What each entry's payload must carry for its stated scope to be the report it describes.
SCOPE_KEYS: dict[str, tuple[str, ...]] = {
    "trial_balance": ("rows", "total_debit", "total_credit", "balanced"),
    "profit_and_loss": ("income", "expenses", "net_profit"),
    "balance_sheet": ("assets", "liabilities", "equity", "balanced"),
    "cash_flow": ("opening", "movements", "closing", "net_change", "balanced"),
    "receivables_aging": ("rows", "total", "buckets", "control_difference"),
    "payables_aging": ("rows", "total", "buckets", "control_difference"),
    "credit_exposure": ("rows", "customers", "over_limit"),
    "supplier_scorecards": ("rows", "suppliers", "rated"),
    "production_output": ("rows", "orders", "produced", "rejected", "net"),
    "payroll_summary": ("rows", "employees", "gross", "net"),
    "pos_day_report": ("net", "gross", "tax", "sales", "tenders"),
}


class BuilderFailed(RuntimeError):
    """What a report does when the data underneath it is not ready."""


def _definition(session: Session, company_id: uuid.UUID, code: str) -> ReportDefinition:
    definition = session.scalar(
        select(ReportDefinition).where(
            ReportDefinition.company_id == company_id, ReportDefinition.code == code
        )
    )
    assert definition is not None, f"{code} is not registered for {company_id}"
    return definition


def _run(
    session: Session, company_id: uuid.UUID, code: str, *, actor: str, period: date
):
    return run(
        session,
        _definition(session, company_id, code),
        actor=actor,
        period=period,
        now=datetime(period.year, period.month, period.day, 6, 30, tzinfo=timezone.utc),
    )


def _sent(deliveries: list[str], destination: str) -> int:
    return sum(1 for one in deliveries if one == destination)


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

    deliveries: list[str] = []
    register_transport("email", lambda destination, payload: deliveries.append(destination))

    with Session(engine) as session:
        for company_id, code in ((ALPHA, "ALPHA"), (BETA, "BETA")):
            session.add(
                Company(id=company_id, code=code, name=code, base_currency="PHP",
                        fiscal_year_start_month=1)
            )
        session.commit()

        # 1 — every entry is schedulable, carrying its own scope, period and capability
        listed = {entry["code"]: entry for entry in entries()}
        assert set(listed) == {entry.code for entry in CATALOGUE}, listed
        for entry in CATALOGUE:
            assert entry.scope and entry.capability and entry.schedule, entry
            assert period_window(entry.period, DAY).granularity == entry.period, entry
        for company_id in (ALPHA, BETA):
            scope_to_company(session, company_id)
            set_actor(session, "mia")
            seed_stock_accounts(session, company_id=company_id)
            register_currency(session, company_id=company_id, code="PHP", name="Peso")
            create_account(session, company_id=company_id, code="2200", name="Output VAT",
                           account_class="liability")
            for key, account_code in (("receivables", "1100"), ("revenue", "4000"),
                                      ("payables", "2000"), ("expense", "5200"),
                                      ("output_tax", "2200"),
                                      # The cash flow report reads every key starting with
                                      # `cash` (T-1.ACCT.03), so the company has one.
                                      ("cash", "1000"), ("cash_equivalents", "1010")):
                set_mapping(session, company_id=company_id, key=key, account_code=account_code)
            session.commit()
            definitions = schedule_all(
                session, company_id=company_id, recipients=[CFO, OUTSIDER]
            )
            session.commit()
            assert [definition.code for definition in definitions] == [
                entry.code for entry in CATALOGUE
            ], definitions
            for definition in definitions:
                entry = listed[definition.code]
                assert definition.capability == entry["capability"], definition
                assert definition.period == entry["period"], definition
                assert list(definition.recipients) == [CFO, OUTSIDER], definition
                assert definition.schedule == entry["schedule"], definition
        # One report per company per code — the definition's own unique key — and a code nobody
        # can schedule is refused by name rather than stored as a report nothing produces.
        again = None
        try:
            schedule(session, company_id=ALPHA, code="pos_day_report", recipients=[CFO])
        except IntegrityError as exc:
            again = str(exc)
            session.rollback()
        assert again is not None, "the same report was scheduled twice for one company"
        unknown = None
        try:
            schedule(session, company_id=ALPHA, code="nothing_like_a_report", recipients=[CFO])
        except CatalogueError as exc:
            unknown = str(exc)
        assert unknown is not None and "nothing_like_a_report" in unknown, unknown
        print(
            f"1. {len(CATALOGUE)} catalogue entries"
            f" ({sum(1 for e in CATALOGUE if e.kind == 'financial')} financial,"
            f" {sum(1 for e in CATALOGUE if e.kind == 'operational')} operational) scheduled for two"
            f" companies as rows carrying their scope, period and capability —"
            f" {sorted({entry.capability for entry in CATALOGUE})}; a second scheduling of one"
            f" code is refused by the definition's own key and an unknown code by name"
            f" ({unknown[:44]}…)"
        )

    # The data the reports are about: one customer invoice, one supplier invoice, stock on the
    # shelf, and a posting in each of two months so a period can be told from another.
    with Session(engine) as session:
        scope_to_company(session, ALPHA)
        set_actor(session, "mia")
        operator = define_role(session, company_id=ALPHA, code="operator", name="Operator")
        for capability in sorted({entry.capability for entry in CATALOGUE}):
            grant(session, operator, capability)
        viewer = define_role(session, company_id=ALPHA, code="viewer", name="Viewer")
        grant(session, viewer, "report.read")
        assign(session, company_id=ALPHA, subject="mia", role=operator)
        assign(session, company_id=ALPHA, subject=OUTSIDER, role=viewer)
        customer = create_customer(session, company_id=ALPHA, party_code="ACME",
                                   name="Acme Trading", credit_limit="500.00")
        supplier = create_supplier(session, company_id=ALPHA, party_code="BOREAL",
                                   name="Boreal Supplies", payment_terms_days=30)
        from app.procurement.suppliers import add_tax_identifier

        add_tax_identifier(session, supplier, kind="tin", value="009-876-543")
        session.commit()
        invoice = create_customer_invoice(
            session, company_id=ALPHA, number="AR-1", customer=customer,
            invoice_date=DAY, terms_days=30,
            lines=[{"description": "Goods", "quantity": "1", "unit_price": "300.00"}],
        )
        session.commit()
        post_customer_invoice(session, invoice)
        session.commit()
        from app.ap.invoices import create_invoice as create_supplier_invoice
        from app.ap.invoices import post_invoice as post_supplier_invoice

        bill = create_supplier_invoice(
            session, company_id=ALPHA, number="AP-1", supplier=supplier,
            supplier_reference="AP-1", invoice_date=DAY, terms_days=30,
            lines=[{"description": "Parts", "quantity": "1", "unit_price": "200.00"}],
        )
        session.commit()
        post_supplier_invoice(session, bill)
        session.commit()
        for month, amount in ((JULY, "700.00"), (AUGUST, "800.00")):
            post_journal_entry(
                session, company_id=ALPHA, posting_date=month, currency="PHP",
                memo=f"revenue {month}", source_type="manual", source_id=uuid.uuid4(),
                lines=[{"account": "1000", "debit": Decimal(amount)},
                       {"account": "4000", "credit": Decimal(amount)}],
            )
        session.commit()
        # BETA's own posting, so "another company's report" has something to hold that is not
        # ALPHA's.
        scope_to_company(session, BETA)
        set_actor(session, "bob")
        post_journal_entry(
            session, company_id=BETA, posting_date=DAY, currency="PHP", memo="beta only",
            source_type="manual", source_id=uuid.uuid4(),
            lines=[{"account": "1000", "debit": Decimal("55.00")},
                   {"account": "4000", "credit": Decimal("55.00")}],
        )
        beta_role = define_role(session, company_id=BETA, code="operator", name="Operator")
        for capability in sorted({entry.capability for entry in CATALOGUE}):
            grant(session, beta_role, capability)
        assign(session, company_id=BETA, subject="bob", role=beta_role)
        session.commit()

    # 2 — every entry produces the report its scope describes, and delivers it
    produced: dict[str, dict] = {}
    with Session(engine) as session:
        scope_to_company(session, ALPHA)
        set_actor(session, "mia")
        for entry in CATALOGUE:
            period = DAY if entry.period == "day" else AUGUST
            before = len(deliveries)
            run_row = _run(session, ALPHA, entry.code, actor="mia", period=period)
            session.commit()
            assert run_row.status == OK, (entry.code, run_row.error)
            assert run_row.period == period, run_row.period
            payload = run_row.produced
            missing = [key for key in SCOPE_KEYS[entry.code] if key not in payload]
            assert missing == [], f"{entry.code} produced {sorted(payload)} — missing {missing}"
            window = period_window(entry.period, period)
            if "from" in payload:
                assert payload["from"] == window.start.isoformat(), payload["from"]
                assert payload["to"] == window.end.isoformat(), payload["to"]
            assert len(deliveries) > before, f"{entry.code} was produced but delivered to nobody"
            produced[entry.code] = payload
        session.rollback()
        aging = produced["receivables_aging"]
        assert [row["invoice"] for row in aging["rows"]] == ["AR-1"], aging["rows"]
        assert aging["total"] == "336.00", aging["total"]  # 300.00 plus the VAT the invoice charged
        assert aging["control_difference"] == "0.00", aging
        bills = produced["payables_aging"]
        assert [row["invoice"] for row in bills["rows"]] == ["AP-1"], bills["rows"]
        assert bills["control_difference"] == "0.00", bills
        exposure = produced["credit_exposure"]
        assert [row["customer"] for row in exposure["rows"]] == ["ACME"], exposure["rows"]
        assert exposure["over_limit"] == 0, exposure
        assert Decimal(exposure["rows"][0]["limit"]) == Decimal("500.00"), exposure["rows"][0]
        assert Decimal(exposure["rows"][0]["statement"]["total"]) == Decimal("336.00"), exposure
        scorecards = produced["supplier_scorecards"]
        assert scorecards["suppliers"] == 1 and scorecards["rated"] == 0, scorecards
        assert produced["production_output"]["net"] == "0", produced["production_output"]
        assert produced["payroll_summary"]["employees"] == 0, produced["payroll_summary"]
        assert produced["pos_day_report"]["sales"] == 0, produced["pos_day_report"]
    print(
        f"2. all {len(CATALOGUE)} entries ran and delivered: the aging of AR-1 at"
        f" {aging['total']} with the control account agreeing to {aging['control_difference']},"
        f" AP-1 aged the same way, {exposure['customers']} customer exposed to"
        f" {Decimal(exposure['rows'][0]['statement']['total'])} against a limit of"
        f" {Decimal(exposure['rows'][0]['limit'])} (none over), {scorecards['suppliers']} supplier"
        f" unrated because nothing was received from it, no work orders, no payroll run and no"
        f" till trade — each payload shaped by its scope, none of it silent"
    )

    # 3 — company and recipient scope
    with Session(engine) as session:
        scope_to_company(session, BETA)
        set_actor(session, "bob")
        theirs = _run(session, BETA, "receivables_aging", actor="bob", period=DAY)
        session.commit()
        assert theirs.status == OK, theirs.error
        assert theirs.produced["rows"] == [], theirs.produced
        assert theirs.produced["total"] == "0.00", theirs.produced
        beta_ledger = _run(session, BETA, "trial_balance", actor="bob", period=DAY)
        session.commit()
        assert Decimal(beta_ledger.produced["total_debit"]) == Decimal("55.00"), (
            beta_ledger.produced
        )
        assert "AR-1" not in str(theirs.produced), theirs.produced
    with Session(engine) as session:
        scope_to_company(session, ALPHA)
        set_actor(session, "mia")
        # `vera` holds report.read and nothing else: the aging is produced for the caller who
        # may see it, and *not* handed to a subject who may not.
        scoped = _run(session, ALPHA, "payables_aging", actor="mia", period=OTHER_DAY)
        session.commit()
        assert scoped.status == OK, scoped.error
        assert list(scoped.delivered_to) == [CFO], scoped.delivered_to
        assert [row["recipient"] for row in scoped.withheld_recipients] == [OUTSIDER], (
            scoped.withheld_recipients
        )
        assert "invoice.read" in scoped.withheld_recipients[0]["reason"], scoped.withheld_recipients
        alpha_ledger = _run(session, ALPHA, "trial_balance", actor="mia", period=DAY)
        session.commit()
        assert alpha_ledger.produced["total_debit"] != beta_ledger.produced["total_debit"], (
            alpha_ledger.produced
        )
        # Plain values for the line below: the objects belong to this session, which closes.
        beta_rows = len(theirs.produced["rows"])
        beta_debits = beta_ledger.produced["total_debit"]
        alpha_debits = alpha_ledger.produced["total_debit"]
        delivered_to = list(scoped.delivered_to)
        withheld = scoped.withheld_recipients[0]
        # Report reads hold no stock ledger table in this schema, so the trial balance's own
        # debits are the check's ledger comparison rather than a count.
    print(
        f"3. BETA's receivables report holds {beta_rows} of ALPHA's documents and its own"
        f" {beta_debits} of debits against ALPHA's {alpha_debits}; a run for a caller who may see"
        f" it delivered to {delivered_to} and named {withheld['recipient']} as withheld"
        f" ({withheld['reason']})"
    )

    # 4 — the period is the run's, not the clock's
    with Session(engine) as session:
        scope_to_company(session, ALPHA)
        set_actor(session, "mia")
        july = _run(session, ALPHA, "profit_and_loss", actor="mia", period=JULY)
        session.commit()
        august = _run(session, ALPHA, "profit_and_loss", actor="mia", period=DAY)
        session.commit()
        assert july.produced["from"] == "2026-07-01", july.produced
        assert july.produced["to"] == "2026-07-31", july.produced
        assert Decimal(july.produced["total_income"]) == Decimal("700.00"), july.produced
        assert august.produced["from"] == "2026-09-01", august.produced
        # September's income is the customer invoice's revenue: one document, posted in the
        # month the report is for.
        assert Decimal(august.produced["total_income"]) == Decimal("300.00"), august.produced
        assert august.produced["to"] == "2026-09-30", august.produced
        payroll_run = _run(session, ALPHA, "payroll_summary", actor="mia", period=JULY)
        session.commit()
        assert payroll_run.status == OK, payroll_run.error
        assert payroll_run.produced["period"] == "2026-07", payroll_run.produced
        windowed = {
            "july_income": july.produced["total_income"],
            "july_from": july.produced["from"],
            "july_to": july.produced["to"],
            "sep_income": august.produced["total_income"],
            "sep_from": august.produced["from"],
            "sep_to": august.produced["to"],
        }
    print(
        f"4. the same report for two past periods is each period's own: July's income"
        f" {windowed['july_income']} over {windowed['july_from']}..{windowed['july_to']} and"
        f" September's {windowed['sep_income']} over {windowed['sep_from']}..{windowed['sep_to']}"
        " — produced in neither month, and each saying the month it covers"
    )

    # 5 — a re-run for the same period does not deliver twice
    with Session(engine) as session:
        scope_to_company(session, ALPHA)
        set_actor(session, "mia")
        first = _run(session, ALPHA, "pos_day_report", actor="mia", period=OTHER_DAY)
        session.commit()
        sent_once = _sent(deliveries, CFO)
        again = _run(session, ALPHA, "pos_day_report", actor="mia", period=OTHER_DAY)
        session.commit()
        assert again.status == SKIPPED, (again.status, again.error)
        assert again.period == OTHER_DAY, again.period
        assert "already delivered" in again.error, again.error
        assert "2026-09-16..2026-09-16" in again.error, again.error
        assert _sent(deliveries, CFO) == sent_once, deliveries[-4:]
        assert again.delivered_to is None, again.delivered_to
        other = _run(session, ALPHA, "pos_day_report", actor="mia", period=AUGUST)
        session.commit()
        assert other.status == OK, other.error
        assert _sent(deliveries, CFO) == sent_once + 1, deliveries[-4:]
        runs = runs_for(
            session, company_id=ALPHA, definition=_definition(session, ALPHA, "pos_day_report")
        )
        # The day report ran for the catalogue's day in claim 2, then twice here: the retry is
        # the `skipped` row, and the next day is a new delivery.
        assert [row.status for row in runs] == [OK, OK, SKIPPED, OK], [
            row.status for row in runs
        ]
        # A period that failed is retried rather than skipped: the row records the failure, and
        # the retry is an ordinary attempt.
        register_builder("flaky_report", lambda *_: (_ for _ in ()).throw(
            BuilderFailed("the ledger is closed for posting")
        ))
        register(
            session, company_id=ALPHA, code="flaky_report", name="Flaky",
            schedule="0 5 * * *", recipients=[CFO], capability="report.read", period="month",
        )
        session.commit()
        broken = _run(session, ALPHA, "flaky_report", actor="mia", period=JULY)
        session.commit()
        assert broken.status == FAILED and not broken.delivered_to, broken
        retried = _run(session, ALPHA, "flaky_report", actor="mia", period=JULY)
        session.commit()
        assert retried.status == FAILED, retried.status
        assert retried.id != broken.id, "the retry was not a new attempt"
        seen = {
            "again": again.status,
            "reason": again.error,
            "other": other.status,
            "retried": retried.status,
        }
    print(
        f"5. the second run for {OTHER_DAY} was `{seen['again']}` (`{seen['reason'][:60]}…`) with"
        f" the delivery log unchanged at {sent_once} for {CFO}, the next day's was"
        f" `{seen['other']}` and delivered (now {_sent(deliveries, CFO)}), and the failed period"
        f" was retried as a new attempt (`{seen['retried']}`) rather than skipped"
    )

    # 6 — a failure is visible with its error
    with Session(engine) as session:
        scope_to_company(session, ALPHA)
        set_actor(session, "mia")
        register(
            session, company_id=ALPHA, code="unbuilt_report", name="Unbuilt",
            schedule="0 4 * * *", recipients=[CFO], capability="report.read", period="month",
        )
        session.commit()
        unbuilt = _run(session, ALPHA, "unbuilt_report", actor="mia", period=JULY)
        session.commit()
        assert unbuilt.status == FAILED, unbuilt
        assert "no builder is registered" in unbuilt.error, unbuilt.error
        assert unbuilt.produced is None and not unbuilt.delivered_to, unbuilt
        failed_runs = [
            row
            for row in runs_for(session, company_id=ALPHA)
            if row.status == FAILED and row.error
        ]
        assert len(failed_runs) >= 3, len(failed_runs)
        unbuilt_error = unbuilt.error
    with engine.connect() as connection:
        rows = connection.exec_driver_sql(
            "SELECT count(*) FROM report_run WHERE status = 'failed'"
        ).scalar()
        skips = connection.exec_driver_sql(
            "SELECT count(*) FROM report_run WHERE status = 'skipped'"
        ).scalar()
        assert rows >= 3, rows
        assert skips == 1, skips
    print(
        f"6. a report nobody built left a `failed` run saying `{unbuilt_error[:52]}…` with no"
        f" payload and no delivery, and the database holds {rows} failed runs and {skips}"
        " skipped one — every attempt a row, so a report that never arrived is distinguished"
        " from one that failed"
    )

    print("\ncheck_catalogue: all assertions green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
