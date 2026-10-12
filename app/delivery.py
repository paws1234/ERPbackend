"""T-8.DELIVERY.01 — the channels a configured install can actually send on, and a row per attempt.

**Why this module exists.** T-0.INT.01 built the boundary — the delivery log, the idempotent
inbound event, the transport registry — but **nothing registered a transport**: every channel
raised :class:`~app.integrations.UnconfiguredChannel`, so a notification that the platform
promised to send (`app/ar/dunning.py`, password reset) either failed the request that sent it
or, worse, was never attempted. §10 states the contract the other way round: SMTP and the SMS
HTTP gateway are *outbound only*, "a failure is a recorded attempt and a retry, never a lost
notification".

Three decisions:

* **Transports are registered from configuration, at start** (:func:`register_configured`).
  A setting that is absent means the channel is *not configured*, which is a fact an
  operator can see in the startup log — not an exception a caller meets mid-request. Only
  a channel that is configured to send is registered, so `UnconfiguredChannel` keeps its
  meaning ("nobody configured this") instead of becoming the normal path.
* **Every attempt is a row** (:class:`DeliveryAttempt`), with its outcome and, when the
  attempt failed, the instant the next one is due. T-0.INT.01's own row says how many
  attempts were made and what the last error was; this says *which* attempt failed with
  *which* error and *when* the platform intended to try again, which is what an operator
  needs when a notification never arrived. The delivery itself stays one row — a retry
  continues it rather than creating a second message (`send_outbound(delivery=…)`).
* **A retry is the schedule's, not the request's** (:func:`retry_due`). Nothing sleeps
  inside a request: the attempt's row carries `next_attempt_at`, and
  `app/schedule.py`'s `retry_outbound_deliveries` sweeps the deliveries whose next attempt
  has come round. §9's `DELIVERY_MAX_ATTEMPTS` (5) and
  `DELIVERY_BACKOFF_MINUTES`/`_MAX_MINUTES` are what the delay is computed from, so the
  policy is configuration rather than a constant in a loop.

**Ceilings, recorded rather than discovered later.**

* The SMS channel speaks **one** shape: an HTTP endpoint that takes the provider's own JSON
  (``POST`` with a bearer token). §9's ``sms_provider`` is an enum with exactly that one
  member, because §12's OQ-11 has not chosen providers and inventing a Twilio-shaped client
  nothing can test would be worse than saying so. Upgrade path: a second member in
  `app/config.py`'s ``SMS_PROVIDERS`` and a second transport factory here.
* There is **no inbound** half here (delivery receipts, bounces): §10 lists SMTP and the SMS
  gateway as outbound only, and a bounce arrives as an inbound delivery through
  `receive_inbound` when a phase needs one.
* Nothing here inspects message *content*. A notification's text is the calling phase's
  (`app/ar/dunning.py` owns dunning letters), and this module only carries it.
"""

from __future__ import annotations

import json
import smtplib
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.company import Company
from app.config import SETTINGS, Settings
from app.db import Base, scope_to_company
from app.integrations import (
    FAILED,
    SENT,
    OutboundDelivery,
    UnconfiguredChannel,
    register_transport,
    send_outbound,
)
from app.observability import log

# The channels this module can configure. The names are the ones the calling phases
# already send on (`app/integrations.py`'s registry is keyed by channel).
EMAIL, SMS = "email", "sms"

# How long a transport may take before the attempt is a failure. A notification that
# cannot be handed over in this long is better retried than waited on inside a request.
TRANSPORT_TIMEOUT_SECONDS = 10


class DeliveryError(RuntimeError):
    """The gateway refused the message — the attempt failed, and the row says so."""


