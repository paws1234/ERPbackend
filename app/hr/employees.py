"""T-5.EMP.01 — the employee master: a role on the shared party, plus its own data.

§5 puts **Party (Customer / Supplier / Employee)** first: a counterparty is created
once and given the roles it holds. T-0.PARTY.01 owns that identity and deliberately
carries no role's own attributes, so nothing here duplicates a name, a tax number or a
role. This module owns the *employment* side of a person:

* **the employment itself** — the employee number a roster and a payslip name, the hire
  date, and the personal details the employee is owed privacy about;
* **the contract history** — the terms in force over time. A new contract
  **supersedes** the one it replaces (the new row names what it replaces, and the
  replaced contract's window closes by derivation rather than by an edit) rather than
  overwriting it, because the payroll of a past period has to be reproducible from the
  terms that then applied;
* **the assets assigned** — what the employee holds and, crucially, when it came back:
  an asset nobody can show as returned is exactly what an offboarding misses.

Three rules the module is built around:

* **One profile per party.** An employee is a *view* of a party, so a person who is
  also a customer is one identity with two roles and one tax number, never two records
  that drift apart. Giving an existing party the employee role **appends** it — a repeat
  is already held and is not stored twice — and a second employee profile for the same
  party is refused outright.
* **Terms are history, not state.** "What does this employee earn" is answered by the
  terms whose window contains the date being asked about (:func:`contract_in_force`), and
  new terms are *appended* naming the contract they supersede (:func:`record_contract`) —
  the table refuses an UPDATE — so a raise agreed in October cannot restate September.
* **Salary and personal fields are restricted, not merely unshown.** Which of them a
  caller receives is decided per request by T-0.SEC.01's field-level permissions
  (:func:`employee_payload`), and a write of one by a role that may not write it is
  refused at entry (:func:`set_personal_details`, :func:`record_contract`) rather than
  noticed later.

What is deliberately *not* here: the reporting hierarchy (T-5.EMP.02), lifecycle
movements such as a transfer or an exit (T-5.EMP.03), and every payroll calculation
(the phase's `PAY` tasks). Retirement follows the T-0.AUDIT.01 master convention —
`deleted_at` by marking, never a `DELETE` — while *exiting employment* is an employment
movement and belongs to T-5.EMP.03, not to a soft delete.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.audit import INCLUDE_SOFT_DELETED, SoftDeleteMixin, append_only, deny_hard_delete
from app.db import Base
from app.ledger.currency import currency_by_code
from app.party import Party, PartyRole, party_by_code
from app.security import hidden_fields, reject_restricted_fields

# The role an employee's party must hold — T-0.PARTY.01's own vocabulary.
EMPLOYEE_ROLE = "employee"

# The fields that are the employee's own business rather than the company's, named
# once so the restriction, the payload and the setter cannot disagree about which
# ones they are. A restriction is a row (T-0.SEC.01), so the list is the vocabulary
# the rows are stated in — not the enforcement itself.
PERSONAL_FIELDS = ("date_of_birth", "personal_email", "mobile_number", "home_address")

# The entities the field-level restrictions are stated under: the tables' own names,
# the way `journal_line` is stated (T-0.SEC.01).
EMPLOYEE_ENTITY = "employee"
CONTRACT_ENTITY = "employment_contract"
ASSET_ENTITY = "employee_asset"

# Exact decimals, like every amount in the platform (DOMAIN-MODELS.md §2).
MONEY = Numeric(20, 6)

# Loose but real, the same shape the customer master enforces: something@something.tld,
# and nothing with whitespace in it.
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class EmployeeError(ValueError):
    """The employee master refused what was asked of it."""


class InvalidEmployeeDataError(EmployeeError):
    """A personal detail, a date or a salary failed validation at entry."""


class UnknownEmployeeError(EmployeeError):
    """A lookup named an employee this company does not have."""


class NotAnEmployeeError(EmployeeError):
    """A party without the employee role was treated as one."""


class DuplicateEmployeeError(EmployeeError):
    """That party already has an employee profile — one identity, one profile."""


class ContractSequenceError(EmployeeError):
    """The terms would not follow the ones already recorded."""


class AssetReturnError(EmployeeError):
    """An asset was returned twice, or returned before it was assigned."""


class Employee(SoftDeleteMixin, Base):
    """The employment side of one party."""

    __tablename__ = "employee"
    __table_args__ = (
        # One profile per party: a second one is how one employee becomes two.
        UniqueConstraint("company_id", "party_id", name="uq_employee_company_party"),
        # The number is what a roster, a timesheet and a payslip name the employee by,
        # so it is unique within the company and is never re-used for a different person.
        UniqueConstraint("company_id", "number", name="uq_employee_company_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    party_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("party.id"), nullable=False, index=True
    )
    number: Mapped[str] = mapped_column(String(32), nullable=False)
    # When employment began. The contract history hangs off it: terms cannot start
    # before the person was hired. A *change* of hire date is a movement (T-5.EMP.03),
    # not an edit here.
    hire_date: Mapped[date] = mapped_column(Date, nullable=False)

    # --- The employee's own business (field-restricted, T-0.SEC.01) ---------------
    # Stated, never defaulted: a birth date nobody gave is unknown, and inventing one
    # would be a fabrication in the one place the platform is owed accuracy. Null is a
    # real answer for each of these, which is why they are nullable rather than required.
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    personal_email: Mapped[str | None] = mapped_column(String(160))
    mobile_number: Mapped[str | None] = mapped_column(String(40))
    home_address: Mapped[str | None] = mapped_column(String(255))

    party: Mapped[Party] = relationship()
    contracts: Mapped[list[EmploymentContract]] = relationship(
        back_populates="employee", order_by="EmploymentContract.effective_from"
    )
    assets: Mapped[list[EmployeeAsset]] = relationship(
        back_populates="employee", order_by="EmployeeAsset.assigned_on"
    )


class EmploymentContract(Base):
    """The terms that took effect on one date — one link in an employee's contract history.

    **The table is append-only** (:func:`app.audit.append_only`): a contract is never
    updated and never deleted, so the terms of a past period stay exactly as they were
    recorded and the payroll of that period remains reproducible. New terms are a new
    row that *names what it supersedes*, and the window of the terms it replaced closes
    by **derivation** (:attr:`effective_to` is the next contract's start date) rather
    than by an UPDATE — a history that anything rewrites is not a history.
    """

    __tablename__ = "employment_contract"
    __table_args__ = (
        CheckConstraint("basic_salary >= 0", name="ck_employment_contract_salary"),
        CheckConstraint(
            "transaction_currency IS NULL OR char_length(transaction_currency) = 3",
            name="ck_employment_contract_currency_length",
        ),
        # Two contracts cannot claim the same start: the history would be ambiguous
        # about which terms applied on that day.
        UniqueConstraint("employee_id", "effective_from", name="uq_employment_contract_start"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id"), nullable=False, index=True
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    # The kind of engagement, as the company states it.
    # ponytail: a free string, not an enumerated kind — the plan names no vocabulary for
    # it (`employment terms` is left to the user), and a CHECK would be this module
    # deciding what a probationary or project engagement is called. Upgrade path: a pack
    # (`statutory_pack` already carries the market's rules) or a configured list states
    # the kinds, and this column validates against it.
    contract_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # The salary for one payroll cycle, as an exact decimal. §4 Phase 5 fixes the cycle at
    # monthly (`payroll_cycle`), so this is a monthly figure; an hourly or daily rate is
    # derived where attendance is costed (T-5.PAY.02), not stored a second time here.
    basic_salary: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    # Null means the company's base currency — the honest way to say "paid in the base
    # currency" rather than storing a copy that a base-currency change would silently date.
    transaction_currency: Mapped[str | None] = mapped_column(String(3))
    # What these terms replaced, so the history is walkable in both directions.
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employment_contract.id")
    )

    employee: Mapped[Employee] = relationship(back_populates="contracts")

    @property
    def effective_to(self) -> date | None:
        """When these terms stopped applying: the next contract's start, or ``None``.

        Derived from the history rather than stored on the row, because storing it means
        updating a contract every time a later one is recorded — and a row of the history
        that gets rewritten is precisely what this table's append-only rule forbids. The
        window is half-open, so the day the next terms start belongs to them alone.
        """
        following = [
            later.effective_from
            for later in self.employee.contracts
            if later.effective_from > self.effective_from
        ]
        return min(following) if following else None


class EmployeeAsset(Base):
    """Something the employee holds, and the date it came back.

    Two dates and no status column: an asset is held while ``returned_on`` is null, so
    "still out" cannot disagree with "the status says held".
    """

    __tablename__ = "employee_asset"
    __table_args__ = (
        CheckConstraint(
            "returned_on IS NULL OR returned_on >= assigned_on",
            name="ck_employee_asset_window",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id"), nullable=False, index=True
    )
    description: Mapped[str] = mapped_column(String(160), nullable=False)
    # The tag or serial on the thing itself, where it has one: the asset register's link
    # to a physical object rather than to a description of one.
    identifier: Mapped[str | None] = mapped_column(String(64))
    assigned_on: Mapped[date] = mapped_column(Date, nullable=False)
    returned_on: Mapped[date | None] = mapped_column(Date)

    employee: Mapped[Employee] = relationship(back_populates="assets")


# An employee is a master: retired by marking, never removed (T-0.AUDIT.01).
deny_hard_delete(Employee.__table__)
# A contract is history: appended, never rewritten (T-0.AUDIT.01). Correcting terms is a
# new contract that supersedes the wrong one, the way the ledger is corrected by posting
# rather than by editing.
append_only(EmploymentContract.__table__)


def _required(value: Any, what: str) -> str:
    stated = "" if value is None else str(value).strip()
    if not stated:
        raise InvalidEmployeeDataError(f"{what} is required")
    return stated


def _date_or_refuse(value: Any, what: str) -> date:
    """A calendar date from a date, a datetime or the ISO string the boundary carries.

    A string is accepted because that is how a date crosses the API boundary; anything
    else — a number, a guess, a timestamp pretending to be a day — is refused rather
    than coerced.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError as exc:
            raise InvalidEmployeeDataError(f"not {what}: {value!r}") from exc
    raise InvalidEmployeeDataError(f"{what} is a date, not {value!r}")


def _personal_value(field: str, value: Any) -> Any:
    """One personal detail, validated for the field it belongs to."""
    if value is None:
        return None
    if field == "date_of_birth":
        return _date_or_refuse(value, "a date of birth")
    stated = _required(value, f"a {field.replace('_', ' ')}")
    if field == "personal_email":
        if not _EMAIL.match(stated.lower()):
            raise InvalidEmployeeDataError(f"not an e-mail address: {stated!r}")
        return stated.lower()
    return stated


def _salary_value(value: Any) -> Decimal:
    """The stated salary as an exact decimal, refusing a float outright.

    The one conversion DOMAIN-MODELS.md §2 forbids for money: a float salary would land
    in the ledger's arithmetic as a binary approximation of what somebody is paid.
    """
    if isinstance(value, float):
        raise InvalidEmployeeDataError(
            f"a salary is an exact decimal or a string, not the float {value!r}"
        )
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, AttributeError, ValueError) as exc:
        raise InvalidEmployeeDataError(
            f"not a salary: {value!r}; give an exact decimal"
        ) from exc
    if not amount.is_finite() or amount < 0:
        raise InvalidEmployeeDataError(
            f"a salary is a finite amount that is not negative, not {amount}"
        )
    return amount


