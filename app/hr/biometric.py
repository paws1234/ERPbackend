"""T-6.OFFLINE.02 — the biometric device feed: readings in, punches out, and what to do with the rest.

T-0.INT.01's boundary says how anything outside the platform gets in; T-5.ATT.02 says a punch
is an append-only event that cannot be counted twice. This module is the meeting of the two:
a device **pull** is a delivery, its readings become punches against the employee the device's
own user id is mapped to, and every reading that cannot become a punch is **parked** with the
platform's own reason rather than dropped or guessed at.

Four decisions:

* **The device talks about people by its own ids.** A reading names a device user, never an
  employee: the mapping (`map_user`) is a stored row, and an unmapped device user's readings
  are *parked* — held with their reason and named on the pull's report — because a reading
  assigned to the wrong person is worse than a reading nobody has taken up yet. A mapping
  that is taken up later records the parked punches then, in one place, so nothing is lost in
  the meantime.
* **A pull is a delivery, so it is logged as one.** Every ingest goes through T-0.INT.01's
  `receive_inbound` under the device's own key, so a re-sent batch is recognised as a
  duplicate *before* anything is recorded — and a re-pull under a *new* key is caught by the
  pull's own window constraint and by T-5.ATT.02's unique punch, so the same reading cannot
  be counted twice by any of the three routes.
* **Nothing is refused into silence.** A direction the platform does not know, a reading for
  somebody who had already left, a device user nobody has mapped: each is parked with the
  reason, counted on the report, and left visible in `parked`. A pull whose *shape* is wrong —
  a reading with no instant, a window that ends before it starts, a device this company has
  not registered — is refused at the door, because that is a feed to fix rather than a
  reading to keep.
* **A device that stops reporting is a fact about the device.** Every pull stamps the
  device's `last_pull_at`, and :func:`silent` names the devices that have been quiet for
  longer than a stated window — with the ones that have never reported listed apart from
  them, because "stopped" and "never started" are different faults to chase.

The window is stated, never read from the clock: a pull answers the same when it is replayed
as it did when it arrived.

**Which half of the adapter this is.** §3 says biometric devices *sync in*, and T-0.INT.01 is
the one boundary they meet: a clock delivers the window it covered, and this module is what
the platform does with the delivery — the readings, the mapping, the parking, the report. The
device's own address and credentials stay in `integration_endpoints` and are read by whoever
schedules the delivery, so nothing here has to know how a particular clock is reached.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.db import Base
from app.hr.attendance import (
    AttendanceError,
    DuplicatePunchError,
    InvalidPunchError,
    record_punch,
)
from app.hr.employees import Employee
from app.hr.shifts import NotEmployedError
from app.integrations import InboundEvent, receive_inbound

# What T-0.INT.01 calls this source in the delivery log. One name, so a reader of the log
# finds every device delivery beside every gateway one.
BIOMETRIC = "biometric"

# How long a device may be quiet before it is surfaced. The plan names no figure for this
# (`integration_endpoints` only says where a device is), so it is a parameter with a stated
# default rather than a constant buried in a query — a site with a different cadence says so.
DEFAULT_SILENCE = timedelta(hours=24)

DIRECTIONS = ("in", "out")


class BiometricError(AttendanceError):
    """The device feed refused what was handed to it."""


class UnknownDeviceError(BiometricError):
    """No device of that code is registered for this company."""


class DuplicateDeviceError(BiometricError):
    """That device code is already registered."""


class AlreadyMappedError(BiometricError):
    """That device user belongs to somebody else; taking it over would mis-assign a day."""


class InvalidReadingError(BiometricError):
    """The pull's own shape is wrong — a feed to fix rather than a reading to park."""


class BiometricDevice(Base):
    """One clock on one site: what it is called, and when it last delivered anything."""

    __tablename__ = "biometric_device"
    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_biometric_device_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    site: Mapped[str | None] = mapped_column(String(128))
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Stamped by every pull: what `silent` reads, and the only evidence a device is alive.
    last_pull_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_window_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    users: Mapped[list[BiometricDeviceUser]] = relationship(back_populates="device")


class BiometricDeviceUser(Base):
    """One device's own user id, and the employee it is."""

    __tablename__ = "biometric_device_user"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "device_id", "device_user_id", name="uq_biometric_device_user"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("biometric_device.id"), nullable=False, index=True
    )
    device_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id"), nullable=False, index=True
    )
    mapped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)

    device: Mapped[BiometricDevice] = relationship(back_populates="users")
    employee: Mapped[Employee] = relationship()


