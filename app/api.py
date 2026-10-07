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
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
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
from app.company import (
    CREDIT_CHECK_MODES,
    Company,
    UnknownCompanyError,
    UnknownCreditCheckMode,
    company_base_currency,
    credit_check_mode_of,
    set_credit_check_mode,
)
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
from app.localization import packs as installed_packs, tax_rules as pack_tax_rules
from app.party import UnknownPartyError
from app.procurement.requisitions import (
    RequisitionError,
    requisition_by_number,
)
from app.procurement.rfq import (
    RfqError,
    invited as rfq_invited,
    issue_rfq,
    record_response as record_rfq_response,
    responses as rfq_responses,
    rfq_by_number,
)
from app.reporting import (
    DEFAULT_CAPABILITY as DEFAULT_REPORT_CAPABILITY,
    ReportDefinition,
    register as register_report,
    run as run_report,
)
from app.sales.customers import CustomerError, customer_by_code
from app.sales.pipeline import (
    BOARD_ENTITY,
    PipelineError,
    PipelineStage,
    board as pipeline_board,
    card_payload,
    convert_to_quotation,
    create_opportunity,
    define_stage,
    latest_move,
    lose_opportunity,
    move_opportunity,
    opportunity_by_id,
    stage_by_name,
)
from app.sales.campaigns import (
    CouponError,
    coupon_by_code,
    define_coupon,
    redeem_coupon,
)
from app.sales.pricing import (
    PricingError,
    define_rule,
    resolve_price,
)
from app.sales.orders import (
    BREACHED,
    CONFIRMED,
    CreditDecision,
    OrderError,
    confirm_order,
    convert_quotation_to_order,
    credit_decision_for,
    order_by_number,
    order_total,
)
from app.sales.fulfilment import (
    FulfilmentError,
    Shipment,
    generate_pick_list,
    pick_list_for,
    pick_lines,
    record_picked,
    remaining_quantity,
    ship_order,
    shipments_for,
)
from app.sales.quotations import (
    QuotationError,
    add_line,
    create_quotation,
    expired,
    line_amount,
    lines_of,
    quotation_by_number,
    reprice_quotation,
)
from app.stock.items import Item, ItemError, item_by_sku
from app.stock.locations import LocationError, location_by_code
from app.ar.invoices import InvoiceError
from app.sales.tax import TaxError
from app.security import (
    AccessDenied,
    hidden_fields,
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
    # T-3.SALES.04: the credit-check policy this company has stated, or null when it has
    # stated none — which the order-time check treats differently from "off".
    credit_check_mode: str | None = None


class CreditCheckModeIn(BaseModel):
    """The policy a company is stating, or null to withdraw it (back to unstated)."""

    mode: str | None


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


@app.exception_handler(UnknownCreditCheckMode)
async def _unknown_credit_mode(
    _request: Request, exc: UnknownCreditCheckMode
) -> JSONResponse:
    return _error(422, "unknown_credit_check_mode", str(exc))


@app.exception_handler(UnknownCurrencyError)
async def _unknown_currency(_request: Request, exc: UnknownCurrencyError) -> JSONResponse:
    return _error(422, "unknown_currency", str(exc))


# The catch-all for the currency service's refusals: an unknown rate, an attempt
# to rewrite a past date, a missing provider. All client errors, all one shape.
@app.exception_handler(CurrencyError)
async def _currency_error(_request: Request, exc: CurrencyError) -> JSONResponse:
    return _error(422, "currency_error", str(exc))


# The procurement documents' own refusals (T-2.PROC.01…): an unapproved requisition,
# a locked one, an RFQ line nobody asked about. Domain rules, all client errors.
@app.exception_handler(RequisitionError)
async def _requisition_error(_request: Request, exc: RequisitionError) -> JSONResponse:
    return _error(422, "requisition_error", str(exc))


@app.exception_handler(RfqError)
async def _rfq_error(_request: Request, exc: RfqError) -> JSONResponse:
    return _error(422, "rfq_error", str(exc))


@app.exception_handler(PipelineError)
async def _pipeline_error(_request: Request, exc: PipelineError) -> JSONResponse:
    return _error(422, "pipeline_error", str(exc))


@app.exception_handler(CustomerError)
async def _customer_error(_request: Request, exc: CustomerError) -> JSONResponse:
    return _error(422, "customer_error", str(exc))


@app.exception_handler(QuotationError)
async def _quotation_error(_request: Request, exc: QuotationError) -> JSONResponse:
    return _error(422, "quotation_error", str(exc))


@app.exception_handler(OrderError)
async def _order_error(_request: Request, exc: OrderError) -> JSONResponse:
    return _error(422, "order_error", str(exc))


@app.exception_handler(CouponError)
async def _coupon_error(_request: Request, exc: CouponError) -> JSONResponse:
    return _error(422, "coupon_error", str(exc))


@app.exception_handler(ItemError)
async def _item_error(_request: Request, exc: ItemError) -> JSONResponse:
    return _error(422, "item_error", str(exc))


@app.exception_handler(PricingError)
async def _pricing_error(_request: Request, exc: PricingError) -> JSONResponse:
    return _error(422, "pricing_error", str(exc))


@app.exception_handler(FulfilmentError)
async def _fulfilment_error(_request: Request, exc: FulfilmentError) -> JSONResponse:
    return _error(422, "fulfilment_error", str(exc))


@app.exception_handler(InvoiceError)
async def _invoice_error(_request: Request, exc: InvoiceError) -> JSONResponse:
    return _error(422, "invoice_error", str(exc))


@app.exception_handler(TaxError)
async def _tax_error(_request: Request, exc: TaxError) -> JSONResponse:
    return _error(422, "tax_error", str(exc))


@app.exception_handler(LocationError)
async def _location_error(_request: Request, exc: LocationError) -> JSONResponse:
    return _error(422, "location_error", str(exc))


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
    response = JSONResponse(
        status_code=201,
        content=_visible_entry(session, context, body),
    )
    session.commit()
    return response


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


# --- T-2.PROC.03 / T-2.PROC.04 — requisitions, RFQs and what suppliers answered ---
# The facts a comparison is built from, published here so the frontend repository can
# render the matrix from the contract alone: the quoted price, the rate it was
# converted at, and the configured procurement tax rate. The *comparison* — the
# side-by-side view, its stated basis and its export — is T-2.PROC.04's, in the
# frontend repository, and computes over these numbers rather than over a database.


class RfqQuotedLineIn(BaseModel):
    line_no: int
    unit_price: str


class RfqIn(BaseModel):
    number: str
    requisition: str
    supplier_codes: list[str]
    response_deadline: date
    issued_on: date | None = None


class RfqResponseIn(BaseModel):
    supplier_code: str
    received_on: date
    lines: list[RfqQuotedLineIn]
    currency: str | None = None
    lead_time_days: int | None = None
    valid_until: date | None = None
    note: str | None = None


class RfqLineOut(BaseModel):
    line_no: int
    requisition_line_no: int
    description: str
    quantity: str
    uom: str


class RfqQuotedLineOut(BaseModel):
    line_no: int
    unit_price: str
    currency: str
    # The rate the quote was converted at, so a reader can see the basis rather
    # than having to trust that one was applied.
    fx_rate: str
    base_unit_price: str


class RfqSupplierOut(BaseModel):
    code: str
    name: str
    # False means the supplier was asked and did not answer — which is not the same
    # as answering with a zero price, and this shape keeps the two apart.
    responded: bool
    late: bool
    received_on: date | None = None
    lead_time_days: int | None = None
    valid_until: date | None = None
    lines: list[RfqQuotedLineOut]


class RfqBasisOut(BaseModel):
    """What makes the quotes comparable — stated, so the matrix can label it."""

    base_currency: str
    fx_on: date
    tax_rule_code: str | None = None
    tax_rate_percent: str | None = None
    tax_inclusive: bool


class RfqOut(BaseModel):
    number: str
    requisition: str
    currency: str
    issued_on: date
    response_deadline: date
    status: str
    basis: RfqBasisOut
    lines: list[RfqLineOut]
    suppliers: list[RfqSupplierOut]


def _procurement_tax_rule() -> dict | None:
    """The pack rule a procurement document carries tax at, or ``None``.

    Only used to *label* the comparison's basis (T-2.PROC.04). Applying tax rules to
    documents, and validating a supplier's tax identifiers, is T-2.PROC.08's — this
    is one lookup so the matrix can say which rate it is inclusive of.

    The market is never hard-coded: it is the pack this deployment ships. Where more
    than one pack is installed there is no single answer, so the basis is stated
    without a rate rather than guessed.
    """
    markets = installed_packs()
    if len(markets) != 1:
        return None
    rules = pack_tax_rules(markets[0], "purchase_order")
    return rules[0] if rules else None


def _rfq_out(session: Session, context: RequestContext, rfq) -> RfqOut:
    """One RFQ as the matrix reads it: the lines, who answered, and on what basis."""
    base_currency = company_base_currency(session, company_id=context.company_id)
    answered = {response.supplier_id: response for response in rfq_responses(session, rfq)}
    rule = _procurement_tax_rule()
    rate = rule["rate_percent"] if rule is not None else None

    suppliers: list[RfqSupplierOut] = []
    for supplier in rfq_invited(session, rfq):
        response = answered.get(supplier.id)
        quoted: list[RfqQuotedLineOut] = []
        if response is not None:
            for line in response.lines:
                fx = rate_for(
                    session,
                    company_id=context.company_id,
                    base_currency=base_currency,
                    currency=line.response.currency,
                    on=rfq.issued_on,
                )
                quoted.append(
                    RfqQuotedLineOut(
                        line_no=line.line_no,
                        unit_price=_money(line.unit_price),
                        currency=line.response.currency,
                        fx_rate=format(fx, "f"),
                        base_unit_price=_base_amount(line.unit_price, fx),
                    )
                )
        suppliers.append(
            RfqSupplierOut(
                code=supplier.party.code,
                name=supplier.party.name,
                responded=response is not None,
                late=bool(response.late) if response is not None else False,
                received_on=response.received_on if response is not None else None,
                lead_time_days=response.lead_time_days if response is not None else None,
                valid_until=response.valid_until if response is not None else None,
                lines=sorted(quoted, key=lambda row: row.line_no),
            )
        )

    return RfqOut(
        number=rfq.number,
        requisition=rfq.requisition.number,
        currency=rfq.currency,
        issued_on=rfq.issued_on,
        response_deadline=rfq.response_deadline,
        status=rfq.status,
        basis=RfqBasisOut(
            base_currency=base_currency,
            fx_on=rfq.issued_on,
            tax_rule_code=rule["code"] if rule is not None else None,
            tax_rate_percent=None if rate is None else str(Decimal(str(rate))),
            tax_inclusive=rule is not None,
        ),
        lines=[
            RfqLineOut(
                line_no=line.line_no,
                requisition_line_no=line.line_no,
                description=line.description,
                quantity=_money(line.quantity),
                uom=line.uom,
            )
            for line in rfq.lines
        ],
        suppliers=suppliers,
    )


@app.post(f"{BASE}/rfqs", response_model=RfqOut, status_code=201, tags=["procurement"])
def new_rfq(payload: RfqIn, context: Context) -> RfqOut:
    """Issue an RFQ against an approved requisition to one or more suppliers."""
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="rfq.post",
        entity="rfq",
    )
    requisition = requisition_by_number(
        session, company_id=context.company_id, number=payload.requisition
    )
    rfq = issue_rfq(
        session,
        requisition=requisition,
        number=payload.number,
        supplier_codes=payload.supplier_codes,
        response_deadline=payload.response_deadline,
        issued_on=payload.issued_on,
    )
    response = _rfq_out(session, context, rfq)
    session.commit()
    return response