def _currency_or_base(session: Session, company_id: uuid.UUID, code: Any) -> str | None:
    """The stated currency, or a refusal — never a guessed one (T-1.ACCT.05's master)."""
    if code is None:
        return None
    return currency_by_code(session, company_id=company_id, code=str(code)).code


def create_employee(
    session: Session,
    *,
    company_id: uuid.UUID,
    party_code: str,
    number: str,
    hire_date: Any,
    subject: str,
    name: str | None = None,
    tax_id: str | None = None,
    date_of_birth: Any = None,
    personal_email: str | None = None,
    mobile_number: str | None = None,
    home_address: str | None = None,
) -> Employee:
    """Give a party the employee role and its employment details.

    The party is created if this company does not have it yet (with the employee role),
    or — when it exists — simply given the role, so a party that is already a customer
    or a supplier becomes an employee **without a second record**. Either way there is
    one profile per party, and a second attempt for the same party is refused.

    `subject` is who is writing. The personal fields are field-restricted
    (T-0.SEC.01), so stating them here is refused for a role that may not write them —
    the same guard the setter applies, at the moment the row is first written.
    """
    stated = {
        field: value
        for field, value in (
            ("date_of_birth", date_of_birth),
            ("personal_email", personal_email),
            ("mobile_number", mobile_number),
            ("home_address", home_address),
        )
        if value is not None
    }
    reject_restricted_fields(
        session,
        company_id=company_id,
        subject=subject,
        entity=EMPLOYEE_ENTITY,
        payload=stated,
    )
    hired = _date_or_refuse(hire_date, "a hire date")
    details = {field: _personal_value(field, value) for field, value in stated.items()}
    born = details.get("date_of_birth")
    if born is not None and born >= hired:
        raise InvalidEmployeeDataError(
            f"a date of birth ({born}) is not on or after the hire date ({hired})"
        )

    # The number is checked before anything is written, so a refusal leaves no half-made
    # party behind for a caller that catches it and commits anyway. Retired rows are
    # included, because the constraint is judged over every row: a number identifies one
    # employee for good, and an operator gets a named refusal rather than the database's
    # raw constraint error.
    stated_number = _required(number, "an employee number")
    taken = session.scalar(
        select(Employee)
        .where(Employee.company_id == company_id, Employee.number == stated_number)
        .execution_options(**{INCLUDE_SOFT_DELETED: True})
    )
    if taken is not None:
        raise DuplicateEmployeeError(
            f"employee number {stated_number!r} is already {taken.party.name!r}'s; a number"
            " identifies one employee for good (T-5.EMP.01)"
        )

    party = session.scalar(
        select(Party).where(Party.company_id == company_id, Party.code == str(party_code))
    )
    if party is None:
        party = Party(
            company_id=company_id,
            code=str(party_code),
            name=_required(name, "a new party's name"),
            tax_id=None if tax_id is None else str(tax_id).strip(),
        )
        party.roles = [PartyRole(role=EMPLOYEE_ROLE)]
        session.add(party)
        session.flush()
    elif not party.has_role(EMPLOYEE_ROLE):
        # One more role on the same identity — the whole point of §5's Party. A repeat is
        # not appended: the row already says the role has been held since it was granted.
        party.roles.append(PartyRole(role=EMPLOYEE_ROLE))
        session.flush()
    elif tax_id is not None and party.tax_id is None:
        party.tax_id = str(tax_id).strip()

    # One profile per party, retired profiles included: the same reasoning as the number
    # above, and the same named refusal instead of a raw constraint error.
    if session.scalar(
        select(Employee)
        .where(Employee.company_id == company_id, Employee.party_id == party.id)
        .execution_options(**{INCLUDE_SOFT_DELETED: True})
    ) is not None:
        raise DuplicateEmployeeError(
            f"party {party.code!r} already has an employee profile; one party, one profile"
            " — edit that one instead (T-5.EMP.01)"
        )

    employee = Employee(
        company_id=company_id,
        party_id=party.id,
        number=stated_number,
        hire_date=hired,
        **details,
    )
    session.add(employee)
    session.flush()
    return employee


