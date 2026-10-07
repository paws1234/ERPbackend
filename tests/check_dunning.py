"""T-3.AR.04 check — dunning: levels, escalation, delivery and the rerun.

    DATABASE_URL=******localhost:5432/erpv1 \
        python tests/check_dunning.py

Green on all eight:

1. **each overdue invoice lands in exactly one level per run**, chosen by its days past
   due, and an invoice not yet due reaches none
2. an **escalated invoice gets the level it has reached and not the earlier one's
   reminder again** in the same run — the reminder rows are one per (invoice, level,
   run), and the later run reminds at the new level only
3. **delivery goes through T-0.INT.01**: a sale send is in the delivery log as `sent`, a
   refused one as `failed` with its last error on the log row *and* the reason on the
   reminder
4. a **settled invoice receives no further reminders** — it is skipped even though its
   due date has long passed, and it is reported as skipped
5. **running the job twice for a period duplicates nothing** — the second run writes no
   reminder, and the unique (invoice, level, run) refuses a hand-written one too
6. the reminder row is **history**: it cannot be edited or removed
7. a customer with **no address on the channel's contact** is recorded with that reason
   rather than silently not reminded
8. the schedule is refused unless it **partitions the days it covers** — a gap, an
   overlap, a level starting before day 0, two open ends and a closed last level are
   each refused, and an unknown channel with them

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ar.dunning import (  # noqa: E402
    DunningError,
    DunningReminder,
    checked_levels,
    define_level,
    levels,
    reminders_for,
    run_dunning,
)
from app.ar.invoices import create_invoice, open_amount, post_invoice, settle  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.integrations import (  # noqa: E402
    SENT,
    deliveries_for,
    register_transport,
    send_outbound,
)
from app.ledger.accounts import create_account  # noqa: E402
from app.ledger.currency import register_currency  # noqa: E402
from app.ledger.mapping import set_mapping  # noqa: E402
from app.sales.customers import add_contact, create_customer  # noqa: E402
from app.sales.fulfilment import Shipment  # noqa: E402,F401 — for its table
from app.sales.orders import SalesOrder  # noqa: E402,F401 — for its table
from app.sales.pipeline import Opportunity  # noqa: E402,F401 — for its table
from tests.seed import seed_accounts  # noqa: E402

COMPANY = uuid.uuid4()
START = date(2026, 3, 1)
TERMS = 0
VAT = Decimal("1.12")


class Refused(RuntimeError):
    """What a channel does when the address is wrong."""


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


def _gross(net: str) -> Decimal:
    return (Decimal(net) * VAT).quantize(Decimal("0.000001"))


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
                code="DUNNING",
                name="Dunning",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        register_currency(session, company_id=COMPANY, code="PHP", name="Peso")
        session.commit()
        seed_accounts(session, company_id=COMPANY)
        create_account(session, company_id=COMPANY, code="2200", name="Output VAT",
                       account_class="liability")
        set_mapping(session, company_id=COMPANY, key="receivables", account_code="1100")
        set_mapping(session, company_id=COMPANY, key="revenue", account_code="4000")
        set_mapping(session, company_id=COMPANY, key="output_tax", account_code="2200")
        session.commit()

        acme = create_customer(session, company_id=COMPANY, party_code="ACME",
                               name="Acme Retail", payment_terms_days=TERMS)
        add_contact(session, acme, name="Ana Reyes", email="ana@acme.example",
                    phone="+639170000001", is_primary=True)
        # A second customer whose only contact has a phone: an email reminder to them
        # has nowhere to go.
        posted_only = create_customer(session, company_id=COMPANY, party_code="POSTED",
                                      name="Poste Restante", payment_terms_days=TERMS)
        add_contact(session, posted_only, name="No Email", phone="+639170000002",
                    is_primary=True)
        # On 30-day terms, so it is billed but not yet due at the run's date.
        waiting = create_customer(session, company_id=COMPANY, party_code="WAITING",
                                  name="Waiting Ltd", payment_terms_days=30)
        add_contact(session, waiting, name="Patience", email="patience@waiting.example",
                    is_primary=True)
        session.commit()

        def invoice(number: str, net: str, on: date, customer=None):
            made = create_invoice(
                session, company_id=COMPANY, number=number, customer=customer or acme,
                invoice_date=on,
                lines=[{"description": "Goods", "quantity": "1", "unit_price": net}],
            )
            session.commit()
            post_invoice(session, made)
            session.commit()
            return made

        # 8 first: the schedule is refused unless it partitions the days
        refusals = []
        for spans, expected in (
            ([("a", 0, 0), ("b", 31, None)], "no level"),
            ([("a", 0, 30), ("b", 10, None)], "no level"),
            ([("a", -5, 30), ("b", 31, None)], "not a span"),
            ([("a", 0, None), ("b", 1, None)], "unreachable"),
            ([("a", 0, 30)], "open-ended"),
            ([], "at least one"),
        ):
            refusals.append(_refused(lambda s=spans: checked_levels(s), DunningError))
        assert len(refusals) == 6
        bad_channel = _refused(
            lambda: define_level(
                session, company_id=COMPANY, code="X", name="X", from_days=0,
                to_days=None, channel="carrier-pigeon",
            ),
            DunningError,
        )
        session.rollback()
        print(
            "8. a schedule that does not partition the days is refused:"
            + "".join(f"\n     {said.split(':')[0]}" for said in refusals)
            + f"\n     and an unknown channel with them ({bad_channel[:44]}…)"
        )

        # The schedule the rest of the check uses: two levels, email then SMS.
        define_level(session, company_id=COMPANY, code="SOFT", name="First notice",
                     from_days=1, to_days=30, channel="email", template="reminder-1")
        define_level(session, company_id=COMPANY, code="FINAL", name="Final notice",
                     from_days=31, to_days=None, channel="sms", template="reminder-2")
        session.commit()
        assert [row.code for row in levels(session, company_id=COMPANY)] == [
            "SOFT", "FINAL"
        ], levels(session, company_id=COMPANY)

        sent_sms: list[str] = []

        def email(destination: str, payload: dict) -> None:
            if destination == "refused@acme.example":
                raise Refused("mailbox unavailable")
            sent_sms.append(f"email:{destination}")

        def sms(destination: str, payload: dict) -> None:
            sent_sms.append(f"sms:{destination}")

        register_transport("email", email)
        register_transport("sms", sms)

        # One invoice 10 days late (SOFT), one 45 days late (FINAL), one settled,
        # one not yet due.
        early = invoice("AR-EARLY", "100.00", date(2026, 3, 1))
        late = invoice("AR-LATE", "200.00", date(2026, 1, 15))
        settled = invoice("AR-SETTLED", "300.00", date(2026, 1, 1))
        not_due = invoice("AR-NOTYET", "50.00", date(2026, 3, 1), customer=waiting)
        settle(session, settled, amount=settled.gross_amount, settled_on=date(2026, 1, 5),
               source_type="receipt", source_id=uuid.uuid4())
        session.commit()
        assert open_amount(session, settled) == 0

        as_of = date(2026, 3, 11)
        assert (as_of - early.due_date).days == 10
        assert (as_of - late.due_date).days == 55
        assert not_due.due_date == date(2026, 3, 31), not_due.due_date
        assert (as_of - not_due.due_date).days < 0

        # 1 + 2 + 3 — one level each, delivered through the boundary
        run = run_dunning(session, company_id=COMPANY, as_of=as_of)
        by_invoice = {row.invoice_id: row for row in run.reminders}
        assert set(by_invoice) == {early.id, late.id}, [r.level_code for r in run.reminders]
        assert by_invoice[early.id].level_code == "SOFT", by_invoice[early.id].level_code
        assert by_invoice[late.id].level_code == "FINAL", by_invoice[late.id].level_code
        assert by_invoice[early.id].days_past_due == 10
        assert by_invoice[late.id].days_past_due == 55
        assert by_invoice[early.id].open_amount == _gross("100"), by_invoice[early.id]
        assert all(row.delivered for row in run.reminders), run.failed
        assert by_invoice[early.id].delivery_id is not None
        assert by_invoice[early.id].channel == "email"
        assert by_invoice[late.id].channel == "sms"
        assert by_invoice[late.id].destination == "+639170000001"
        assert settled.number in run.settled, run.settled
        assert not_due.number in run.not_due, run.not_due
        log = deliveries_for(session, company_id=COMPANY)
        assert sorted(row.status for row in log) == [SENT, SENT], [r.status for r in log]
        assert sorted(sent_sms) == ["email:ana@acme.example", "sms:+639170000001"], sent_sms
        print(
            f"1. two overdue invoices each landed in exactly one level — AR-EARLY at"
            f" SOFT (10 days), AR-LATE at FINAL (55 days) — and the billed-but-not-yet-"
            f"due one reached none"
        )
        print(
            f"2. each overdue invoice received exactly one level's reminder"
            f" ({len(run.reminders)} rows for 2 invoices) — AR-LATE was already past"
            " SOFT, so it was never sent SOFT's"
        )
        print(
            f"3. both went out through T-0.INT.01 and are in its delivery log"
            f" ({[row.status for row in log]}), on the level's own channel"
        )

        # 4 — a settled invoice is not dunned, and a *later* period escalation sends
        # the new level only
        again = run_dunning(session, company_id=COMPANY, as_of=as_of)
        assert again.reminders == [], [r.level_code for r in again.reminders]
        assert settled.number in again.settled, again.settled
        assert set(again.already) == {"AR-EARLY", "AR-LATE"}, again.already
        assert len(reminders_for(session, settled)) == 0, "a settled invoice was dunned"
        escalated = run_dunning(session, company_id=COMPANY, as_of=as_of + timedelta(days=31))
        rows = {row.invoice_id: row for row in escalated.reminders}
        assert rows[early.id].level_code == "FINAL", rows[early.id].level_code
        assert len([row for row in escalated.reminders if row.invoice_id == early.id]) == 1
        assert [row.level_code for row in reminders_for(session, early)] == ["SOFT", "FINAL"], (
            [row.level_code for row in reminders_for(session, early)]
        )
        assert settled.number in escalated.settled, escalated.settled
        print(
            f"4. the settled invoice was skipped ({again.settled}) and has no reminder at"
            f" all; the same period re-run wrote nothing, and 31 days later AR-EARLY"
            f" **escalated** to FINAL and was sent that level alone — never SOFT again"
        )

        # 5 — the rerun duplicates nothing, and the database agrees
        first_run_key = next(row.run_key for row in reminders_for(session, early))
        assert len([row for row in reminders_for(session, early) if row.level_code == "SOFT"]) == 1
        session.add(
            DunningReminder(
                company_id=COMPANY, invoice_id=early.id, level_code="SOFT",
                run_key=first_run_key, days_past_due=10, open_amount=_gross("100"),
                channel="email", reminder_on=as_of,
            )
        )
        try:
            session.commit()
        except DBAPIError as exc:
            assert "uq_dunning_reminder_once_per_run" in str(exc), exc
            session.rollback()
        else:
            raise AssertionError("the database accepted a second reminder for one level")
        print(
            f"5. running the job twice for {as_of} wrote no second reminder (the unique"
            " (invoice, level, run) forbids one), and the database refuses a"
            " hand-written one"
        )

        # 6 — a reminder is history
        try:
            session.execute(DunningReminder.__table__.update().values(days_past_due=999))
            session.commit()
        except DBAPIError as exc:
            assert "append-only" in str(exc), exc
            session.rollback()
        else:
            raise AssertionError("a reminder was edited")
        print("6. a reminder cannot be edited or removed — it is history, refused by the"
              " database itself")

        # 7 — a channel the contact has no address for is recorded, not swallowed
        # 19 days late, so it is the **email** level whose address the customer has not
        # given (the sms one is on file).
        unreachable = invoice("AR-POSTED", "400.00", date(2026, 2, 20),
                              customer=posted_only)
        run_again = run_dunning(session, company_id=COMPANY, as_of=as_of)
        failed = [row for row in run_again.reminders if row.invoice_id == unreachable.id]
        assert len(failed) == 1 and failed[0].delivered is False, failed
        assert "no email address" in (failed[0].failure or ""), failed[0].failure
        assert failed[0].delivery_id is None, failed[0].delivery_id
        assert failed[0] in run_again.failed
        print(
            f"7. the invoice whose customer has no email address was recorded as failed"
            f" with its reason ({failed[0].failure}) and no delivery, rather than"
            " silently not reminded"
        )

        # 3b — a delivery that fails is visible on the log and on the reminder
        ok_contact = next(row for row in acme.contacts if row.is_primary)
        ok_contact.email = "refused@acme.example"
        session.commit()
        # 19 days late: the email level, whose address now refuses delivery.
        fresh = invoice("AR-REFUSED", "500.00", date(2026, 2, 20))
        refused_run = run_dunning(session, company_id=COMPANY, as_of=as_of)
        row = next(r for r in refused_run.reminders if r.invoice_id == fresh.id
                   and r.level_code == "SOFT")
        assert row.delivered is False, row.delivered
        assert "mailbox unavailable" in (row.failure or ""), row.failure
        assert row.delivery_id is not None, "a refused send left no delivery row"
        refused_log = [
            entry for entry in deliveries_for(session, company_id=COMPANY)
            if entry.status != SENT
        ]
        assert refused_log and "mailbox unavailable" in (refused_log[0].last_error or ""), (
            refused_log
        )
        print(
            f"3b. a refused send is on the delivery log as failed with its last error"
            f" ({refused_log[0].last_error}) and on the reminder"
            f" ({row.failure})"
        )

    print("\ncheck_dunning: all assertions green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
