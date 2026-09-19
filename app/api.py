"""T-0.API.01 — the conventions every endpoint follows, and the contract they publish.

Every phase adds endpoints; these are the rules they are all held to, so the
frontend can be written once against a shape that does not move:

* **Versioned URLs.** Everything lives under ``/api/v1/...``. The version is
  :data:`API_VERSION`, and the published contract carries it in its path
  (``contract/<version>/openapi.json``) — a breaking change is a *new* version
  with a new directory, never an edit of the file the frontend already pinned.
* **One error shape.** Every refusal — validation, a domain rule, a missing
  route, an unexpected failure — answers
  ``{"error": {"code": ..., "message": ..., "details": [...]}}``. A client needs
  one branch, not one per endpoint.
* **Pagination.** A list answers ``{"items": [...], "limit":, "offset":, "total":}``
  with ``limit``/``offset`` query parameters, so a caller can page without a
  bespoke contract per collection.
* **Money is an exact decimal string.** Amounts are ``str`` in both directions —
  the rule DOMAIN-MODELS.md §2 states for the whole platform. A float never
  crosses this boundary.
* **Identity on every request.** ``X-Company-Id`` binds the request to one
  company (``scope_to_company``, so the database's row-level security does the
  isolating) and ``X-Actor`` names the actor the audit trail records
  (``set_actor``). T-0.SEC.01 replaces both with the authenticated claims and
  enforces permissions; the conventions above do not change when it does.
* **Posting endpoints take an ``Idempotency-Key``.** A retry with the same key
  and body answers with the first result instead of posting a second time; the
  same key with a different body is refused. The key and its answer are stored
  in the caller's transaction, so a failed document stores neither.

**The contract is generated, never written by hand.** It is served at
``/api/v1/openapi.json`` and published by ``tools/publish_contract.py`` into
``contract/v1/openapi.json`` in this repository, which the frontend repository
pulls at a pinned ref — no source, no sibling checkout. The check that guards it
compares the two: edit an endpoint without republishing and it fails, so the
artifact the frontend builds against cannot drift from the code.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import (
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
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.audit import set_actor
from app.company import Company, UnknownCompanyError, company_base_currency
from app.db import Base, scope_to_company
from app.ledger.accounts import (
    UnknownAccountError,
    account_by_code,
    create_account as create_account_row,
    import_coa_template,
    tree as account_tree,
)
from app.ledger.currency import (
    Currency,
    CurrencyError,
    UnknownCurrencyError,
    UnknownRateError,
    currency_by_code,
    rate_for,
    register_currency,
    store_rate,
)
from app.ledger.mapping import (
    MissingMappingError,
    mapped_account,
    mappings,
    set_mapping,
)
from app.ledger.posting import (
    IncompleteSourceError,
    JournalEntry,
    UnbalancedEntryError,
    post_journal_entry,
)
from app.party import UnknownPartyError
from app.reporting import (
    DEFAULT_CAPABILITY as DEFAULT_REPORT_CAPABILITY,
    ReportDefinition,
    register as register_report,
    run as run_report,
)
from app.security import (
    AccessDenied,
    readable_fields,
    reject_restricted_fields,
    require,
)

# The published version. A breaking change means a new value here, a new
# `contract/<version>/` directory and the old one left exactly as it was.
API_VERSION = "v1"
BASE = f"/api/{API_VERSION}"

# Pagination bounds: the default page and the largest one a caller may ask for.
DEFAULT_LIMIT = 25
MAX_LIMIT = 200


class ApiError(Exception):
    """A refusal stated in the platform's one error shape."""

    def __init__(self, status_code: int, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def _error(status_code: int, code: str, message: str, details: Any = None) -> JSONResponse:
    """The one error shape, as a response."""
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "details": details}},
    )


# --- Identity, stated on the request -----------------------------------------
# SEC.01 owns authentication and permissions; until it exists the request states
# who it is and which company it is for, and both are recorded the same way the
# authenticated claims will be.
# ponytail: headers, not a token. Ceiling: any caller may claim any actor.
# Upgrade path: T-0.SEC.01 verifies the claims and calls these same setters.


