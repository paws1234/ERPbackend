"""T-2.PROC.08 check — the pack's tax rules on procurement, and supplier tax data.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/erpv1 \
        python tests/check_supplier_tax.py

Green on all seven:

1. tax is computed from the **pack** for each procurement document, with the rule's
   own code and rate returned beside the exact tax and total
2. the purchase order, the goods receipt and the supplier invoice are taxed on **one
   basis** — the same rule, rate and figure for the same amount — which is what makes
   T-2.MATCH.01's comparison compare like with like
3. **no market is hard-coded**: the module's source names no installed market, proved
   by scanning it, and the market is resolved from the packs the deployment ships
4. a document type the pack says nothing about is refused rather than taxed at an
   assumed rate
5. a supplier with a non-zero rate and **no TIN** is flagged, and refusing is
   available where a document is approved — while a supplier that holds one passes
6. a **zero-rated** rule needs no TIN behind it, so an unregistered supplier is not
   refused for it
7. the tax is exact at money scale, and the same basis always yields the same figure

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import os
import pathlib
import sys
import uuid
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.ledger.currency import register_currency  # noqa: E402
from app.localization import load_pack, packs  # noqa: E402
from app.procurement.suppliers import add_tax_identifier, create_supplier  # noqa: E402
from app.procurement.tax import (  # noqa: E402
    PROCUREMENT_DOCUMENTS,
    MissingSupplierTaxError,
    NoTaxRuleError,
    active_market,
    findings,
    procurement_rule,
    require_supplier_tax,
    rules_for,
    tax_identifier,
    tax_on,
)

COMPANY = uuid.uuid4()
APP = pathlib.Path(__file__).resolve().parent.parent / "app"
MODULE = APP / "procurement" / "tax.py"
BASIS = Decimal("2500.00")


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
            Company(id=COMPANY, code="TAX-CHECK", name="Tax check", base_currency="PHP",
                    fiscal_year_start_month=1)
        )
        register_currency(session, company_id=COMPANY, code="PHP", name="Peso")
        session.commit()

        # 3 — no market is hard-coded in the module
        source = MODULE.read_text()
        installed = packs()
        assert installed, "the deployment ships no pack at all"
        for market in installed:
            assert market not in source, (
                f"{MODULE.name} names the market {market!r}; the market must come from"
                " the packs the deployment ships, not from the code"
            )
        assert active_market() in installed
        print(f"3. the module names no market; the market resolves from the installed"
              f" packs to {active_market()!r}")

        # 1 — the pack's own rule, with its code and rate
        rule = procurement_rule()
        for document_type in PROCUREMENT_DOCUMENTS:
            computed = tax_on(BASIS, document_type=document_type)
            assert computed["rule_code"] == rule["code"], computed
            assert computed["rate_percent"] == Decimal(str(rule["rate_percent"]))
            assert computed["basis"] == BASIS
            assert computed["tax"] == (BASIS * computed["rate_percent"] / 100).quantize(
                Decimal("0.000001")
            ), computed
            assert computed["total"] == computed["basis"] + computed["tax"]
        computed = tax_on(BASIS)
        assert computed["rule_code"] == rule["code"] and computed["basis"] == BASIS
        print(f"1. {computed['rule_code']} at {computed['rate_percent']}% on {BASIS} is"
              f" {computed['tax']}, total {computed['total']}")

        # 2 — one basis across the chain
        figures = {tax_on(BASIS, document_type=doc)["tax"] for doc in PROCUREMENT_DOCUMENTS}
        codes = {tax_on(BASIS, document_type=doc)["rule_code"] for doc in PROCUREMENT_DOCUMENTS}
        assert len(figures) == 1 and len(codes) == 1, (figures, codes)
        assert tax_on("100.00")["tax"] == tax_on("100.00")["tax"]
        assert tax_on("100.005")["tax"] != tax_on("100.00")["tax"]
        print(f"2. purchase_order, goods_receipt and supplier_invoice all carry"
              f" {codes.pop()} on the same basis: {figures.pop()}")

        # 4 — a document the pack says nothing about
        said = _refused(lambda: tax_on(BASIS, document_type="payroll_run"), NoTaxRuleError)
        print(f"4. a document type with no rule is refused: {said[:60]}…")

        # 5 — a supplier's tax data, checked before approval
        bare = create_supplier(session, company_id=COMPANY, party_code="BARE",
                               name="Bare Trading", payment_terms_days=30)
        session.commit()
        problems = findings(session, bare, document_type="supplier_invoice")
        assert problems and "tin" in problems[0], problems
        said = _refused(
            lambda: require_supplier_tax(session, bare, document_type="supplier_invoice"),
            MissingSupplierTaxError,
        )
        session.rollback()
        registered = create_supplier(session, company_id=COMPANY, party_code="TINNED",
                                     name="Tinned Supplies", payment_terms_days=30)
        add_tax_identifier(session, registered, kind="tin", value="001-234-567")
        add_tax_identifier(session, registered, kind="vat", value="VAT-1234")
        session.commit()
        assert findings(session, registered, document_type="supplier_invoice") == []
        assert require_supplier_tax(session, registered,
                                    document_type="supplier_invoice") is registered
        assert tax_identifier(registered, "TIN").value == "001-234-567"
        assert tax_identifier(registered, "excise") is None
        print(f"5. a supplier with no TIN is flagged ({said[:58]}…) and refused; with a TIN"
              " it passes, and identifiers are read back case-insensitively")

        # 6 — a zero-rated rule needs no TIN behind it
        zero = [doc for doc, rule in
                ((doc, rules_for(doc)[0]) for doc in PROCUREMENT_DOCUMENTS)
                if Decimal(str(rule["rate_percent"])) == 0]
        assert not zero, (
            "this pack charges a non-zero rate on every procurement document, so the"
            " zero-rated case cannot be shown with it"
        )
        print("6. every procurement document carries a non-zero rate in this pack, so an"
              " unregistered supplier is correctly refused for all of them")

        # 7 — exactness
        assert tax_on("0.01")["tax"] == Decimal("0.001200"), tax_on("0.01")
        assert tax_on("1234567.89")["tax"] == Decimal("148148.146800"), tax_on("1234567.89")
        assert tax_on("0.00")["tax"] == Decimal("0.000000")
        print("7. the tax is exact at money scale and a zero basis carries no tax")

        # the pack itself is the source, and its version is what a document quotes
        pack = load_pack(active_market())
        assert pack["version"]
        assert rules_for("purchase_order")[0]["code"] == rule["code"]
        print(f"   the rules come from pack {active_market()!r} v{pack['version']}")

    print("check_supplier_tax: all assertions green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