def employee_by_number(session: Session, *, company_id: uuid.UUID, number: str) -> Employee:
    """The live employee a roster or a payslip names by its number, or a refusal."""
    employee = session.scalar(
        select(Employee).where(
            Employee.company_id == company_id, Employee.number == str(number).strip()
        )
    )
    if employee is None:
        raise UnknownEmployeeError(
            f"no employee {number!r} in this company; create it first (T-5.EMP.01)"
        )
    return employee


def employee_by_code(session: Session, *, company_id: uuid.UUID, code: str) -> Employee:
    """The live employee behind a party code, or a refusal naming what is missing."""
    party = party_by_code(session, company_id=company_id, code=code)
    employee = session.scalar(
        select(Employee).where(
            Employee.company_id == company_id, Employee.party_id == party.id
        )
    )
    if employee is None:
        raise NotAnEmployeeError(
            f"party {code!r} is not an employee in this company; give it the employee role"
            " first (T-5.EMP.01)"
        )
    return employee


def latest_contract(employee: Employee) -> EmploymentContract | None:
    """The most recent terms on file, or ``None`` when the employee has none.

    What a new contract supersedes, and the reference the sequence rule reads. Taken as
    the maximum start date rather than as "the last item of the relationship", so it is
    right even in the transaction that just added one: a collection loaded before the
    insert does not necessarily carry the new row in order until it is reloaded.
    """
    return max(employee.contracts, key=lambda contract: contract.effective_from, default=None)


