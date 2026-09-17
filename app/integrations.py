"""T-0.INT.01 — the one boundary between the platform and everything outside it.

§3 names the externals: payment gateways (which call *in*) , biometric devices
(which sync *in*), barcode printers and email/SMS (which go *out*). Every one of
them meets this module and nothing else, so the rules a boundary exists for are
kept in one place:

* **Inbound delivery is processed once.** A gateway retries a webhook, a device
  re-sends a batch — the event is stored under the sender's idempotency key and
  the second copy is recognised as a duplicate instead of booking the payment or
  the attendance twice (:func:`receive_inbound`).
* **Outbound delivery is retried and logged.** Every send is a row in the
  delivery log with its channel, destination, payload, attempts and last error,
  written before the send is attempted and kept whatever happens to the caller's
  transaction — a send that failed is visible, not silent
  (:func:`send_outbound`).
* **Credentials are configuration.** Endpoint URLs and tokens come from the
  environment (:func:`endpoint_for`); nothing secret is written in the code, and
  a token is never copied into the delivery log.
* **A consumer cannot bypass the boundary.** A phase that needs a gateway
  registers its transport (:func:`register_transport`) and sends through
  :func:`send_outbound`; the transports themselves are private, so there is no
  route out that skips the log.

The gateway-specific, device-specific and printer-specific mappings belong to the
phases that use them (Phase 2 payments, Phase 3/6 devices and printers, AR
dunning) — this module owns only the boundary they all meet.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable
from datetime import datetime
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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db import Base, scope_to_company

# The environment variable holding every endpoint and its credentials, as JSON:
#   {"email": {"url": "...", "token": "..."}, "gateway": {...}}
# One variable rather than one per channel, so a deployment states its boundary
# settings in one place and a new channel needs no code.
ENDPOINTS_SETTING = "INTEGRATION_ENDPOINTS"

# Delivery states, as the log reports them.
SENDING, SENT, FAILED = "sending", "sent", "failed"

# How many times a send is attempted before the log calls it failed.
DEFAULT_ATTEMPTS = 3


class IntegrationError(RuntimeError):
    """The boundary cannot do what it was asked."""


class UnconfiguredChannel(IntegrationError):
    """No endpoint or transport is configured for that channel."""


class InboundEvent(Base):
    """One inbound delivery, stored under the sender's idempotency key.

    The unique constraint is the guard, not the lookup alone: two copies arriving
    at once cannot both insert, so "processed once" holds even then.
    """

    __tablename__ = "inbound_event"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "source", "idempotency_key", name="uq_inbound_event_once"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    # Who sent it ("gateway", "biometric", …) — the phases own the names.
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Set once the handler has dealt with it; null means the platform has not
    # finished with this delivery yet.
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OutboundDelivery(Base):
    """The delivery log: one row per send, whatever became of it."""

    __tablename__ = "outbound_delivery"
    __table_args__ = (
        CheckConstraint(
            "status IN ('sending', 'sent', 'failed')", name="ck_outbound_delivery_status"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    # Where it went — the destination only. The channel's credentials stay in the
    # environment and are never copied here.
    destination: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# The transports a phase registers: channel -> callable(destination, payload).
# Private on purpose — a consumer sends through `send_outbound`, which is what
# writes the log, so no phase can quietly talk to a gateway on its own.
_transports: dict[str, Callable[[str, dict], Any]] = {}


def register_transport(channel: str, transport: Callable[[str, dict], Any]) -> None:
    """Register how one channel is delivered — the phase's half of the boundary."""
    _transports[channel] = transport


def endpoint_for(channel: str) -> dict:
    """The configured endpoint and credentials for a channel, from the environment."""
    raw = os.environ.get(ENDPOINTS_SETTING)
    if not raw:
        raise UnconfiguredChannel(
            f"{ENDPOINTS_SETTING} is not set; the integration boundary has no"
            f" endpoints configured (needed for {channel!r})"
        )
    try:
        configured = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UnconfiguredChannel(f"{ENDPOINTS_SETTING} is not valid JSON: {exc}") from exc
    try:
        return configured[channel]
    except KeyError as exc:
        raise UnconfiguredChannel(
            f"no endpoint configured for {channel!r} in {ENDPOINTS_SETTING}"
        ) from exc


def receive_inbound(
    session: Session,
    *,
    company_id: uuid.UUID,
    source: str,
    idempotency_key: str,
    payload: dict,
    handle: Callable[[InboundEvent], Any] | None = None,
) -> tuple[InboundEvent, bool]:
    """Record one inbound delivery, processing it once.

    Returns ``(event, duplicate)``. A delivery whose key has been seen before is
    returned as it stands and `handle` is **not** called again — that is what
    makes a retried webhook or a re-synced device safe.

    The call commits: the delivery is on the record before the handler runs, so a
    handler that fails leaves the event visible rather than losing it with the
    rollback. The session is left bound to `company_id` again, ready for the rest
    of the caller's work.
    """
    seen = session.scalar(
        select(InboundEvent).where(
            InboundEvent.company_id == company_id,
            InboundEvent.source == source,
            InboundEvent.idempotency_key == idempotency_key,
        )
    )
    if seen is not None:
        return seen, True

    event = InboundEvent(
        company_id=company_id, source=source, idempotency_key=idempotency_key, payload=payload
    )
    session.add(event)
    session.flush()
    if handle is not None:
        handle(event)
        event.processed_at = func.now()
        session.flush()
    session.commit()
    scope_to_company(session, company_id)
    return event, False


def send_outbound(
    session: Session,
    *,
    company_id: uuid.UUID,
    channel: str,
    destination: str,
    payload: dict,
    max_attempts: int = DEFAULT_ATTEMPTS,
) -> OutboundDelivery:
    """Deliver one message, retrying a failure and logging every attempt.

    The log row is committed before the outcome is known and again afterwards, so
    a send that failed — or that died mid-flight — is visible in the delivery log
    rather than lost with the caller's transaction. The channel's credentials are
    not part of the row.
    """
    transport = _transports.get(channel)
    if transport is None:
        raise UnconfiguredChannel(f"no transport is registered for {channel!r}")

    delivery = OutboundDelivery(
        company_id=company_id,
        channel=channel,
        destination=str(destination),
        payload=payload,
        status=SENDING,
        attempts=0,
    )
    session.add(delivery)
    session.commit()
    # The company binding is transaction-scoped (app.db), and this function
    # commits: state it again so the attempts and the outcome land on the row.
    scope_to_company(session, company_id)

    for attempt in range(1, max_attempts + 1):
        delivery.attempts = attempt
        try:
            transport(destination, payload)
        except Exception as exc:  # a boundary: whatever the transport raised
            delivery.last_error = f"{type(exc).__name__}: {exc}"
            delivery.status = FAILED
        else:
            delivery.last_error = None
            delivery.status = SENT
        session.commit()
        scope_to_company(session, company_id)
        if delivery.status == SENT:
            return delivery

    return delivery


def deliveries_for(session: Session, *, company_id: uuid.UUID, channel: str | None = None):
    """Read the delivery log back — what a phase shows when a send went wrong."""
    statement = (
        select(OutboundDelivery)
        .where(OutboundDelivery.company_id == company_id)
        .order_by(OutboundDelivery.created_at, OutboundDelivery.id)
    )
    if channel is not None:
        statement = statement.where(OutboundDelivery.channel == channel)
    return list(session.scalars(statement))
