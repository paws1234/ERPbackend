"""T-0.INT.01 check — the boundary processes a duplicate once and logs every send.

    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/postgres \
        python tests/check_integrations.py

It fails (non-zero exit) if any of these stops holding:

1. a duplicated inbound delivery — the same idempotency key sent twice, as a
   gateway retry or a re-synced device arrives — is processed once
2. a failing outbound send is retried and its failure is **visible**: status,
   attempt count and the error are in the delivery log
3. credentials are configuration: the endpoint and its token come from the
   environment, they are absent from the code, and the token is never copied
   into the delivery log
4. a consumer cannot bypass the boundary: the transports are private to the
   boundary module, so nothing else in the application can send without a log
   row

**Scratch database only**: it drops and recreates the schema.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import uuid

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.audit import read_trail  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base, scope_to_company  # noqa: E402
from app.integrations import (  # noqa: E402
    ENDPOINTS_SETTING,
    FAILED,
    SENT,
    InboundEvent,
    OutboundDelivery,
    UnconfiguredChannel,
    deliveries_for,
    endpoint_for,
    receive_inbound,
    register_transport,
    send_outbound,
)
from app.ledger import posting  # noqa: E402,F401 — every check builds the one schema

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOKEN = "tok-live-9f3c2b7a-not-in-the-code"


class Boom(RuntimeError):
    """What a gateway does when it is having a bad day."""


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is required (a scratch Postgres)", file=sys.stderr)
        return 2

    # 3 — credentials arrive from the environment
    os.environ[ENDPOINTS_SETTING] = json.dumps(
        {"email": {"url": "smtp://relay.internal:25", "token": TOKEN}}
    )
    assert endpoint_for("email")["token"] == TOKEN
    assert endpoint_for("email")["url"] == "smtp://relay.internal:25"
    try:
        endpoint_for("biometric")
    except UnconfiguredChannel as exc:
        assert ENDPOINTS_SETTING in str(exc), f"unclear error: {exc}"
    else:
        raise AssertionError("an unconfigured channel was accepted")
    source = (ROOT / "app" / "integrations.py").read_text()
    assert TOKEN not in source, "the credential is written in the code"
    print("the endpoint and its token come from the environment, not the code")

    engine = create_engine(url)
    # The checks share one scratch database, so reset the schema rather than only
    # the tables this file imports: a table another module added keeps a foreign
    # key on `company` and would block the rebuild.
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    Base.metadata.create_all(engine)

    company_id = uuid.uuid4()
    with Session(engine) as session:
        session.add(
            Company(
                id=company_id,
                code="INT-CHECK",
                name="Integration check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()

        # 1 — the gateway's retry books nothing twice
        handled: list[str] = []
        webhook = {"reference": "PAY-77", "amount": "1500.00"}
        first, duplicate = receive_inbound(
            session,
            company_id=company_id,
            source="gateway",
            idempotency_key="evt-1",
            payload=webhook,
            handle=lambda event: handled.append(event.idempotency_key),
        )
        assert duplicate is False and handled == ["evt-1"]
        again, duplicate = receive_inbound(
            session,
            company_id=company_id,
            source="gateway",
            idempotency_key="evt-1",
            payload=webhook,
            handle=lambda event: handled.append(event.idempotency_key),
        )
        assert duplicate is True and again.id == first.id, "the duplicate was a new event"
        assert handled == ["evt-1"], "the duplicate was handled a second time"
        other, duplicate = receive_inbound(
            session,
            company_id=company_id,
            source="gateway",
            idempotency_key="evt-2",
            payload={"reference": "PAY-78"},
        )
        assert duplicate is False and other.id != first.id
        assert session.scalar(select(func.count()).select_from(InboundEvent)) == 2
        print("the same webhook twice is one event, handled once")

        # 2 — a failing send is retried, and the log says so
        attempts: list[int] = []

        def flaky(destination: str, payload: dict) -> None:
            attempts.append(len(attempts) + 1)
            if len(attempts) < 3:
                raise Boom("connection reset by peer")

        register_transport("email", flaky)
        sent = send_outbound(
            session,
            company_id=company_id,
            channel="email",
            destination="customer@example.com",
            payload={"subject": "Your invoice"},
        )
        assert sent.status == SENT and sent.attempts == 3, (sent.status, sent.attempts)
        assert sent.last_error is None
        print(f"a send that failed twice succeeded on attempt {sent.attempts}")

        def dead(destination: str, payload: dict) -> None:
            raise Boom("mailbox unavailable")

        register_transport("sms", dead)
        failed = send_outbound(
            session,
            company_id=company_id,
            channel="sms",
            destination="+639170000000",
            payload={"text": "Your invoice is ready"},
            max_attempts=4,
        )
        assert failed.status == FAILED and failed.attempts == 4, (failed.status, failed.attempts)
        assert "mailbox unavailable" in (failed.last_error or ""), failed.last_error
        log = deliveries_for(session, company_id=company_id)
        assert [row.status for row in log] == [SENT, FAILED], "the delivery log lost a send"
        print(f"a send that never succeeds is logged as {failed.status}: {failed.last_error}")

        # 3 — the log carries the message, never the credential
        written = json.dumps(
            [
                {"destination": row.destination, "payload": row.payload, "error": row.last_error}
                for row in log
            ]
        )
        assert TOKEN not in written, "the credential was copied into the delivery log"
        assert "customer@example.com" in written, "the log lost the destination"

        # a send whose channel has no transport is refused rather than dropped
        try:
            send_outbound(
                session,
                company_id=company_id,
                channel="printer",
                destination="warehouse-1",
                payload={},
            )
        except UnconfiguredChannel as exc:
            assert "printer" in str(exc), f"unclear error: {exc}"
        else:
            raise AssertionError("a channel with no transport was accepted silently")

        # the boundary's own work is on the trail like everything else
        assert read_trail(session, entity="outbound_delivery"), "the deliveries are not audited"
        scope_to_company(session, company_id)

    # 4 — nothing outside the boundary can reach a transport
    private = 0
    for path in sorted((ROOT / "app").rglob("*.py")):
        if path.name == "integrations.py":
            continue
        source = path.read_text()
        assert "_transports" not in source, f"{path.name} reaches the transports directly"
        private += 1
    print(f"the transports are private to the boundary ({private} other modules checked)")

    engine.dispose()
    print("ok — duplicates are processed once, every send is logged, and credentials stay in the environment")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
