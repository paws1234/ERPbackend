"""T-8.DELIVERY.01 check — a configured channel reaches its sink, an unconfigured one is a
recorded attempt rather than a failed request, and every failure carries its attempt.

    DATABASE_URL=postgresql+psycopg://… python tests/check_delivery.py

`app/delivery.py` registers the outbound transports from configuration and writes a row per
attempt; `app/integrations.py` is the boundary every attempt goes through. This check drives
both against a **real SMTP server** — a socket of this file's own, spoken to by `smtplib` —
so "a delivery reaches the sink" is a message the sink received rather than a call this check
watched.

It fails (non-zero exit) if any of these stops holding:

1. **a configured channel reaches its sink** — 100 messages sent through `deliver` on the
   registration `register_configured` makes for a configured install, at least 99 % of them
   received by the sink and logged `sent` (§6's Delivery metric)
2. **an unconfigured channel is a recorded attempt, not a failed request** — `deliver`
   returns a `failed` row, its attempt row carries the reason, and nothing raises; the
   scheduled sweep (`retry_due`, which sends with no request behind it) survives the same
   case and reports it rather than dying on it
3. **every failure carries its attempt count** — every `failed` row has one attempt row per
   attempt, a failed attempt that still has attempts left records the instant the next is due
   (the configured backoff), and the last one records none; no row is left `sending`
4. **a delivery is never lost silently** — the retry continues the delivery it retries rather
   than making a second message, it reaches `sent` once the channel is configured, and no
   failed delivery is without its error

**Scratch database only**: it drops and recreates the schema, as the other checks do.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import delivery  # noqa: E402
from app.company import Company  # noqa: E402
from app.config import (  # noqa: E402
    SETTINGS,
    SMTP_FROM,
    SMTP_HOST,
    SMTP_PORT,
    SMTP_STARTTLS,
    load_settings,
)
from app.db import scope_to_company  # noqa: E402
from app.delivery import DeliveryAttempt, attempts_for  # noqa: E402
from app.integrations import (  # noqa: E402
    FAILED,
    SENDING,
    SENT,
    OutboundDelivery,
    register_transport,
)

ALEMBIC = [sys.executable, "-m", "alembic", "-c", str(ROOT / "alembic.ini")]
# §6's Delivery metric is stated over a run of this size ("≥ 99 % succeed over a
# 100-message run"), so the run is the metric's own denominator rather than a smaller proxy.
BATCH = 100
SINK_HOST = "127.0.0.1"
SENDER = "erp@example.test"
DESTINATION = "customer@example.test"
SMS_DESTINATION = "+639170000000"
COMPANY_CODE = "DELIVERY-CHECK"


class SmtpSink:
    """An SMTP server on a socket: what proves a configured send is *received*.

    One connection per message, because that is what `smtplib.SMTP` in
    `app/delivery.py`'s `smtp_transport` does — the check drives the transport the
    configuration registered, not a stand-in for it.
    """

    def __init__(self) -> None:
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((SINK_HOST, 0))
        self._listener.listen(16)
        # A timeout on `accept` only, so the thread notices the check closing the sink.
        self._listener.settimeout(0.5)
        self.port: int = self._listener.getsockname()[1]
        self.messages: list[str] = []
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> SmtpSink:
        self._thread.start()
        return self

    def __exit__(self, *_exception: object) -> None:
        self._stopped.set()
        self._listener.close()
        self._thread.join(timeout=30)
        assert not self._thread.is_alive(), "the sink did not stop, so its messages are unsafe to read"

    def _serve(self) -> None:
        while not self._stopped.is_set():
            try:
                client, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:  # the check closed the listener under us
                return
            try:
                self._converse(client)
            except OSError:
                pass  # a client that hung up mid-message: the next one still counts
            finally:
                client.close()

    def _converse(self, client: socket.socket) -> None:
        def answer(line: bytes) -> None:
            client.sendall(line + b"\r\n")

        answer(b"220 the delivery check's own sink")
        with client.makefile("rb") as reader:
            body: list[bytes] = []
            reading_body = False
            for raw in reader:
                line = raw.rstrip(b"\r\n")
                if reading_body:
                    if line == b".":
                        reading_body = False
                        answer(b"250 2.0.0 stored")
                        self.messages.append(b"\n".join(body).decode("utf-8", "replace"))
                    else:
                        body.append(line)
                    continue
                command = line.split(b" ", 1)[0].upper()
                if command == b"EHLO":
                    answer(b"250 the delivery check's own sink")
                elif command == b"DATA":
                    reading_body = True
                    body = []
                    answer(b"354 send the message")
                elif command == b"QUIT":
                    answer(b"221 2.0.0 bye")
                    return
                else:  # HELO, MAIL, RCPT, RSET and anything else a client opens with
                    answer(b"250 2.1.0 ok")


def child_env(url: str) -> dict[str, str]:
    """This process's environment pointed at `url`, without an inherited `PYTHONPATH`."""
    env = {name: value for name, value in os.environ.items() if name != "PYTHONPATH"}
    env["DATABASE_URL"] = url
    return env


