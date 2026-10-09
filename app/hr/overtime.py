"""T-5.ATT.03 — overtime and lateness: what the day meant, from rules that are rows.

The day itself is T-5.ATT.02's: punches in, worked minutes out. This module answers the next
question — *which* minutes are ordinary, which are overtime, at which multiplier, and how
late somebody was — and it answers it from **configuration**, because the ledger states no
multipliers (`overtime_rules` is "rate multiplier + thresholds", not stated) and inventing
one would be inventing a country's labour law.

Three rules this module is built around:

* **A band is a day type, and the day type comes from the calendar and the roster.** A day is
  a `holiday` when T-5.LEAVE.01's calendar says so, a `rest` day when nobody rostered a shift
  for it, and `working` otherwise — in that order, because a holiday is a holiday whether or
  not somebody worked it. Overtime on a rest day is *all* the time worked; on a working day it
  is what exceeds the shift plus the band's own threshold.
* **Ordinary time is a working day's.** Overtime is what exceeds the shift **plus the
  band's threshold** on a working day; on a rest day or a holiday there is nothing to exceed,
  so every minute worked carries that band's rate. `overtime_minutes` is therefore "the
  minutes priced at the band's rate", and the schedule it was measured against is reported
  beside it rather than left to be inferred.
* **The rule is dated, so a past period is not restated.** Rules are **appended** — a new one
  from the day it takes effect, the previous one closing by derivation — and
  :func:`classify_day` reads the rule in force **on the day being classified**. Raising a
  multiplier today therefore cannot re-price last month, which is the criterion's "rule
  changes are dated".
* **Lateness respects the grace and is not absence.** Late minutes are the arrival after the
  shift's start *less the shift's grace*, and nothing here turns them into an absence: an
  employee who arrives inside the grace is not late at all, and one who arrives after it is
  late by the difference — the day is still worked, and T-5.PAY.02 decides what it is worth.

What is deliberately *not* here: the money. A multiplier is reported with the minutes it
applies to and the rule it came from; turning that into an amount (and into the statutory
deductions and premiums a market adds on top) is payroll's, from T-5.PAY.01 onward.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.audit import append_only
from app.db import Base
from app.hr.attendance import attendance_day, punches_on
from app.hr.employees import Employee
from app.hr.holidays import WORKING, day_status
from app.hr.shifts import Shift, resolved_shift

# The bands overtime is separated into — the ledger's own examples ("normal-day vs rest-day",
# and holiday work "classified according to the calendar") as the three day types. A rule is
# stated per band, so a market's own multipliers are configuration rather than code.
DAY_TYPES = ("working", "rest", "holiday")

# Multipliers are exact decimals (DOMAIN-MODELS §2): 1.25 must not become 1.2500000000002.
RATE = Numeric(6, 3)


class OvertimeError(ValueError):
    """The rules refused what was asked of them."""


class InvalidRuleError(OvertimeError):
    """A band, an amount or a multiplier failed validation at entry."""


class RuleSequenceError(OvertimeError):
    """The rule would not follow the one already in force for that band."""


class OvertimeRule(Base):
    """The multiplier and threshold in force for one band, from one date.

    Append-only history: a change is a new row, and the rule it replaces closes by
    derivation. Nothing updates a row, so the rule a past period was classified under stays
    readable — which is what "rule changes are dated" has to mean to be worth anything.
    """

    __tablename__ = "overtime_rule"
    __table_args__ = (
        CheckConstraint(
            "day_type IN (" + ", ".join(f"'{day}'" for day in DAY_TYPES) + ")",
            name="ck_overtime_rule_day_type",
        ),
        CheckConstraint("threshold_minutes >= 0", name="ck_overtime_rule_threshold"),
        CheckConstraint("multiplier > 0", name="ck_overtime_rule_multiplier"),
        # One rule per band per start date: two would make the band's rate ambiguous.
        UniqueConstraint(
            "company_id", "day_type", "effective_from", name="uq_overtime_rule_start"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    day_type: Mapped[str] = mapped_column(String(16), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    # How much beyond the shift counts as overtime at all: a market with a five-minute
    # rounding rule states five, and one that pays from the first minute states zero.
    threshold_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    multiplier: Mapped[Decimal] = mapped_column(RATE, nullable=False)
    # What the rate is, in the company's own words — an inspection aid, not a rule.
    note: Mapped[str | None] = mapped_column(String(160))


# A rule is history: appended, never rewritten (T-0.AUDIT.01).
append_only(OvertimeRule.__table__)


def _required(value: Any, what: str) -> str:
    stated = "" if value is None else str(value).strip()
    if not stated:
        raise InvalidRuleError(f"{what} is required")
    return stated


def _date_or_refuse(value: Any, what: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError as exc:
            raise InvalidRuleError(f"not {what}: {value!r}") from exc
    raise InvalidRuleError(f"{what} is a date, not {value!r}")


def _minutes(value: Any, what: str) -> int:
    try:
        stated = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidRuleError(f"{what} is a number of minutes, not {value!r}") from exc
    if stated < 0:
        raise InvalidRuleError(f"{what} is not negative: {stated}")
    return stated


def _multiplier(value: Any) -> Decimal:
    if isinstance(value, float):
        raise InvalidRuleError(
            f"a multiplier is an exact decimal or a string, not the float {value!r}"
        )
    try:
        rate = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except Exception as exc:  # noqa: BLE001 — any parse failure is the same refusal
        raise InvalidRuleError(f"not a multiplier: {value!r}") from exc
    if not rate.is_finite() or rate <= 0:
        raise InvalidRuleError(f"a multiplier is a finite positive rate, not {rate}")
    return rate


def state_rule(
    session: Session,
    *,
    company_id: uuid.UUID,
    day_type: str,
    effective_from: Any,
    multiplier: Any,
    threshold_minutes: Any = 0,
    note: str | None = None,
) -> OvertimeRule:
    """State the rate for one band from `effective_from`, superseding what it replaces.

    Appended, never edited: a change of rate is a new row, and the rule in force on a date is
    a reading (T-0.AUDIT.01's shape, and why raising a rate cannot re-price a closed month).
    A date that does not come after the band's last rule is refused — two rates claiming one
    day is how a period becomes unanswerable.
    """
    band = _required(day_type, "a day type").lower()
    if band not in DAY_TYPES:
        raise InvalidRuleError(
            f"unknown overtime band {day_type!r}; overtime is classified per"
            f" {', '.join(DAY_TYPES)}"
        )
    starts = _date_or_refuse(effective_from, "the date a rule takes effect")
    latest = latest_rule(session, company_id=company_id, day_type=band)
    if latest is not None and starts <= latest.effective_from:
        raise RuleSequenceError(
            f"a {band} rule from {starts} would not follow the one already recorded"
            f" ({latest.effective_from}); a rate change takes effect from the day it states,"
            " and the rule it replaces is not rewritten"
        )
    rule = OvertimeRule(
        company_id=company_id,
        day_type=band,
        effective_from=starts,
        threshold_minutes=_minutes(threshold_minutes, "an overtime threshold"),
        multiplier=_multiplier(multiplier),
        note=None if note is None else _required(note, "a note"),
    )
    session.add(rule)
    session.flush()
    return rule


def rules_for(session: Session, *, company_id: uuid.UUID, day_type: str) -> list[OvertimeRule]:
    """One band's rules, oldest first — the history a date is read against."""
    return list(
        session.scalars(
            select(OvertimeRule)
            .where(OvertimeRule.company_id == company_id, OvertimeRule.day_type == day_type)
            .order_by(OvertimeRule.effective_from)
        )
    )


def latest_rule(
    session: Session, *, company_id: uuid.UUID, day_type: str
) -> OvertimeRule | None:
    """The most recent rule recorded for that band, or ``None``."""
    return max(
        rules_for(session, company_id=company_id, day_type=day_type),
        key=lambda rule: rule.effective_from,
        default=None,
    )


def rule_in_force(
    session: Session, *, company_id: uuid.UUID, day_type: str, on: date
) -> OvertimeRule | None:
    """The rule that applied to that band on `on`, or ``None`` — never today's by accident.

    The band's history is walked with the same half-open window every dated thing in this
    phase uses: the rule applies from its own date until the next one starts.
    """
    history = rules_for(session, company_id=company_id, day_type=day_type)
    for rule in history:
        following = [
            later.effective_from
            for later in history
            if later.effective_from > rule.effective_from
        ]
        ends = min(following) if following else None
        if rule.effective_from <= on and (ends is None or on < ends):
            return rule
    return None


def day_type_of(session: Session, employee: Employee, *, on: date) -> str:
    """Which band `on` falls in: the calendar first, then the roster, then working.

    Order matters and is stated rather than implied: a holiday is a holiday whether or not
    somebody worked it (an override can put work *on* a holiday, which is exactly what the
    holiday rate is for), and a day nobody rostered is a rest day.
    """
    if day_status(session, company_id=employee.company_id, on=on) != WORKING:
        return "holiday"
    return "rest" if resolved_shift(session, employee, on=on) is None else "working"


def _scheduled_minutes(shift: Shift) -> int:
    """What the shift itself is worth, its break removed — 0 when there is no shift."""
    start = shift.starts_at.hour * 60 + shift.starts_at.minute
    end = shift.ends_at.hour * 60 + shift.ends_at.minute
    window = end - start if end > start else (24 * 60 - start) + end
    return max(0, window - shift.break_minutes)


def _minutes_of(moment: time) -> int:
    """Minutes past midnight, as `_scheduled_minutes` counts them."""
    return moment.hour * 60 + moment.minute


def classify_day(session: Session, employee: Employee, *, on: date) -> dict:
    """One day, classified: its band, its late minutes, and the overtime rate that applies.

    Everything the day's own capture established is carried through (worked minutes, pairs,
    exceptions) and the classification is added beside it, with the rule it used named — so a
    figure can be followed back to the minute it came from and the rate it was priced at. A
    band with **no rule in force** is reported as unclassified rather than assumed to be
    ordinary time: quietly paying overtime at 1.0 is how a market's premium disappears.
    """
    worked = attendance_day(session, employee, on=on)
    shift = resolved_shift(session, employee, on=on)
    band = day_type_of(session, employee, on=on)
    # Ordinary time only exists on a **working** day: on a rest day there is no schedule to
    # exceed, and on a holiday the shift somebody deliberately put on is not a schedule
    # either — the whole of what was worked carries the band's rate. Collapsing those three
    # into "the shift's minutes" is how a holiday premium quietly disappears.
    scheduled = 0 if (shift is None or band != "working") else _scheduled_minutes(shift)

    # Lateness: the first arrival of the day, against the shift's start and its grace. The
    # punch's clock time is read as the site's (both are stored naive-of-site, see
    # T-5.ATT.02); a multi-zone deployment states its zone rather than this inferring one.
    # ponytail: one zone for the site. Ceiling: a punch is read in UTC against a local shift.
    # Upgrade path: the employee's or the location's zone, stated once and applied here.
    late_minutes = 0
    grace = 0 if shift is None else shift.late_grace_minutes
    if band == "working" and shift is not None:
        arrivals = [
            punch.at for punch in punches_on(session, employee, on=on) if punch.direction == "in"
        ]
        if arrivals:
            late_minutes = max(
                0, _minutes_of(arrivals[0].time()) - _minutes_of(shift.starts_at) - grace
            )

    rule = rule_in_force(session, company_id=employee.company_id, day_type=band, on=on)
    threshold = 0 if rule is None else rule.threshold_minutes
    overtime = max(0, worked["worked_minutes"] - scheduled - threshold)

    return {
        **worked,
        "day_type": band,
        "scheduled_minutes": scheduled,
        "late_minutes": late_minutes,
        "late_grace_minutes": grace,
        "absence": False,
        "overtime_minutes": overtime if rule is not None else 0,
        "band": band if rule is not None else None,
        "multiplier": None if rule is None else format(rule.multiplier, "f"),
        "rule_effective_from": None if rule is None else rule.effective_from.isoformat(),
        "unclassified_overtime_minutes": 0 if rule is not None else overtime,
        "unclassified_reason": None
        if rule is not None
        else f"no {band} overtime rule is in force on {on}; state one (T-5.ATT.03)",
    }


def classify_week(session: Session, employee: Employee, *, starts_on: date) -> list[dict]:
    """Seven days from `starts_on`, classified — the week as the payroll reads it."""
    return [
        classify_day(session, employee, on=starts_on + timedelta(days=offset))
        for offset in range(7)
    ]
