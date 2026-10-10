"""T-6.OFFLINE.02 check — the device feed: readings in, punches out, and what is parked.

    DATABASE_URL=postgresql+psycopg://erpv1:erpv1@localhost:5432/erpv1 \
        python tests/check_biometric.py

Green on all seven:

1. **a device's readings land against the mapped employee** — the punches are in T-5.ATT.02's
   append-only log with the device as their source, and the day's derivation pairs them and
   reports the worked minutes
2. **a re-pull of the same window records nothing twice** — the same delivery answers with
   the pull it already made, and the same readings sent under a new key are counted as
   duplicates by T-5.ATT.02's own guard, leaving one punch per event
3. **an event for an unknown device user is parked and reported** — named on the pull's report
   with the reason, still readable in `parked`, and recorded against nobody
4. **mapping the device user takes the parked readings up** — the punches are recorded then,
   the parked rows are resolved rather than deleted, and a device user already somebody else's
   is refused because taking it over would move a day's work
5. **a device that stops reporting is surfaced** — `silent` names it with how long it has been
   quiet, lists a device that never reported apart from it, and leaves a device that just
   delivered alone
6. **the ingestion is in the delivery log** — one `inbound_event` per delivery under the
   device's own key, processed, with the pull pointing at it; a batch re-sent under the *same*
   key is answered by the delivery already stored rather than logged twice
7. **a feed whose shape is wrong is refused at the door** — an unregistered device, a reading
   with no instant, a naive instant, a window that ends before it starts, and a pull key
   stated as blank

**Scratch database only**: it drops and recreates the public schema.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.audit import set_actor  # noqa: E402
from app.company import Company  # noqa: E402
from app.db import Base  # noqa: E402
from app.hr.attendance import (  # noqa: E402
    AttendanceEvent,
    attendance_day,
    punches_on,
)
from app.hr.biometric import (  # noqa: E402
    BIOMETRIC,
    AlreadyMappedError,
    BiometricDevice,
    BiometricPull,
    InvalidReadingError,
    ParkedPunch,
    UnknownDeviceError,
    ingest,
    map_user,
    mapping_for,
    parked,
    pulls_of,
    register_device,
    silent,
)
from app.hr.employees import create_employee  # noqa: E402
from app.hr.holidays import seed_calendar  # noqa: E402
from app.hr.movements import record_movement  # noqa: E402
from app.hr.shifts import define_shift, roster_employee  # noqa: E402
from app.integrations import InboundEvent  # noqa: E402
from app.ledger import posting  # noqa: E402,F401 — every check builds the one schema

DAY = date(2026, 6, 9)  # a Tuesday
SHIFT = "DAY"
CLOCK_IN = datetime(2026, 6, 9, 8, 0, tzinfo=timezone.utc)
CLOCK_OUT = datetime(2026, 6, 9, 17, 0, tzinfo=timezone.utc)


def _refused(call, expected: type[Exception]) -> str:
    try:
        call()
    except expected as exc:  # noqa: BLE001 — the type and the message are the point
        return str(exc)
    raise AssertionError(f"accepted what it must refuse ({expected.__name__})")


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

    company_id = uuid.uuid4()
    with Session(engine) as session:
        session.add(
            Company(
                id=company_id,
                code="BIO-CHECK",
                name="Biometric check",
                base_currency="PHP",
                fiscal_year_start_month=1,
            )
        )
        session.commit()
        set_actor(session, "hr")
        seed_calendar(session, company_id=company_id, market="philippines", year=2026)

        ana = create_employee(
            session, company_id=company_id, party_code="ANA", number="E-301",
            hire_date="2026-06-08", subject="hr", name="Ana Reyes",
        )
        leo = create_employee(
            session, company_id=company_id, party_code="LEO", number="E-302",
            hire_date="2026-06-01", subject="hr", name="Leo Reyes",
        )
        # A day shift with half an hour of break, so the day's minutes are checkable.
        shift = define_shift(
            session, company_id=company_id, code=SHIFT, name="Day",
            starts_at="08:00", ends_at="17:00", break_minutes=30,
        )
        for employee in (ana, leo):
            roster_employee(session, employee, shift=shift, effective_from="2026-06-08")
        session.commit()

        front_door = register_device(
            session, company_id=company_id, code="DOOR-1", name="Front door", site="Manila",
        )
        back_door = register_device(
            session, company_id=company_id, code="DOOR-2", name="Back door", site="Manila",
        )
        session.commit()
        map_user(
            session, device=front_door, device_user_id="7", employee=ana, actor="hr:maria",
        )
        session.commit()

        # 1 — the readings land against the mapped employee
        first = ingest(
            session,
            company_id=company_id,
            device_code="DOOR-1",
            window_from=datetime(2026, 6, 9, 0, 0, tzinfo=timezone.utc),
            window_to=datetime(2026, 6, 9, 23, 59, tzinfo=timezone.utc),
            readings=[
                {"device_user_id": "7", "at": CLOCK_IN, "direction": "in"},
                {"device_user_id": "7", "at": CLOCK_OUT, "direction": "out"},
                {"device_user_id": "9", "at": CLOCK_IN, "direction": "in"},
            ],
            pulled_at=datetime(2026, 6, 10, 0, 5, tzinfo=timezone.utc),
        )
        session.commit()
        assert first["recorded"] == 2, first
        assert first["parked"] == 1, first
        punches = punches_on(session, ana, on=DAY)
        assert [(row.at, row.direction, row.source) for row in punches] == [
            (CLOCK_IN, "in", "biometric device"),
            (CLOCK_OUT, "out", "biometric device"),
        ], punches
        derived = attendance_day(session, ana, on=DAY)
        assert derived["gross_minutes"] == 540, derived
        assert derived["break_deducted"] == 30 and derived["worked_minutes"] == 510, derived
        assert len(derived["pairs"]) == 1 and derived["pairs"][0]["minutes"] == 540, derived
        print(
            f"1. the door's readings became punches on {ana.number}: in at"
            f" {CLOCK_IN.isoformat()} and out at {CLOCK_OUT.isoformat()}, source"
            f" 'biometric device', and the day derives {derived['worked_minutes']} worked"
            f" minutes from the {derived['pairs'][0]['minutes']}-minute pair less the"
            " shift's 30-minute break"
        )

        # 2 — a re-pull of the same window records nothing twice
        again = ingest(
            session,
            company_id=company_id,
            device_code="DOOR-1",
            window_from=datetime(2026, 6, 9, 0, 0, tzinfo=timezone.utc),
            window_to=datetime(2026, 6, 9, 23, 59, tzinfo=timezone.utc),
            readings=[{"device_user_id": "7", "at": CLOCK_IN, "direction": "in"}],
            pulled_at=datetime(2026, 6, 10, 0, 30, tzinfo=timezone.utc),
        )
        session.commit()
        assert again["duplicate"] is True, again
        assert again["pull"] == first["pull"], (again["pull"], first["pull"])
        assert len(punches_on(session, ana, on=DAY)) == 2, "the re-pull recorded a punch"
        # The same readings under a *new* key: the delivery log cannot catch it, so the
        # punch and the window do.
        fresh_key = ingest(
            session,
            company_id=company_id,
            device_code="DOOR-1",
            window_from=datetime(2026, 6, 9, 0, 0, tzinfo=timezone.utc),
            window_to=datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc),
            readings=[
                {"device_user_id": "7", "at": CLOCK_IN, "direction": "in"},
                {"device_user_id": "7", "at": CLOCK_OUT, "direction": "out"},
            ],
            pull_key="DOOR-1-batch-2",
            pulled_at=datetime(2026, 6, 10, 0, 40, tzinfo=timezone.utc),
        )
        session.commit()
        assert fresh_key["readings"] == 2 and fresh_key["duplicates"] == 2, fresh_key
        assert fresh_key["recorded"] == 0, fresh_key
        assert len(punches_on(session, ana, on=DAY)) == 2, "an overlapping pull recorded again"
        print(
            f"2. the same window delivered twice came back as pull {first['pull']} with"
            f" duplicate=True and no punch recorded; the same two readings under a new key"
            f" ({fresh_key['pull']}) were counted as {fresh_key['duplicates']} duplicates and"
            " recorded none — one punch per event, whatever the route"
        )

        # 3 — an unknown device user is parked and reported
        reported = first["parked_readings"][0]
        assert reported["device_user_id"] == "9" and reported["at"] == CLOCK_IN, reported
        assert "mapped to nobody" in reported["reason"], reported
        waiting = parked(session, company_id=company_id)
        assert [row["device_user_id"] for row in waiting] == ["9"], waiting
        stored = session.scalar(select(AttendanceEvent).where(AttendanceEvent.at == CLOCK_IN))
        assert stored.employee_id == ana.id, "the recorded punch is not the mapped employee's"
        assert (
            session.scalar(
                select(AttendanceEvent).where(
                    AttendanceEvent.company_id == company_id,
                    AttendanceEvent.at == CLOCK_IN,
                    AttendanceEvent.employee_id != ana.id,
                )
            )
            is None
        ), "the unknown user's reading was assigned to somebody"
        print(
            f"3. device user 9 is nobody's, so the reading is parked with its reason"
            f" ('{reported['reason'][:48]}…') and named on the pull's report rather than"
            " dropped or assigned to whoever was nearest"
        )

        # 4 — mapping the device user takes the parked readings up
        # Mapped on the device that parked it: a device user id is the device's own, so a
        # reading is taken up by the mapping on the clock it came from.
        back_mapping = map_user(
            session, device=front_door, device_user_id="9", employee=leo, actor="hr:maria",
        )
        session.commit()
        assert back_mapping.employee_id == leo.id, back_mapping
        assert len(punches_on(session, leo, on=DAY)) == 1, "the parked reading was not taken up"
        assert parked(session, company_id=company_id) == [], "the parked row stayed waiting"
        resolved = session.scalar(
            select(ParkedPunch).where(
                ParkedPunch.company_id == company_id,
                ParkedPunch.device_id == front_door.id,
            )
        )
        assert resolved is not None and resolved.resolved_at is not None, resolved
        assert resolved.resolved_employee_id == leo.id, resolved
        assert resolved.device_id == front_door.id, resolved
        # The same device user on the same device cannot be taken over by somebody else.
        message = _refused(
            lambda: map_user(
                session, device=front_door, device_user_id="9", employee=ana, actor="hr:maria"
            ),
            AlreadyMappedError,
        )
        session.rollback()
        assert leo.number in message, message
        assert mapping_for(session, device=front_door, device_user_id="9").employee_id == leo.id
        print(
            f"4. mapping device user 9 to {leo.number} recorded the parked reading and"
            f" resolved the row; taking it over for {ana.number} was refused"
            f" ({message[:60]}…) because that would move somebody's day"
        )

        # 5 — a device that stops reporting is surfaced
        now = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)
        quiet = silent(session, company_id=company_id, now=now)
        assert [row["device"] for row in quiet["silent"]] == ["DOOR-1"], quiet
        assert quiet["silent"][0]["silent_for"] == now - datetime(
            2026, 6, 10, 0, 40, tzinfo=timezone.utc
        ), quiet
        assert quiet["never_reported"] == [
            {"device": "DOOR-2", "name": "Back door", "site": "Manila"}
        ], quiet
        recent = silent(
            session, company_id=company_id, now=now, silence=timedelta(days=7)
        )
        assert recent["silent"] == [], recent
        print(
            f"5. DOOR-1 last delivered at 2026-06-10T00:40 and is named silent for"
            f" {quiet['silent'][0]['silent_for']} by 2026-06-11T12:00; DOOR-2 is listed"
            " separately as never having reported, and a seven-day window names neither"
        )

        # 6 — the ingestion is in the delivery log
        events = list(
            session.scalars(
                select(InboundEvent)
                .where(InboundEvent.company_id == company_id, InboundEvent.source == BIOMETRIC)
                .order_by(InboundEvent.received_at, InboundEvent.id)
            )
        )
        assert len(events) == 2, events
        assert all(event.processed_at is not None for event in events), events
        assert events[0].idempotency_key == (
            "DOOR-1:2026-06-09T00:00:00+00:00..2026-06-09T23:59:00+00:00"
        ), events[0].idempotency_key
        assert events[1].idempotency_key == "DOOR-1-batch-2", events[1].idempotency_key
        assert again["event"] == first["event"], (again["event"], first["event"])
        linked = session.scalar(
            select(BiometricPull).where(BiometricPull.inbound_event_id == events[1].id)
        )
        assert linked is not None and str(linked.id) == fresh_key["pull"], "no pull on the delivery"
        pulls = pulls_of(session, company_id=company_id)
        assert [str(pull.id) for pull in pulls] == [first["pull"], fresh_key["pull"]], pulls
        print(
            f"6. two deliveries are in the boundary's own log, both processed and both naming"
            f" their pull; the batch re-sent under the same key was answered by the delivery"
            f" already stored ({again['event']}) rather than logged twice, and the two pulls"
            " are one per window"
        )

        # 7 — a feed whose shape is wrong is refused at the door
        unknown = _refused(
            lambda: ingest(
                session, company_id=company_id, device_code="DOOR-9",
                window_from=CLOCK_IN, window_to=CLOCK_OUT, readings=[],
            ),
            UnknownDeviceError,
        )
        assert "DOOR-9" in unknown, unknown
        no_instant = _refused(
            lambda: ingest(
                session, company_id=company_id, device_code="DOOR-1",
                window_from=CLOCK_IN, window_to=CLOCK_OUT,
                readings=[{"device_user_id": "7", "direction": "in"}],
            ),
            InvalidReadingError,
        )
        assert "reading 0's instant" in no_instant, no_instant
        naive = _refused(
            lambda: ingest(
                session, company_id=company_id, device_code="DOOR-1",
                window_from=CLOCK_IN, window_to=CLOCK_OUT,
                readings=[
                    {"device_user_id": "7", "at": datetime(2026, 6, 9, 8, 0), "direction": "in"}
                ],
            ),
            InvalidReadingError,
        )
        assert "states no zone" in naive, naive
        backwards = _refused(
            lambda: ingest(
                session, company_id=company_id, device_code="DOOR-1",
                window_from=CLOCK_OUT, window_to=CLOCK_IN, readings=[],
            ),
            InvalidReadingError,
        )
        assert "before it starts" in backwards, backwards
        blank_key = _refused(
            lambda: ingest(
                session, company_id=company_id, device_code="DOOR-1",
                window_from=CLOCK_IN, window_to=CLOCK_OUT, readings=[], pull_key="   ",
            ),
            InvalidReadingError,
        )
        assert "a pull key is required" in blank_key, blank_key
        assert len(pulls_of(session, company_id=company_id)) == 2, "a refused feed wrote a pull"
        assert session.scalar(
            select(BiometricDevice).where(BiometricDevice.code == "DOOR-9")
        ) is None, "the unknown device registered itself"
        print(
            f"7. five malformed feeds were refused at the door — {unknown.splitlines()[0]}"
            f" · {no_instant} · {naive} · {backwards} · {blank_key} — and wrote no pull,"
            " no punch and no device"
        )

    print("\ncheck_biometric: all assertions green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