@app.post(
    f"{BASE}/rfqs/{{number}}/responses",
    response_model=RfqOut,
    status_code=201,
    tags=["procurement"],
)
def record_rfq_answer(number: str, payload: RfqResponseIn, context: Context) -> RfqOut:
    """Record one invited supplier's answer, line by line.

    A late answer is stored with `late = true` rather than refused: deciding whether
    to still use it is the buyer's, and the record has to be able to say so.
    """
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="rfq.post",
        entity="rfq",
    )
    rfq = rfq_by_number(session, company_id=context.company_id, number=number)
    record_rfq_response(
        session,
        rfq,
        supplier_code=payload.supplier_code,
        received_on=payload.received_on,
        lines=[{"line_no": line.line_no, "unit_price": _amount(line.unit_price)}
               for line in payload.lines],
        currency=payload.currency,
        lead_time_days=payload.lead_time_days,
        valid_until=payload.valid_until,
        note=payload.note,
    )
    response = _rfq_out(session, context, rfq)
    session.commit()
    return response


@app.get(f"{BASE}/rfqs/{{number}}", response_model=RfqOut, tags=["procurement"])
def read_rfq(number: str, context: Context) -> RfqOut:
    """One RFQ with its lines, its invitees and what each of them answered.

    What T-2.PROC.04's comparative statement matrix reads. A quote in another
    currency is carried at the rate for the RFQ's issue date, and the rate is in the
    payload, so the basis is visible rather than implied.
    """
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="rfq.read",
        entity="rfq",
    )
    rfq = rfq_by_number(session, company_id=context.company_id, number=number)
    return _rfq_out(session, context, rfq)


