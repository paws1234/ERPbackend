"""T-5.PAY.06 — the payment file a payroll run is paid with, in the pack's format.

The bank decides what a payment file looks like: the columns, their order, what separates them
and what is required. All of that is the localization pack's `bank_file_format` (T-0.LOC.01),
so nothing here knows a market's layout — it reads the file's own specification and fills it
from the run.

Four rules the module is built around:

* **The file's money is the run's money.** Every row's amount is the line's own net pay, and
  the file states the figures that must add up: what it pays, what it could not pay, **the cents
  a bank cannot pay** (an obligation of `3766.666667` is paid as `3766.67`, and the third of a
  cent is stated rather than lost) and the run's net total —
  `file_total + excluded_total − rounding_total == run_net_total`. A payment file that pays less
  than the payroll, without saying by how much and for whom, is how an employee's salary goes
  missing quietly.
* **Nobody is left out silently.** An employee with no bank details in force, or with an amount
  that is not positive, is **excluded and listed** with the reason and the amount, and the file
  carries the list.
* **The format is validated, not hoped for.** A row is checked against the pack's own column
  list before it is written: a required column that is empty, or a column the format does not
  state, refuses the row rather than producing a file the bank rejects or, worse, accepts with a
  field in the wrong place.
* **The same run produces the same file, byte for byte.** The rows come from the run's stored
  lines and the format from the pack: no clock, no iteration order that is not stated, no
  numbers from anywhere else. An amount is written with two decimals and no separator, as the
  pack's own note requires.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from sqlalchemy import CheckConstraint, Date, ForeignKey, String, UniqueConstraint, Uuid, select
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.audit import append_only
from app.db import Base
from app.hr.employees import Employee
from app.localization import bank_file_format
from app.payroll.engine import PayrollRun, lines_of

# The states a file is produced from: a run that has not been computed has no net pay to pay.
PAYABLE_STATES = ("computed", "approved")


class BankFileError(ValueError):
    """The bank file subsystem refused what was asked of it."""


class InvalidBankDetailsError(BankFileError):
    """An account number, a bank code or a holder name failed validation at entry."""


class RunNotPayableError(BankFileError):
    """The run has not been computed, so there is nothing to pay."""


class UnpayableRowError(BankFileError):
    """A row does not match the format the pack states."""


class EmployeeBankAccount(Base):
    """Where an employee's pay is sent, as it stood from a date.

    Dated and appended like every other history in this phase: when somebody changes bank, the
    new details take effect from their own date and a payslip's or a file's period reads the
    details that were in force then.
    """

    __tablename__ = "employee_bank_account"
    __table_args__ = (
        UniqueConstraint(
            "employee_id", "effective_from", name="uq_employee_bank_account_effective"
        ),
        CheckConstraint("char_length(bank_code) > 0", name="ck_employee_bank_code"),
        CheckConstraint("char_length(holder_name) > 0", name="ck_employee_bank_holder"),
        # The pack's own note: "account number, digits only".
        CheckConstraint("account_number ~ '^[0-9]+$'", name="ck_employee_bank_account_digits"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id"), nullable=False, index=True
    )
    bank_code: Mapped[str] = mapped_column(String(32), nullable=False)
    account_number: Mapped[str] = mapped_column(String(32), nullable=False)
    holder_name: Mapped[str] = mapped_column(String(160), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    stated_by: Mapped[str] = mapped_column(String(64), nullable=False)

    employee: Mapped[Employee] = relationship()


# Bank details are history: appended, never rewritten — see the class docstring.
append_only(EmployeeBankAccount.__table__)


def _text(value: Any, what: str) -> str:
    """The text as written, or a refusal."""
    written = "" if value is None else str(value).strip()
    if not written:
        raise InvalidBankDetailsError(f"{what} is required")
    return written


def _date_or_refuse(value: Any, what: str) -> date:
    """A `date`, or a refusal."""
    if isinstance(value, date) and not hasattr(value, "hour"):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise InvalidBankDetailsError(f"not {what}: {value!r} (a date is `YYYY-MM-DD`)") from exc


def define_bank_details(
    session: Session,
    employee: Employee,
    *,
    bank_code: str,
    account_number: str,
    holder_name: str,
    effective_from: Any,
    actor: str,
) -> EmployeeBankAccount:
    """State where an employee's pay is sent, from a date.

    The account number is **digits only**, which is the pack's own requirement for the file's
    `payee_account` column: an account number with a dash in it is refused here rather than
    written into a payment file the bank will bounce.
    """
    starts = _date_or_refuse(effective_from, "the effective date")
    number = _text(account_number, "an account number")
    if not number.isdigit():
        raise InvalidBankDetailsError(
            f"the account number {number!r} is digits only, as the pack's bank file format"
            " requires for its payee account column"
        )
    existing = session.scalar(
        select(EmployeeBankAccount).where(
            EmployeeBankAccount.employee_id == employee.id,
            EmployeeBankAccount.effective_from == starts,
        )
    )
    if existing is not None:
        raise InvalidBankDetailsError(
            f"{employee.number!r} already has bank details from {starts}; state the change from"
            " a later date instead of restating the same day"
        )
    account = EmployeeBankAccount(
        company_id=employee.company_id,
        employee_id=employee.id,
        bank_code=_text(bank_code, "a bank code"),
        account_number=number,
        holder_name=_text(holder_name, "the account holder's name"),
        effective_from=starts,
        stated_by=_text(actor, "who stated the details"),
    )
    session.add(account)
    session.flush()
    return account


def bank_details_in_force(
    session: Session, employee: Employee, *, on: date
) -> EmployeeBankAccount | None:
    """The employee's bank details in force on a date, or ``None`` when there are none."""
    return session.scalar(
        select(EmployeeBankAccount)
        .where(
            EmployeeBankAccount.employee_id == employee.id,
            EmployeeBankAccount.effective_from <= on,
        )
        .order_by(EmployeeBankAccount.effective_from.desc())
    )