class RequestContext(BaseModel):
    """What one request carries: its company, its actor and its session."""

    model_config = {"arbitrary_types_allowed": True}

    company_id: uuid.UUID
    actor: str
    session: Session


def context(
    x_company_id: Annotated[str, Header()],
    x_actor: Annotated[str, Header()] = "unknown",
) -> Iterator[RequestContext]:
    """Bind a session to the requesting company and actor, then close it."""
    try:
        company_id = uuid.UUID(x_company_id)
    except ValueError as exc:
        raise ApiError(
            400, "invalid_company", f"X-Company-Id is not a company id: {x_company_id!r}"
        ) from exc

    with Session(_engine()) as session:
        scope_to_company(session, company_id)
        set_actor(session, x_actor)
        yield RequestContext(company_id=company_id, actor=x_actor, session=session)


Context = Annotated[RequestContext, Depends(context)]


# --- The contract's payloads -------------------------------------------------
# Amounts are strings on the way in and on the way out (DOMAIN-MODELS.md §2),
# never floats.


class JournalLineIn(BaseModel):
    account: str
    debit: str | None = None
    credit: str | None = None
    party: str | None = None


class JournalEntryIn(BaseModel):
    posting_date: date
    currency: str
    memo: str | None = None
    # The document that produced the entry (T-1.ACCT.02), as its type and id — the
    # pair the drill-down reads. Omitted for a manual entry.
    source_type: str | None = None
    source_id: str | None = None
    lines: list[JournalLineIn]


class JournalLineOut(BaseModel):
    line_no: int
    account: str
    debit: str
    credit: str
    # The same amounts restated in the company's base currency (T-1.ACCT.05):
    # `amount × exchange_rate`, exact, so a reader never has to apply the rate
    # themselves or wonder which rate applied.
    base_debit: str
    base_credit: str
    party: str | None = None


class JournalEntryOut(BaseModel):
    id: str
    company_id: str
    posting_date: date
    currency: str
    exchange_rate: str
    memo: str | None = None
    source_type: str | None = None
    source_id: str | None = None
    lines: list[JournalLineOut]


class PageOut(BaseModel):
    items: list[JournalEntryOut]
    limit: int
    offset: int
    total: int


class CompanyOut(BaseModel):
    id: str
    code: str
    name: str
    base_currency: str
    fiscal_year_start_month: int


# --- T-1.ACCT.01 — the chart of accounts --------------------------------


class AccountIn(BaseModel):
    code: str
    name: str
    account_class: str
    parent_code: str | None = None


class AccountOut(BaseModel):
    id: str
    code: str
    name: str
    account_class: str
    parent_id: str | None = None


class AccountTreeNode(AccountOut):
    # Always present, empty for a leaf: an optional field must also be nullable
    # (the convention the contract check enforces), and "no children" is not null.
    children: list[AccountTreeNode]


class CoaImportIn(BaseModel):
    market: str


class CoaImportOut(BaseModel):
    market: str
    imported: int
    accounts: list[AccountOut]


# --- T-1.ACCT.03 — the account mapping a posting module books to -----------


class AccountMappingIn(BaseModel):
    account_code: str


class AccountMappingOut(BaseModel):
    key: str
    account_code: str
    account_id: str


# --- T-1.ACCT.05 — currencies and their dated rates -------------------------


class CurrencyIn(BaseModel):
    code: str
    name: str


class CurrencyOut(BaseModel):
    code: str
    name: str


class FxRateIn(BaseModel):
    currency: str
    on: date
    rate: str


class FxRateOut(BaseModel):
    base_currency: str
    currency: str
    rate_date: date
    rate: str
    source: str


# --- T-1.ACCT.07 — the statements, run through the reporting framework ------


class ReportIn(BaseModel):
    code: str
    name: str
    schedule: str
    recipients: list[str]
    # Absent means the framework's default (`report.read`) — an optional field
    # must also be nullable, so it is stated as such rather than defaulted here.
    capability: str | None = None