# --- T-3.SALES.02: the opportunity board --------------------------------------


class PipelineCardOut(BaseModel):
    """One deal on the board.

    **Every** field may be absent from the payload: a field the caller's role may not
    read is left out rather than nulled (T-0.SEC.01), and no field is exempt — a
    restriction can be stated against any of them. The contract therefore marks them
    all optional, and the shell renders a missing field as "not shown" rather than as
    an empty one.

    That includes `id`, which is what a card is addressed by when it is moved, lost or
    converted: a caller whose role may not read it gets a board it can look at but not
    drive, rather than one that offers an action that would fail.
    """

    id: str | None = None
    name: str | None = None
    value: str | None = None
    owner: str | None = None
    expected_close: str | None = None
    lost_reason: str | None = None


class PipelineStageOut(BaseModel):
    """One configured column: its name, its place on the board and what it means."""

    name: str
    position: int
    is_won: bool
    is_lost: bool


class PipelineColumnOut(BaseModel):
    """A column and the cards standing in it."""

    stage: PipelineStageOut
    cards: list[PipelineCardOut]


@app.get(
    f"{BASE}/pipeline/board",
    response_model=list[PipelineColumnOut],
    tags=["sales"],
)
def pipeline_board_view(context: Context) -> JSONResponse:
    """The opportunity board as this caller may see it.

    Read-only, and the columns are whatever the company configured — no stage list
    is compiled in. The payload is filtered per field permission before it leaves,
    so a restricted value is not in the response at all; it is returned as a
    `JSONResponse` for that reason, rather than validated into a model whose
    defaults would put a null back where a field was deliberately omitted.
    """
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="pipeline.read",
        entity="opportunity",
    )
    return JSONResponse(
        content=pipeline_board(
            session, company_id=context.company_id, subject=context.actor
        )
    )


# --- T-3.SALES.02: driving the board ------------------------------------------
# The board above is a read. Without these the Kanban is a picture: the stages could
# not be configured and a card could not be created, moved, lost or converted through
# the published API at all. Every one of them is thin on purpose — the rules live in
# `app/sales/pipeline.py`, and these endpoints only bind a request to them.


class PipelineStageIn(BaseModel):
    """A column to add: its name, its place, and what it means on the board.

    `is_won`/`is_lost` are nullable rather than defaulted, like every other optional
    field on this boundary: an omitted field says "not stated", and the contract keeps
    optional and nullable the same thing so a client can tell the two apart.
    """

    name: str
    position: int
    is_won: bool | None = None
    is_lost: bool | None = None


class OpportunityIn(BaseModel):
    """A new deal.

    The customer is named by its **code**, the way Phase 2's documents name their
    suppliers, so a caller never has to hold a database id to state who a deal is for.
    """

    customer_code: str
    name: str
    owner: str
    value: str | None = None
    expected_close: date | None = None
    stage: str | None = None


class PipelineMoveIn(BaseModel):
    """One drag: the stage the card goes to, and the reason where one is needed."""

    to_stage: str
    reason: str | None = None


class PipelineLossIn(BaseModel):
    """Why a deal ended. Required — a loss that says nothing teaches nothing."""

    reason: str


class QuotationForOpportunityIn(BaseModel):
    """The number the quotation is filed under, and the day it is issued."""

    number: str
    issued_on: date | None = None


class PipelineMoveOut(BaseModel):
    """The step just recorded: where from, where to, who, when and (if lost) why.

    The actor is the request's own `X-Actor` and the instant is the server's, so a
    move cannot be back-dated or attributed by the caller.
    """

    from_stage: str | None = None
    to_stage: str
    actor: str
    reason: str | None = None
    moved_at: str


class OpportunityOut(BaseModel):
    """One card after a mutation: the fields the caller may read, where it now stands,
    and the move that put it there.

    The `card` half is filtered exactly as the board filters it — the same
    `card_payload` through the same `hidden_fields` — so a mutation can never become a
    way of reading a value the board withholds. The envelope is the response's own
    framing rather than a card field, so it is not itself filterable.
    """

    card: PipelineCardOut
    stage: str
    closed_at: str | None = None
    move: PipelineMoveOut | None = None


class QuotationLineOut(BaseModel):
    """One priced line: what it is, what it costs, and why it costs that."""

    line_no: int
    description: str
    quantity: str
    uom: str
    unit_price: str
    rule_code: str | None = None
    # T-3.SALES.06: the rank that decided between overlapping rules, recorded with the
    # price so the line states the resolution order and not only the winner.
    rule_priority: int | None = None
    priced_on: date
    amount: str


class QuotationOut(BaseModel):
    """One quotation: who it is for, what it prices, and whether it still holds.

    `expired` is what a conversion asks before it acts, and `priced_on` on each line is
    what says *when* the price was fixed — so "these prices are stale" is a fact in the
    payload rather than something a client has to work out.
    """

    number: str
    customer_code: str
    currency: str | None = None
    opportunity_id: str | None = None
    issued_on: date
    valid_until: date | None = None
    expired: bool
    lines: list[QuotationLineOut]
    total: str


def _stage_out(stage: PipelineStage) -> dict[str, Any]:
    return {
        "name": stage.name,
        "position": stage.position,
        "is_won": stage.is_won,
        "is_lost": stage.is_lost,
    }


def _move_out(session: Session, opportunity) -> dict[str, Any] | None:
    """The card's most recent step, with both stages **named** rather than identified.

    A caller reads "Qualified → Won by maria at 09:15", not three uuids.
    """
    move = latest_move(session, opportunity)
    if move is None:
        return None
    target = session.get(PipelineStage, move.to_stage_id)
    source = (
        None
        if move.from_stage_id is None
        else session.get(PipelineStage, move.from_stage_id)
    )
    return {
        "from_stage": None if source is None else source.name,
        "to_stage": target.name if target is not None else "",
        "actor": move.actor,
        "reason": move.reason,
        "moved_at": move.moved_at.isoformat(),
    }