def _rounded(value: Decimal) -> Decimal:
    """The amount a bank can pay: to the cent, because the pack's format says two decimals."""
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _amount_text(value: Decimal) -> str:
    """Two decimals, no thousands separator, no currency symbol — the pack's own note."""
    return f"{_rounded(value):.2f}"


def validate_row(row: dict, columns: list[dict]) -> None:
    """Refuse a row that does not match the format's own column list.

    Every required column must be present and not empty, and no column may be stated that the
    format does not have: a file the bank reads by position cannot survive a field in the wrong
    place, and a missing required field is a payment that arrives wrong or not at all.
    """
    known = [column["name"] for column in columns]
    unknown = [name for name in row if name not in known]
    if unknown:
        raise UnpayableRowError(
            f"the row states {', '.join(sorted(unknown))}, which the format does not have"
            f" ({', '.join(known)})"
        )
    for column in columns:
        if column.get("required") and not str(row.get(column["name"], "")).strip():
            raise UnpayableRowError(
                f"the row states nothing for {column['name']!r}, which the format requires"
            )


def _row_for(session: Session, line, *, columns: list[dict], on: date, reference: str) -> dict:
    """One employee's row, in the format's own columns."""
    details = bank_details_in_force(session, line.employee, on=on)
    row = {
        "payee_name": None if details is None else details.holder_name,
        "payee_account": None if details is None else details.account_number,
        "bank_code": None if details is None else details.bank_code,
        "amount": _amount_text(line.net),
        "reference": reference,
        "purpose": f"Payroll {line.run.period}",
    }
    return {name: row.get(name) for name in [column["name"] for column in columns]}