class ReportOut(BaseModel):
    code: str
    name: str
    schedule: str
    capability: str
    recipients: list[str]


class ReportRunOut(BaseModel):
    code: str
    status: str
    produced: dict[str, Any] | None = None
    error: str | None = None
    delivered_to: list[str] | None = None


class HealthOut(BaseModel):
    status: str
    version: str


class ErrorDetail(BaseModel):
    """The one thing every refusal says: a machine code, a message, the detail."""

    code: str
    message: str
    details: list[dict[str, Any]] | None = None


class ErrorOut(BaseModel):
    """The one error shape — documented for every endpoint, because every one
    answers with it."""

    error: ErrorDetail


# Documented on the app rather than left to the generator: FastAPI's default
# 422 shape is not what this app returns, so the contract states the real one
# (see the handlers below).
ERROR_RESPONSES = {
    code: {"model": ErrorOut, "description": description}
    for code, description in (
        (400, "The request could not be understood"),
        (401, "The caller is not identified"),
        (403, "The caller may not do this"),
        (404, "The company, document or route does not exist"),
        (409, "The request clashes with one already made"),
        (422, "The request did not match the contract, or a domain rule refused it"),
        (500, "The platform failed unexpectedly"),
    )
}


# --- Posting, with the key that makes a retry safe ---------------------------