def _opportunity_out(
    session: Session, context: RequestContext, opportunity
) -> dict[str, Any]:
    """One card after a mutation, filtered per field permission like the board."""
    hidden = hidden_fields(
        session,
        company_id=context.company_id,
        subject=context.actor,
        entity=BOARD_ENTITY,
    )
    stage = session.get(PipelineStage, opportunity.stage_id)
    return {
        "card": {
            field: value
            for field, value in card_payload(opportunity).items()
            if field not in hidden
        },
        "stage": stage.name if stage is not None else "",
        "closed_at": (
            None if opportunity.closed_at is None else opportunity.closed_at.isoformat()
        ),
        "move": _move_out(session, opportunity),
    }


@app.post(
    f"{BASE}/pipeline/stages",
    response_model=PipelineStageOut,
    status_code=201,
    tags=["sales"],
)
def new_pipeline_stage(payload: PipelineStageIn, context: Context) -> PipelineStageOut:
    """Add a column to the company's board.

    This is the whole of "configurable without code change": the board is the rows
    this endpoint has written, in their stated order, and `is_won`/`is_lost` are what
    give a column its meaning while the name stays the company's own.
    """
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="pipeline.configure",
        entity="pipeline_stage",
    )
    stage = define_stage(
        session,
        company_id=context.company_id,
        name=payload.name,
        position=payload.position,
        # An unstated flag is not won and not lost — the service takes a plain bool.
        is_won=bool(payload.is_won),
        is_lost=bool(payload.is_lost),
    )
    session.commit()
    return PipelineStageOut(**_stage_out(stage))


@app.post(
    f"{BASE}/opportunities",
    response_model=OpportunityOut,
    status_code=201,
    tags=["sales"],
)
def new_opportunity(payload: OpportunityIn, context: Context) -> JSONResponse:
    """Put a deal on the board, in the first stage unless another is named.

    The opening move is recorded like every other one, and its actor is the request's,
    so a card's history starts at the column it was created in rather than at the
    first time somebody dragged it.
    """
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="opportunity.write",
        entity=BOARD_ENTITY,
    )
    # `exclude_unset`: only what the caller actually stated is held against the field
    # permissions, so an omitted `value` is not a write to `value`.
    reject_restricted_fields(
        session,
        company_id=context.company_id,
        subject=context.actor,
        entity=BOARD_ENTITY,
        payload=payload.model_dump(exclude_unset=True),
    )
    customer = customer_by_code(
        session, company_id=context.company_id, code=payload.customer_code
    )
    stage = (
        None
        if payload.stage is None
        else stage_by_name(session, company_id=context.company_id, name=payload.stage)
    )
    opportunity = create_opportunity(
        session,
        company_id=context.company_id,
        customer=customer,
        name=payload.name,
        owner=payload.owner,
        value=_amount(payload.value),
        expected_close=payload.expected_close,
        stage=stage,
        actor=context.actor,
    )
    body = _opportunity_out(session, context, opportunity)
    session.commit()
    return JSONResponse(status_code=201, content=body)


@app.post(
    f"{BASE}/opportunities/{{opportunity_id}}/moves",
    response_model=OpportunityOut,
    tags=["sales"],
)
def move_opportunity_card(
    opportunity_id: uuid.UUID, payload: PipelineMoveIn, context: Context
) -> JSONResponse:
    """Move one card, recording who moved it and when.

    Both come from the request rather than from the body: the actor is `X-Actor` and
    the instant is the server's, so a move cannot be misattributed or back-dated. A
    move into a column marked lost without a reason is refused by the domain rule.
    """
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="opportunity.write",
        entity=BOARD_ENTITY,
        entity_id=opportunity_id,
    )
    opportunity = opportunity_by_id(
        session, company_id=context.company_id, opportunity_id=opportunity_id
    )
    to_stage = stage_by_name(
        session, company_id=context.company_id, name=payload.to_stage
    )
    move_opportunity(
        session,
        opportunity,
        to_stage=to_stage,
        actor=context.actor,
        reason=payload.reason,
    )
    body = _opportunity_out(session, context, opportunity)
    session.commit()
    return JSONResponse(content=body)


@app.post(
    f"{BASE}/opportunities/{{opportunity_id}}/loss",
    response_model=OpportunityOut,
    tags=["sales"],
)
def lose_opportunity_card(
    opportunity_id: uuid.UUID, payload: PipelineLossIn, context: Context
) -> JSONResponse:
    """Mark a deal lost, with its reason.

    The column it lands in is the company's own — the one it marked `is_lost` — so a
    client never has to know which column that is, and the reason is required by the
    domain rule rather than by this endpoint. The move is still recorded on the trail,
    which is what an auditor reads.
    """
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="opportunity.write",
        entity=BOARD_ENTITY,
        entity_id=opportunity_id,
    )
    opportunity = opportunity_by_id(
        session, company_id=context.company_id, opportunity_id=opportunity_id
    )
    lose_opportunity(
        session, opportunity, actor=context.actor, reason=payload.reason
    )
    body = _opportunity_out(session, context, opportunity)
    session.commit()
    return JSONResponse(content=body)


@app.post(
    f"{BASE}/opportunities/{{opportunity_id}}/quotation",
    response_model=QuotationOut,
    status_code=201,
    tags=["sales"],
)
def convert_opportunity_card(
    opportunity_id: uuid.UUID,
    payload: QuotationForOpportunityIn,
    context: Context,
) -> QuotationOut:
    """Turn a won deal into a quotation, carrying the customer's details across.

    Two capabilities, because the call writes two documents: it acts on the
    opportunity and it creates a quotation (T-3.SALES.03's document, whose endpoints
    will be gated the same way). One win produces one quotation — the refusal and the
    schema's own partial unique index both hold however the request arrives.
    """
    session = context.session
    for capability in ("opportunity.write", "quotation.write"):
        require(
            session,
            company_id=context.company_id,
            subject=context.actor,
            capability=capability,
            entity=BOARD_ENTITY,
            entity_id=opportunity_id,
        )
    opportunity = opportunity_by_id(
        session, company_id=context.company_id, opportunity_id=opportunity_id
    )
    quotation = convert_to_quotation(
        session,
        opportunity,
        number=payload.number,
        issued_on=payload.issued_on,
    )
    response = _quotation_out(session, quotation)
    session.commit()
    return response


# --- T-3.SALES.03: quotations, their lines, and the order they become ---------
# T-3.SALES.02 could raise a quotation but not price one. These are the paths that
# make a quotation a document: priced lines with the rule that produced them, a
# validity window, the re-pricing an expired offer needs, and the conversion that
# turns an accepted one into an order with identical lines.