class BiometricPull(Base):
    """One delivery from one device: the window it covered and what became of each reading."""

    __tablename__ = "biometric_pull"
    __table_args__ = (
        # One window is one pull: a device that sends the same period twice — with a new
        # batch id, so the delivery log does not catch it — writes nothing the second time.
        UniqueConstraint(
            "company_id", "device_id", "window_from", "window_to", name="uq_biometric_pull_window"
        ),
        CheckConstraint("readings >= 0", name="ck_biometric_pull_readings"),
        CheckConstraint(
            "recorded >= 0 AND duplicates >= 0 AND parked >= 0",
            name="ck_biometric_pull_counts",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("biometric_device.id"), nullable=False, index=True
    )
    # The delivery this pull arrived as — the row in T-0.INT.01's log.
    inbound_event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("inbound_event.id"), unique=True
    )
    window_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_to: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    pulled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    readings: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recorded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    parked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    device: Mapped[BiometricDevice] = relationship()


class ParkedPunch(Base):
    """A reading that could not become a punch, kept with why — never dropped, never guessed.

    Resolved rather than deleted: :func:`map_user` records the punches it can once the device
    user is mapped, and stamps `resolved_at`, so the pull's report and the person who fixed it
    both stay readable.
    """

    __tablename__ = "parked_punch"
    __table_args__ = (
        CheckConstraint(
            "direction IS NULL OR direction IN ('in', 'out')",
            name="ck_parked_punch_direction",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("biometric_device.id"), nullable=False, index=True
    )
    pull_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("biometric_pull.id"))
    device_user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    direction: Mapped[str | None] = mapped_column(String(8))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_employee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("employee.id"))

    device: Mapped[BiometricDevice] = relationship()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _moment(value: Any, what: str) -> datetime:
    """An instant a punch can be dated at — zoned on purpose, as T-5.ATT.02 stores them."""
    if not isinstance(value, datetime):
        raise InvalidReadingError(f"{what} is an instant, got {value!r}")
    if value.tzinfo is None:
        raise InvalidReadingError(
            f"{what} ({value.isoformat()}) states no zone; a device reports an instant,"
            " and an instant without a zone is not one"
        )
    return value


def _text(value: Any, what: str) -> str:
    stated = "" if value is None else str(value).strip()
    if not stated:
        raise InvalidReadingError(f"{what} is required")
    return stated


def device_by_code(session: Session, *, company_id: uuid.UUID, code: str) -> BiometricDevice:
    """The device a feed names, or a refusal naming what is missing."""
    device = session.scalar(
        select(BiometricDevice).where(
            BiometricDevice.company_id == company_id,
            BiometricDevice.code == str(code).strip(),
        )
    )
    if device is None:
        raise UnknownDeviceError(
            f"no biometric device {code!r} is registered for this company; the feed's device"
            " has to be stated before its readings can be taken"
        )
    return device


def register_device(
    session: Session,
    *,
    company_id: uuid.UUID,
    code: str,
    name: str,
    site: str | None = None,
    at: datetime | None = None,
) -> BiometricDevice:
    """Register one clock: its code is what every later feed calls it by."""
    wanted = _text(code, "a device code")
    if session.scalar(
        select(BiometricDevice).where(
            BiometricDevice.company_id == company_id, BiometricDevice.code == wanted
        )
    ) is not None:
        raise DuplicateDeviceError(f"this company already has a device {wanted!r}")
    device = BiometricDevice(
        company_id=company_id,
        code=wanted,
        name=_text(name, "a device name"),
        site=None if site is None else str(site).strip(),
        registered_at=at or _now(),
    )
    session.add(device)
    session.flush()
    return device


def mapping_for(
    session: Session, *, device: BiometricDevice, device_user_id: str
) -> BiometricDeviceUser | None:
    """Which employee a device's own user id is, or nobody yet."""
    return session.scalar(
        select(BiometricDeviceUser).where(
            BiometricDeviceUser.company_id == device.company_id,
            BiometricDeviceUser.device_id == device.id,
            BiometricDeviceUser.device_user_id == str(device_user_id).strip(),
        )
    )