class ApiIdempotencyKey(Base):
    """The answer a posted request already gave, so a retry can repeat it.

    Scoped to the company and unique on the key, so two companies cannot see each
    other's keys and one key cannot answer twice with different results. Written
    in the caller's transaction: a document that fails stores no key, and a
    retried document finds the key it wrote.
    """

    __tablename__ = "api_idempotency_key"
    __table_args__ = (
        UniqueConstraint("company_id", "key", name="uq_api_idempotency_company_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(String(200), nullable=False)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    path: Mapped[str] = mapped_column(String(128), nullable=False)
    # The request's body, hashed: the same key with a different body is a client
    # bug, not a retry, and is refused rather than answered.
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


def _fingerprint(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _amount(value: str | None) -> Decimal:
    """An exact decimal from the string form; anything else is refused."""
    if value is None:
        return Decimal(0)
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ApiError(422, "invalid_amount", f"not a decimal amount: {value!r}") from exc


def _money(value: Decimal) -> str:
    return format(value, "f")


def _base_amount(value: Decimal, exchange_rate: Decimal) -> str:
    """An amount in the company's base currency: the exact product, at money scale."""
    return _money((value * exchange_rate).quantize(Decimal("0.000001")))


def _document_id(value: str | None) -> uuid.UUID | None:
    """A document id from the wire, or a refusal naming what arrived instead."""
    if value is None:
        return None
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise ApiError(422, "invalid_source_id", f"not a document id: {value!r}") from exc


def _rate_out(stored) -> FxRateOut:
    return FxRateOut(
        base_currency=stored.base_currency,
        currency=stored.currency,
        rate_date=stored.rate_date,
        rate=format(stored.rate, "f"),
        source=stored.source,
    )


def _account_out(account) -> AccountOut:
    return AccountOut(
        id=str(account.id),
        code=account.code,
        name=account.name,
        account_class=account.account_class,
        parent_id=str(account.parent_id) if account.parent_id else None,
    )


def _entry_out(entry: JournalEntry) -> JournalEntryOut:
    return JournalEntryOut(
        id=str(entry.id),
        company_id=str(entry.company_id),
        posting_date=entry.posting_date,
        currency=entry.currency,
        memo=entry.memo,
        source_type=entry.source_type,
        source_id=None if entry.source_id is None else str(entry.source_id),
        exchange_rate=format(entry.exchange_rate, "f"),
        lines=[
            JournalLineOut(
                line_no=line.line_no,
                account=line.account,
                debit=_money(line.debit),
                credit=_money(line.credit),
                base_debit=_base_amount(line.debit, entry.exchange_rate),
                base_credit=_base_amount(line.credit, entry.exchange_rate),
                party=line.party,
            )
            for line in entry.lines
        ],
    )


def _visible_entry(session: Session, context: RequestContext, payload: dict) -> dict:
    """An entry as *this* caller may read it: a restricted line field is absent.

    The body itself is whole; what a caller sees of it is decided per request
    (T-0.SEC.01), so a replayed answer is filtered like a fresh one.
    """
    return {
        **payload,
        "lines": [
            readable_fields(
                session,
                company_id=context.company_id,
                subject=context.actor,
                entity="journal_line",
                payload=line,
            )
            for line in payload["lines"]
        ],
    }


_ENGINE = None


def _engine():
    """The application's engine, from the environment like every other setting."""
    from sqlalchemy import create_engine

    global _ENGINE
    if _ENGINE is None:
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise ApiError(500, "not_configured", "DATABASE_URL is not set")
        _ENGINE = create_engine(url, pool_pre_ping=True)
    return _ENGINE


app = FastAPI(
    title="ERP API",
    version="1.0.0",
    summary="Modular ERP platform — core financial, inventory and operations API.",
    openapi_url=f"{BASE}/openapi.json",
    responses=ERROR_RESPONSES,
)


@app.exception_handler(ApiError)
async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
    return _error(exc.status_code, exc.code, exc.message, exc.details)


@app.exception_handler(RequestValidationError)
async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    return _error(422, "invalid_request", "the request did not match the contract", exc.errors())


# A missing route or a disallowed method is a refusal like any other and answers
# in the same shape, so a client never meets two error formats.
_HTTP_CODES = {
    400: "bad_request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
}


@app.exception_handler(StarletteHTTPException)
async def _http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = _HTTP_CODES.get(exc.status_code, "http_error")
    return _error(exc.status_code, code, str(exc.detail))


@app.exception_handler(AccessDenied)
async def _access_denied(_request: Request, exc: AccessDenied) -> JSONResponse:
    # T-0.SEC.01 refused it at the boundary; the refusal is already on the trail.
    return _error(403, exc.code, str(exc))


# A link that names something the company does not have is a client error, not a
# failure of the platform: T-1.ACCT.01 turned the posting line's account into a
# reference and T-0.PARTY.01 did the same for the party link.
@app.exception_handler(UnknownAccountError)
async def _unknown_account(_request: Request, exc: UnknownAccountError) -> JSONResponse:
    return _error(422, "unknown_account", str(exc))


@app.exception_handler(UnknownPartyError)
async def _unknown_party(_request: Request, exc: UnknownPartyError) -> JSONResponse:
    return _error(422, "unknown_party", str(exc))


@app.exception_handler(MissingMappingError)
async def _missing_mapping(_request: Request, exc: MissingMappingError) -> JSONResponse:
    return _error(422, "unmapped_account", str(exc))


@app.exception_handler(UnknownCompanyError)
async def _unknown_company(_request: Request, exc: UnknownCompanyError) -> JSONResponse:
    return _error(422, "unknown_company", str(exc))


@app.exception_handler(UnknownCurrencyError)
async def _unknown_currency(_request: Request, exc: UnknownCurrencyError) -> JSONResponse:
    return _error(422, "unknown_currency", str(exc))


# The catch-all for the currency service's refusals: an unknown rate, an attempt
# to rewrite a past date, a missing provider. All client errors, all one shape.
@app.exception_handler(CurrencyError)
async def _currency_error(_request: Request, exc: CurrencyError) -> JSONResponse:
    return _error(422, "currency_error", str(exc))


@app.exception_handler(IncompleteSourceError)
async def _incomplete_source(_request: Request, exc: IncompleteSourceError) -> JSONResponse:
    return _error(422, "incomplete_source", str(exc))


@app.exception_handler(Exception)
async def _unexpected(_request: Request, exc: Exception) -> JSONResponse:
    # Registered so even a bug answers in the one shape; a client's error
    # handling does not have to know which failure it met.
    return _error(500, "internal_error", f"{type(exc).__name__}: {exc}")


@app.get(f"{BASE}/health", response_model=HealthOut, tags=["platform"])
def health() -> HealthOut:
    """Liveness, and the contract version this instance publishes."""
    return HealthOut(status="ok", version=API_VERSION)


@app.get(f"{BASE}/companies/current", response_model=CompanyOut, tags=["platform"])
def current_company(context: Context) -> CompanyOut:
    """The company the request is bound to — what the frontend shell reads first."""
    require(
        context.session,
        company_id=context.company_id,
        subject=context.actor,
        capability="company.read",
        entity="company",
        entity_id=context.company_id,
    )
    company = context.session.get(Company, context.company_id)
    if company is None:
        raise ApiError(404, "company_not_found", f"no company {context.company_id}")
    return CompanyOut(
        id=str(company.id),
        code=company.code,
        name=company.name,
        base_currency=company.base_currency,
        fiscal_year_start_month=company.fiscal_year_start_month,
    )


@app.post(f"{BASE}/reports", response_model=ReportOut, status_code=201, tags=["reporting"])
def register_report_definition(payload: ReportIn, context: Context) -> ReportOut:
    """Register a report: its schedule and its recipients are rows (T-0.REPORT.01)."""
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="report.configure",
        entity="report_definition",
    )
    definition = register_report(
        session,
        company_id=context.company_id,
        code=payload.code,
        name=payload.name,
        schedule=payload.schedule,
        recipients=list(payload.recipients),
        capability=payload.capability or DEFAULT_REPORT_CAPABILITY,
    )
    session.commit()
    return ReportOut(
        code=definition.code,
        name=definition.name,
        schedule=definition.schedule,
        capability=definition.capability,
        recipients=list(definition.recipients),
    )


@app.post(f"{BASE}/reports/{{code}}/run", response_model=ReportRunOut, tags=["reporting"])
def run_report_now(code: str, context: Context) -> ReportRunOut:
    """Run one registered report for this company, and deliver it to its recipients.

    The capability the definition carries is asked for by the framework itself
    (T-0.REPORT.01), so a caller who may not see the report is refused before
    anything is built — and a builder that fails leaves a `failed` run, not a
    silent absence.
    """
    session = context.session
    definition = session.scalar(
        select(ReportDefinition).where(
            ReportDefinition.company_id == context.company_id, ReportDefinition.code == code
        )
    )
    if definition is None:
        raise ApiError(
            404, "report_not_configured", f"no report {code!r} is registered for this company"
        )
    run_row = run_report(session, definition, actor=context.actor)
    session.commit()
    return ReportRunOut(
        code=code,
        status=run_row.status,
        produced=run_row.produced,
        error=run_row.error,
        delivered_to=list(run_row.delivered_to) if run_row.delivered_to else None,
    )


@app.post(f"{BASE}/currencies", response_model=CurrencyOut, status_code=201, tags=["currency"])
def new_currency(payload: CurrencyIn, context: Context) -> CurrencyOut:
    """Register a currency in the master (T-1.ACCT.05) — global, three letters."""
    require(
        context.session,
        company_id=context.company_id,
        subject=context.actor,
        capability="currency.write",
        entity="currency",
    )
    currency = register_currency(
        context.session, company_id=context.company_id, code=payload.code, name=payload.name
    )
    context.session.commit()
    return CurrencyOut(code=currency.code, name=currency.name)


@app.get(f"{BASE}/currencies", response_model=list[CurrencyOut], tags=["currency"])
def list_currencies(context: Context) -> list[CurrencyOut]:
    """The currencies this installation knows about."""
    require(
        context.session,
        company_id=context.company_id,
        subject=context.actor,
        capability="currency.read",
        entity="currency",
    )
    return [
        CurrencyOut(code=currency.code, name=currency.name)
        for currency in context.session.scalars(
            select(Currency)
            .where(Currency.company_id == context.company_id)
            .order_by(Currency.code)
        )
    ]


@app.put(f"{BASE}/fx-rates", response_model=FxRateOut, tags=["currency"])
def put_fx_rate(payload: FxRateIn, context: Context) -> FxRateOut:
    """Store one dated rate against the company's base currency.

    A past date's rate cannot be rewritten (T-1.ACCT.05): the refusal is
    `currency_error` with the reason, not a silent overwrite.
    """
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="fx.write",
        entity="fx_rate",
    )
    stored = store_rate(
        session,
        company_id=context.company_id,
        base_currency=company_base_currency(session, company_id=context.company_id),
        currency=payload.currency,
        on=payload.on,
        rate=_amount(payload.rate),
    )
    session.commit()
    return _rate_out(stored)


@app.get(f"{BASE}/fx-rates", response_model=FxRateOut, tags=["currency"])
def get_fx_rate(
    context: Context,
    currency: Annotated[str, Query()],
    on: Annotated[date, Query()],
) -> FxRateOut:
    """The rate for one currency on one date — what a posting would use."""
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="fx.read",
        entity="fx_rate",
    )
    base = company_base_currency(session, company_id=context.company_id)
    wanted = str(currency).strip().upper()
    return FxRateOut(
        base_currency=base,
        currency=wanted,
        rate_date=on,
        rate=format(
            rate_for(session, company_id=context.company_id, base_currency=base, currency=wanted, on=on),
            "f",
        ),
        source="stored",
    )