class QuotationLineIn(BaseModel):
    """One priced line at quote time.

    `rule_code` is the pricing rule that produced the price, where a rule did — the
    engine is T-3.SALES.06/07's and fills it; a price a person stated names no rule,
    which is an honest null rather than an invented code. `priced_on` defaults to the
    day the line is written, so the price's age is recorded either way.
    """

    line_no: int
    description: str
    quantity: str
    unit_price: str
    uom: str | None = None
    rule_code: str | None = None
    priced_on: date | None = None


class QuotationIn(BaseModel):
    """A quotation and everything on it, in one request.

    Lines come with the header because a quotation with no lines is not a document —
    and because a partially-created quotation would be a quotation nobody quoted.
    There is no endpoint that adds a line to an existing one, so an empty list would
    create a document that can never become an order; the boundary refuses it here
    rather than letting the store hold it.
    """

    number: str
    customer_code: str
    currency: str | None = None
    issued_on: date | None = None
    valid_until: date | None = None
    lines: Annotated[list[QuotationLineIn], Field(min_length=1)]


class QuotationPriceIn(BaseModel):
    """One line restated: its number, its new price, and the rule behind it."""

    line_no: int
    unit_price: str
    rule_code: str | None = None


class QuotationRepriceIn(BaseModel):
    """A whole re-price: every line restated, and the window the new prices hold for.

    `valid_until` is required rather than defaulted: the plan names no quotation
    validity, so there is no honest default to invent — the caller states how long
    their own re-price stands for.
    """

    valid_until: date
    prices: list[QuotationPriceIn]


class OrderLineIn(BaseModel):
    """The number the order is filed under, and the day it is placed."""

    number: str
    on: date | None = None


class CreditDecisionOut(BaseModel):
    """The order-time credit decision as it was recorded (T-3.SALES.04).

    Every field is the value *at the moment of confirmation* — the mode then, the limit
    then, the exposure then — which is why a later change to any of them leaves this
    answer untouched.
    """

    mode: str
    limit: str | None = None
    exposure: str
    order_value: str
    exposure_after: str
    breached: bool
    acknowledged_by: str | None = None
    decided_at: datetime


class OrderLineOut(BaseModel):
    """One ordered line — the quotation's, carried across without re-keying."""

    line_no: int
    description: str
    quantity: str
    uom: str
    unit_price: str
    rule_code: str | None = None
    # Carried across from the quotation with the price (T-3.SALES.06).
    rule_priority: int | None = None
    priced_on: date
    amount: str
    # T-3.SALES.05: what has left, and what the order still owes on this line.
    shipped: str
    remaining: str


class PickListLineOut(BaseModel):
    """One line of the picker's paper: what the order asks, and what was picked."""

    line_no: int
    item_sku: str | None = None
    quantity: str
    uom: str
    picked_quantity: str


class PickListOut(BaseModel):
    number: str
    created_on: date
    lines: list[PickListLineOut]


class PickListIn(BaseModel):
    number: str
    on: date | None = None


class PickedQuantityIn(BaseModel):
    """How much of one pick-list line was picked."""

    quantity: str


class ShipmentLineOut(BaseModel):
    """One line as it left, and the stock movement that carried it out."""

    line_no: int
    quantity: str
    uom: str
    movement: str | None = None


class ShipmentOut(BaseModel):
    number: str
    warehouse: str
    shipped_on: date
    lines: list[ShipmentLineOut]


class ShipmentLineIn(BaseModel):
    line_no: int
    quantity: str


class ShipmentIn(BaseModel):
    """What a shipment needs: where from, and how much of which lines.

    The lines come with the header because a shipment with nothing on it moves no
    stock — the boundary refuses an empty list rather than storing a document that
    describes nothing.
    """

    number: str
    warehouse: str
    on: date | None = None
    lines: Annotated[list[ShipmentLineIn], Field(min_length=1)]


class OrderOut(BaseModel):
    """The order a quotation became, and the quotation it came from."""

    number: str
    customer_code: str
    quotation: str | None = None
    currency: str | None = None
    ordered_on: date
    lines: list[OrderLineOut]
    total: str
    # T-3.SALES.04: the lifecycle, and the credit decision taken at confirmation.
    status: str
    confirmed_at: datetime | None = None
    confirmed_by: str | None = None
    credit_decision: CreditDecisionOut | None = None
    # T-3.SALES.05: what fulfilment had to say — the picker's paper, and what shipped.
    pick_list: PickListOut | None = None
    shipments: list[ShipmentOut]


class ConfirmOrderIn(BaseModel):
    """What confirming an order needs: the customer's exposure, and any acceptance.

    The **exposure is stated by the caller** until T-3.AR.06 computes it across open
    AR — the path this task's own recorded stop chose. `acknowledge_breach` is the
    acknowledgement `warn` mode requires: an explicit act, so a breach is never
    accepted by the mere act of asking.
    """

    exposure: str
    # Nullable rather than merely defaulted: T-0.API.01's convention is that an
    # optional field says so in the contract, so "not acknowledged" is an explicit null
    # and not an implicit absence. Only `true` acknowledges.
    acknowledge_breach: bool | None = None


def _quotation_out(session: Session, quotation) -> QuotationOut:
    """One quotation as the API states it: its lines, its window, and its total."""
    today = datetime.now(timezone.utc).date()
    lines = lines_of(session, quotation)
    return QuotationOut(
        number=quotation.number,
        customer_code=quotation.customer.party.code,
        currency=quotation.currency,
        opportunity_id=(
            None if quotation.opportunity_id is None else str(quotation.opportunity_id)
        ),
        issued_on=quotation.issued_on,
        valid_until=quotation.valid_until,
        expired=expired(quotation, on=today),
        lines=[
            QuotationLineOut(
                line_no=line.line_no,
                description=line.description,
                quantity=_money(line.quantity),
                uom=line.uom,
                unit_price=_money(line.unit_price),
                rule_code=line.rule_code,
                rule_priority=line.rule_priority,
                priced_on=line.priced_on,
                amount=_money(line_amount(line)),
            )
            for line in lines
        ],
        total=_money(sum((line_amount(line) for line in lines), Decimal(0))),
    )


def _credit_decision_out(decision: CreditDecision | None) -> CreditDecisionOut | None:
    """One recorded decision as the API states it, or nothing while the order is a draft."""
    if decision is None:
        return None
    return CreditDecisionOut(
        mode=decision.mode,
        limit=None if decision.limit_amount is None else _money(decision.limit_amount),
        exposure=_money(decision.exposure),
        order_value=_money(decision.order_value),
        exposure_after=_money(decision.exposure + decision.order_value),
        breached=decision.outcome == BREACHED,
        acknowledged_by=decision.acknowledged_by,
        decided_at=decision.decided_at,
    )