def contract_in_force(employee: Employee, *, on: date) -> EmploymentContract | None:
    """The terms that applied on `on`, or ``None`` if the employee had none then.

    The window is half-open — `effective_from <= on < effective_to` — so the day a new
    contract starts belongs to the new one and to one only. The date is stated rather than
    read from the clock: a past period must answer the same today as it did then, which is
    why a payroll run passes the date it is paying for.
    """
    for contract in employee.contracts:
        ends = contract.effective_to
        if contract.effective_from <= on and (ends is None or on < ends):
            return contract
    return None


def record_contract(
    session: Session,
    employee: Employee,
    *,
    subject: str,
    effective_from: Any,
    basic_salary: Any,
    contract_type: str,
    transaction_currency: str | None = None,
) -> EmploymentContract:
    """Record the terms that take effect on `effective_from`, superseding what they replace.

    **Nothing already recorded is touched**: the new row names the contract it supersedes,
    and the superseded contract's window ends where these begin — derived from the history
    rather than written onto the old row, because the table refuses an UPDATE outright. A
    date that does not come after the latest terms already recorded is refused: two
    contracts claiming one day is how a salary change becomes unanswerable.

    `subject` is who is writing: `basic_salary` is field-restricted, so a role that may
    not write it is refused here rather than found out in a payroll run.
    """
    reject_restricted_fields(
        session,
        company_id=employee.company_id,
        subject=subject,
        entity=CONTRACT_ENTITY,
        payload={"basic_salary": basic_salary, "transaction_currency": transaction_currency},
        entity_id=employee.id,
    )
    starts = _date_or_refuse(effective_from, "a contract's start date")
    if starts < employee.hire_date:
        raise ContractSequenceError(
            f"terms cannot start before the employee was hired: {starts} is before"
            f" {employee.hire_date}; T-5.EMP.03 records a change of hire date as a movement"
        )
    superseded = latest_contract(employee)
    if superseded is not None and starts <= superseded.effective_from:
        raise ContractSequenceError(
            f"terms starting {starts} would not follow the ones already recorded"
            f" ({superseded.effective_from}…); new terms supersede the previous ones, they do"
            " not overlap them"
        )
    contract = EmploymentContract(
        company_id=employee.company_id,
        effective_from=starts,
        contract_type=_required(contract_type, "a contract type"),
        basic_salary=_salary_value(basic_salary),
        transaction_currency=_currency_or_base(
            session, employee.company_id, transaction_currency
        ),
        supersedes_id=None if superseded is None else superseded.id,
    )
    # Appended to the employee's history rather than added beside it, so the collection
    # this module reads is correct in the same transaction that wrote the row.
    employee.contracts.append(contract)
    session.flush()
    return contract


