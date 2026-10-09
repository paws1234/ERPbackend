"""T-5.PAY.01 — the payroll structure: what is paid, what is withheld, and in what order.

The structure is **rows, dated, per company** — never a table of constants in this file. A
component states its kind (an earning, a deduction, an employer contribution), its
**calculation basis** (how its amount is arrived at), whether it is taxable, and where it sits
in the order the run applies. Changing what a company pays is a new row from a new date; the
rows that came before are left exactly as they were, which is what keeps a computed period
reproducible.

Three rules this module is built around:

* **No rate, and no market, is written in code.** The statutory deductions are **loaded from
  the localization pack** (T-0.LOC.01): their codes, their rates, the accounts they post to
  and the pack version they came from are read from the pack's `statutory_rules` and stored
  with their source. Nothing in this module knows what a market's
  contribution is called or what it charges — the pack says it, and a pack update is a load,
  not a release.
* **A component's amount follows from its basis, so the two cannot disagree.** A `fixed` or
  `per_worked_day` component carries an amount and no rate; a `percent_of_basic` or
  `percent_of_gross` one carries a rate and no amount. The database enforces the pairing, so a
  row that would leave the engine guessing cannot be written — by this module or by any other
  path.
* **A later version changes later periods only.** Components are append-only and read **as of
  a date**: `components_in_force(on=…)` answers with the row in force on that day for each
  code, so re-loading a pack or restating an allowance applies from its own date onwards and
  the structure of an earlier period reads as it did then. Stating a component effective
  inside a **locked** month is refused, because that period has been closed.

What is deliberately *not* here: working out any amount (T-5.PAY.02's engine reads this
structure), loan recovery (T-5.PAY.03, which is what the pack's `loan` rule is a place for),
report production (T-5.PAY.04) and posting to the ledger (T-5.PAY.07, which reads the
`account_code` each component carries).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import (
    Boolean,
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
from app.ledger.periods import period_is_locked
from app.localization import load_pack, statutory_rules

# What a component is to the run: money added, money withheld from the employee, or money the
# employer pays on their behalf (which is not deducted from anybody's pay).
COMPONENT_KINDS = ("earning", "deduction", "employer_contribution")

# How a component's amount is arrived at — the `payroll_components` calculation basis:
#
# * `fixed`                — the amount, once per run
# * `percent_of_basic`     — the rate applied to the contract's monthly basic salary
# * `percent_of_gross`     — the rate applied to the gross earnings the run has computed
# * `per_worked_day`       — the amount for each day the employee worked in the period
# * `loan_schedule`        — nothing of its own: the instalment a loan schedule states
#                            (T-5.PAY.03). The pack names loan recovery as a deduction "so
#                            payroll has one place to look", and this is that place.
BASES = ("fixed", "percent_of_basic", "percent_of_gross", "per_worked_day", "loan_schedule")
AMOUNT_BASES = ("fixed", "per_worked_day")
RATE_BASES = ("percent_of_basic", "percent_of_gross")

# Where a row came from. A pack row is one the localization pack stated; a company row is one
# the company stated. They live in one table so the run reads one ordered structure, and the
# column keeps their provenance visible rather than implied.
PACK_SOURCE = "pack"
COMPANY_SOURCE = "company"

MONEY = Numeric(20, 6)
PERCENT = Numeric(6, 3)


class ComponentError(ValueError):
    """The payroll structure refused what was asked of it."""


class InvalidComponentError(ComponentError):
    """A code, a kind, a basis, an amount or a rate failed validation at entry."""


class DuplicateComponentError(ComponentError):
    """That component code already has a row effective on that date."""


class ClosedPeriodError(ComponentError):
    """The component would take effect inside a locked month."""


class PayrollComponent(Base):
    """One line of the company's payroll structure, as it stood from one date.

    Append-only (:func:`app.audit.append_only`): a component is never edited, so what a
    computed period was computed from stays readable, and a change is a new row from a new
    date. `effective_to` is **derived** — the next row for the same code — rather than stored,
    the same way every other dated history in the platform states its window.
    """

    __tablename__ = "payroll_component"
    __table_args__ = (
        # One statement of a component per date: two would make the structure ambiguous about
        # what applied on the day.
        UniqueConstraint(
            "company_id", "code", "effective_from", name="uq_payroll_component_effective"
        ),
        CheckConstraint(
            "kind IN (" + ", ".join(f"'{kind}'" for kind in COMPONENT_KINDS) + ")",
            name="ck_payroll_component_kind",
        ),
        CheckConstraint(
            "basis IN (" + ", ".join(f"'{basis}'" for basis in BASES) + ")",
            name="ck_payroll_component_basis",
        ),
        # The basis and the figure it needs are one statement: an amount without a basis that
        # uses one, or a rate without one, cannot be read by the engine.
        CheckConstraint(
            "(basis IN (" + ", ".join(f"'{basis}'" for basis in AMOUNT_BASES) + "))"
            " = (amount IS NOT NULL)",
            name="ck_payroll_component_amount_basis",
        ),
        CheckConstraint(
            "(basis IN (" + ", ".join(f"'{basis}'" for basis in RATE_BASES) + "))"
            " = (rate_percent IS NOT NULL)",
            name="ck_payroll_component_rate_basis",
        ),
        CheckConstraint("amount IS NULL OR amount >= 0", name="ck_payroll_component_amount"),
        CheckConstraint(
            "rate_percent IS NULL OR (rate_percent >= 0 AND rate_percent <= 100)",
            name="ck_payroll_component_rate",
        ),
        CheckConstraint(
            "source IN ('" + PACK_SOURCE + "', '" + COMPANY_SOURCE + "')",
            name="ck_payroll_component_source",
        ),
        CheckConstraint("char_length(code) > 0", name="ck_payroll_component_code"),
        CheckConstraint("char_length(name) > 0", name="ck_payroll_component_name"),
        CheckConstraint("order_no >= 0", name="ck_payroll_component_order"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    basis: Mapped[str] = mapped_column(String(24), nullable=False)
    # The figure the basis reads: an amount for `fixed`/`per_worked_day`, a rate for the
    # percentage bases, and neither for `loan_schedule`. Exact decimals, never floats.
    amount: Mapped[Decimal | None] = mapped_column(MONEY)
    rate_percent: Mapped[Decimal | None] = mapped_column(PERCENT)
    # For an earning: whether the amount is part of **taxable compensation**. For a deduction:
    # whether it is taken **before** taxable compensation is measured (a pre-tax deduction).
    # For an employer contribution: whether it is part of the employee's taxable compensation
    # — which an employer's own share is not.
    taxable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Where it sits in the order the run applies. Spaced by tens for the pack's rows so a
    # company can put its own line between two of them without renumbering anything.
    order_no: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default=COMPANY_SOURCE)
    # The pack version a loaded row came from, so a run can state what it was computed with
    # (T-5.PAY.02) and a reader can tell which version stated a rate.
    pack_version: Mapped[str | None] = mapped_column(String(32))
    # The ledger account the pack states for the component, where it states one — what
    # T-5.PAY.07 posts the liability to.
    account_code: Mapped[str | None] = mapped_column(String(32))


# The structure is history: appended, never rewritten — see the class docstring.
append_only(PayrollComponent.__table__)


def _text(value: Any, what: str) -> str:
    """The text as written, or a refusal."""
    written = "" if value is None else str(value).strip()
    if not written:
        raise InvalidComponentError(f"{what} is required")
    return written


def _date_or_refuse(value: Any, what: str) -> date:
    """A `date`, or a refusal — a `datetime` is quietly reduced to its day."""
    if isinstance(value, date) and not hasattr(value, "hour"):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise InvalidComponentError(f"not {what}: {value!r} (a date is `YYYY-MM-DD`)") from exc


def _decimal_or_refuse(value: Any, what: str) -> Decimal:
    """An exact decimal, or a refusal — a float never reaches money or a rate (DOMAIN §2)."""
    if isinstance(value, float):
        raise InvalidComponentError(
            f"{what} is an exact decimal or a string, not the float {value!r}"
        )
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, AttributeError, ValueError) as exc:
        raise InvalidComponentError(f"not {what}: {value!r}") from exc
    if not number.is_finite():
        raise InvalidComponentError(f"{what} is a finite number, not {number}")
    return number


def _pack_number(value: Any) -> Decimal:
    """A number the pack's JSON states, as the exact decimal its text spells.

    JSON numbers arrive from the parser as floats, so this is the one place a float is read —
    and it is read as its shortest text (`Decimal(str(value))`), not through float arithmetic,
    so a rate of `2.5` is `2.500` and never `2.5000000000000004`. Nothing downstream of here
    sees a float: :func:`define_component` refuses one (DOMAIN-MODELS §2).
    """
    return Decimal(str(value))


def _refuse_locked_period(session: Session, *, company_id: uuid.UUID, on: date) -> None:
    """A component cannot take effect inside a locked month: that period is closed.

    The same guard a stated holiday carries (T-5.LEAVE.01): a period that has been closed is
    not restated by adding structure to it, and a run that already happened has its own copy
    of what it used.
    """
    if period_is_locked(session, company_id=company_id, on=on):
        raise ClosedPeriodError(
            f"{on:%Y-%m} is locked; a component effective from {on} would restate a closed"
            " period — state it from the first open month instead"
        )


def define_component(
    session: Session,
    *,
    company_id: uuid.UUID,
    code: str,
    name: str,
    kind: str,
    basis: str,
    effective_from: Any,
    amount: Any = None,
    rate_percent: Any = None,
    taxable: bool = True,
    order: int = 100,
    account_code: str | None = None,
    source: str = COMPANY_SOURCE,
    pack_version: str | None = None,
) -> PayrollComponent:
    """State one component of the payroll structure, from a date.

    The figure is checked against the basis rather than coerced into it: a `fixed` component
    with no amount, or a `percent_of_basic` one with no rate, is refusable here rather than
    discoverable when a run computes. A rate is a percentage between 0 and 100, and both the
    amount and the rate are exact decimals.
    """
    wanted_kind = _text(kind, "a component kind").lower()
    if wanted_kind not in COMPONENT_KINDS:
        raise InvalidComponentError(
            f"unknown component kind {kind!r}; a component is one of"
            f" {', '.join(COMPONENT_KINDS)}"
        )
    wanted_basis = _text(basis, "a calculation basis").lower()
    if wanted_basis not in BASES:
        raise InvalidComponentError(
            f"unknown calculation basis {basis!r}; a component's amount is arrived at by"
            f" {', '.join(BASES)}"
        )
    wanted_source = _text(source, "a component source").lower()
    if wanted_source not in (PACK_SOURCE, COMPANY_SOURCE):
        raise InvalidComponentError(
            f"unknown component source {source!r}; a component is stated by the company"
            f" ({COMPANY_SOURCE!r}) or loaded from a pack ({PACK_SOURCE!r})"
        )
    wanted_code = _text(code, "a component code")
    wanted_name = _text(name, "a component name")
    starts = _date_or_refuse(effective_from, "the effective date")

    if wanted_basis in AMOUNT_BASES and amount is None:
        raise InvalidComponentError(
            f"{wanted_code} is stated on the {wanted_basis!r} basis, which needs an amount"
        )
    if wanted_basis in RATE_BASES and rate_percent is None:
        raise InvalidComponentError(
            f"{wanted_code} is stated on the {wanted_basis!r} basis, which needs a rate"
        )
    if wanted_basis not in AMOUNT_BASES and amount is not None:
        raise InvalidComponentError(
            f"{wanted_code} is stated on the {wanted_basis!r} basis, which carries no amount"
            " — the figure would be read by nothing"
        )
    if wanted_basis not in RATE_BASES and rate_percent is not None:
        raise InvalidComponentError(
            f"{wanted_code} is stated on the {wanted_basis!r} basis, which carries no rate"
        )

    money = None if amount is None else _decimal_or_refuse(amount, f"the amount of {wanted_code}")
    if money is not None and money < 0:
        raise InvalidComponentError(f"the amount of {wanted_code} is not negative: {money}")
    rate = (
        None
        if rate_percent is None
        else _decimal_or_refuse(rate_percent, f"the rate of {wanted_code}")
    )
    if rate is not None and not Decimal(0) <= rate <= Decimal(100):
        raise InvalidComponentError(
            f"the rate of {wanted_code} is a percentage between 0 and 100, not {rate}"
        )
    if not isinstance(order, int) or isinstance(order, bool) or order < 0:
        raise InvalidComponentError(f"the order of {wanted_code} is a whole number, not {order!r}")

    _refuse_locked_period(session, company_id=company_id, on=starts)
    existing = session.scalar(
        select(PayrollComponent).where(
            PayrollComponent.company_id == company_id,
            PayrollComponent.code == wanted_code,
            PayrollComponent.effective_from == starts,
        )
    )
    if existing is not None:
        raise DuplicateComponentError(
            f"{wanted_code} is already stated in this company from {starts}; state the change"
            " from a later date instead of restating the same day"
        )

    component = PayrollComponent(
        company_id=company_id,
        code=wanted_code,
        name=wanted_name,
        kind=wanted_kind,
        basis=wanted_basis,
        amount=money,
        rate_percent=rate,
        taxable=bool(taxable),
        order_no=order,
        effective_from=starts,
        source=wanted_source,
        pack_version=None if pack_version is None else str(pack_version),
        account_code=None if account_code is None else str(account_code).strip() or None,
    )
    session.add(component)
    session.flush()
    return component


def load_statutory_components(
    session: Session,
    *,
    company_id: uuid.UUID,
    market: str,
    effective_from: Any,
) -> list[PayrollComponent]:
    """Load a market's statutory deductions from the pack (T-0.LOC.01), as dated rows.

    Every rule the pack states becomes a component: its code, its name, its rate, the account
    it posts to, and the pack version it came from. **How** a rule reads the employee's pay is
    the platform's own vocabulary (the basis), because the pack states its basis in words
    ("monthly basic salary, within the ceiling and floor in force") rather than as a formula —
    and a rule whose basis is the outstanding balance of a loan is the place T-5.PAY.03 fills.

    Loading is stated **from a date**, so a pack update is a load from its own date rather than
    a rewrite: the periods computed before it keep the rows they were computed from. A market
    the packs do not cover is refused by the localization package, not waved through.
    """
    starts = _date_or_refuse(effective_from, "the effective date")
    version = load_pack(market)["version"]
    rules = statutory_rules(market)
    if not rules:
        raise InvalidComponentError(
            f"the {market!r} pack states no statutory rules; there are no deductions to load"
        )
    # A rule another rule names as its employer share is the employer's half of the same
    # contribution — read from the pack's own pairing rather than from a code ending in "ER".
    codes = {rule["code"] for rule in rules}
    employer_rules = {
        rule["employer_rule"] for rule in rules if rule.get("employer_rule") in codes
    }
    loaded: list[PayrollComponent] = []
    for index, rule in enumerate(rules, start=1):
        code = _text(rule.get("code"), "a statutory rule code")
        if code in employer_rules:
            kind = "employer_contribution"
        else:
            kind = "deduction"
        basis = "loan_schedule" if rule.get("kind") == "loan" else "percent_of_basic"
        loaded.append(
            define_component(
                session,
                company_id=company_id,
                code=code,
                name=_text(rule.get("name"), f"the name of {code}"),
                kind=kind,
                basis=basis,
                effective_from=starts,
                rate_percent=(
                    None if basis == "loan_schedule" else _pack_number(rule.get("rate_percent", 0))
                ),
                # The pack states no tax treatment for a contribution, so nothing is claimed
                # on its behalf: an employer's share is not the employee's taxable
                # compensation, and a pre-tax treatment is not read into a deduction the pack
                # describes only as a contribution. ponytail: when a pack states the
                # treatment, this reads it instead of stating the conservative answer.
                taxable=kind == "earning",
                order=10 * index,
                account_code=rule.get("account"),
                source=PACK_SOURCE,
                pack_version=version,
            )
        )
    return loaded


def components_of(session: Session, *, company_id: uuid.UUID) -> list[PayrollComponent]:
    """Every row of the structure, oldest first — the whole history, not only what is in force."""
    return list(
        session.scalars(
            select(PayrollComponent)
            .where(PayrollComponent.company_id == company_id)
            .order_by(
                PayrollComponent.effective_from,
                PayrollComponent.order_no,
                PayrollComponent.code,
            )
        )
    )


def components_in_force(
    session: Session, *, company_id: uuid.UUID, on: date
) -> list[PayrollComponent]:
    """Each component's row in force on `on`, in the order the run applies them.

    The row in force is the latest one stated on or before the day (the same derived-window
    reading the roster and the contracts use), so a structure stated from a date applies from
    that date onward and no earlier.
    """
    latest: dict[str, PayrollComponent] = {}
    for row in components_of(session, company_id=company_id):
        if row.effective_from > on:
            continue
        current = latest.get(row.code)
        if current is None or row.effective_from >= current.effective_from:
            latest[row.code] = row
    return sorted(latest.values(), key=lambda row: (row.order_no, row.code))


def component_payload(component: PayrollComponent) -> dict:
    """One component as data: what it is, how it is calculated, and where it came from."""
    return {
        "code": component.code,
        "name": component.name,
        "kind": component.kind,
        "basis": component.basis,
        "amount": None if component.amount is None else str(component.amount),
        "rate_percent": None if component.rate_percent is None else str(component.rate_percent),
        "taxable": bool(component.taxable),
        "order": component.order_no,
        "effective_from": component.effective_from.isoformat(),
        "source": component.source,
        "pack_version": component.pack_version,
        "account_code": component.account_code,
    }


def structure_on(session: Session, *, company_id: uuid.UUID, on: date) -> dict:
    """The whole structure in force on `on`: what is added, what is withheld, what is owed.

    The reading a run takes (T-5.PAY.02), and it states the pack version(s) the statutory rows
    came from, because a payroll figure that cannot say which rules produced it cannot be
    re-checked later.
    """
    in_force = components_in_force(session, company_id=company_id, on=on)
    grouped: dict[str, list[dict]] = {kind: [] for kind in COMPONENT_KINDS}
    for component in in_force:
        grouped[component.kind].append(component_payload(component))
    versions = sorted({row.pack_version for row in in_force if row.pack_version is not None})
    return {
        "on": on.isoformat(),
        "pack_versions": versions,
        "order": [component.code for component in in_force],
        "earnings": grouped["earning"],
        "deductions": grouped["deduction"],
        "employer_contributions": grouped["employer_contribution"],
    }