def _pick_list_out(session: Session, pick_list) -> PickListOut | None:
    """The picker's paper as the API states it, or nothing when none was drawn."""
    if pick_list is None:
        return None
    return PickListOut(
        number=pick_list.number,
        created_on=pick_list.created_on,
        lines=[
            PickListLineOut(
                line_no=line.line_no,
                item_sku=(
                    None
                    if line.item_id is None
                    else session.get(Item, line.item_id).sku
                ),
                quantity=_money(line.quantity),
                uom=line.uom,
                picked_quantity=_money(line.picked_quantity),
            )
            for line in pick_lines(session, pick_list)
        ],
    )


def _shipment_out(shipment: Shipment) -> ShipmentOut:
    """One shipment as the API states it, each line pointing at its own movement."""
    return ShipmentOut(
        number=shipment.number,
        warehouse=shipment.location.code,
        shipped_on=shipment.shipped_on,
        lines=[
            ShipmentLineOut(
                line_no=line.line_no,
                quantity=_money(line.quantity),
                uom=line.uom,
                movement=None if line.movement_id is None else str(line.movement_id),
            )
            for line in shipment.lines
        ],
    )


def _order_out(session: Session, order) -> OrderOut:
    """One order as the API states it, with the quotation it came from named."""
    lines = list(order.lines)
    return OrderOut(
        number=order.number,
        customer_code=order.customer.party.code,
        quotation=(
            None if order.quotation is None else order.quotation.number
        ),
        currency=order.currency,
        ordered_on=order.ordered_on,
        status=order.status,
        confirmed_at=order.confirmed_at,
        confirmed_by=order.confirmed_by,
        credit_decision=_credit_decision_out(credit_decision_for(session, order)),
        pick_list=_pick_list_out(session, pick_list_for(session, order)),
        shipments=[
            _shipment_out(shipment)
            for shipment in shipments_for(session, order)
        ],
        lines=[
            OrderLineOut(
                line_no=line.line_no,
                description=line.description,
                quantity=_money(line.quantity),
                uom=line.uom,
                unit_price=_money(line.unit_price),
                rule_code=line.rule_code,
                rule_priority=line.rule_priority,
                priced_on=line.priced_on,
                amount=_money(line.quantity * line.unit_price),
                shipped=_money(line.shipped_quantity),
                remaining=_money(remaining_quantity(line)),
            )
            for line in lines
        ],
        total=_money(order_total(order)),
    )


@app.post(
    f"{BASE}/quotations",
    response_model=QuotationOut,
    status_code=201,
    tags=["sales"],
)
def new_quotation(payload: QuotationIn, context: Context) -> QuotationOut:
    """Raise a quotation with its priced lines and its validity window."""
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="quotation.write",
        entity="quotation",
    )
    customer = customer_by_code(
        session, company_id=context.company_id, code=payload.customer_code
    )
    quotation = create_quotation(
        session,
        company_id=context.company_id,
        customer_id=customer.id,
        number=payload.number,
        currency=payload.currency,
        issued_on=payload.issued_on,
        valid_until=payload.valid_until,
    )
    for line in payload.lines:
        add_line(
            session,
            quotation,
            line_no=line.line_no,
            description=line.description,
            quantity=_amount(line.quantity),
            unit_price=_amount(line.unit_price),
            uom=line.uom or "unit",
            rule_code=line.rule_code,
            priced_on=line.priced_on,
        )
    response = _quotation_out(session, quotation)
    session.commit()
    return response


@app.get(f"{BASE}/quotations/{{number}}", response_model=QuotationOut, tags=["sales"])
def read_quotation(number: str, context: Context) -> QuotationOut:
    """One quotation, with the lines it prices and whether it still holds."""
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="quotation.read",
        entity="quotation",
    )
    return _quotation_out(
        session,
        quotation_by_number(session, company_id=context.company_id, number=number),
    )


@app.post(
    f"{BASE}/quotations/{{number}}/reprice",
    response_model=QuotationOut,
    tags=["sales"],
)
def reprice(number: str, payload: QuotationRepriceIn, context: Context) -> QuotationOut:
    """Re-price a whole quotation and restate how long the new prices hold.

    This is what an expired quotation needs before it can become an order, and it is
    refused on a quotation that already became one — an order is not re-priced behind
    the customer's back.
    """
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="quotation.write",
        entity="quotation",
    )
    quotation = quotation_by_number(
        session, company_id=context.company_id, number=number
    )
    reprice_quotation(
        session,
        quotation,
        prices={
            line.line_no: _amount(line.unit_price) for line in payload.prices
        },
        rules={line.line_no: line.rule_code for line in payload.prices},
        valid_until=payload.valid_until,
    )
    response = _quotation_out(session, quotation)
    session.commit()
    return response


@app.post(
    f"{BASE}/quotations/{{number}}/order",
    response_model=OrderOut,
    status_code=201,
    tags=["sales"],
)
def order_from_quotation(
    number: str, payload: OrderLineIn, context: Context
) -> OrderOut:
    """Convert an accepted quotation into an order — once.

    Two capabilities, because the call reads one document and writes another: it acts
    on the quotation and it creates an order (whose lifecycle is T-3.SALES.04's).
    """
    session = context.session
    # Two capabilities, each recorded against the document it is about: the call reads
    # one document and writes another, and the order's lifecycle is T-3.SALES.04's.
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="quotation.write",
        entity="quotation",
    )
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="order.write",
        entity="sales_order",
    )
    quotation = quotation_by_number(
        session, company_id=context.company_id, number=number
    )
    order = convert_quotation_to_order(
        session, quotation, number=payload.number, on=payload.on
    )
    response = _order_out(session, order)
    session.commit()
    return response


# --- T-3.SALES.04: the order's lifecycle and its order-time credit decision ----
# T-3.SALES.03 could raise an order but not confirm one. These are the paths that
# make confirmation what it is: the company states a credit-check policy, confirming
# applies it, and the decision is written down with the limit, the exposure and the
# order value that produced it, so a later change to any of them cannot restate it.