def map_user(
    session: Session,
    *,
    device: BiometricDevice,
    device_user_id: str,
    employee: Employee,
    actor: str,
    at: datetime | None = None,
) -> BiometricDeviceUser:
    """Say which employee a device's user id is, and take up what was parked for it.

    Refused when that device user is already somebody else's: a device id re-used for a new
    person would otherwise move a day's punches onto them, which is the mis-assignment the
    parked state exists to prevent. A mapping that is already to this employee is returned as
    it stands, so a caller can state it twice without a second row.
    """
    if employee.company_id != device.company_id:
        raise BiometricError(f"{employee.number!r} belongs to another company")
    wanted = _text(device_user_id, "a device user id")
    who = _text(actor, "who is making the mapping")
    mapping = mapping_for(session, device=device, device_user_id=wanted)
    if mapping is not None:
        if mapping.employee_id == employee.id:
            return mapping
        other = session.get(Employee, mapping.employee_id)
        raise AlreadyMappedError(
            f"device user {wanted!r} on {device.code!r} is {other.number!r};"
            " taking it over would move their punches, so state a distinct device user for"
            " the new person"
        )
    moment = at or _now()
    mapping = BiometricDeviceUser(
        company_id=device.company_id,
        device_id=device.id,
        device_user_id=wanted,
        employee_id=employee.id,
        mapped_at=moment,
        actor=who,
    )
    session.add(mapping)
    session.flush()
    take_up_parked(session, mapping=mapping, at=moment)
    return mapping


def parked(
    session: Session, *, company_id: uuid.UUID, device_code: str | None = None
) -> list[dict]:
    """Readings still waiting for somebody to be mapped, oldest first."""
    statement = select(ParkedPunch).where(
        ParkedPunch.company_id == company_id, ParkedPunch.resolved_at.is_(None)
    )
    if device_code is not None:
        device = device_by_code(session, company_id=company_id, code=device_code)
        statement = statement.where(ParkedPunch.device_id == device.id)
    return [
        {
            "device": row.device.code,
            "device_user_id": row.device_user_id,
            "at": row.at,
            "direction": row.direction,
            "reason": row.reason,
            "pull": row.pull_id,
        }
        for row in session.scalars(statement.order_by(ParkedPunch.at, ParkedPunch.id))
    ]


def take_up_parked(
    session: Session, *, mapping: BiometricDeviceUser, at: datetime | None = None
) -> list[dict]:
    """Record the punches a device user's readings could not become until they were mapped.

    Only **this device's** parked readings: a device user id is the device's own, and an id
    that means somebody on one clock need not mean them on the next one — taking up a reading
    of a device this mapping does not name would be the mis-assignment the parked state
    exists to prevent.

    A parked reading with no instant, or whose person was no longer employed on the day, is
    left parked with its reason updated: taking it up would be inventing a punch rather than
    recording one.
    """
    employee = session.get(Employee, mapping.employee_id)
    done: list[dict] = []
    rows = list(
        session.scalars(
            select(ParkedPunch)
            .where(
                ParkedPunch.company_id == mapping.company_id,
                ParkedPunch.device_id == mapping.device_id,
                ParkedPunch.device_user_id == mapping.device_user_id,
                ParkedPunch.resolved_at.is_(None),
            )
            .order_by(ParkedPunch.at, ParkedPunch.id)
        )
    )
    for row in rows:
        if row.at is None or row.direction not in DIRECTIONS:
            row.reason = f"{row.reason} — and it cannot be taken up: it states {row.at} {row.direction}"
            continue
        try:
            event = record_punch(
                session,
                employee,
                at=row.at,
                direction=row.direction,
                source="biometric device",
            )
        except (DuplicatePunchError, NotEmployedError, InvalidPunchError) as exc:
            # A duplicate is the same reading already recorded (by a later pull, say): the row
            # is resolved by it, because the punch it held exists.
            if not isinstance(exc, DuplicatePunchError):
                row.reason = str(exc)
                continue
            row.resolved_at = at or _now()
            row.resolved_employee_id = employee.id
            continue
        row.resolved_at = at or _now()
        row.resolved_employee_id = employee.id
        done.append({"employee": employee.number, "at": row.at, "direction": row.direction})
    session.flush()
    return done


def _readings(session: Session, *, device: BiometricDevice, readings: Iterable[Any]) -> list[dict]:
    """The pull's readings, checked for shape before anything is recorded."""
    out: list[dict] = []
    for index, reading in enumerate(readings):
        if not isinstance(reading, dict):
            raise InvalidReadingError(f"reading {index} is an object with its own fields")
        out.append(
            {
                "device_user_id": _text(reading.get("device_user_id"), "a device user id"),
                "at": _moment(reading.get("at"), f"reading {index}'s instant"),
                "direction": _text(reading.get("direction"), f"reading {index}'s direction"),
            }
        )
    return out