def assign_asset(
    session: Session,
    employee: Employee,
    *,
    description: str,
    assigned_on: Any,
    identifier: str | None = None,
) -> EmployeeAsset:
    """Record an asset handed to the employee, on the date it went out.

    The date is stated, never read from the clock: what a return is judged against has to
    be the date it actually left, and a check that runs on a different day must reach the
    same answer.
    """
    asset = EmployeeAsset(
        company_id=employee.company_id,
        description=_required(description, "what the asset is"),
        identifier=None if identifier is None else _required(identifier, "an asset identifier"),
        assigned_on=_date_or_refuse(assigned_on, "an assignment date"),
    )
    employee.assets.append(asset)
    session.flush()
    return asset


def return_asset(session: Session, asset: EmployeeAsset, *, returned_on: Any) -> EmployeeAsset:
    """Record that an assigned asset came back — once, and never before it went out."""
    when = _date_or_refuse(returned_on, "a return date")
    if asset.returned_on is not None:
        raise AssetReturnError(
            f"{asset.description!r} was already returned on {asset.returned_on}; an asset"
            " comes back once"
        )
    if when < asset.assigned_on:
        raise AssetReturnError(
            f"{asset.description!r} cannot be returned on {when}: it was assigned on"
            f" {asset.assigned_on}"
        )
    asset.returned_on = when
    session.flush()
    return asset