@app.post(
    f"{BASE}/companies/current/credit-check-mode",
    response_model=CompanyOut,
    tags=["platform"],
)
def set_company_credit_check_mode(
    payload: CreditCheckModeIn, context: Context
) -> CompanyOut:
    """State, change or withdraw this company's credit-check policy.

    Plan §8 leaves the mode undecided, so the body carries a value or an explicit null
    rather than relying on a default: "not stated" is a state a company may be in, and
    the order-time check treats it as a refusal rather than as `off`.

    Changing the mode is forward-looking only — decisions already recorded keep the mode
    they were taken under, because each one stored it.
    """
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="company.configure",
        entity="company",
        entity_id=context.company_id,
    )
    company = session.get(Company, context.company_id)
    if company is None:
        raise ApiError(404, "company_not_found", f"no company {context.company_id}")
    set_credit_check_mode(session, company, mode=payload.mode)
    response = CompanyOut(
        id=str(company.id),
        code=company.code,
        name=company.name,
        base_currency=company.base_currency,
        fiscal_year_start_month=company.fiscal_year_start_month,
        credit_check_mode=company.credit_check_mode,
    )
    session.commit()
    return response


@app.get(
    f"{BASE}/sales-orders/{{number}}", response_model=OrderOut, tags=["sales"]
)
def sales_order(number: str, context: Context) -> OrderOut:
    """One order with its lifecycle and the credit decision taken at confirmation."""
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="order.read",
        entity="sales_order",
    )
    order = order_by_number(session, company_id=context.company_id, number=number)
    return _order_out(session, order)


@app.post(
    f"{BASE}/sales-orders/{{number}}/confirm",
    response_model=OrderOut,
    tags=["sales"],
)
def confirm_sales_order(
    number: str, payload: ConfirmOrderIn, context: Context
) -> OrderOut:
    """Confirm an order, applying the company's credit-check mode as it is placed.

    The exposure is **stated by the caller** until T-3.AR.06 computes it across open
    AR. The refusal a `block` breach produces is the point of the endpoint: it is the
    one place an order stops being an intention.
    """
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="order.write",
        entity="sales_order",
    )
    order = order_by_number(session, company_id=context.company_id, number=number)
    confirm_order(
        session,
        order,
        exposure=payload.exposure,
        actor=context.actor,
        acknowledge_breach=payload.acknowledge_breach is True,
    )
    response = _order_out(session, order)
    session.commit()
    return response


# --- T-3.SALES.05: pick lists, shipping, and the stock they move ---------------
# Confirming an order promises nothing physically. These paths are the other half:
# a pick list drawn from the confirmed lines, what was picked off the shelf, and the
# shipment that issues stock out of a warehouse — through T-1.INV.05's `issue`, so the
# valuation, the no-negative-stock rule and the GL posting are the shared ones.


@app.post(
    f"{BASE}/sales-orders/{{number}}/pick-list",
    response_model=OrderOut,
    status_code=201,
    tags=["sales"],
)
def create_order_pick_list(
    number: str, payload: PickListIn, context: Context
) -> OrderOut:
    """Draw the pick list for a confirmed order — exactly its lines, once."""
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="order.write",
        entity="sales_order",
    )
    order = order_by_number(session, company_id=context.company_id, number=number)
    generate_pick_list(session, order, number=payload.number, on=payload.on)
    response = _order_out(session, order)
    session.commit()
    return response


@app.post(
    f"{BASE}/sales-orders/{{number}}/pick-list/lines/{{line_no}}",
    response_model=OrderOut,
    tags=["sales"],
)
def record_order_pick(
    number: str, line_no: int, payload: PickedQuantityIn, context: Context
) -> OrderOut:
    """Record how much of one pick-list line was picked."""
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="order.write",
        entity="sales_order",
    )
    order = order_by_number(session, company_id=context.company_id, number=number)
    listed = pick_list_for(session, order)
    if listed is None:
        raise ApiError(
            404,
            "no_pick_list",
            f"sales order {order.number!r} has no pick list to record against",
        )
    record_picked(session, listed, line_no=line_no, quantity=payload.quantity)
    response = _order_out(session, order)
    session.commit()
    return response


@app.post(
    f"{BASE}/sales-orders/{{number}}/shipments",
    response_model=OrderOut,
    status_code=201,
    tags=["sales"],
)
def ship_sales_order(number: str, payload: ShipmentIn, context: Context) -> OrderOut:
    """Ship named lines of a confirmed order, issuing stock out of one warehouse.

    The refusal a second shipment for the same quantity produces is the point: what an
    order still owes is read from the line, and shipping more is refused rather than
    silently clamped.
    """
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="order.write",
        entity="sales_order",
    )
    order = order_by_number(session, company_id=context.company_id, number=number)
    warehouse = location_by_code(
        session, company_id=context.company_id, code=payload.warehouse
    )
    ship_order(
        session,
        order,
        number=payload.number,
        warehouse=warehouse,
        lines=[(line.line_no, line.quantity) for line in payload.lines],
        on=payload.on,
    )
    response = _order_out(session, order)
    session.commit()
    return response


# --- T-3.SALES.06: the pricing engine — ordered rules over tier and volume -------
# A rule is a scope (item, customer tier, volume band) plus a discount. The engine
# resolves overlapping rules in one fixed order and says so: `code` and `priority` are
# written onto the line that was priced, so an offer can be reproduced after the rules
# behind it have changed.


class PriceRuleIn(BaseModel):
    """One rule as it is filed: its scope, and the discount it applies.

    Every dimension is optional and an omitted one means **no constraint** — an absent
    tier is any tier — while `discount_type` must be stated, because the ledger records
    no default and a rule with an invented one would silently discount by the wrong
    measure.
    """

    code: str
    name: str
    discount_type: str
    discount_value: str
    item_sku: str | None = None
    tier: str | None = None
    # Nullable rather than merely defaulted (T-0.API.01's convention): an omitted band
    # floor or ordering is an explicit null that the caller's own defaults fill in.
    min_quantity: str | None = None
    max_quantity: str | None = None
    priority: int | None = None


class PriceRuleOut(BaseModel):
    code: str
    name: str
    item_sku: str | None = None
    tier: str | None = None
    min_quantity: str
    max_quantity: str | None = None
    priority: int
    discount_type: str
    discount_value: str


class PriceQueryIn(BaseModel):
    """What to price, before any rule has been applied to it.

    `base_price` is stated by the caller because the plan names no price list and the
    item master holds none: the engine decides which rule applies and what it does, not
    what the goods list at.
    """

    base_price: str
    quantity: str | None = None
    item_sku: str | None = None
    customer_code: str | None = None
    tier: str | None = None


