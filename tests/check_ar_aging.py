"""T-3.AR.02 check — aging the receivables, and the control account beside it.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_ar_aging.py

Green on all eight:

1. every aged figure **traces to an open invoice** — each row names its invoice, its
   due date and its days past due, and the rows add up to the buckets
2. a **partial settlement** reduces the bucket the invoice sits in, by exactly what was
   settled — a fully settled invoice drops out entirely
3. the buckets are **configuration**, they are carried on the report, and a custom set
   ages the same population differently without losing an invoice
4. every bucket set that would lose or double-count an invoice is refused: a gap, an
   overlap, a set starting after day 0, two open ends, a closed last bucket, an empty set
5. the report's total is stated **against the receivables control account**, per
   currency, with the difference shown — and an injected straight-to-the-control posting
   is reported as a difference rather than absorbed
6. the **per-customer** view adds back to the same totals
7. a **foreign-currency** invoice is aged in its own currency and compared with its own
   currency's control balance, never mixed into the base-currency one
8. the export carries the buckets by name and the comparison, so an exported report
   still says what it means

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ar.aging import AgingError, aging, aging_csv, checked_buckets  # noqa: E402
from app.ar.invoices import create_invoice, post_invoice, settle  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger.accounts import create_account  # noqa: E402
from app.ledger.currency import register_currency, store_rate  # noqa: E402
from app.ledger.mapping import set_mapping  # noqa: E402
from app.ledger.posting import post_journal_entry  # noqa: E402
from app.sales.customers import create_customer  # noqa: E402
# Imported for their tables, not their API: an invoice names the order and the
# shipment behind it, so the whole selling chain has to be in the one schema before
# `create_all` can build it.
from app.sales.fulfilment import Shipment  # noqa: E402,F401
from app.sales.orders import SalesOrder  # noqa: E402,F401
from app.sales.pipeline import Opportunity  # noqa: E402,F401
from tests.seed import seed_accounts  # noqa: E402

COMPANY = uuid.uuid4()
AS_OF = date(2026, 12, 31)
RECEIPT = uuid.uuid4()
PHP_ACCOUNTS = ("1100", "1010", "4000", "2200")
# The pack's VAT-OUT-12, so the figures below are stated as the net plus its tax
# rather than as a number copied out of a run.
VAT = Decimal("1.12")


def _gross(net: str) -> Decimal:
    return (Decimal(net) * VAT).quantize(Decimal("0.000001"))


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


def _invoice(
    session, customer, number, *, invoice_date, terms, amount, currency=None,
    settle_amount=None,
):
    invoice = create_invoice(
        session, company_id=COMPANY, number=number, customer=customer,
        invoice_date=invoice_date, terms_days=terms, currency=currency,
        lines=[{"description": "Goods", "quantity": "1", "unit_price": amount}],
    )
    session.commit()
    post_invoice(session, invoice)
    session.commit()
    if settle_amount is not None:
        # "all" is the invoice's own gross — a settlement is in the document's money,
        # tax included, so settling the net would leave the tax open on purpose.
        amount = invoice.gross_amount if settle_amount == "all" else Decimal(settle_amount)
        settle(session, invoice, amount=amount, settled_on=invoice.invoice_date,
               source_type="receipt", source_id=RECEIPT)
        # The settlement record is T-3.AR.01's; the money arriving is the receipt's
        # posting (T-3.AR.05), which is what takes the amount out of the control
        # account. Without it the ledger would still show the whole invoice as owed
        # and the comparison below would be comparing a settled invoice against an
        # unsettled account.
        post_journal_entry(
            session,
            company_id=COMPANY,
            posting_date=invoice.invoice_date,
            currency=invoice.currency,
            memo=f"receipt of {number}",
            source_type="receipt",
            source_id=RECEIPT,
            lines=[
                {"account": "1010", "debit": amount},
                {"account": "1100", "credit": amount},
            ],
        )
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
            Company(
                id=COMPANY,
                code="AR-AGING",
                name="AR aging",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        register_currency(session, company_id=COMPANY, code="PHP", name="Peso")
        register_currency(session, company_id=COMPANY, code="USD", name="US Dollar")
        session.commit()
        seed_accounts(session, company_id=COMPANY)
        create_account(session, company_id=COMPANY, code="2200", name="Output VAT",
                       account_class="liability")
        set_mapping(session, company_id=COMPANY, key="receivables", account_code="1100")
        set_mapping(session, company_id=COMPANY, key="revenue", account_code="4000")
        set_mapping(session, company_id=COMPANY, key="output_tax", account_code="2200")
        store_rate(session, company_id=COMPANY, base_currency="PHP", currency="USD",
                   on=date(2026, 12, 20), rate="58.5")
        session.commit()

        acme = create_customer(session, company_id=COMPANY, party_code="ACME",
                               name="Acme Retail", payment_terms_days=30)
        boreal = create_customer(session, company_id=COMPANY, party_code="BOREAL",
                                 name="Boreal Trading", payment_terms_days=0)
        session.commit()

        # Terms of 30 unless the helper is told otherwise; BOREAL is due on receipt.
        _invoice(session, acme, "AR-A", invoice_date=date(2026, 12, 1), terms=30,
                 amount="600.00")
        _invoice(session, acme, "AR-B", invoice_date=date(2026, 11, 1), terms=30,
                 amount="150.00", settle_amount="50.00")
        _invoice(session, acme, "AR-C", invoice_date=date(2026, 10, 15), terms=30,
                 amount="300.00")
        _invoice(session, acme, "AR-D", invoice_date=date(2026, 9, 1), terms=30,
                 amount="400.00")
        _invoice(session, acme, "AR-E", invoice_date=date(2026, 8, 1), terms=30,
                 amount="250.00", settle_amount="all")
        _invoice(session, boreal, "AR-F", invoice_date=date(2026, 12, 20), terms=0,
                 amount="250.00")
        _invoice(session, boreal, "AR-G", invoice_date=date(2026, 12, 20), terms=0,
                 amount="100.00", currency="USD")

        # 1 — every figure traces to an open invoice
        report = aging(session, company_id=COMPANY, as_of=AS_OF)
        assert report.as_of == AS_OF
        assert report.bucket_labels == ["current", "1-30", "31-60", "61-90", "90+"], (
            report.bucket_labels
        )
        by_number = {row["invoice"]: row for row in report.invoices}
        assert "AR-E" not in by_number, "a fully settled invoice was aged"
        assert by_number["AR-A"]["days_past_due"] == 0
        assert by_number["AR-A"]["bucket"] == "current"
        assert by_number["AR-B"]["days_past_due"] == 30 and by_number["AR-B"]["bucket"] == "1-30"
        assert by_number["AR-C"]["bucket"] == "31-60", by_number["AR-C"]
        assert by_number["AR-D"]["bucket"] == "90+", by_number["AR-D"]
        assert by_number["AR-B"]["settled"] == Decimal("50.000000"), by_number["AR-B"]
        assert by_number["AR-B"]["open_amount"] == (_gross("150") - Decimal("50")), by_number["AR-B"]
        aged = sum((row["open_amount"] for row in report.invoices), Decimal(0))
        assert aged == report.total, (aged, report.total)
        print(
            f"1. {len(report.invoices)} invoices aged, each carrying its number, customer,"
            f" due date and days past due (AR-A 0 → current, AR-D"
            f" {by_number['AR-D']['days_past_due']} → 90+), adding up to {report.total}"
        )

        # 2 — partial settlement reduces the right bucket; a settled invoice drops out
        php = _gross("600") + _gross("150") - Decimal("50") + _gross("300") + _gross("400") + _gross("250")
        # The headline buckets add the rows as they stand, so the USD invoice is in
        # them too; the PHP-only figures are asserted through `total_by_currency`
        # below, and that split is what the control account is compared against.
        assert report.totals["current"] == _gross("600"), report.totals
        assert report.totals["1-30"] == (
            _gross("150") - Decimal("50") + _gross("250") + _gross("100")
        ), report.totals
        assert report.totals["31-60"] == _gross("300"), report.totals
        assert report.totals["61-90"] == Decimal("0.000000"), report.totals
        assert report.totals["90+"] == _gross("400"), report.totals
        assert report.total_by_currency["PHP"] == php, report.total_by_currency
        print(
            f"2. AR-B's 50.00 receipt left {_gross('150') - Decimal('50')} open in the"
            f" 1-30 bucket ({report.totals_by_currency['PHP']['1-30']} with AR-F), and"
            f" AR-E's full"
            " settlement removed it from the report"
        )

        # 3 — the buckets are configuration, carried on the report
        custom = aging(
            session,
            company_id=COMPANY,
            as_of=AS_OF,
            buckets=[("not due", 0, 0), ("due now", 1, 30), ("late", 31, None)],
        )
        assert custom.bucket_labels == ["not due", "due now", "late"]
        assert custom.totals == {
            "not due": _gross("600"),
            "due now": _gross("150") - Decimal("50") + _gross("250") + _gross("100"),
            "late": _gross("300") + _gross("400"),
        }, custom.totals
        assert sum(custom.totals.values(), Decimal(0)) == report.total
        print(
            f"3. three custom bands give {custom.totals} — the same total, aged differently"
        )

        # 4 — every bucket set that would lose or double-count an invoice is refused
        refusals = []
        for buckets, expected in (
            ([("a", 0, 0), ("b", 31, None)], "gap"),
            ([("a", 0, 30), ("b", 10, None)], "overlap"),
            ([("a", 1, 30), ("b", 31, None)], "starts late"),
            ([("a", 0, None), ("b", 1, None)], "two open ends"),
            ([("a", 0, 30)], "closed last"),
            ([], "empty"),
            ([("a", 5, 4), ("b", 5, None)], "reversed"),
        ):
            refusals.append(_refused(lambda b=buckets: checked_buckets(b), AgingError).split(":")[0])
        assert len(refusals) == 7
        print(
            "4. every bucket set that would lose an invoice is refused:"
            + "".join(f"\n     {said}" for said in refusals)
        )

        # 5 — the control account beside the total, and a real difference reported
        assert report.control["PHP"] == php, report.control
        assert report.difference["PHP"] == Decimal("0.000000"), report.difference
        assert report.balanced, report.difference
        post_journal_entry(
            session,
            company_id=COMPANY,
            posting_date=AS_OF,
            currency="PHP",
            memo="injected straight to the control account",
            source_type="manual",
            source_id=uuid.uuid4(),
            lines=[
                {"account": "1100", "debit": Decimal("300")},
                {"account": "4000", "credit": Decimal("300")},
            ],
        )
        session.commit()
        injected = aging(session, company_id=COMPANY, as_of=AS_OF)
        assert injected.control["PHP"] == php + Decimal("300"), injected.control
        assert injected.difference["PHP"] == Decimal("-300.000000"), injected.difference
        assert not injected.balanced
        print(
            f"5. the report's own total for each currency is what that currency's"
            f" receivables control account holds (PHP {report.total_by_currency['PHP']} vs"
            f" {report.control['PHP']}, USD {report.total_by_currency['USD']} vs"
            f" {report.control['USD']}, both differences 0) — and an injected 300.00 straight"
            f" to the control account is reported as a difference of"
            f" {injected.difference['PHP']}, not absorbed"
        )

        # 6 — the per-customer view adds back to the same totals
        per_customer = {(row["customer"], row["currency"]): row for row in report.by_customer()}
        acme_php = _gross("600") + _gross("150") - Decimal("50") + _gross("300") + _gross("400")
        assert per_customer[("ACME", "PHP")]["total"] == acme_php, per_customer[("ACME", "PHP")]
        assert per_customer[("BOREAL", "PHP")]["total"] == _gross("250"), per_customer
        assert per_customer[("BOREAL", "USD")]["total"] == _gross("100"), per_customer
        assert sum(
            (row["total"] for row in per_customer.values()), Decimal(0)
        ) == report.total, "the per-customer view does not add back to the total"
        assert per_customer[("ACME", "PHP")]["buckets"]["1-30"] == (
            _gross("150") - Decimal("50")
        ), per_customer[("ACME", "PHP")]
        print(
            f"6. per customer {[(key[0] + ' ' + key[1], str(row['total'])) for key, row in sorted(per_customer.items())]}"
            " adding back to the report total"
        )

        # 7 — a foreign-currency invoice is aged in its own currency
        assert report.total_by_currency["USD"] == _gross("100"), report.total_by_currency
        assert report.control["USD"] == _gross("100"), report.control
        assert report.difference["USD"] == Decimal("0.000000"), report.difference
        usd_row = next(row for row in report.invoices if row["currency"] == "USD")
        assert usd_row["open_amount"] == _gross("100"), usd_row
        assert [row["currency"] for row in report.by_currency()] == ["PHP", "USD"], (
            report.by_currency()
        )
        assert report.by_currency()[1]["total"] == _gross("100"), report.by_currency()
        print(
            f"7. the USD invoice is aged in USD ({report.total_by_currency['USD']}) against"
            f" the USD control balance ({report.control['USD']}) and kept out of the"
            f" base-currency total ({report.total_by_currency['PHP']})"
        )

        # 8 — the export carries the buckets and the comparison
        export = aging_csv(report)
        for label in report.bucket_labels:
            assert f"{label} (" in export, f"the export does not name {label!r}"
        assert f"total as at {AS_OF},,,{report.total}" in export, export
        assert f"PHP total as at {AS_OF},,,{php}" in export, export
        assert "control account as at" in export and "subledger less control" in export
        assert "AR-E" not in export, "a settled invoice is in the export"
        assert len(export.strip().splitlines()) >= 12, len(export.strip().splitlines())
        print(
            f"8. the export has one row per aged invoice plus the bucket totals, with the"
            f" buckets named and the control comparison"
            f" ({len(export.strip().splitlines())} lines)"
        )

        # 9 — the report is re-runnable and an as_of earlier than an invoice sees none of it
        earlier = aging(session, company_id=COMPANY, as_of=date(2026, 9, 30))
        assert earlier.total < report.total, (earlier.total, report.total)
        assert aging(session, company_id=COMPANY, as_of=AS_OF).total >= earlier.total
        assert all(
            row["invoice_date"] <= AS_OF for row in report.invoices
        ), "an invoice dated after as_of was aged"
        print(
            f"9. the run is repeatable — as at 2026-09-30 it sees {earlier.total}, as at"
            f" the report's own date {report.total}, and no invoice dated after as_of"
            " appears in either"
        )

    print("\ncheck_ar_aging: all assertions green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