@app.post(f"{BASE}/accounts", response_model=AccountOut, status_code=201, tags=["accounting"])
def new_account(payload: AccountIn, context: Context) -> AccountOut:
    """Create one account in the company's chart of accounts."""
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="account.write",
        entity="account",
    )
    reject_restricted_fields(
        session,
        company_id=context.company_id,
        subject=context.actor,
        entity="account",
        payload=payload.model_dump(),
    )
    parent = (
        account_by_code(session, company_id=context.company_id, code=payload.parent_code)
        if payload.parent_code is not None
        else None
    )
    account = create_account_row(
        session,
        company_id=context.company_id,
        code=payload.code,
        name=payload.name,
        account_class=payload.account_class,
        parent_id=parent.id if parent is not None else None,
    )
    session.commit()
    return _account_out(account)


@app.get(f"{BASE}/accounts/tree", response_model=list[AccountTreeNode], tags=["accounting"])
def accounts_tree(context: Context) -> JSONResponse:
    """The company's whole chart, nested, parents before their children."""
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="account.read",
        entity="account",
    )
    return JSONResponse(status_code=200, content=account_tree(session, company_id=context.company_id))


@app.post(
    f"{BASE}/accounts/import-coa",
    response_model=CoaImportOut,
    status_code=201,
    tags=["accounting"],
)
def import_coa(payload: CoaImportIn, context: Context) -> CoaImportOut:
    """Seed the chart from a market's localization pack template (T-0.LOC.01)."""
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="account.write",
        entity="account",
    )
    created = import_coa_template(
        session, company_id=context.company_id, market=payload.market
    )
    session.commit()
    return CoaImportOut(
        market=payload.market,
        imported=len(created),
        accounts=[_account_out(account) for account in created],
    )