def _record(
    session: Session,
    *,
    device: BiometricDevice,
    event: InboundEvent | None,
    readings: list[dict],
    window_from: datetime,
    window_to: datetime,
    pulled_at: datetime,
) -> tuple[BiometricPull, bool]:
    """Turn one pull's readings into punches, parking what cannot become one.

    Returns ``(pull, duplicate)``: a window that was already pulled comes back as its first
    pull, its counts untouched, so a device that re-sends a period writes nothing.
    """
    existing = session.scalar(
        select(BiometricPull).where(
            BiometricPull.company_id == device.company_id,
            BiometricPull.device_id == device.id,
            BiometricPull.window_from == window_from,
            BiometricPull.window_to == window_to,
        )
    )
    if existing is not None:
        return existing, True

    pull = BiometricPull(
        company_id=device.company_id,
        device_id=device.id,
        inbound_event_id=None if event is None else event.id,
        window_from=window_from,
        window_to=window_to,
        pulled_at=pulled_at,
        readings=len(readings),
    )
    session.add(pull)
    session.flush()

    for reading in readings:
        direction = reading["direction"].lower()
        if direction not in DIRECTIONS:
            _park(
                session,
                pull=pull,
                device=device,
                reading=reading,
                reason=f"unknown direction {reading['direction']!r}; a punch is in or out",
            )
            continue
        mapping = mapping_for(session, device=device, device_user_id=reading["device_user_id"])
        if mapping is None:
            _park(
                session,
                pull=pull,
                device=device,
                reading=reading,
                reason=(
                    f"device user {reading['device_user_id']!r} is mapped to nobody on"
                    f" {device.code!r}; map them on {device.code!r} and the reading is"
                    " taken up"
                ),
            )
            continue
        employee = session.get(Employee, mapping.employee_id)
        try:
            record_punch(
                session, employee, at=reading["at"], direction=direction,
                source="biometric device",
            )
        except DuplicatePunchError:
            # The same reading, already recorded: a re-pull of a window that overlaps, or a
            # device that sent the punch twice. Counted, never recorded again.
            pull.duplicates += 1
            continue
        except (NotEmployedError, InvalidPunchError) as exc:
            _park(session, pull=pull, device=device, reading=reading, reason=str(exc))
            continue
        pull.recorded += 1

    device.last_pull_at = pulled_at
    device.last_window_to = window_to
    session.flush()
    return pull, False


def _park(
    session: Session,
    *,
    pull: BiometricPull,
    device: BiometricDevice,
    reading: dict,
    reason: str,
) -> None:
    """Hold one reading with the platform's own reason for not recording it."""
    pull.parked += 1
    session.add(
        ParkedPunch(
            company_id=device.company_id,
            device_id=device.id,
            pull_id=pull.id,
            device_user_id=reading["device_user_id"],
            at=reading["at"],
            direction=reading["direction"].lower(),
            reason=reason,
        )
    )
    session.flush()


def ingest(
    session: Session,
    *,
    company_id: uuid.UUID,
    device_code: str,
    readings: Iterable[Any],
    window_from: Any,
    window_to: Any,
    pull_key: str | None = None,
    pulled_at: datetime | None = None,
) -> dict:
    """Take one delivery from one device, and report what became of each reading.

    The delivery is recorded at T-0.INT.01's boundary under the device's own key (the window
    it covered, unless the caller states one), so a re-sent batch is answered with the pull it
    already made instead of being recorded twice.
    """
    device = device_by_code(session, company_id=company_id, code=device_code)
    opened = _moment(window_from, "the window's start")
    closed = _moment(window_to, "the window's end")
    if closed < opened:
        raise InvalidReadingError(
            f"the window ends ({closed.isoformat()}) before it starts ({opened.isoformat()})"
        )
    checked = _readings(session, device=device, readings=readings)
    moment = pulled_at or _now()
    key = _text(pull_key, "a pull key") if pull_key is not None else (
        f"{device.code}:{opened.isoformat()}..{closed.isoformat()}"
    )

    answer: dict[str, Any] = {}

    def handle(event: InboundEvent) -> None:
        pull, duplicate = _record(
            session,
            device=device,
            event=event,
            readings=checked,
            window_from=opened,
            window_to=closed,
            pulled_at=moment,
        )
        answer["pull"] = pull
        answer["duplicate"] = duplicate

    event, seen = receive_inbound(
        session,
        company_id=company_id,
        source=BIOMETRIC,
        idempotency_key=key,
        payload={
            "device": device.code,
            "window_from": opened.isoformat(),
            "window_to": closed.isoformat(),
            "readings": [
                {
                    "device_user_id": row["device_user_id"],
                    "at": row["at"].isoformat(),
                    "direction": row["direction"],
                }
                for row in checked
            ],
        },
        handle=handle,
    )
    if "pull" not in answer:
        # The delivery was already in the log: the answer is the pull it made the first time,
        # so a re-sent batch is told what happened rather than silently doing nothing.
        pull = session.scalar(
            select(BiometricPull).where(BiometricPull.inbound_event_id == event.id)
        )
        if pull is None:
            pull = session.scalar(
                select(BiometricPull).where(
                    BiometricPull.company_id == company_id,
                    BiometricPull.device_id == device.id,
                    BiometricPull.window_from == opened,
                    BiometricPull.window_to == closed,
                )
            )
        if pull is None:
            raise BiometricError(
                f"delivery {key!r} was already received and left no pull to read back"
            )
        answer["pull"] = pull
    pull = answer["pull"]
    return {
        "event": str(event.id),
        "duplicate": bool(seen or answer.get("duplicate")),
        **pull_report(pull),
        "parked_readings": [
            row
            for row in parked_readings(session, pull=pull)
        ],
    }


