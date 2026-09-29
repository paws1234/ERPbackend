"""T-2.AP.02 check — aging the payables.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/erpv1 \
        python tests/check_ap_aging.py

Green on all seven:

1. every open invoice is aged into the right bucket, and each aged amount **traces to
   that invoice** — number, supplier, due date, days past due and what is open
2. the buckets are stated on the report, and the total equals the sum of the buckets
   equals the sum of the open amounts
3. a **partial settlement** reduces the bucket the invoice sits in, and by exactly what
   was settled — a fully settled invoice drops out entirely
4. custom buckets are honoured (this company's own bands, not the default)
5. buckets that would **lose** an invoice — an overlap, a gap, a wrong start, a second
   open end or a closed last bucket — are refused rather than reported on
6. per supplier, the same figures add back up to the company total
7. the export carries one row per aged invoice plus the bucket totals, with the buckets
   named

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ap.aging import (  # noqa: E402
    DEFAULT_BUCKETS,
    AgingError,
    aging,
    aging_csv,
    checked_buckets,
)
from app.ap.invoices import create_invoice, post_invoice, settle  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger.accounts import create_account  # noqa: E402
from app.ledger.currency import register_currency  # noqa: E402
from app.ledger.mapping import set_mapping  # noqa: E402
from app.procurement import receipts as _receipts  # noqa: E402,F401 — the FK target
from app.procurement.suppliers import create_supplier  # noqa: E402

COMPANY = uuid.uuid4()
AS_OF = date(2026, 12, 31)
BATCH = uuid.uuid4()


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


def _invoice(session, supplier, number, *, invoice_date, terms, amount, tax="0",
             settle_amount=None):
    invoice = create_invoice(
        session, company_id=COMPANY, number=number, supplier=supplier,
        supplier_reference=number, invoice_date=invoice_date, terms_days=terms,
        lines=[{"description": "Goods", "quantity": "1", "unit_price": amount,
                "tax_amount": tax}],
    )
    session.commit()
    post_invoice(session, invoice)
    session.commit()
    if settle_amount is not None:
        settle(session, invoice, amount=settle_amount, settled_on=invoice.invoice_date,
               source_type="payment_batch", source_id=BATCH)
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
            Company(id=COMPANY, code="AGING", name="Aging", base_currency="PHP",
                    fiscal_year_start_month=1)
        )
        register_currency(session, company_id=COMPANY, code="PHP", name="Peso")
        session.commit()
        create_account(session, company_id=COMPANY, code="2000", name="Accounts Payable",
                       account_class="liability")
        create_account(session, company_id=COMPANY, code="5200", name="Rent Expense",
                       account_class="expense")
        set_mapping(session, company_id=COMPANY, key="payables", account_code="2000")
        set_mapping(session, company_id=COMPANY, key="expense", account_code="5200")
        session.commit()
        acme = create_supplier(session, company_id=COMPANY, party_code="ACME",
                               name="Acme Supplies", payment_terms_days=30)
        boreal = create_supplier(session, company_id=COMPANY, party_code="BOREAL",
                                 name="Boreal Trading", payment_terms_days=0)
        session.commit()

        # due dates: 2026-12-31 (day 0), 2026-12-01 (30), 2026-11-01 (60),
        # 2026-10-01 (91), and two in the future
        just_due = _invoice(session, acme, "AP-A", invoice_date=date(2026, 12, 1),
                            terms=30, amount="100.00")
        thirty = _invoice(session, acme, "AP-B", invoice_date=date(2026, 11, 1),
                          terms=30, amount="200.00", settle_amount="50.00")
        sixty = _invoice(session, boreal, "AP-C", invoice_date=date(2026, 10, 2),
                         terms=30, amount="300.00")
        older = _invoice(session, boreal, "AP-D", invoice_date=date(2026, 9, 1),
                         terms=30, amount="400.00")
        future = _invoice(session, acme, "AP-E", invoice_date=date(2027, 1, 15),
                          terms=30, amount="500.00")
        settled_all = _invoice(session, acme, "AP-F", invoice_date=date(2026, 11, 15),
                               terms=30, amount="60.00", settle_amount="60.00")

        # 1 + 2 — the report and its traces
        report = aging(session, company_id=COMPANY, as_of=AS_OF)
        assert report.as_of == AS_OF
        assert report.bucket_labels == [label for label, _, _ in DEFAULT_BUCKETS]
        found = {row["invoice"]: row for row in report.invoices}
        assert found["AP-A"]["bucket"] == "current" and found["AP-A"]["days_past_due"] == 0, \
            found["AP-A"]
        assert found["AP-B"]["bucket"] == "1-30", found["AP-B"]
        assert found["AP-C"]["bucket"] == "31-60", found["AP-C"]
        assert found["AP-D"]["bucket"] == "90+", found["AP-D"]
        assert found["AP-E"]["bucket"] == "current" and found["AP-E"]["days_past_due"] < 0
        assert all(row["supplier"] in {"ACME", "BOREAL"} for row in report.invoices)
        assert found["AP-A"]["open_amount"] == Decimal("100.000000")
        assert found["AP-A"]["gross_amount"] == Decimal("100.000000")
        print(f"1. {len(report.invoices)} invoices aged, each carrying its number, supplier,"
              f" due date and days past due (AP-A 0 days → current, AP-D 91 → 90+)")

        assert report.totals["current"] == Decimal("600.000000"), report.totals
        assert report.totals["1-30"] == Decimal("150.000000"), report.totals
        assert report.totals["31-60"] == Decimal("300.000000"), report.totals
        assert report.totals["61-90"] == Decimal("0.000000"), report.totals
        assert report.totals["90+"] == Decimal("400.000000"), report.totals
        assert report.total == sum(
            (row["open_amount"] for row in report.invoices), Decimal(0)
        ), report.total
        assert report.total == sum(report.totals.values())
        assert "AP-F" not in found, "a fully settled invoice is still aged"
        print(f"2. buckets { {k: str(v) for k, v in report.totals.items()} } summing to"
              f" {report.total}, which is what the invoices add up to")

        # 3 — a partial settlement reduces its own bucket by exactly what was settled
        assert found["AP-B"]["settled"] == Decimal("50.000000"), found["AP-B"]
        assert found["AP-B"]["open_amount"] == Decimal("150.000000"), found["AP-B"]
        assert found["AP-B"]["bucket"] == "1-30"
        print("3. AP-B's 50.00 partial settlement left 150.000000 open in the 1-30 bucket,"
              " and AP-F's full settlement removed it from the report")

        # 4 — this company's own buckets
        monthly = aging(session, company_id=COMPANY, as_of=AS_OF,
                        buckets=(("not due", 0, 0), ("due now", 1, 45), ("late", 46, None)))
        assert monthly.bucket_labels == ["not due", "due now", "late"]
        assert monthly.totals["not due"] == Decimal("600.000000"), monthly.totals
        assert monthly.totals["due now"] == Decimal("150.000000"), monthly.totals
        assert monthly.totals["late"] == Decimal("700.000000"), monthly.totals
        assert monthly.total == report.total
        print(f"4. three custom bands give { {k: str(v) for k, v in monthly.totals.items()} }"
              " — the same total, aged differently")

        # 5 — buckets that would lose an invoice
        cases = {
            "gap": (("current", 0, 0), ("late", 31, None)),
            "overlap": (("current", 0, 10), ("late", 10, None)),
            "starts late": (("late", 1, None),),
            "two open ends": (("current", 0, None), ("late", 1, None)),
            "closed last": (("current", 0, 30), ("late", 31, 60)),
            "empty": (),
            "reversed": (("current", 0, 10), ("late", 5, None)),
        }
        messages = []
        for name, buckets in cases.items():
            messages.append(f"{name}: {_refused(lambda b=buckets: checked_buckets(b), AgingError)[:40]}")
            session.rollback()
        assert checked_buckets() == DEFAULT_BUCKETS
        print("5. every bucket set that would lose an invoice is refused:")
        for message in messages:
            print(f"     {message}")

        # 6 — per supplier adds back to the company total
        per_supplier = report.by_supplier()
        assert [row["supplier"] for row in per_supplier] == ["ACME", "BOREAL"]
        assert sum(row["total"] for row in per_supplier) == report.total
        for row in per_supplier:
            assert sum(row["buckets"].values()) == row["total"]
        assert per_supplier[1]["buckets"]["90+"] == Decimal("400.000000")
        print(f"6. per supplier {[(row['supplier'], str(row['total'])) for row in per_supplier]}"
              f" adding back to {report.total}")

        # 7 — the export
        csv = aging_csv(report)
        rows = csv.strip().split("\n")
        assert rows[0].startswith("invoice,supplier,supplier_name,invoice_date,due_date,")
        assert len([row for row in rows if row.startswith("AP-")]) == len(report.invoices)
        assert "current (0 days)" in csv and "90+ (91+ days)" in csv
        assert f"total as at {AS_OF}" in csv
        assert f",{report.total}" in csv
        print(f"7. the export has one row per aged invoice plus the bucket totals, with"
              f" the buckets named ({len(rows)} lines)")

        narrowed = aging(session, company_id=COMPANY, as_of=AS_OF, supplier=boreal)
        assert {row["supplier"] for row in narrowed.invoices} == {"BOREAL"}
        assert narrowed.total == Decimal("700.000000")
        assert just_due.id and future.id and settled_all.id and older.id and sixty.id and thirty.id

    print("check_ap_aging: all assertions green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