@app.put(
    f"{BASE}/account-mappings/{{key}}",
    response_model=AccountMappingOut,
    tags=["accounting"],
)
def put_account_mapping(
    key: str, payload: AccountMappingIn, context: Context
) -> AccountMappingOut:
    """Point one posting key at one account — the configuration a module posts through."""
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="account.write",
        entity="account_mapping",
    )
    mapping = set_mapping(
        session,
        company_id=context.company_id,
        key=key,
        account_code=payload.account_code,
    )
    session.commit()
    return AccountMappingOut(
        key=mapping.key,
        account_code=mapped_account(
            session, company_id=context.company_id, key=mapping.key
        ).code,
        account_id=str(mapping.account_id),
    )


@app.get(
    f"{BASE}/account-mappings",
    response_model=list[AccountMappingOut],
    tags=["accounting"],
)
def list_account_mappings(context: Context) -> list[AccountMappingOut]:
    """Every key this company has mapped — what a settings screen reads."""
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="account.read",
        entity="account_mapping",
    )
    # ponytail: one lookup per key, because the list is a settings screen's handful
    # of rows. Ceiling: a company with hundreds of keys. Upgrade path: join the
    # mapping to the account in one statement.
    return [
        AccountMappingOut(
            key=mapping.key,
            account_code=mapped_account(
                session, company_id=context.company_id, key=mapping.key
            ).code,
            account_id=str(mapping.account_id),
        )
        for mapping in mappings(session, company_id=context.company_id)
    ]