class PriceDecisionOut(BaseModel):
    """The engine's answer, with the whole ordering it decided in."""

    base_price: str
    price: str
    rule_code: str | None = None
    rule_priority: int | None = None
    # Every rule that matched, best first — so a caller can say which rules lost and why.
    considered: list[PriceRuleOut]


def _price_rule_out(rule, session: Session) -> PriceRuleOut:
    return PriceRuleOut(
        code=rule.code,
        name=rule.name,
        item_sku=None if rule.item_id is None else session.get(Item, rule.item_id).sku,
        tier=rule.tier,
        min_quantity=_money(rule.min_quantity),
        max_quantity=None if rule.max_quantity is None else _money(rule.max_quantity),
        priority=rule.priority,
        discount_type=rule.discount_type,
        discount_value=_money(rule.discount_value),
    )


@app.post(
    f"{BASE}/price-rules", response_model=PriceRuleOut, status_code=201, tags=["sales"]
)
def create_price_rule(payload: PriceRuleIn, context: Context) -> PriceRuleOut:
    """File one pricing rule."""
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="pricing.configure",
        entity="price_rule",
    )
    item_id = None
    if payload.item_sku is not None:
        item_id = item_by_sku(
            session, company_id=context.company_id, sku=payload.item_sku
        ).id
    rule = define_rule(
        session,
        company_id=context.company_id,
        code=payload.code,
        name=payload.name,
        discount_type=payload.discount_type,
        discount_value=payload.discount_value,
        item_id=item_id,
        tier=payload.tier,
        min_quantity=payload.min_quantity or 1,
        max_quantity=payload.max_quantity,
        priority=payload.priority if payload.priority is not None else 100,
    )
    response = _price_rule_out(rule, session)
    session.commit()
    return response


@app.post(
    f"{BASE}/price-rules/resolve",
    response_model=PriceDecisionOut,
    tags=["sales"],
)
def resolve_price_rule(payload: PriceQueryIn, context: Context) -> PriceDecisionOut:
    """What the engine makes of one prospective line, and the order it decided in.

    The tier comes from the customer when one is named, so a caller cannot price a
    customer's order under a tier that customer does not sit in.
    """
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="order.read",
        entity="price_rule",
    )
    item_id = None
    if payload.item_sku is not None:
        item_id = item_by_sku(
            session, company_id=context.company_id, sku=payload.item_sku
        ).id
    tier = payload.tier
    if payload.customer_code is not None:
        tier = customer_by_code(
            session, company_id=context.company_id, code=payload.customer_code
        ).tier
    decision = resolve_price(
        session,
        company_id=context.company_id,
        base_price=payload.base_price,
        quantity=payload.quantity or 1,
        item_id=item_id,
        tier=tier,
    )
    return PriceDecisionOut(
        base_price=_money(decision.base_price),
        price=_money(decision.price),
        rule_code=decision.rule_code,
        rule_priority=decision.rule_priority,
        considered=[_price_rule_out(rule, session) for rule in decision.considered],
    )


# --- T-3.SALES.07: campaigns and coupons — scoped codes with a life --------------
# The engine resolves rules over item, tier, volume **and campaign**; a coupon is a
# code that adds a discount on top, inside its own window, up to its own usage limit,
# and within the strictest stacking allowance on the document it is used on. Its use
# is recorded against that document, which is what stops it being spent forever.


class CouponIn(BaseModel):
    """A coupon as it is filed: its campaign, its discount and its own limits.

    Every limit is stated here rather than defaulted, because the plan states none of
    them: an omitted window is *always open* and an omitted usage limit is *no limit*,
    which are different answers from "closed today" and "unusable".
    """

    code: str
    name: str
    campaign: str
    discount_type: str
    discount_value: str
    stacking_allowance: int
    valid_from: date | None = None
    valid_until: date | None = None
    max_redemptions: int | None = None


class CouponOut(BaseModel):
    code: str
    name: str
    campaign: str
    discount_type: str
    discount_value: str
    valid_from: date | None = None
    valid_until: date | None = None
    max_redemptions: int | None = None
    stacking_allowance: int


class CouponRedeemIn(BaseModel):
    """Which document is using the coupon, and what the price is before it applies."""

    document_type: str
    document_id: str
    base_price: str
    on: date | None = None


class CouponRedemptionOut(BaseModel):
    code: str
    campaign: str
    document_type: str
    document_id: str
    discount_amount: str


def _coupon_out(coupon) -> CouponOut:
    return CouponOut(
        code=coupon.code,
        name=coupon.name,
        campaign=coupon.campaign,
        discount_type=coupon.discount_type,
        discount_value=_money(coupon.discount_value),
        valid_from=coupon.valid_from,
        valid_until=coupon.valid_until,
        max_redemptions=coupon.max_redemptions,
        stacking_allowance=coupon.stacking_allowance,
    )


@app.post(
    f"{BASE}/coupons", response_model=CouponOut, status_code=201, tags=["sales"]
)
def create_coupon(payload: CouponIn, context: Context) -> CouponOut:
    """File one coupon."""
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="pricing.configure",
        entity="coupon",
    )
    coupon = define_coupon(
        session,
        company_id=context.company_id,
        code=payload.code,
        name=payload.name,
        campaign=payload.campaign,
        discount_type=payload.discount_type,
        discount_value=payload.discount_value,
        stacking_allowance=payload.stacking_allowance,
        valid_from=payload.valid_from,
        valid_until=payload.valid_until,
        max_redemptions=payload.max_redemptions,
    )
    response = _coupon_out(coupon)
    session.commit()
    return response


@app.post(
    f"{BASE}/coupons/{{code}}/redeem",
    response_model=CouponRedemptionOut,
    tags=["sales"],
)
def redeem(payload: CouponRedeemIn, code: str, context: Context) -> CouponRedemptionOut:
    """Use a coupon on a document, recording the use against that document.

    The refusals are the point: an unknown code, one outside its window, one used to
    its limit and one stacked past the allowance each answer with the reason, and none
    of them writes a redemption.
    """
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="order.write",
        entity="coupon",
    )
    coupon = coupon_by_code(session, company_id=context.company_id, code=code)
    row = redeem_coupon(
        session,
        coupon,
        document_type=payload.document_type,
        document_id=_document_id(payload.document_id),
        base_price=payload.base_price,
        on=payload.on,
    )
    response = CouponRedemptionOut(
        code=coupon.code,
        campaign=coupon.campaign,
        document_type=row.document_type,
        document_id=str(row.document_id),
        discount_amount=_money(row.discount_amount),
    )
    session.commit()
    return response