def pull_report(pull: BiometricPull) -> dict:
    """One pull as it is read back: its device, its window and what each reading became."""
    return {
        "pull": str(pull.id),
        "device": pull.device.code,
        "window_from": pull.window_from,
        "window_to": pull.window_to,
        "pulled_at": pull.pulled_at,
        "readings": pull.readings,
        "recorded": pull.recorded,
        "duplicates": pull.duplicates,
        "parked": pull.parked,
    }


def parked_readings(session: Session, *, pull: BiometricPull) -> list[dict]:
    """The readings this pull parked, with the device and the reason — what a person reads."""
    return [
        {
            "device": pull.device.code,
            "device_user_id": row.device_user_id,
            "at": row.at,
            "direction": row.direction,
            "reason": row.reason,
        }
        for row in session.scalars(
            select(ParkedPunch)
            .where(ParkedPunch.pull_id == pull.id)
            .order_by(ParkedPunch.at, ParkedPunch.id)
        )
    ]


def pulls_of(
    session: Session, *, company_id: uuid.UUID, device_code: str | None = None
) -> list[BiometricPull]:
    """This company's pulls, oldest first — one per window, per device."""
    statement = select(BiometricPull).where(BiometricPull.company_id == company_id)
    if device_code is not None:
        device = device_by_code(session, company_id=company_id, code=device_code)
        statement = statement.where(BiometricPull.device_id == device.id)
    return list(
        session.scalars(statement.order_by(BiometricPull.pulled_at, BiometricPull.id))
    )


def silent(
    session: Session,
    *,
    company_id: uuid.UUID,
    now: datetime | None = None,
    silence: timedelta = DEFAULT_SILENCE,
) -> dict:
    """The devices that have gone quiet, and those that have never reported at all.

    "Stopped reporting" is measured from the last pull the platform received, against the
    stated window: a device that has never delivered is listed apart, because a site that
    never switched one on is a different fault from a clock that has gone down.
    """
    moment = now or _now()
    if silence <= timedelta(0):
        raise BiometricError(f"a silence window is above zero, got {silence}")
    quiet: list[dict] = []
    never: list[dict] = []
    for device in session.scalars(
        select(BiometricDevice)
        .where(BiometricDevice.company_id == company_id)
        .order_by(BiometricDevice.code)
    ):
        if device.last_pull_at is None:
            never.append({"device": device.code, "name": device.name, "site": device.site})
            continue
        for_how_long = moment - device.last_pull_at
        if for_how_long > silence:
            quiet.append(
                {
                    "device": device.code,
                    "name": device.name,
                    "site": device.site,
                    "last_pull_at": device.last_pull_at,
                    "last_window_to": device.last_window_to,
                    "silent_for": for_how_long,
                }
            )
    return {"silent": quiet, "never_reported": never}


__all__ = [
    "AlreadyMappedError",
    "BIOMETRIC",
    "BiometricDevice",
    "BiometricDeviceUser",
    "BiometricError",
    "BiometricPull",
    "DEFAULT_SILENCE",
    "DuplicateDeviceError",
    "InvalidReadingError",
    "ParkedPunch",
    "UnknownDeviceError",
    "device_by_code",
    "ingest",
    "map_user",
    "mapping_for",
    "parked",
    "parked_readings",
    "pull_report",
    "pulls_of",
    "register_device",
    "silent",
    "take_up_parked",
]
