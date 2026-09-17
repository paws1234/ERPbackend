"""T-0.REPORT.01 — the reporting framework: what to run, when, for whom, and what became of it.

§3 asks for "real-time dashboards + scheduled financial and operational reports".
The *content* of those reports is not here — the financial statements are
T-1.ACCT.07 and the catalogue/dashboards are Phase 6. What is here is the frame
every report is registered in and run through:

* **A report is a definition.** One row per company × report code, carrying when
  it runs (`report_schedule`, a standard five-field cron expression) and who
  receives it (`report_schedule`'s recipient list). A builder is registered
  against the code; the two together are the report. Adding or re-scheduling one
  is a row change, and re-pointing its recipients is not a deploy.
* **A run is scoped.** :func:`run` asks T-0.SEC.01 for the definition's
  capability first, so a report is produced for a caller who may see it, for the
  caller's company only, and a refused attempt is on the audit trail.
* **A failure is visible.** Every run is a row — requested by whom, started and
  finished, `ok` or `failed` with the error — so a report that never arrived can
  be told from one that failed, and neither is silent.
* **Delivery is the boundary's.** A run hands each recipient to T-0.INT.01
  (`send_outbound`), so the delivery log of every scheduled report is in one
  place and a failed send is retried by the code that owns retries.

When the job queue is pinned (T-0.REPORT.01's open `job_queue` variable), the
runner is what it calls; the timing rule it will follow is :func:`due`.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db import Base
from app.integrations import send_outbound
from app.security import require

# Run states, as the framework reports them.
OK, FAILED = "ok", "failed"

# The channel a report is delivered over, and the capability a definition carries
# when it names none.
DELIVERY_CHANNEL = "email"
DEFAULT_CAPABILITY = "report.read"


class ScheduleError(ValueError):
    """The schedule is not a five-field cron expression this runner understands."""


class ReportError(RuntimeError):
    """The report cannot be produced — no builder, or the builder failed."""


def _values(spec: str, low: int, high: int) -> set[int]:
    """The numbers one cron field selects: ``*``, ``a``, ``a,b``, ``a-b``, ``*/n``."""
    selected: set[int] = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, _, step_text = part.partition("/")
            try:
                step = int(step_text)
            except ValueError as exc:
                raise ScheduleError(f"step {step_text!r} is not a number") from exc
            if step < 1:
                raise ScheduleError(f"step {step_text!r} must be at least 1")
        if part in ("*", ""):
            start, end = low, high
        elif "-" in part:
            start_text, _, end_text = part.partition("-")
            try:
                start, end = int(start_text), int(end_text)
            except ValueError as exc:
                raise ScheduleError(f"range {part!r} is not numeric") from exc
        else:
            try:
                start = end = int(part)
            except ValueError as exc:
                raise ScheduleError(f"{part!r} is not a number") from exc
        if not (low <= start <= high and low <= end <= high and start <= end):
            raise ScheduleError(f"{part!r} is outside {low}..{high}")
        selected.update(range(start, end + 1, step))
    return selected


def validate_schedule(schedule: str) -> None:
    """Parse a five-field cron expression, raising :class:`ScheduleError` if it is not one."""
    fields = schedule.split()
    if len(fields) != 5:
        raise ScheduleError(
            f"{schedule!r} is not a five-field cron expression"
            " (minute hour day-of-month month day-of-week)"
        )
    for spec, low, high in zip(fields, (0, 0, 1, 1, 0), (59, 23, 31, 12, 7)):
        _values(spec, low, high)


def schedule_matches(schedule: str, moment: datetime) -> bool:
    """Whether a five-field cron expression fires at this minute.

    Fields are minute, hour, day-of-month, month, day-of-week — the standard
    reading, including its one odd rule: when both day fields are restricted, a
    moment matching *either* counts, which is how "the 1st, and every Monday"
    is written.
    """
    validate_schedule(schedule)
    minute, hour, day, month, weekday = schedule.split()
    if moment.minute not in _values(minute, 0, 59):
        return False
    if moment.hour not in _values(hour, 0, 23):
        return False
    if moment.month not in _values(month, 1, 12):
        return False

    days = _values(day, 1, 31)
    # cron counts Sunday as 0 (and 7); Python's weekday() counts Monday as 0.
    weekdays = {value % 7 for value in _values(weekday, 0, 7)}
    sunday_based = (moment.weekday() + 1) % 7
    day_matches = moment.day in days
    weekday_matches = sunday_based in weekdays
    if day.strip() != "*" and weekday.strip() != "*":
        return day_matches or weekday_matches
    return day_matches and weekday_matches


class ReportDefinition(Base):
    """One report this company runs, when, and to whom."""

    __tablename__ = "report_definition"
    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_report_definition_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    # The report's own name — the builder registry's key and what a person says.
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    # What a caller must hold to run it (T-0.SEC.01).
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    # `report_schedule`: a five-field cron expression, and who receives the result.
    schedule: Mapped[str] = mapped_column(String(64), nullable=False)
    recipients: Mapped[list] = mapped_column(JSONB, nullable=False)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ReportRun(Base):
    """One attempt at one report — the record that makes a failure visible."""

    __tablename__ = "report_run"
    __table_args__ = (
        CheckConstraint("status IN ('ok', 'failed')", name="ck_report_run_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    definition_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("report_definition.id"), nullable=False, index=True
    )
    requested_by: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # What the builder produced, and what went wrong instead — one of the two.
    produced: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    # The recipients the run was handed to (T-0.INT.01 owns delivery and retry).
    delivered_to: Mapped[list | None] = mapped_column(JSONB)


# report code -> callable(session, definition) -> dict. Registered by the phase
# that owns the report (T-1.ACCT.07 for the statements, Phase 6 for the rest).
_builders: dict[str, Callable[[Session, ReportDefinition], dict]] = {}


def register_builder(code: str, builder: Callable[[Session, ReportDefinition], dict]) -> None:
    """Register what produces one report — the phase's half of the framework."""
    _builders[code] = builder