def migrate(url: str) -> None:
    """The operator's own command, in its own process — the schema the deliveries run against."""
    done = subprocess.run(
        [*ALEMBIC, "upgrade", "head"], cwd=ROOT, env=child_env(url), capture_output=True, text=True
    )
    assert done.returncode == 0, (
        f"alembic upgrade head failed on an empty schema:\n{done.stdout}{done.stderr}"
    )


def configured(sink: SmtpSink):
    """The configuration a real install has: the sink's host and port, from the environment."""
    return load_settings(
        {
            **os.environ,
            SMTP_HOST: SINK_HOST,
            SMTP_PORT: str(sink.port),
            SMTP_FROM: SENDER,
            SMTP_STARTTLS: "false",
        }
    )


def a_company(engine) -> uuid.UUID:
    company_id = uuid.uuid4()
    with Session(engine) as session:
        session.add(
            Company(
                id=company_id,
                code=COMPANY_CODE,
                name="The delivery check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()
    return company_id


def the_delivery(engine, company_id: uuid.UUID, delivery_id: uuid.UUID) -> OutboundDelivery:
    """The delivery row, re-read: a row held across `deliver`'s commits would be stale."""
    with Session(engine) as session:
        scope_to_company(session, company_id)
        row = session.get(OutboundDelivery, delivery_id)
        assert row is not None, f"delivery {delivery_id} is not on the log at all"
        return row


def its_attempts(engine, delivery_id: uuid.UUID) -> list[DeliveryAttempt]:
    """This delivery's attempts, re-read from the table — what an operator would see."""
    with Session(engine) as session:
        return list(
            session.scalars(
                select(DeliveryAttempt)
                .where(DeliveryAttempt.delivery_id == delivery_id)
                .order_by(DeliveryAttempt.attempt)
            )
        )


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is required (a scratch Postgres)", file=sys.stderr)
        return 2

    engine = create_engine(url)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    migrate(url)
    company_id = a_company(engine)

    # 2 — a channel nobody configured is an attempt on the record, and nothing raises. No
    # transport is registered yet, and `register_configured` has not run, which is exactly a
    # deployment with no SMTP settings.
    with Session(engine) as session:
        refused = delivery.deliver(
            session,
            company_id=company_id,
            channel=delivery.EMAIL,
            destination=DESTINATION,
            payload={"subject": "nobody configured this", "body": "the attempt is the record"},
        )
        assert refused.status == FAILED, (
            f"an unconfigured channel answered {refused.status!r} — it must be a recorded failure"
        )
        assert refused.attempts == 1, f"the attempt count is {refused.attempts}"
        assert refused.last_error and delivery.EMAIL in refused.last_error, refused.last_error
        refused_id = refused.id
    first = its_attempts(engine, refused_id)
    assert [row.attempt for row in first] == [1], [row.attempt for row in first]
    assert first[0].status == FAILED and delivery.EMAIL in (first[0].error or ""), first[0].error
    assert first[0].next_attempt_at is not None, "no next attempt was recorded, so nothing retries"
    due_at = first[0].next_attempt_at + timedelta(seconds=1)
    print(
        f"an unconfigured channel recorded attempt 1 as {first[0].status!r} with its reason,"
        f" and a retry due {first[0].next_attempt_at.isoformat()}"
    )

    # 2 (continued) — the same case through the caller that has no request behind it. The
    # sweep is what a scheduled send arrives on (app/schedule.py), and it must report the
    # failure rather than die on it.
    with Session(engine) as session:
        report = delivery.retry_due(session, now=due_at)
    assert report["attempted"] == 1, f"the sweep did not retry the due delivery: {report}"
    assert report["sent"] == 0, f"the sweep reported a send on an unconfigured channel: {report}"
    retried = the_delivery(engine, company_id, refused_id)
    assert retried.status == FAILED and retried.attempts == 2, (retried.status, retried.attempts)
    assert [row.attempt for row in its_attempts(engine, refused_id)] == [1, 2]
    print("the scheduled sweep survived the same case and recorded attempt 2, still failed")

    # 1 — a configured install, from the configuration, against the sink
    with SmtpSink() as sink:
        registered = delivery.register_configured(configured(sink))
        assert registered == [delivery.EMAIL], f"the registration is {registered}"

        sent = 0
        with Session(engine) as session:
            for number in range(BATCH):
                row = delivery.deliver(
                    session,
                    company_id=company_id,
                    channel=delivery.EMAIL,
                    destination=DESTINATION,
                    payload={"subject": f"Delivery {number}", "body": f"message {number}"},
                )
                sent += 1 if row.status == SENT else 0
        assert sent * 100 >= BATCH * 99, f"{sent} of {BATCH} succeeded — below §6's 99 %"
        assert len(sink.messages) == sent, (
            f"{sent} sends reported success and the sink received {len(sink.messages)}"
        )
        assert f"Subject: Delivery {BATCH - 1}" in sink.messages[-1], (
            f"the last message the sink received is not the last one sent:\n{sink.messages[-1]}"
        )
        print(f"{sent} of {BATCH} messages reached the sink and are logged {SENT!r}")

        # 4 — the retry continues the delivery it retries, and reaches the sink now that the
        # channel is configured: one row, still, with its third attempt on it
        with Session(engine) as session:
            report = delivery.retry_due(session, now=due_at + timedelta(hours=1))
        assert report["sent"] == 1, f"the configured retry did not reach the sink: {report}"
        recovered = the_delivery(engine, company_id, refused_id)
        assert recovered.status == SENT and recovered.attempts == 3, (
            recovered.status,
            recovered.attempts,
        )
        assert recovered.last_error is None, recovered.last_error
        assert [row.status for row in its_attempts(engine, refused_id)] == [FAILED, FAILED, SENT]
        with Session(engine) as session:
            scope_to_company(session, company_id)
            same_channel = session.scalars(
                select(OutboundDelivery).where(OutboundDelivery.id == refused_id)
            ).all()
        assert len(same_channel) == 1, "the retry made a second message instead of continuing one"
        print("the retry continued its own delivery row and reached the sink on attempt 3")

    # 3 & 4 — a channel that keeps refusing: attempts up to the configured maximum, each with
    # its own row and its own backoff, and the last one with no next attempt.
    def refuses(destination: str, payload: dict) -> None:
        raise RuntimeError("the gateway refused it")

    register_transport(delivery.SMS, refuses)
    with Session(engine) as session:
        spent = delivery.deliver(
            session,
            company_id=company_id,
            channel=delivery.SMS,
            destination=SMS_DESTINATION,
            payload={"body": "refused every time"},
        )
        while spent.attempts < SETTINGS.delivery_max_attempts:
            spent = delivery.deliver(
                session,
                company_id=company_id,
                channel=delivery.SMS,
                destination=SMS_DESTINATION,
                payload={"body": "refused every time"},
                delivery=spent,
            )
    assert spent.attempts == SETTINGS.delivery_max_attempts, spent.attempts

    with Session(engine) as session:
        scope_to_company(session, company_id)
        rows = list(
            session.scalars(select(OutboundDelivery).order_by(OutboundDelivery.created_at))
        )
    assert len(rows) == BATCH + 2, f"the delivery log holds {len(rows)} rows, not {BATCH + 2}"
    assert not [row for row in rows if row.status == SENDING], (
        "a delivery was left mid-flight:"
        f" {[(str(row.id), row.channel, row.status) for row in rows if row.status == SENDING]}"
    )
    failed = [row for row in rows if row.status == FAILED]
    assert failed, "no delivery failed, so none of the failure claims below is exercised"
    for row in failed:
        assert row.attempts >= 1, f"a failed delivery has an attempt count of {row.attempts}"
        assert row.last_error, f"delivery {row.id} failed with nothing recorded"
        attempts = its_attempts(engine, row.id)
        assert [attempt.attempt for attempt in attempts] == list(
            range(1, row.attempts + 1)
        ), f"delivery {row.id}'s attempts are {[a.attempt for a in attempts]}"
        assert all(attempt.status == FAILED for attempt in attempts), (
            f"delivery {row.id} is failed and an attempt of it is not:"
            f" {[(a.attempt, a.status) for a in attempts]}"
        )
        for attempt in attempts:
            if attempt.next_attempt_at is None:
                assert attempt.attempt == SETTINGS.delivery_max_attempts, (
                    f"attempt {attempt.attempt} of {row.id} has no next attempt and the"
                    f" maximum is {SETTINGS.delivery_max_attempts}"
                )
            else:
                expected = timedelta(minutes=SETTINGS.delivery_backoff(attempt.attempt))
                recorded = attempt.next_attempt_at - attempt.attempted_at
                assert abs(recorded - expected) <= timedelta(seconds=30), (
                    f"attempt {attempt.attempt} of {row.id} waits {recorded},"
                    f" and the configuration says {expected}"
                )
    print(
        f"{len(failed)} delivery(ies) failed with their attempts on the record —"
        f" {', '.join(f'{row.channel}:{row.attempts}' for row in failed)}"
        f" — each failed attempt carrying the configured backoff and the last one none"
    )

    engine.dispose()
    print("ok — a configured channel reaches its sink, an unconfigured one is a recorded attempt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