class DeliveryAttempt(Base):
    """One attempt at one delivery: how it ended and when the next one is due.

    A child of the delivery it belongs to (``app/db.py``'s ``CHILD_TABLES``): it carries no
    company dimension of its own and is isolated through its parent, which does. The parent
    row changes on every attempt, so its own trail row names the change and the attempts are
    the detail underneath it (`tests/check_audit_coverage.py` records the same reasoning for
    `journal_line`).
    """

    __tablename__ = "delivery_attempt"
    __table_args__ = (
        CheckConstraint(
            "status IN ('sending', 'sent', 'failed')", name="ck_delivery_attempt_status"
        ),
        UniqueConstraint("delivery_id", "attempt", name="uq_delivery_attempt_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    delivery_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("outbound_delivery.id"), nullable=False, index=True
    )
    # 1-based, and continuous across retries: attempt 3 is the third time this message was
    # handed to the gateway, whether the previous two were in this request or in the sweep.
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # When the platform intends to try again — null when it does not (the attempt worked,
    # or the message has used its attempts up).
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --- The transports -----------------------------------------------------------


def smtp_transport(settings: Settings):
    """SMTP, as a transport the boundary can call: ``(destination, payload)``.

    ``payload`` is the boundary's own dict. ``subject`` and ``body`` are what a notification
    carries; anything else is ignored rather than guessed at (a caller that wants an HTML
    part adds it here, where the protocol lives).
    """

    def send(destination: str, payload: dict) -> None:
        message = EmailMessage()
        message["From"] = settings.smtp_from or ""
        message["To"] = destination
        message["Subject"] = str(payload.get("subject") or "Notification")
        message.set_content(str(payload.get("body") or ""))
        with smtplib.SMTP(
            settings.smtp_host or "", settings.smtp_port, timeout=TRANSPORT_TIMEOUT_SECONDS
        ) as client:
            if settings.smtp_starttls:
                client.starttls()
            if settings.smtp_user:
                client.login(settings.smtp_user, settings.smtp_password or "")
            client.send_message(message)

    return send


def sms_transport(settings: Settings):
    """The SMS HTTP gateway: one ``POST`` with the provider's own JSON.

    See the module docstring's ceiling: one shape, stated, because §12's OQ-11 has not
    chosen a provider. A non-2xx answer is a failed attempt with the gateway's status in it,
    not a silent success.
    """

    def send(destination: str, payload: dict) -> None:
        body = json.dumps(
            {
                "to": destination,
                "sender": settings.sms_sender,
                "message": str(payload.get("body") or ""),
            }
        ).encode()
        request = urllib.request.Request(
            settings.sms_gateway_url or "",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {settings.sms_gateway_token or ''}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=TRANSPORT_TIMEOUT_SECONDS) as answer:
                if answer.status >= 300:
                    raise DeliveryError(f"the gateway answered {answer.status}")
        except urllib.error.HTTPError as exc:
            raise DeliveryError(f"the gateway answered {exc.code}") from exc

    return send


def register_configured(settings: Settings | None = None) -> list[str]:
    """Register every channel this configuration can send on; return their names.

    Called at start by the application (`app/api.py`) and by the worker
    (`app/worker.py`), so a configured install sends instead of raising. Idempotent: a
    second call re-registers the same transports, and an unconfigured channel is left
    unregistered on purpose.
    """
    settings = settings or SETTINGS
    registered: list[str] = []
    if settings.smtp_configured:
        register_transport(EMAIL, smtp_transport(settings))
        registered.append(EMAIL)
    if settings.sms_configured:
        register_transport(SMS, sms_transport(settings))
        registered.append(SMS)
    log(
        "info",
        "outbound channels registered",
        channels=", ".join(registered) or "none",
        smtp_configured=settings.smtp_configured,
        sms_configured=settings.sms_configured,
    )
    return registered


# --- Sending, and retrying ----------------------------------------------------


def deliver(
    session: Session,
    *,
    company_id: uuid.UUID,
    channel: str,
    destination: str,
    payload: dict,
    delivery: OutboundDelivery | None = None,
) -> OutboundDelivery:
    """Hand one message to its channel **once**, recording the attempt and the next one.

    One attempt per call is the decision the module docstring states: the retry is the
    schedule's. A caller that wants the send to be visible in the same transaction gets the
    delivery row (and its attempt) back.

    An unconfigured channel **does not raise from here**: §4b Phase 8's exit criterion 8 is
    *"without it, the attempt is recorded — never a `500`"*, so nobody having configured the
    channel is a `failed` delivery, with its attempt and the reason on the record. A caller
    that wants the refusal itself asks the boundary directly
    (:class:`~app.integrations.UnconfiguredChannel`, still raised by `send_outbound`).
    """
    settings = SETTINGS

    def record(row: OutboundDelivery, attempt: int, status: str, error: str | None) -> None:
        next_attempt_at = None
        if status == FAILED and attempt < settings.delivery_max_attempts:
            next_attempt_at = datetime.now(timezone.utc) + timedelta(
                minutes=settings.delivery_backoff(attempt)
            )
        session.add(
            DeliveryAttempt(
                delivery_id=row.id,
                attempt=attempt,
                status=status,
                error=error,
                next_attempt_at=next_attempt_at,
            )
        )
        # The attempt is committed even though the send failed: a retry nobody can see is
        # a retry nobody can trust.
        session.commit()
        scope_to_company(session, company_id)

    try:
        return send_outbound(
            session,
            company_id=company_id,
            channel=channel,
            destination=destination,
            payload=payload,
            max_attempts=1,
            delivery=delivery,
            on_attempt=record,
        )
    except UnconfiguredChannel as exc:
        # The boundary refuses *before* it writes a row (T-0.INT.01's contract, which
        # `app/ar/dunning.py` relies on), so the delivery row is made here: without one
        # there is nothing for the attempt to hang on, and an attempt nobody recorded is
        # exactly the "lost silently" this task exists to refuse.
        #
        # ponytail: the row is constructed in two places — here and in `send_outbound` —
        # because the refusal happens before the boundary's own row exists. Ceiling: one
        # duplicated constructor call, kept in step by this comment. Upgrade path: a
        # `send_outbound(..., record_refusal=True)` flag the day a second caller needs it.
        row = delivery
        if row is None:
            row = OutboundDelivery(
                company_id=company_id,
                channel=channel,
                destination=str(destination),
                payload=payload,
                status=FAILED,
                attempts=0,
            )
            session.add(row)
        row.status = FAILED
        row.destination = str(destination)
        row.payload = payload
        row.last_error = f"{type(exc).__name__}: {exc}"
        row.attempts = row.attempts + 1
        session.commit()
        scope_to_company(session, company_id)
        record(row, row.attempts, FAILED, row.last_error)
        return row


def attempts_for(session: Session, delivery: OutboundDelivery) -> list[DeliveryAttempt]:
    """This delivery's attempts, in order — what an operator reads when it never arrived."""
    return list(
        session.scalars(
            select(DeliveryAttempt)
            .where(DeliveryAttempt.delivery_id == delivery.id)
            .order_by(DeliveryAttempt.attempt)
        )
    )


def due_deliveries(
    session: Session, *, company_id: uuid.UUID, now: datetime | None = None, limit: int = 100
) -> list[OutboundDelivery]:
    """This company's deliveries whose next attempt has come round.

    A delivery is due when it is `failed`, its **latest** attempt says when to try again,
    and that instant has passed — so a delivery that succeeded, or that has used its
    attempts up, is never picked. Row-level security isolates this to the company the
    session is scoped to; the caller is responsible for the scope.
    """
    moment = now or datetime.now(timezone.utc)
    latest = (
        select(
            DeliveryAttempt.delivery_id,
            func.max(DeliveryAttempt.attempt).label("attempt"),
        )
        .group_by(DeliveryAttempt.delivery_id)
        .subquery()
    )
    statement = (
        select(OutboundDelivery)
        .join(latest, latest.c.delivery_id == OutboundDelivery.id)
        .join(
            DeliveryAttempt,
            (DeliveryAttempt.delivery_id == OutboundDelivery.id)
            & (DeliveryAttempt.attempt == latest.c.attempt),
        )
        .where(
            OutboundDelivery.company_id == company_id,
            OutboundDelivery.status == FAILED,
            DeliveryAttempt.next_attempt_at.is_not(None),
            DeliveryAttempt.next_attempt_at <= moment,
        )
        .order_by(OutboundDelivery.created_at, OutboundDelivery.id)
        .limit(limit)
    )
    return list(session.scalars(statement))


def retry_due(session: Session, *, now: datetime | None = None) -> dict[str, Any]:
    """Re-attempt every due delivery, company by company (the scheduled job's work).

    Reads the company list from the company master — the one table with no company
    dimension — and scopes a transaction per company rather than reading around the
    row-level policy (T-0.CORE.03). Returns a report, because a job that says nothing is a
    job nobody can tell apart from one that did not run.
    """
    moment = now or datetime.now(timezone.utc)
    company_ids = list(session.scalars(select(Company.id).order_by(Company.code)))
    attempted = 0
    sent = 0
    for company_id in company_ids:
        scope_to_company(session, company_id)
        for delivery in due_deliveries(session, company_id=company_id, now=moment):
            outcome = deliver(
                session,
                company_id=company_id,
                channel=delivery.channel,
                destination=delivery.destination,
                payload=dict(delivery.payload or {}),
                delivery=delivery,
            )
            attempted += 1
            sent += 1 if outcome.status == SENT else 0
        session.commit()
    return {"companies": len(company_ids), "attempted": attempted, "sent": sent}