def register(
    session: Session,
    *,
    company_id: uuid.UUID,
    code: str,
    name: str,
    schedule: str,
    recipients: list[str],
    capability: str = DEFAULT_CAPABILITY,
) -> ReportDefinition:
    """Register a report: its schedule and recipients are rows, not code."""
    validate_schedule(schedule)
    if not recipients:
        raise ReportError(f"{code} needs at least one recipient")
    definition = ReportDefinition(
        company_id=company_id,
        code=code,
        name=name,
        capability=capability,
        schedule=schedule,
        recipients=list(recipients),
    )
    session.add(definition)
    session.flush()
    return definition


def due(
    session: Session, *, company_id: uuid.UUID, now: datetime | None = None
) -> list[ReportDefinition]:
    """The reports scheduled for this minute, for one company."""
    moment = (now or datetime.now(timezone.utc)).replace(second=0, microsecond=0)
    return [
        definition
        for definition in session.scalars(
            select(ReportDefinition).where(ReportDefinition.company_id == company_id)
        )
        if schedule_matches(definition.schedule, moment)
    ]


def run(
    session: Session,
    definition: ReportDefinition,
    *,
    actor: str,
    now: datetime | None = None,
) -> ReportRun:
    """Produce one report for one caller, and deliver it to its recipients.

    The capability is asked for first: a caller who may not see the report is
    refused before anything is built, and the refusal is on the trail. Whatever
    happens afterwards is a run row — a builder that raises leaves a `failed`
    run with its error, not a silent absence.
    """
    require(
        session,
        company_id=definition.company_id,
        subject=actor,
        capability=definition.capability,
        entity="report_definition",
        entity_id=definition.id,
    )
    run_row = ReportRun(
        company_id=definition.company_id,
        definition_id=definition.id,
        requested_by=str(actor),
        status=FAILED,
    )
    session.add(run_row)
    session.flush()

    builder = _builders.get(definition.code)
    if builder is None:
        run_row.error = (
            f"no builder is registered for {definition.code!r}; the phase that owns"
            " the report registers one"
        )
    else:
        try:
            run_row.produced = builder(session, definition)
        except Exception as exc:  # a run that failed is a row, not an exception
            run_row.error = f"{type(exc).__name__}: {exc}"
        else:
            run_row.status = OK

    if run_row.status == OK:
        for recipient in definition.recipients:
            send_outbound(
                session,
                company_id=definition.company_id,
                channel=DELIVERY_CHANNEL,
                destination=str(recipient),
                payload={
                    "report": definition.code,
                    "name": definition.name,
                    "requested_by": str(actor),
                    "produced": run_row.produced,
                },
            )
        run_row.delivered_to = list(definition.recipients)

    run_row.finished_at = func.now()
    session.flush()
    return run_row


def runs_for(
    session: Session, *, company_id: uuid.UUID, definition: ReportDefinition | None = None
) -> list[ReportRun]:
    """Read the runs back, newest last — how a failure is noticed."""
    statement = (
        select(ReportRun)
        .where(ReportRun.company_id == company_id)
        .order_by(ReportRun.started_at, ReportRun.id)
    )
    if definition is not None:
        statement = statement.where(ReportRun.definition_id == definition.id)
    return list(session.scalars(statement))