def bank_file(session: Session, run: PayrollRun, *, market: str, on: date | None = None) -> dict:
    """The payment file for a run: the rows, the ones that could not be paid, and the totals.

    The date the details are read at is the run's own period end unless the caller states one,
    so a file regenerated later pays the same people to the same accounts as the payroll it
    belongs to. A run that has been computed but **not yet approved** can be paid from — the
    file states the run's own state, so a file made from an unapproved run says so on its face
    rather than looking like any other.
    """
    if run.state not in PAYABLE_STATES:
        raise RunNotPayableError(
            f"{run.period} is {run.state}; a payment file is produced from a run that has been"
            " computed (T-5.PAY.02)"
        )
    format_ = bank_file_format(market)
    columns = list(format_["columns"])
    when = run.to_date if on is None else _date_or_refuse(on, "the date the file is read at")
    reference = f"PAYROLL {run.period}"
    rows: list[dict] = []
    paid: list[dict] = []
    excluded: list[dict] = []
    rounding = Decimal(0)
    run_lines = lines_of(session, run)
    for line in run_lines:
        if line.net <= 0:
            excluded.append(
                {
                    "employee": line.employee.number,
                    "name": line.employee.party.name,
                    "net": str(line.net),
                    "reason": "nothing is payable: the net pay for the period is not positive",
                }
            )
            continue
        row = _row_for(session, line, columns=columns, on=when, reference=reference)
        missing = [name for name, value in row.items() if not str(value or "").strip()]
        if any(column.get("required") and column["name"] in missing for column in columns):
            excluded.append(
                {
                    "employee": line.employee.number,
                    "name": line.employee.party.name,
                    "net": str(line.net),
                    "reason": (
                        f"not payable: the format requires {', '.join(missing)}, and no bank"
                        f" details in force on {when.isoformat()} state it (T-5.PAY.06:"
                        " define_bank_details)"
                    ),
                }
            )
            continue
        validate_row(row, columns)
        rows.append(row)
        # What the bank cannot pay: the line's obligation is exact, the payment is to the cent,
        # and the difference is stated rather than rounded away.
        carried = _rounded(line.net) - line.net
        rounding += carried
        paid.append(
            {
                "employee": line.employee.number,
                "net": str(line.net),
                "paid": _amount_text(line.net),
                "rounding": str(carried),
            }
        )
    total = sum((Decimal(row["amount"]) for row in rows), Decimal(0))
    return {
        "market": market,
        "format": format_["name"],
        "delimiter": format_["delimiter"],
        "encoding": format_["encoding"],
        "columns": [column["name"] for column in columns],
        "notes": list(format_.get("notes", [])),
        "read_at": when.isoformat(),
        "period": run.period,
        "run_revision": run.revision,
        "run_state": run.state,
        "reference": reference,
        "rows": rows,
        "paid": paid,
        "excluded": excluded,
        "total": str(total),
        "excluded_total": str(
            sum((Decimal(row["net"]) for row in excluded), Decimal(0))
        ),
        "rounding_total": str(rounding),
        "run_net_total": str(sum((line.net for line in run_lines), Decimal(0))),
    }


def render(file_payload: dict, *, header: bool = True) -> str:
    """The file itself: the format's own columns, in its own order, separated as it states.

    Whether the file carries a header line is the bank's business (the pack's own note says the
    layout is confirmed with the bank), so the caller states it; the rows are the same either
    way, and so is the total.
    """
    delimiter = file_payload["delimiter"]
    lines: list[str] = []
    if header:
        lines.append(delimiter.join(file_payload["columns"]))
    for row in file_payload["rows"]:
        lines.append(
            delimiter.join(
                "" if row[name] is None else str(row[name]) for name in file_payload["columns"]
            )
        )
    return "\n".join(lines) + "\n"


def reconciles(file_payload: dict) -> Decimal:
    """What the file pays, what it could not pay and the cents it cannot, against the run's net.

    Zero means the payment file accounts for the whole payroll: nobody's pay is missing from the
    file without being on the list of those who were not paid, and nothing is lost to rounding
    without being counted.
    """
    return (
        Decimal(file_payload["total"])
        + Decimal(file_payload["excluded_total"])
        - Decimal(file_payload["rounding_total"])
        - Decimal(file_payload["run_net_total"])
    )