@app.post(
    f"{BASE}/journal-entries",
    response_model=JournalEntryOut,
    status_code=201,
    tags=["ledger"],
)
def post_entry(
    payload: JournalEntryIn,
    context: Context,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> JSONResponse:
    """Post one balanced journal entry — retry-safe by its idempotency key."""
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="journal.post",
        entity="journal_entry",
    )
    for line in payload.lines:
        reject_restricted_fields(
            session,
            company_id=context.company_id,
            subject=context.actor,
            entity="journal_line",
            payload=line.model_dump(),
        )
    fingerprint = _fingerprint(payload.model_dump(mode="json"))
    path = f"{BASE}/journal-entries"

    seen = session.scalar(
        select(ApiIdempotencyKey).where(
            ApiIdempotencyKey.company_id == context.company_id,
            ApiIdempotencyKey.key == idempotency_key,
        )
    )
    if seen is not None:
        if seen.fingerprint != fingerprint:
            raise ApiError(
                409,
                "idempotency_key_reused",
                f"key {idempotency_key!r} was used for a different body",
            )
        # The retry gets the first answer, marked as a replay, and nothing is
        # posted a second time. The stored body is the whole record; what this
        # caller may see of it is decided per request, below.
        return JSONResponse(
            status_code=seen.status_code,
            content=_visible_entry(session, context, json.loads(seen.body)),
            headers={"Idempotent-Replay": "true"},
        )

    try:
        entry = post_journal_entry(
            session,
            company_id=context.company_id,
            posting_date=payload.posting_date,
            currency=payload.currency,
            memo=payload.memo,
            source_type=payload.source_type,
            source_id=_document_id(payload.source_id),
            lines=[
                {
                    "account": line.account,
                    "debit": _amount(line.debit),
                    "credit": _amount(line.credit),
                    "party": line.party,
                }
                for line in payload.lines
            ],
        )
    except UnbalancedEntryError as exc:
        raise ApiError(422, "unbalanced_entry", str(exc)) from exc

    body = _entry_out(entry).model_dump(mode="json")
    session.add(
        ApiIdempotencyKey(
            company_id=context.company_id,
            key=idempotency_key,
            method="POST",
            path=path,
            fingerprint=fingerprint,
            status_code=201,
            body=json.dumps(body),
        )
    )
    session.commit()
    return JSONResponse(status_code=201, content=_visible_entry(session, context, body))


@app.get(f"{BASE}/journal-entries", response_model=PageOut, tags=["ledger"])
def list_entries(
    context: Context,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
    source_type: Annotated[str | None, Query()] = None,
    source_id: Annotated[str | None, Query()] = None,
) -> PageOut:
    """One page of the company's ledger, in posting order.

    Filtered to one source document when the pair is given — the drill-down from
    a document to the postings it produced (T-1.ACCT.02).
    """
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="journal.read",
        entity="journal_entry",
    )
    filters = [JournalEntry.company_id == context.company_id]
    if source_type is not None or source_id is not None:
        filters.append(JournalEntry.source_type == source_type)
        filters.append(JournalEntry.source_id == _document_id(source_id))
    total = session.scalar(select(func.count()).select_from(JournalEntry).where(*filters))
    entries = session.scalars(
        select(JournalEntry)
        .where(*filters)
        .order_by(JournalEntry.posting_date, JournalEntry.created_at)
        .limit(limit)
        .offset(offset)
    ).all()
    page = PageOut(
        items=[_entry_out(entry) for entry in entries],
        limit=limit,
        offset=offset,
        total=total or 0,
    )
    payload = page.model_dump(mode="json")
    payload["items"] = [_visible_entry(session, context, item) for item in payload["items"]]
    return JSONResponse(status_code=200, content=payload)