def assets_held(employee: Employee) -> list[EmployeeAsset]:
    """What the employee still holds — the list an offboarding has to empty."""
    return [asset for asset in employee.assets if asset.returned_on is None]


def set_personal_details(session: Session, employee: Employee, *, subject: str, **stated: Any) -> Employee:
    """Set the personal details the caller states, refusing any it may not write.

    Only the fields **named** are touched, so an absent field means "not stated" and a
    field set to ``None`` means "clear it" — the two are different answers and are
    stored as such. A field that is not one of :data:`PERSONAL_FIELDS` is refused rather
    than quietly set: this is not a general-purpose editor for the row.
    """
    unknown = sorted(field for field in stated if field not in PERSONAL_FIELDS)
    if unknown:
        raise InvalidEmployeeDataError(
            f"not a personal detail: {', '.join(unknown)}; an employee's own fields are"
            f" {', '.join(PERSONAL_FIELDS)}"
        )
    if not stated:
        return employee
    reject_restricted_fields(
        session,
        company_id=employee.company_id,
        subject=subject,
        entity=EMPLOYEE_ENTITY,
        payload=stated,
        entity_id=employee.id,
    )
    for field, value in stated.items():
        setattr(employee, field, _personal_value(field, value))
    session.flush()
    return employee


def _visible(payload: dict, hidden: set[str]) -> dict:
    """`payload` without the fields this subject may not read — absent, not nulled.

    The same one-line filter `readable_fields` applies, for a caller that has already read
    the restrictions once and is applying them to many rows (T-0.SEC.01 states both).
    """
    return {field: value for field, value in payload.items() if field not in hidden}


def _contract_payload(contract: EmploymentContract) -> dict:
    return {
        "id": str(contract.id),
        "effective_from": contract.effective_from.isoformat(),
        "effective_to": None if contract.effective_to is None else contract.effective_to.isoformat(),
        "contract_type": contract.contract_type,
        "basic_salary": format(contract.basic_salary, "f"),
        "transaction_currency": contract.transaction_currency,
        "supersedes_id": None if contract.supersedes_id is None else str(contract.supersedes_id),
    }


def _asset_payload(asset: EmployeeAsset) -> dict:
    return {
        "id": str(asset.id),
        "description": asset.description,
        "identifier": asset.identifier,
        "assigned_on": asset.assigned_on.isoformat(),
        "returned_on": None if asset.returned_on is None else asset.returned_on.isoformat(),
    }


def employee_payload(
    session: Session, *, company_id: uuid.UUID, subject: str, employee: Employee
) -> dict:
    """The employee as *this* caller may read it — a restricted field is **absent**.

    The personal details and each contract's salary are the employee's own business, so
    which of them leaves this function is decided per request by T-0.SEC.01's field-level
    restrictions rather than by the shape of the row. A field a role may not read is
    removed from the payload, never blanked, so "not allowed to see it" cannot be read as
    "not filled in".

    The restrictions are read **once per entity** and reused for every contract and every
    asset the employee carries: they depend only on the subject and the entity, so asking
    per row would put an identical query behind each one and make the payload's cost grow
    with how long somebody has worked here.
    """
    hidden_employee = hidden_fields(
        session, company_id=company_id, subject=subject, entity=EMPLOYEE_ENTITY
    )
    hidden_contract = hidden_fields(
        session, company_id=company_id, subject=subject, entity=CONTRACT_ENTITY
    )
    hidden_asset = hidden_fields(
        session, company_id=company_id, subject=subject, entity=ASSET_ENTITY
    )
    return _visible(
        {
            "id": str(employee.id),
            "number": employee.number,
            "party_code": employee.party.code if employee.party is not None else None,
            "name": employee.party.name if employee.party is not None else None,
            "hire_date": employee.hire_date.isoformat(),
            "date_of_birth": None
            if employee.date_of_birth is None
            else employee.date_of_birth.isoformat(),
            "personal_email": employee.personal_email,
            "mobile_number": employee.mobile_number,
            "home_address": employee.home_address,
            "contracts": [
                _visible(_contract_payload(contract), hidden_contract)
                for contract in employee.contracts
            ],
            "assets": [
                _visible(_asset_payload(asset), hidden_asset) for asset in employee.assets
            ],
        },
        hidden_employee,
    )
