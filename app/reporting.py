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
  caller's company only, and a refused attempt is on the audit trail. A recipient
  is scoped too (T-6.ANALYTICS.02): a mailbox is handed the report, a *subject* is
  handed it only if it may see that report, and one that may not is named on the
  run rather than quietly sent the figures.
* **A run covers a period.** The definition states its granularity and the run
  states the period it covered, so a re-run for the same period is a `skipped`
  run — visible, and delivered to nobody — instead of a second copy of the same
  report, and a report for August says August rather than whatever month it
  happens to be run in.
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
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import (
    CheckConstraint,
    Date,
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
from app.security import capabilities, require

# Run states, as the framework reports them. `skipped` is a run that was asked for a period
# that was already delivered: a row, so the attempt is visible, and no delivery.
OK, FAILED, SKIPPED = "ok", "failed", "skipped"

# The granularity a definition covers, and the windows each one means.
DAY, MONTH, QUARTER, YEAR = "day", "month", "quarter", "year"
PERIODS = (DAY, MONTH, QUARTER, YEAR)


class PeriodError(ValueError):
    """A period the framework cannot read."""


@dataclass(frozen=True)
class Window:
    """The period one run covers — stated, so a report for August says August.

    A builder reads this rather than asking what month it is: a report re-run for a past period
    must produce that period's figures, and a report produced this morning for last month must
    not silently cover this one.
    """

    granularity: str
    period: date
    start: date
    end: date


def period_window(granularity: str, on: date) -> Window:
    """The window `granularity` means for the date `on`."""
    chosen = str(granularity).strip().lower()
    if chosen == DAY:
        start, end = on, on
    elif chosen == MONTH:
        start = on.replace(day=1)
        end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    elif chosen == QUARTER:
        start = on.replace(month=((on.month - 1) // 3) * 3 + 1, day=1)
        end = (start + timedelta(days=100)).replace(day=1) - timedelta(days=1)
    elif chosen == YEAR:
        start, end = on.replace(month=1, day=1), on.replace(month=12, day=31)
    else:
        raise PeriodError(
            f"unknown period {granularity!r}; a definition covers one of {', '.join(PERIODS)}"
        )
    return Window(granularity=chosen, period=on, start=start, end=end)

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
    # What one run of it covers: a day, a month, a quarter or a year (T-6.ANALYTICS.02).
    period: Mapped[str] = mapped_column(String(16), nullable=False, server_default="month")
    recipients: Mapped[list] = mapped_column(JSONB, nullable=False)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ReportRun(Base):
    """One attempt at one report — the record that makes a failure visible."""

    __tablename__ = "report_run"
    __table_args__ = (
        CheckConstraint("status IN ('ok', 'failed', 'skipped')", name="ck_report_run_status"),
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
    # The period this run covered, so "was September delivered?" is a query rather than a guess
    # (and a second run for it is the `skipped` row below).
    period: Mapped[date | None] = mapped_column(Date)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # What the builder produced, and what went wrong instead — one of the two.
    produced: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    # The recipients the run was handed to (T-0.INT.01 owns delivery and retry), and the ones
    # it was **not** handed to, each with the reason (T-6.ANALYTICS.02).
    delivered_to: Mapped[list | None] = mapped_column(JSONB)
    withheld_recipients: Mapped[list | None] = mapped_column(JSONB)


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
    period: str = MONTH,
) -> ReportDefinition:
    """Register a report: its schedule, its period and its recipients are rows, not code."""
    validate_schedule(schedule)
    period_window(period, date.today())  # refuses a granularity nothing can read
    if not recipients:
        raise ReportError(f"{code} needs at least one recipient")
    definition = ReportDefinition(
        company_id=company_id,
        code=code,
        name=name,
        capability=capability,
        schedule=schedule,
        period=period,
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


def deliverable_recipients(
    session: Session, definition: ReportDefinition
) -> tuple[list[str], list[dict]]:
    """Which recipients may be handed this report, and which may not, with the reason.

    A **mailbox** is handed it: an address is not a subject of this platform, and the definition
    naming it is the decision. A recipient that names a **subject** is checked against the
    report's own capability first, so a run can never post the figures to somebody the API would
    refuse to show them to — the run says who it left out instead.
    """
    allowed: list[str] = []
    withheld: list[dict] = []
    for recipient in definition.recipients:
        named = str(recipient)
        if "@" in named:
            allowed.append(named)
            continue
        held = capabilities(session, company_id=definition.company_id, subject=named)
        if definition.capability in held:
            allowed.append(named)
        else:
            withheld.append(
                {
                    "recipient": named,
                    "reason": f"{named!r} may not {definition.capability!r} (holds"
                    f" {', '.join(sorted(held)) or 'no capabilities'})",
                }
            )
    return allowed, withheld


def delivered_period(
    session: Session, definition: ReportDefinition, period: date
) -> ReportRun | None:
    """The run that already delivered this period, if one did — what a re-run is measured against."""
    return session.scalar(
        select(ReportRun).where(
            ReportRun.definition_id == definition.id,
            ReportRun.period == period,
            ReportRun.status == OK,
        )
    )


def run(
    session: Session,
    definition: ReportDefinition,
    *,
    actor: str,
    now: datetime | None = None,
    period: date | None = None,
) -> ReportRun:
    """Produce one report for one caller and period, and deliver it to its recipients.

    The capability is asked for first: a caller who may not see the report is refused before
    anything is built, and the refusal is on the trail. Whatever happens afterwards is a run
    row — a builder that raises leaves a `failed` run with its error, not a silent absence.

    A period that was **already delivered** leaves a `skipped` run instead: the attempt stays
    visible, and the same report for the same period does not arrive twice. A period that
    *failed* is not skipped — retrying a failure is the reason the run is a row.
    """
    require(
        session,
        company_id=definition.company_id,
        subject=actor,
        capability=definition.capability,
        entity="report_definition",
        entity_id=definition.id,
    )
    moment = period or (now or datetime.now(timezone.utc)).date()
    window = period_window(definition.period, moment)

    already = delivered_period(session, definition, moment)
    if already is not None:
        skipped = ReportRun(
            company_id=definition.company_id,
            definition_id=definition.id,
            requested_by=str(actor),
            period=moment,
            status=SKIPPED,
            error=(
                f"the {definition.period} of {window.start}..{window.end} was already delivered"
                f" on {already.finished_at or already.started_at}"
            ),
        )
        session.add(skipped)
        session.flush()
        return skipped

    run_row = ReportRun(
        company_id=definition.company_id,
        definition_id=definition.id,
        requested_by=str(actor),
        period=moment,
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
            run_row.produced = builder(session, definition, window)
        except Exception as exc:  # a run that failed is a row, not an exception
            run_row.error = f"{type(exc).__name__}: {exc}"
        else:
            run_row.status = OK

    if run_row.status == OK:
        allowed, withheld = deliverable_recipients(session, definition)
        for recipient in allowed:
            send_outbound(
                session,
                company_id=definition.company_id,
                channel=DELIVERY_CHANNEL,
                destination=str(recipient),
                payload={
                    "report": definition.code,
                    "name": definition.name,
                    "period": window.start.isoformat(),
                    "produced": run_row.produced,
                },
            )
        run_row.delivered_to = allowed
        if withheld:
            run_row.withheld_recipients = withheld

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
