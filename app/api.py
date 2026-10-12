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
import logging
import os
import uuid
from collections.abc import Iterator
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    create_engine,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import config, delivery, observability
from app.audit import set_actor
from app.company import (
    CREDIT_CHECK_MODES,
    Company,
    UnknownCompanyError,
    UnknownCreditCheckMode,
    company_base_currency,
    credit_check_mode_of,
    set_cash_drawer_required,
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
from app.procurement.orders import (
    PurchaseOrder,
    order_by_number as purchase_order_by_number,
)
from app.procurement.portal import (
    CAPABILITY as portal_capability,
)
from app.procurement.portal import (
    NoPortalAccountError as NoPortalAccount,
    NotYoursError as PortalNotYours,
    PortalError,
    account_for as portal_account_for,
    acknowledge_order_for as portal_acknowledge,
    documents_for as portal_documents,
    respond_to_rfq as portal_respond,
    submit_invoice as portal_submit_invoice,
)
from app.procurement.rfq import (
    RfqError,
    invited as rfq_invited,
    issue_rfq,
    record_response as record_rfq_response,
    responses as rfq_responses,
    rfq_by_number,
)
from app.catalogue import entries as catalogue_entries  # noqa: E402
from app.reporting import (
    DEFAULT_CAPABILITY as DEFAULT_REPORT_CAPABILITY,
    MONTH as DEFAULT_REPORT_PERIOD,
    PeriodError,
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
from app.ar.aging import AgingError
from app.ar.dunning import DunningError
from app.ar.exposure import ExposureError
from app.ar.gateway import GatewayError
from app.ar.invoices import InvoiceError
from app.ar.reconciliation import ReconciliationError as ArReconciliationError
from app.analytics import UnknownTileError, WindowError, dashboard as dashboard_tiles
from app.ar.recurring import RecurringError
from app.sales.tax import TaxError
from app.pos.drawer import DrawerError
from app.pos.drawer import record_movement
from app.pos.offline import (
    report_of as sync_report_rows,
    reports_of as sync_reports,
    sync_sales,
)
from app.pos.reconciliation import reconcile as reconcile_pos
from app.pos.reports import ReportError, day_report, shift_report, void_sale
from app.pos.sales import (
    PosError,
    complete_sale,
    open_sale,
    receipt as receipt_rows,
    sale_by_number as pos_sale_by_number,
    sale_change,
    sale_tendered,
    scan,
    tender,
)
from app.pos.shifts import (
    ShiftError,
    close_shift,
    current_shift,
    open_shift,
    PosShift,
)
from app.hr.employees import EMPLOYEE_ENTITY
from app.hr.org import org_chart
from app.security import (
    AccessDenied,
    Role,
    assign as assign_role,
    capabilities as capabilities_held,
    define_role,
    grant,
    hidden_fields,
    readable_fields,
    reject_restricted_fields,
    require,
    restrict,
    roles_for,
)
from app import auth, identity
from app.bootstrap import (
    ADMINISTRATOR as ADMINISTRATOR_ROLE,
    BootstrapError,
    create_company as installer,
    declared_capabilities,
)
from app.company import (
    UnknownCompanyStatus,
    set_company_status,
)
from app.identity import (
    CompanySuspended,
    IdentityError,
    MfaError,
    NotAMemberError,
    RefusedSignIn,
    UnknownCompanyCode,
    UserExistsError,
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


# --- Identity, from the verified session -------------------------------------
# T-7.AUTH.02 replaced the two request headers T-0.API.01 documented as a placeholder
# ("any caller may claim any actor"). Company and actor now come from a session row
# minted against a verified credential, and the ceiling that comment carried is closed
# rather than restated. Nothing downstream moved: a session resolves to the same login
# string (`actor`) that roles, field restrictions, the trail and every endpoint already
# keyed on.


class RequestContext(BaseModel):
    """What one request carries: its company, its actor and its session."""

    model_config = {"arbitrary_types_allowed": True}

    company_id: uuid.UUID
    actor: str
    session: Session
    # The session this request came in on, so a route can revoke exactly this one.
    session_token: str | None = None
    # The resolved sign-in itself, for the few routes that need more than the company
    # and the subject (the session flow and the company switch).
    resolved: identity.SignIn | None = None


def _resolved(context: RequestContext) -> identity.SignIn:
    """The sign-in behind this request. Every request has one — `context` refuses without."""
    if context.resolved is None:  # pragma: no cover - context always resolves one
        raise ApiError(401, auth.UNAUTHENTICATED_CODE, "this request carries no session")
    return context.resolved


def _bound_context(session: Session, resolved: identity.SignIn, token: str | None):
    """Bind a request's session to the company and actor the session resolved to."""
    scope_to_company(session, resolved.company.id)
    set_actor(session, resolved.subject)
    return RequestContext(
        company_id=resolved.company.id,
        actor=resolved.subject,
        session=session,
        session_token=token,
        resolved=resolved,
    )


def context(request: Request) -> Iterator[RequestContext]:
    """Bind a session to the identity the request's cookie proves, then close it.

    Every refusal here is raised by :mod:`app.auth` and answered by the handlers
    below: `401` for no session, `401 mfa_required` for one that still owes a second
    factor, `403 company_suspended` for a company that is not trading.
    """
    with Session(_engine()) as session:
        resolved = auth.current_identity(session, request)
        yield _bound_context(session, resolved, auth.request_token(request))


def pending_context(request: Request) -> Iterator[RequestContext]:
    """The same binding, for a session that still owes its second factor.

    Used by the MFA route alone: a half-finished sign-in must be able to finish, and
    may reach nothing else.
    """
    with Session(_engine()) as session:
        resolved = auth.current_identity(session, request, allow_pending_mfa=True)
        yield _bound_context(session, resolved, auth.request_token(request))


def suspended_context(request: Request) -> Iterator[RequestContext]:
    """The binding for the one route a suspended company may still reach.

    Reinstatement has to be possible from inside the thing that was suspended, or
    suspension is a one-way door. The route that uses this still asks for the
    capability it needs; only the suspension refusal is lifted.
    """
    with Session(_engine()) as session:
        resolved = auth.current_identity(session, request, allow_suspended=True)
        yield _bound_context(session, resolved, auth.request_token(request))


Context = Annotated[RequestContext, Depends(context)]
PendingContext = Annotated[RequestContext, Depends(pending_context)]
SuspendedContext = Annotated[RequestContext, Depends(suspended_context)]


def _over_https(request: Request) -> bool:
    """Whether the request arrived over TLS — what decides the cookie's `Secure` flag.

    Read from the forwarded scheme first, because behind the reverse proxy of
    T-9.EDGE.01 the connection to this process is plain HTTP while the connection from
    the browser is not.
    """
    forwarded = request.headers.get("x-forwarded-proto", "")
    if forwarded:
        return forwarded.split(",")[0].strip().lower() == "https"
    return request.url.scheme == "https"


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
    # T-7.ADMIN.03: whether this company is trading. Published so a shell can say so
    # rather than showing a screen that will refuse everything it asks for.
    status: str
    # T-3.SALES.04: the credit-check policy this company has stated, or null when it has
    # stated none — which the order-time check treats differently from "off".
    credit_check_mode: str | None = None
    # T-3.POS.03: whether this company's tills must trade inside an open shift. Null is
    # "not stated", which reads as "no drawer management" — the ordinary shop.
    cash_drawer_required: bool | None = None


def _company_out(company: Company) -> CompanyOut:
    """A company as the boundary publishes it.

    One place, because there is more than one route that answers with a company, and
    T-7.ADMIN.03 added `status` to that payload and missed two of them — a field added to
    five of six responses is a 500 on the sixth, which is how this was found.
    """
    return CompanyOut(
        id=str(company.id),
        code=company.code,
        name=company.name,
        base_currency=company.base_currency,
        fiscal_year_start_month=company.fiscal_year_start_month,
        status=company.status,
        credit_check_mode=company.credit_check_mode,
        cash_drawer_required=company.cash_drawer_required,
    )


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
    # What one run covers: `day`, `month`, `quarter` or `year` (T-6.ANALYTICS.02).
    period: str | None = None


class ReportOut(BaseModel):
    code: str
    name: str
    schedule: str
    period: str
    capability: str
    recipients: list[str]


class CatalogueEntryOut(BaseModel):
    """One report an operator can schedule: what it covers, over how long, and who may."""

    code: str
    name: str
    kind: str
    scope: str
    capability: str
    schedule: str
    period: str
    built: bool


class CatalogueOut(BaseModel):
    entries: list[CatalogueEntryOut]


class WithheldRecipientOut(BaseModel):
    """A recipient the run did not deliver to, and why — a subject without the capability."""

    recipient: str
    reason: str


class ReportRunOut(BaseModel):
    code: str
    status: str
    period: date | None = None
    produced: dict[str, Any] | None = None
    error: str | None = None
    delivered_to: list[str] | None = None
    withheld_recipients: list[WithheldRecipientOut] | None = None


class DashboardTileOut(BaseModel):
    """One figure, what it has to agree with, and (when asked) the rows behind it."""

    code: str
    label: str
    capability: str
    figures: dict[str, Any]
    reconciled_to: dict[str, Any]
    basis_label: str
    # Present only for the tile the request asked to drill into: the rows the figure was
    # computed from. Naming a field nobody asked for would make every dashboard call carry
    # every ledger it reads.
    basis: list[dict[str, Any]] | None = None
    # How many rows stand behind the figure, and whether `basis` was cut to fit: a reader
    # closing a dashboard onto a ledger of ten thousand invoices needs to know.
    basis_rows: int | None = None
    basis_truncated: bool | None = None


class DashboardWithheldOut(BaseModel):
    """A tile this subject may not see: named, with the refusal's own sentence and no figure."""

    code: str
    label: str
    capability: str
    reason: str


class DashboardOut(BaseModel):
    as_of: date
    start: date
    generated_at: datetime
    drill_down: str | None = None
    tiles: list[DashboardTileOut]
    withheld: list[DashboardWithheldOut]


class HealthOut(BaseModel):
    status: str
    version: str


class ReadinessCheckOut(BaseModel):
    """One dependency, its state, and a reason safe to publish (T-8.OBS.01)."""

    name: str
    ok: bool
    detail: str
    # The revision the database reports, when the check is the migration one.
    revision: str | None = None


class ReadinessOut(BaseModel):
    """What `/api/v1/ready` answers: whether this instance can serve, and each check."""

    status: str
    version: str
    checks: list[ReadinessCheckOut]


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
    other's keys and one key cannot answer twice with different results. The row records
    the **method, path and actor** that used the key, not only the body: a key is one
    caller's retry token, and a replay is answered only to the same caller asking the same
    endpoint — otherwise a key would hand one caller another's document. Written in the
    caller's transaction: a document that fails stores no key, and a retried document finds
    the key it wrote.
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
    # Whose retry this key is. Compared on replay, so a second caller presenting someone
    # else's key is refused instead of being shown the answer to a request they did not make.
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
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
_READY_ENGINE = None


def _engine():
    """The application's engine, from the configuration read at start.

    T-8.CFG.01: the URL is validated when the process starts (``app/config.py``'s
    :data:`~app.config.SETTINGS`), so it is not read here at all — this used to read
    ``DATABASE_URL`` on the first request and answer that request with a `500`
    (finding 26 of the 2026-10-10 audit). A missing or malformed URL stops the process
    instead, naming the variable.
    """
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = create_engine(config.SETTINGS.database_url, pool_pre_ping=True)
    return _ENGINE


def _readiness_engine():
    """The engine the readiness probe uses: its own connection, on a stated timeout.

    Separate from the application's pool on purpose — a probe must not wait behind a queue
    of requests, and it must give up in ``READINESS_TIMEOUT_SECONDS`` (§9) rather than
    whenever libpq's own default expires. ``statement_timeout`` is what makes "the database
    answers" a claim with a deadline: a database that accepts a connection and then hangs
    would otherwise answer the probe later than the caller was willing to wait.
    """
    global _READY_ENGINE
    if _READY_ENGINE is None:
        seconds = config.SETTINGS.readiness_timeout_seconds
        _READY_ENGINE = create_engine(
            config.SETTINGS.database_url,
            pool_pre_ping=True,
            pool_size=1,
            max_overflow=0,
            connect_args={
                "connect_timeout": seconds,
                "options": f"-c statement_timeout={seconds * 1000}",
            },
        )
    return _READY_ENGINE


app = FastAPI(
    title="ERP API",
    version="1.0.0",
    summary="Modular ERP platform — core financial, inventory and operations API.",
    openapi_url=f"{BASE}/openapi.json",
    responses=ERROR_RESPONSES,
)

# --- Start (T-8.OBS.01, T-8.DELIVERY.01) --------------------------------------
# Importing this module *is* the start: uvicorn imports it before it binds, so everything
# below has happened before the first request is accepted — the log format, the request
# middleware, the outbound channels a configured install can actually send on, and the
# configuration itself. `app/config.py` has already refused a broken environment by here.
observability.configure(config.SETTINGS)
app.add_middleware(observability.RequestContext)
delivery.register_configured()
observability.log(
    "info",
    "the application is starting",
    version=API_VERSION,
    **{key: value for key, value in config.describe().items()},
)


@app.exception_handler(ApiError)
async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
    return _error(exc.status_code, exc.code, exc.message, exc.details)


# T-7.AUTH.02 — the three refusals the session boundary raises, answered in the one
# shape like every other refusal in this tree.


@app.exception_handler(auth.NotAuthenticated)
async def _not_authenticated(_request: Request, exc: auth.NotAuthenticated) -> JSONResponse:
    return _error(401, auth.UNAUTHENTICATED_CODE, str(exc))


@app.exception_handler(auth.MfaPending)
async def _mfa_pending(_request: Request, exc: auth.MfaPending) -> JSONResponse:
    return _error(401, auth.MFA_REQUIRED_CODE, str(exc))


@app.exception_handler(auth.SuspendedCompany)
async def _suspended_company(_request: Request, exc: auth.SuspendedCompany) -> JSONResponse:
    return _error(403, auth.SUSPENDED_COMPANY_CODE, str(exc))


# T-7.AUTH.01 … T-7.ADMIN.03 — identity and administration. A sign-in that was refused
# answers like any other refusal, and always with the same uninformative message: a
# caller cannot tell a wrong password from a login that does not exist.


@app.exception_handler(RefusedSignIn)
async def _refused_sign_in(_request: Request, exc: RefusedSignIn) -> JSONResponse:
    return _error(401, auth.UNAUTHENTICATED_CODE, str(exc))


@app.exception_handler(MfaError)
async def _mfa_error(_request: Request, exc: MfaError) -> JSONResponse:
    return _error(401, "invalid_second_factor", str(exc))


@app.exception_handler(UserExistsError)
async def _user_exists(_request: Request, exc: UserExistsError) -> JSONResponse:
    return _error(409, "user_exists", str(exc))


@app.exception_handler(NotAMemberError)
async def _not_a_member(_request: Request, exc: NotAMemberError) -> JSONResponse:
    return _error(403, "not_a_member", str(exc))


@app.exception_handler(CompanySuspended)
async def _company_suspended(_request: Request, exc: CompanySuspended) -> JSONResponse:
    """A sign-in into a suspended company answers like any other suspended-company call."""
    return _error(403, auth.SUSPENDED_COMPANY_CODE, str(exc))


@app.exception_handler(UnknownCompanyCode)
async def _unknown_company_code(_request: Request, exc: UnknownCompanyCode) -> JSONResponse:
    return _error(404, "unknown_company", str(exc))


@app.exception_handler(UnknownCompanyStatus)
async def _unknown_company_status(
    _request: Request, exc: UnknownCompanyStatus
) -> JSONResponse:
    return _error(422, "unknown_company_status", str(exc))


@app.exception_handler(IdentityError)
async def _identity_error(_request: Request, exc: IdentityError) -> JSONResponse:
    return _error(422, "identity_error", str(exc))


@app.exception_handler(BootstrapError)
async def _bootstrap_error(_request: Request, exc: BootstrapError) -> JSONResponse:
    """A company that cannot be provisioned as asked — a taken code, an unconfirmed
    fiscal calendar — is refused with the pack's own reason, not a 500."""
    return _error(422, "provisioning_refused", str(exc))


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


@app.exception_handler(NoPortalAccount)
async def _no_portal_account(_request: Request, exc: NoPortalAccount) -> JSONResponse:
    return _error(403, "no_portal_account", str(exc))


@app.exception_handler(PortalError)
async def _portal_error(_request: Request, exc: PortalError) -> JSONResponse:
    return _error(422, "portal_error", str(exc))


@app.exception_handler(PosError)
async def _pos_error(_request: Request, exc: PosError) -> JSONResponse:
    return _error(422, "pos_error", str(exc))


@app.exception_handler(DrawerError)
async def _drawer_error(_request: Request, exc: DrawerError) -> JSONResponse:
    return _error(422, "drawer_error", str(exc))


@app.exception_handler(ShiftError)
async def _shift_error(_request: Request, exc: ShiftError) -> JSONResponse:
    return _error(422, "shift_error", str(exc))


@app.exception_handler(ReportError)
async def _report_error(_request: Request, exc: ReportError) -> JSONResponse:
    return _error(422, "report_error", str(exc))


@app.exception_handler(InvoiceError)
async def _invoice_error(_request: Request, exc: InvoiceError) -> JSONResponse:
    return _error(422, "invoice_error", str(exc))


@app.exception_handler(GatewayError)
async def _gateway_error(_request: Request, exc: GatewayError) -> JSONResponse:
    return _error(422, "gateway_error", str(exc))


@app.exception_handler(TaxError)
async def _tax_error(_request: Request, exc: TaxError) -> JSONResponse:
    return _error(422, "tax_error", str(exc))


@app.exception_handler(AgingError)
async def _aging_error(_request: Request, exc: AgingError) -> JSONResponse:
    return _error(422, "aging_error", str(exc))


@app.exception_handler(ExposureError)
async def _exposure_error(_request: Request, exc: ExposureError) -> JSONResponse:
    return _error(422, "exposure_error", str(exc))


@app.exception_handler(DunningError)
async def _dunning_error(_request: Request, exc: DunningError) -> JSONResponse:
    return _error(422, "dunning_error", str(exc))


@app.exception_handler(RecurringError)
async def _recurring_error(_request: Request, exc: RecurringError) -> JSONResponse:
    return _error(422, "recurring_error", str(exc))


@app.exception_handler(ArReconciliationError)
async def _ar_reconciliation_error(
    _request: Request, exc: ArReconciliationError
) -> JSONResponse:
    return _error(422, "reconciliation_error", str(exc))


@app.exception_handler(LocationError)
async def _location_error(_request: Request, exc: LocationError) -> JSONResponse:
    return _error(422, "location_error", str(exc))


@app.exception_handler(IncompleteSourceError)
async def _incomplete_source(_request: Request, exc: IncompleteSourceError) -> JSONResponse:
    return _error(422, "incomplete_source", str(exc))


@app.exception_handler(Exception)
async def _unexpected(_request: Request, exc: Exception) -> JSONResponse:
    """A bug answers like any other refusal, and says nothing about itself.

    Registered so a client's error handling needs one branch. What changed with
    T-8.OBS.01 (finding 2 of the 2026-10-10 audit): the body used to carry
    ``f"{type(exc).__name__}: {exc}"``, which is the exception's class and message —
    a table name, a column, a path, whatever the failure happened to say — handed to
    whoever asked. The detail now goes to the **log** (with the traceback), and the caller
    gets the correlation id (`X-Request-Id`, echoed by the middleware) to quote in a
    report. `tests/check_error_hygiene.py` drives a forced failure and requires that no
    exception class, message, path or SQL appears in the body.
    """
    request_id = observability.carried_request_id(_request)
    logging.getLogger("erp").error(
        "unhandled exception: %s", type(exc).__name__, exc_info=exc, extra={"erp_request_id": request_id}
    )
    observability.METRICS.inc("erp_errors_total", {"kind": "unhandled"})
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "internal_error",
                "message": (
                    "the request could not be completed; it has been logged under the"
                    " correlation id in the X-Request-Id header"
                ),
                "details": [{"request_id": request_id}],
            }
        },
        headers={observability.REQUEST_ID_HEADER: request_id},
    )


@app.get(f"{BASE}/health", response_model=HealthOut, tags=["platform"])
def health() -> HealthOut:
    """Liveness: the process is up and serving — nothing about its dependencies.

    A liveness probe must not fail for a reason a restart cannot fix, so this touches no
    database and answers as long as the process runs. Readiness is the other half of
    T-8.OBS.01's split (`/api/v1/ready`), and it is the one a load balancer should ask
    before sending traffic.
    """
    return HealthOut(status="ok", version=API_VERSION)


@app.get(
    f"{BASE}/ready",
    response_model=ReadinessOut,
    responses={
        200: {"model": ReadinessOut, "description": "the instance can serve"},
        503: {"model": ReadinessOut, "description": "the instance cannot serve yet"},
    },
    tags=["platform"],
)
def ready() -> Response:
    """Readiness: the database answers and the schema is at the revision this build expects.

    Answers `503` (not `500`) when it is not ready, because "not ready yet" is a state a
    deployment moves out of, and a load balancer needs the distinction. The body names what
    is not ready and never why — the exception is in the log, and this route is public
    (T-8.OBS.01, `SECURITY-REVIEW.md`'s access matrix).
    """
    ok, checks = observability.readiness(_readiness_engine())
    observability.METRICS.set(observability.READY, 1.0 if ok else 0.0)
    payload = ReadinessOut(
        status="ok" if ok else "unavailable",
        version=API_VERSION,
        checks=[ReadinessCheckOut(**check) for check in checks],
    )
    return JSONResponse(status_code=200 if ok else 503, content=payload.model_dump(mode="json"))


@app.get(
    f"{BASE}/metrics",
    response_class=PlainTextResponse,
    responses={200: {"content": {"text/plain": {}}, "description": "Prometheus text format"}},
    tags=["platform"],
)
def metrics() -> Response:
    """The counters this process reports, in Prometheus' text exposition format.

    `metrics_enabled=false` answers `404` rather than serving an empty document: a scrape
    that is switched off should look switched off, and a `404` is what an operator's scrape
    config will report as a failure to collect. The registry is this process's own
    (`app/observability.py` states the ceiling): no cross-process aggregation, so a scrape
    reads the web container's numbers and the worker's separately.
    """
    if not config.SETTINGS.metrics_enabled:
        return _error(404, "not_found", "metrics are disabled on this instance")
    observability.METRICS.set(
        observability.BUILD_INFO, 1.0, {"version": API_VERSION}
    )
    return PlainTextResponse(
        observability.METRICS.render(), media_type="text/plain; version=0.0.4; charset=utf-8"
    )


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
    return _company_out(company)


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
        period=payload.period or DEFAULT_REPORT_PERIOD,
    )
    session.commit()
    return ReportOut(
        code=definition.code,
        name=definition.name,
        schedule=definition.schedule,
        period=definition.period,
        capability=definition.capability,
        recipients=list(definition.recipients),
    )


@app.get(f"{BASE}/reports/catalogue", response_model=CatalogueOut, tags=["reporting"])
def read_report_catalogue(context: Context) -> CatalogueOut:
    """What can be scheduled, with each report's scope, period and capability.

    Read rather than delivered: an operator scheduling the month-end pack needs to know what
    the platform produces and what it takes to see each one (T-6.ANALYTICS.02). What is
    *registered* for this company is `report_definition`'s own rows.
    """
    require(
        context.session,
        company_id=context.company_id,
        subject=context.actor,
        capability="report.read",
        entity="report_catalogue",
    )
    return CatalogueOut(entries=[CatalogueEntryOut(**entry) for entry in catalogue_entries()])


@app.post(f"{BASE}/reports/{{code}}/run", response_model=ReportRunOut, tags=["reporting"])
def run_report_now(
    code: str, context: Context, period: Annotated[date | None, Query()] = None
) -> ReportRunOut:
    """Run one registered report for this company and period, and deliver it to its recipients.

    The capability the definition carries is asked for by the framework itself
    (T-0.REPORT.01), so a caller who may not see the report is refused before
    anything is built — and a builder that fails leaves a `failed` run, not a
    silent absence. Running a period that was already delivered leaves a `skipped`
    run and sends nothing: the same report for the same period does not arrive twice,
    while a period that *failed* is retried as an ordinary run.
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
    try:
        run_row = run_report(session, definition, actor=context.actor, period=period)
    except PeriodError as exc:
        raise ApiError(422, "invalid_period", str(exc)) from exc
    session.commit()
    return ReportRunOut(
        code=code,
        status=run_row.status,
        period=run_row.period,
        produced=run_row.produced,
        error=run_row.error,
        delivered_to=list(run_row.delivered_to) if run_row.delivered_to else None,
        withheld_recipients=(
            [WithheldRecipientOut(**row) for row in run_row.withheld_recipients]
            if run_row.withheld_recipients
            else None
        ),
    )


# --- T-6.ANALYTICS.01: the dashboard ------------------------------------------


@app.get(f"{BASE}/dashboard", response_model=DashboardOut, tags=["reporting"])
def read_dashboard(
    context: Context,
    as_of: Annotated[date | None, Query()] = None,
    start: Annotated[date | None, Query()] = None,
    drill_down: Annotated[str | None, Query()] = None,
) -> DashboardOut:
    """The tiles this subject may see, with the records behind one of them.

    Read fresh on every request and stamped with its own `generated_at`, so what a reader
    sees is the ledger as it stands rather than a snapshot from last night. A tile the
    caller may not see is absent and named in `withheld` with the refusal's sentence — the
    figure is not zeroed — and the refusal is on the trail like every other. Asking for a
    tile's `drill_down` returns the rows that figure was computed from, in one step.
    """
    try:
        answer = dashboard_tiles(
            context.session,
            company_id=context.company_id,
            subject=context.actor,
            as_of=as_of,
            start=start,
            drill_down=drill_down,
        )
    except UnknownTileError as exc:
        raise ApiError(404, "no_dashboard_tile", str(exc)) from exc
    except WindowError as exc:
        raise ApiError(422, "invalid_window", str(exc)) from exc
    return DashboardOut(**answer)


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
        if (seen.method, seen.path, seen.actor) != ("POST", path, context.actor):
            raise ApiError(
                409,
                "idempotency_key_reused",
                f"key {idempotency_key!r} belongs to another caller's request",
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
            actor=context.actor,
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

    Since T-3.AR.06 an **unstated exposure means the live one**: the service computes
    it across open invoices, unbilled orders and on-account receipts in the order's
    own currency. A caller may still state a number, and it is validated and recorded
    as it stands — but stating nothing is what asks for the figure the credit
    statement would show. `acknowledge_breach` is the acknowledgement `warn` mode
    requires: an explicit act, so a breach is never accepted by the mere act of asking.
    """

    # Nullable rather than merely defaulted: T-0.API.01's convention is that an
    # optional field says so in the contract, so "compute it" is an explicit null and
    # not an implicit absence.
    exposure: str | None = None
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
    response = _company_out(company)
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

    A body that states no exposure is judged against the customer's **live** exposure
    (T-3.AR.06) — the same figure a credit statement shows — while a stated one is
    validated and recorded as it stands. The refusal a `block` breach produces is the
    point of the endpoint: it is the one place an order stops being an intention.
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


# --- T-3.POS.01 / .02 / .03 / .04 / .05 — the till ------------------------------
# A till has no database of its own (T-0.API.02): it rings a sale up through these
# endpoints, so the scan, the price, the posting and the receipt are the same code
# whether they were reached from a keyboard or from the API. The capabilities are
# `pos.sell`, `pos.drawer` and `pos.shift`, so a company can let somebody use the
# drawer without letting them change a shift, or vice versa.


class PosSaleIn(BaseModel):
    """Ring up a sale: which till, which location, and for whom (if anybody)."""

    number: str = Field(min_length=1, max_length=32)
    terminal: str = Field(min_length=1, max_length=32)
    location_code: str = Field(min_length=1, max_length=32)
    customer_code: str | None = None
    currency: str | None = None
    sold_on: date | None = None


class PosScanIn(BaseModel):
    """Scan one code onto an open sale, stating the shelf price it is discounted from."""

    barcode: str = Field(min_length=1, max_length=64)
    base_price: str
    quantity: str | None = None
    uom: str | None = None
    campaign: str | None = None


class PosTenderIn(BaseModel):
    """Take one payment: what kind, how much was handed over, and its reference."""

    tender_type: str
    amount: str
    reference: str | None = None


class PosVoidIn(BaseModel):
    """Void an abandoned basket or refund a completed sale, saying who decided and why."""

    reason: str = Field(min_length=1, max_length=200)


# --- T-6.PORTAL.01 — the supplier's own door ---------------------------------


class PortalLineIn(BaseModel):
    """One line a supplier states: the RFQ's own line number, or an invoice line."""

    line_no: int = Field(ge=1)
    unit_price: str
    quantity: str | None = None
    description: str | None = None


class PortalResponseIn(BaseModel):
    """A supplier's answer to an RFQ, line by line."""

    received_on: date
    lines: list[PortalLineIn] = Field(min_length=1)
    currency: str | None = None
    lead_time_days: int | None = None
    valid_until: date | None = None
    note: str | None = None


class PortalInvoiceIn(BaseModel):
    """A supplier's invoice against an order, as it is submitted."""

    number: str = Field(min_length=1, max_length=32)
    invoice_date: date
    supplier_reference: str = Field(min_length=1, max_length=64)
    lines: list[PortalLineIn] = Field(min_length=1)
    order_number: str | None = None


class PortalDocumentsOut(BaseModel):
    """What this supplier may see: its RFQs, its orders and its invoices."""

    documents: dict


class PortalWriteOut(BaseModel):
    """What a portal write recorded, as the supplier reads it back."""

    recorded: dict


# --- T-6.OFFLINE.01 — the queue a terminal replays when it is back online ------


class PosSyncLineIn(BaseModel):
    """One queued line as the till rang it: the code, the shelf price and how many."""

    barcode: str = Field(min_length=1, max_length=64)
    base_price: str
    quantity: str | None = None
    uom: str | None = None
    campaign: str | None = None


class PosSyncTenderIn(BaseModel):
    """One queued payment: what kind, how much, and its reference."""

    tender_type: str
    amount: str
    reference: str | None = None


class PosSyncSaleIn(BaseModel):
    """One sale rung up while the terminal was away, under the number the till gave it."""

    # The till's own number, which is what makes a re-sent batch land once: see the
    # endpoint and `app.pos.offline`.
    number: str = Field(min_length=1, max_length=32)
    sold_on: date | None = None
    customer_code: str | None = None
    currency: str | None = None
    lines: list[PosSyncLineIn] = Field(min_length=1)
    tenders: list[PosSyncTenderIn] = Field(min_length=1)


class PosSyncIn(BaseModel):
    """A terminal's whole queue, and the policy it was trading under offline."""

    terminal: str = Field(min_length=1, max_length=32)
    location_code: str = Field(min_length=1, max_length=32)
    # Whether the till was told it may sell beyond what its cached stock showed — stated
    # on the report, because a shortfall reads differently under each policy. Absent
    # means the terminal stated no policy, which is the conservative reading: it was not
    # allowed to oversell.
    oversell_allowed: bool | None = None
    sales: list[PosSyncSaleIn] = Field(min_length=1)


class PosSyncOut(BaseModel):
    """The run's one reconciliation report: its counts, its outcomes and its differences."""

    report: dict


class PosSyncReportsOut(BaseModel):
    """A terminal's sync runs, in the order it made them — one per run, never per sale."""

    reports: list[dict]


class PosLineOut(BaseModel):
    line_no: int
    description: str
    barcode: str | None
    quantity: str
    uom: str
    unit_price: str
    tax: str
    tax_rule: str | None
    rule: str | None


class PosTenderOut(BaseModel):
    tender_no: int
    tender_type: str
    tendered: str
    applied: str
    reference: str | None


class PosSaleOut(BaseModel):
    number: str
    terminal: str
    status: str
    sold_on: date
    currency: str
    shift: str | None
    net: str
    tax: str
    total: str
    tendered: str
    change: str
    lines: list[PosLineOut]
    tenders: list[PosTenderOut]


class PosReceiptOut(BaseModel):
    receipt: dict


class PosShiftIn(BaseModel):
    """Open a shift: the terminal and the float the drawer starts with."""

    terminal: str = Field(min_length=1, max_length=32)
    opening_float: str | None = None
    on: date | None = None


class PosShiftCloseIn(BaseModel):
    """Close a shift on a count, with the reason a variance needs."""

    counted_cash: str
    reason: str | None = None


class PosShiftOut(BaseModel):
    id: str
    terminal: str
    status: str
    opened_on: date
    opening_float: str
    counted: str | None
    expected: str | None
    variance: str | None
    variance_reason: str | None


class DrawerMovementIn(BaseModel):
    """One note in or out of the drawer, with the reason it moved."""

    terminal: str = Field(min_length=1, max_length=32)
    movement_type: str
    amount: str
    reason: str = Field(min_length=1, max_length=200)
    on: date | None = None


class DrawerMovementOut(BaseModel):
    id: str
    terminal: str
    movement_type: str
    amount: str
    reason: str
    moved_on: date
    actor: str


class PosReportOut(BaseModel):
    report: dict


class CashDrawerPolicyIn(BaseModel):
    """Whether this company's tills must trade inside an open shift (null = not stated)."""

    required: bool | None = None


def _pos_sale_out(sale) -> PosSaleOut:
    return PosSaleOut(
        number=sale.number,
        terminal=sale.terminal,
        status=sale.status,
        sold_on=sale.sold_on,
        currency=sale.currency,
        shift=None if sale.shift_id is None else str(sale.shift_id),
        net=_money(sale.net_amount),
        tax=_money(sale.tax_amount),
        total=_money(sale.gross_amount),
        tendered=_money(sale_tendered(sale)),
        change=_money(sale_change(sale)),
        lines=[
            PosLineOut(
                line_no=line.line_no,
                description=line.description,
                barcode=line.barcode,
                quantity=_money(line.quantity),
                uom=line.uom,
                unit_price=_money(line.unit_price),
                tax=_money(line.tax_amount),
                tax_rule=line.tax_rule_code,
                rule=line.rule_code,
            )
            for line in sale.lines
        ],
        tenders=[
            PosTenderOut(
                tender_no=row.tender_no,
                tender_type=row.tender_type,
                tendered=_money(row.tendered),
                applied=_money(row.applied),
                reference=row.reference,
            )
            for row in sale.tenders
        ],
    )


def _pos_shift_out(shift) -> PosShiftOut:
    return PosShiftOut(
        id=str(shift.id),
        terminal=shift.terminal,
        status=shift.status,
        opened_on=shift.opened_on,
        opening_float=_money(shift.opening_float),
        counted=None if shift.counted_cash is None else _money(shift.counted_cash),
        expected=None if shift.expected_cash is None else _money(shift.expected_cash),
        variance=None if shift.variance is None else _money(shift.variance),
        variance_reason=shift.variance_reason,
    )


@app.post(f"{BASE}/pos/sales", response_model=PosSaleOut, status_code=201, tags=["pos"])
def open_pos_sale(payload: PosSaleIn, context: Context) -> PosSaleOut:
    """Start a basket at one till. Nothing has moved: no stock, no posting."""
    session = context.session
    require(session, company_id=context.company_id, subject=context.actor,
            capability="pos.sell", entity="pos_sale")
    location = location_by_code(
        session, company_id=context.company_id, code=payload.location_code
    )
    customer = (
        customer_by_code(session, company_id=context.company_id, code=payload.customer_code)
        if payload.customer_code
        else None
    )
    sale = open_sale(
        session,
        company_id=context.company_id,
        number=payload.number,
        terminal=payload.terminal,
        location=location,
        customer=customer,
        currency=payload.currency,
        sold_on=payload.sold_on,
    )
    response = _pos_sale_out(sale)
    session.commit()
    return response


@app.post(f"{BASE}/pos/sales/{{number}}/scan", response_model=PosSaleOut, tags=["pos"])
def scan_pos_sale(number: str, payload: PosScanIn, context: Context) -> PosSaleOut:
    """Scan a code onto the basket: it resolves to an item, and the engine prices it."""
    session = context.session
    require(session, company_id=context.company_id, subject=context.actor,
            capability="pos.sell", entity="pos_sale")
    sale = pos_sale_by_number(session, company_id=context.company_id, number=number)
    scan(
        session,
        sale,
        barcode=payload.barcode,
        base_price=payload.base_price,
        quantity=payload.quantity or 1,
        uom=payload.uom,
        campaign=payload.campaign,
    )
    response = _pos_sale_out(sale)
    session.commit()
    return response


@app.post(f"{BASE}/pos/sales/{{number}}/tender", response_model=PosSaleOut, tags=["pos"])
def tender_pos_sale(number: str, payload: PosTenderIn, context: Context) -> PosSaleOut:
    """Take one payment, recording what was handed over and what it covered."""
    session = context.session
    require(session, company_id=context.company_id, subject=context.actor,
            capability="pos.sell", entity="pos_sale")
    sale = pos_sale_by_number(session, company_id=context.company_id, number=number)
    tender(session, sale, tender_type=payload.tender_type, amount=payload.amount,
           reference=payload.reference)
    response = _pos_sale_out(sale)
    session.commit()
    return response


@app.post(f"{BASE}/pos/sales/{{number}}/complete", response_model=PosSaleOut, tags=["pos"])
def complete_pos_sale(number: str, context: Context) -> PosSaleOut:
    """Finish the sale: issue its stock and post its revenue.

    Refused unless the tenders cover it, and — where the company manages drawers —
    unless the terminal has an open shift.
    """
    session = context.session
    require(session, company_id=context.company_id, subject=context.actor,
            capability="pos.sell", entity="pos_sale")
    sale = pos_sale_by_number(session, company_id=context.company_id, number=number)
    complete_sale(session, sale)
    response = _pos_sale_out(sale)
    session.commit()
    return response


@app.get(f"{BASE}/pos/sales/{{number}}/receipt", response_model=PosReceiptOut, tags=["pos"])
def pos_receipt(number: str, context: Context) -> PosReceiptOut:
    """The receipt, rebuilt from the stored sale — a reprint is what was handed over."""
    session = context.session
    require(session, company_id=context.company_id, subject=context.actor,
            capability="pos.read", entity="pos_sale")
    sale = pos_sale_by_number(session, company_id=context.company_id, number=number)
    return PosReceiptOut(receipt=receipt_rows(sale))


@app.post(f"{BASE}/pos/sales/{{number}}/void", response_model=PosSaleOut, tags=["pos"])
def void_pos_sale(number: str, payload: PosVoidIn, context: Context) -> PosSaleOut:
    """Void an abandoned basket, or refund a completed sale and reverse what it did."""
    session = context.session
    require(session, company_id=context.company_id, subject=context.actor,
            capability="pos.sell", entity="pos_sale")
    sale = pos_sale_by_number(session, company_id=context.company_id, number=number)
    void_sale(session, sale, reason=payload.reason, actor=context.actor)
    response = _pos_sale_out(sale)
    session.commit()
    return response


@app.post(f"{BASE}/pos/drawer-movements", response_model=DrawerMovementOut,
          status_code=201, tags=["pos"])
def record_drawer_movement(
    payload: DrawerMovementIn, context: Context
) -> DrawerMovementOut:
    """Record cash in or out of the drawer outside a sale, with its reason."""
    session = context.session
    require(session, company_id=context.company_id, subject=context.actor,
            capability="pos.drawer", entity="pos_drawer_movement")
    row = record_movement(
        session,
        company_id=context.company_id,
        terminal=payload.terminal,
        movement_type=payload.movement_type,
        amount=payload.amount,
        reason=payload.reason,
        actor=context.actor,
        moved_on=payload.on,
    )
    response = DrawerMovementOut(
        id=str(row.id), terminal=row.terminal, movement_type=row.movement_type,
        amount=_money(row.amount), reason=row.reason, moved_on=row.moved_on,
        actor=row.actor,
    )
    session.commit()
    return response


@app.post(f"{BASE}/pos/cash-drawer-policy", response_model=CompanyOut, tags=["pos"])
def set_pos_cash_drawer_policy(
    payload: CashDrawerPolicyIn, context: Context
) -> CompanyOut:
    """State, change or withdraw whether this company's tills need an open shift."""
    session = context.session
    require(session, company_id=context.company_id, subject=context.actor,
            capability="company.configure", entity="company",
            entity_id=context.company_id)
    company = session.get(Company, context.company_id)
    if company is None:
        raise ApiError(404, "company_not_found", f"no company {context.company_id}")
    set_cash_drawer_required(session, company, required=payload.required)
    response = _company_out(company)
    session.commit()
    return response


@app.post(f"{BASE}/pos/shifts", response_model=PosShiftOut, status_code=201, tags=["pos"])
def open_pos_shift(payload: PosShiftIn, context: Context) -> PosShiftOut:
    """Open a shift on a terminal with its float; a second on that till is refused."""
    session = context.session
    require(session, company_id=context.company_id, subject=context.actor,
            capability="pos.shift", entity="pos_shift")
    shift = open_shift(
        session,
        company_id=context.company_id,
        terminal=payload.terminal,
        opening_float=payload.opening_float or 0,
        actor=context.actor,
        on=payload.on,
    )
    response = _pos_shift_out(shift)
    session.commit()
    return response


@app.get(f"{BASE}/pos/shifts/current", response_model=PosShiftOut, tags=["pos"])
def current_pos_shift(terminal: str, context: Context) -> PosShiftOut:
    """The shift this terminal is trading on, or a refusal naming what is missing."""
    session = context.session
    require(session, company_id=context.company_id, subject=context.actor,
            capability="pos.read", entity="pos_shift")
    shift = current_shift(session, company_id=context.company_id, terminal=terminal)
    if shift is None:
        raise ApiError(404, "no_open_shift", f"terminal {terminal!r} is not trading")
    return _pos_shift_out(shift)


@app.post(f"{BASE}/pos/shifts/{{shift_id}}/close", response_model=PosShiftOut, tags=["pos"])
def close_pos_shift(
    shift_id: str, payload: PosShiftCloseIn, context: Context
) -> PosShiftOut:
    """Close a shift on a counted drawer, recording the expected figure and the variance."""
    session = context.session
    require(session, company_id=context.company_id, subject=context.actor,
            capability="pos.shift", entity="pos_shift", entity_id=shift_id)
    shift = _pos_shift(session, context, shift_id)
    close_shift(
        session, shift, counted_cash=payload.counted_cash, actor=context.actor,
        reason=payload.reason,
    )
    response = _pos_shift_out(shift)
    session.commit()
    return response


def _pos_shift(session: Session, context: RequestContext, shift_id: str):
    """The shift an endpoint names, or a refusal — never another company's."""
    try:
        wanted = uuid.UUID(shift_id)
    except ValueError as exc:
        raise ApiError(422, "invalid_shift_id", f"not a shift id: {shift_id!r}") from exc
    shift = session.get(PosShift, wanted)
    if shift is None or shift.company_id != context.company_id:
        raise ApiError(404, "shift_not_found", f"no shift {shift_id!r} in this company")
    return shift


@app.get(f"{BASE}/pos/z-reports/shift/{{shift_id}}", response_model=PosReportOut, tags=["pos"])
def pos_shift_report(shift_id: str, context: Context) -> PosReportOut:
    """The shift's Z-Report: sales, tax, tenders, voids, refunds and the drawer."""
    session = context.session
    require(session, company_id=context.company_id, subject=context.actor,
            capability="pos.read", entity="pos_report", entity_id=shift_id)
    shift = _pos_shift(session, context, shift_id)
    return PosReportOut(report=_jsonable(shift_report(session, shift)))


@app.get(f"{BASE}/pos/z-reports/day", response_model=PosReportOut, tags=["pos"])
def pos_day_report(on: date, context: Context) -> PosReportOut:
    """The day's Z-Report: every shift of that day, added exactly."""
    session = context.session
    require(session, company_id=context.company_id, subject=context.actor,
            capability="pos.read", entity="pos_report")
    return PosReportOut(
        report=_jsonable(day_report(session, company_id=context.company_id, on=on))
    )


@app.get(f"{BASE}/pos/reconciliation", response_model=PosReportOut, tags=["pos"])
def pos_reconciliation(
    on: date, context: Context, terminal: str | None = None
) -> PosReportOut:
    """The day's takings and stock against the ledger, per day and per terminal."""
    session = context.session
    require(session, company_id=context.company_id, subject=context.actor,
            capability="pos.read", entity="pos_sale")
    return PosReportOut(
        report=_jsonable(
            reconcile_pos(session, company_id=context.company_id, on=on, terminal=terminal)
        )
    )


# --- T-6.OFFLINE.01: the terminal's queue, replayed ---------------------------


@app.post(f"{BASE}/pos/sync", response_model=PosSyncOut, status_code=201, tags=["pos"])
def sync_pos_sales(
    payload: PosSyncIn,
    context: Context,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> JSONResponse:
    """Replay a terminal's queued sales — retry-safe by key *and* by the till's numbers.

    The whole queue is one report, and each sale in it is completed through
    T-3.POS.01's own calls, so what a synced sale posts is what an online one posts.
    A sale the location cannot fill is refused and named in the report rather than
    completing against a negative location.
    """
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="pos.sell",
        entity="pos_sync_report",
    )
    location = location_by_code(
        session, company_id=context.company_id, code=payload.location_code
    )
    fingerprint = _fingerprint(payload.model_dump(mode="json"))
    path = f"{BASE}/pos/sync"
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
                f"key {idempotency_key!r} was used for a different queue",
            )
        if (seen.method, seen.path, seen.actor) != ("POST", path, context.actor):
            raise ApiError(
                409,
                "idempotency_key_reused",
                f"key {idempotency_key!r} belongs to another caller's request",
            )
        return JSONResponse(
            status_code=seen.status_code,
            content=json.loads(seen.body),
            headers={"Idempotent-Replay": "true"},
        )

    report = sync_sales(
        session,
        company_id=context.company_id,
        terminal=payload.terminal,
        location=location,
        sales=[sale.model_dump(mode="json") for sale in payload.sales],
        oversell_allowed=bool(payload.oversell_allowed),
    )
    # The contract wraps the report (`{"report": {...}}`); the stored body is the same
    # wrapped answer, so a replay returns what the first call returned.
    body = {"report": _jsonable(sync_report_rows(report))}
    session.add(
        ApiIdempotencyKey(
            company_id=context.company_id,
            key=idempotency_key,
            method="POST",
            path=path,
            actor=context.actor,
            fingerprint=fingerprint,
            status_code=201,
            body=json.dumps(body),
        )
    )
    response = JSONResponse(status_code=201, content=body)
    session.commit()
    return response


@app.get(f"{BASE}/pos/sync/reports", response_model=PosSyncReportsOut, tags=["pos"])
def list_pos_sync_reports(
    context: Context, terminal: Annotated[str | None, Query()] = None
) -> PosSyncReportsOut:
    """The company's sync runs (one terminal's, where one is named), oldest first."""
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="pos.read",
        entity="pos_sync_report",
    )
    return PosSyncReportsOut(
        reports=[
            _jsonable(sync_report_rows(report))
            for report in sync_reports(
                session, company_id=context.company_id, terminal=terminal
            )
        ]
    )



# --- T-5.EMP.02: the org chart ------------------------------------------------
# The phase's one frontend consumer: the hierarchy is modelled in `app/hr/org.py`, and
# this is the read the chart screen draws from. Read-only on purpose — placing somebody is
# a reorganisation, and T-5.EMP.03 owns the movements that record one.


class OrgChartEntryOut(BaseModel):
    """One person's place in the hierarchy on the date asked about."""

    number: str
    name: str | None = None
    manager_number: str | None = None
    department: str | None = None
    cost_centre: str | None = None
    # 1 for the root, 2 for those reporting to it, and so on — what the chart indents by.
    depth: int
    reports: int


class OrgChartUnplacedOut(BaseModel):
    """Somebody the tree cannot draw — never placed, or their manager is not a live employee."""

    number: str
    name: str | None = None


class OrgChartOut(BaseModel):
    """The hierarchy on one date: one root, everybody under it, and who is not in the tree."""

    as_of: date
    root_number: str | None = None
    entries: list[OrgChartEntryOut]
    unplaced: list[OrgChartUnplacedOut]


# --- T-6.PORTAL.01: the supplier's own documents -----------------------------


def _portal_scope(session: Session, context: RequestContext):
    """The supplier this request acts for, and the capability that lets it in.

    Both are needed and neither is implied by the other: the capability says the subject may
    use the portal at all, and the account says *which* supplier it is. A subject with the
    capability and no account is refused (403), not shown an empty page.
    """
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability=portal_capability,
        entity="supplier_portal_account",
    )
    return portal_account_for(session, company_id=context.company_id, subject=context.actor)


@app.get(f"{BASE}/portal/documents", response_model=PortalDocumentsOut, tags=["portal"])
def portal_view(context: Context) -> PortalDocumentsOut:
    """Everything this supplier may see — its RFQs, its released orders and its invoices."""
    session = context.session
    account = _portal_scope(session, context)
    return PortalDocumentsOut(documents=_jsonable(portal_documents(session, account=account)))


@app.post(
    f"{BASE}/portal/rfqs/{{number}}/responses",
    response_model=PortalWriteOut,
    status_code=201,
    tags=["portal"],
)
def portal_rfq_response(
    number: str, payload: PortalResponseIn, context: Context
) -> PortalWriteOut:
    """Submit this supplier's answer — the same record an internally captured one is."""
    session = context.session
    account = _portal_scope(session, context)
    response = portal_respond(
        session,
        account=account,
        number=number,
        lines=[
            {
                "line_no": line.line_no,
                "unit_price": line.unit_price,
                "quantity": line.quantity,
            }
            for line in payload.lines
        ],
        received_on=payload.received_on,
        currency=payload.currency,
        lead_time_days=payload.lead_time_days,
        valid_until=payload.valid_until,
        note=payload.note,
    )
    recorded = _jsonable(
        {
            "number": response.rfq.number,
            "supplier": account.supplier.party.code,
            "received_on": response.received_on,
            "late": bool(response.late),
            "lines": [
                {"line_no": line.line_no, "unit_price": Decimal(line.unit_price)}
                for line in response.lines
            ],
        }
    )
    session.commit()
    return PortalWriteOut(recorded=recorded)


@app.post(
    f"{BASE}/portal/orders/{{number}}/acknowledge",
    response_model=PortalWriteOut,
    tags=["portal"],
)
def portal_acknowledge_order(number: str, context: Context) -> PortalWriteOut:
    """Take one of this supplier's orders on — the domain's own transition, once."""
    session = context.session
    account = _portal_scope(session, context)
    order = portal_acknowledge(session, account=account, number=number)
    recorded = _jsonable(
        {
            "number": order.number,
            "status": order.status,
            "acknowledged": order.status == "acknowledged",
        }
    )
    session.commit()
    return PortalWriteOut(recorded=recorded)


@app.post(
    f"{BASE}/portal/invoices", response_model=PortalWriteOut, status_code=201, tags=["portal"]
)
def portal_invoice(payload: PortalInvoiceIn, context: Context) -> PortalWriteOut:
    """Submit an invoice as a draft — posting and matching stay the buyer's review."""
    session = context.session
    account = _portal_scope(session, context)
    invoice = portal_submit_invoice(
        session,
        account=account,
        number=payload.number,
        invoice_date=payload.invoice_date,
        supplier_reference=payload.supplier_reference,
        lines=[
            {
                "line_no": line.line_no,
                "description": line.description or f"line {line.line_no}",
                "quantity": line.quantity or "1",
                "unit_price": line.unit_price,
            }
            for line in payload.lines
        ],
        order_number=payload.order_number,
    )
    recorded = _jsonable(
        {
            "number": invoice.number,
            "status": invoice.status,
            "gross_amount": Decimal(invoice.gross_amount),
            "supplier": account.supplier.party.code,
        }
    )
    session.commit()
    return PortalWriteOut(recorded=recorded)


@app.get(f"{BASE}/org-chart", response_model=OrgChartOut, tags=["hr"])
def org_chart_view(on: date, context: Context) -> JSONResponse:
    """The reporting hierarchy on `on`, including the people it cannot place.

    The date is stated rather than assumed to be today, so the same request draws the
    current structure and any past one. Fields a role may not read are **absent** from the
    payload rather than nulled: the restrictions of both entities the chart reads are read
    once, not once per employee, and the response is returned unfiltered by a model for
    that reason (a model's defaults would put a null back where a field was withheld).
    """
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="employee.read",
        entity=EMPLOYEE_ENTITY,
    )
    hidden_employee = hidden_fields(
        session,
        company_id=context.company_id,
        subject=context.actor,
        entity=EMPLOYEE_ENTITY,
    )
    hidden_placement = hidden_fields(
        session,
        company_id=context.company_id,
        subject=context.actor,
        entity="org_placement",
    )
    chart = org_chart(session, company_id=context.company_id, on=on)
    return JSONResponse(
        content=_jsonable(
            {
                **chart,
                "entries": [
                    {
                        field: value
                        for field, value in entry.items()
                        if field not in hidden_employee and field not in hidden_placement
                    }
                    for entry in chart["entries"]
                ],
                "unplaced": [
                    {
                        field: value
                        for field, value in row.items()
                        if field not in hidden_employee
                    }
                    for row in chart["unplaced"]
                ],
            }
        )
    )


def _jsonable(value: Any) -> Any:
    """A report as JSON: decimals as their exact string form, uuids as strings.

    The platform's money rule (DOMAIN-MODELS §2) holds at the boundary too: an amount
    crosses as a decimal *string*, never as a float.
    """
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (uuid.UUID, date, datetime)):
        return str(value)
    return value


# --- T-7.AUTH.04 — the session flow ------------------------------------------


class SessionOut(BaseModel):
    """Who the caller is and what the shell may show them.

    Deliberately carries no company *id* the caller could reuse: the session is the
    only thing that names the company (T-7.AUTH.02), so a shell that wants to say
    "Acme Trading" reads the code, and one that wants another company switches.
    """

    login: str
    display_name: str | None = None
    email: str | None = None
    company: str
    companies: list[str]
    capabilities: list[str]
    must_change_password: bool
    mfa_pending: bool


class SignInIn(BaseModel):
    """What a person types. `company` is only needed when they work in several."""

    login: str
    password: str
    company: str | None = None


class CompanySwitchIn(BaseModel):
    company: str


class PasswordChangeIn(BaseModel):
    current_password: str
    new_password: str


class MfaCodeIn(BaseModel):
    code: str


class MfaEnrolmentOut(BaseModel):
    """The secret, its otpauth URL, and the recovery codes — shown once, never again."""

    secret: str
    otpauth_url: str
    recovery_codes: list[str] | None = None


def _client_address(request: Request) -> str | None:
    """Where the request came from, for the session's own record.

    The forwarded address is read first because behind the reverse proxy of
    T-9.EDGE.01 every request arrives from the proxy.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client is not None else None


def _session_out(session: Session, state: identity.SignIn) -> SessionOut:
    """A session as the shell reads it: who, where, and what they may do here."""
    scope_to_company(session, state.company.id)
    return SessionOut(
        login=state.subject,
        display_name=state.user.display_name,
        email=state.user.email,
        company=state.company.code,
        companies=[company.code for company in identity.memberships_for(session, state.user)],
        capabilities=sorted(
            capabilities_held(
                session, company_id=state.company.id, subject=state.subject
            )
        ),
        must_change_password=bool(state.user.must_change_password),
        mfa_pending=state.mfa_pending,
    )


@app.post(f"{BASE}/auth/login", response_model=SessionOut, tags=["identity"])
def sign_in(payload: SignInIn, request: Request, response: Response) -> SessionOut:
    """Sign in and receive the session cookie.

    Public by necessity — there is no session to check yet — and the one route that
    mints one. Every refusal is the same answer with the same wording, so a caller
    cannot enumerate logins, and the credential's failure count is written even
    though the request is refused (app/identity.py).
    """
    with Session(_engine()) as session:
        state = identity.sign_in(
            session,
            login=payload.login,
            password=payload.password,
            company_code=payload.company,
            user_agent=(request.headers.get("user-agent") or "")[:256] or None,
            address=_client_address(request),
        )
        session.commit()
        answer = _session_out(session, state)
    auth.set_session_cookie(response, state.token, over_https=_over_https(request))
    return answer


@app.get(f"{BASE}/auth/session", response_model=SessionOut, tags=["identity"])
def read_session(context: Context) -> SessionOut:
    """Who this cookie is, for a shell that has one and wants to know what it may do."""
    return _session_out(context.session, _resolved(context))


@app.post(f"{BASE}/auth/logout", status_code=204, tags=["identity"])
def sign_out(context: Context, request: Request, response: Response) -> Response:
    """Sign out: the row is revoked, so the token is dead even if the cookie is copied."""
    identity.revoke_session(context.session, context.session_token or "")
    context.session.commit()
    auth.clear_session_cookie(response, over_https=_over_https(request))
    response.status_code = 204
    return response


@app.post(f"{BASE}/auth/password", status_code=204, tags=["identity"])
def change_password(
    payload: PasswordChangeIn, context: Context, response: Response
) -> Response:
    """Change your own password. The current one is required, and the rest are revoked.

    Revoking the other sessions is the point of a password change: if the password was
    changed *because* somebody else had it, leaving their sessions live would leave the
    problem in place. This session survives, so the person is not signed out of the
    screen they just used.
    """
    session = context.session
    user = identity.user_by_login(session, context.actor)
    if user is None or not identity.verify_password(session, user, payload.current_password):
        raise ApiError(401, "unauthenticated", "the current password is not right")
    identity.set_password(session, user, payload.new_password)
    for row in identity.sessions_for(session, user=user):
        if str(row.id) != str(_resolved(context).session_row.id) and row.revoked_at is None:
            row.revoked_at = datetime.now(timezone.utc)
    session.commit()
    response.status_code = 204
    return response


@app.post(f"{BASE}/auth/company", response_model=SessionOut, tags=["identity"])
def switch_company(payload: CompanySwitchIn, context: Context) -> SessionOut:
    """Move this session to another company the person belongs to.

    The membership is what permits it, never a header or a payload field: a caller
    cannot reach a company they are not a member of, which is the hole the old
    ``X-Company-Id`` header left open (finding 2 of the 2026-10-10 audit).
    """
    session = context.session
    state = identity.switch_company(session, _resolved(context), payload.company)
    session.commit()
    return _session_out(session, state)


@app.post(f"{BASE}/auth/mfa/enrol", response_model=MfaEnrolmentOut, tags=["identity"])
def enrol_second_factor(context: PendingContext) -> MfaEnrolmentOut:
    """Issue a second factor. It is not demanded until a code confirms it.

    Reachable with a session that already owes a factor, so a person can replace a lost
    authenticator without an administrator — and reachable before one is owed, because
    holding an `mfa_required_roles` role requires having enrolled one (the assignment
    route refuses otherwise).
    """
    user = identity.user_by_login(context.session, context.actor)
    if user is None:
        raise ApiError(401, "unauthenticated", "no such user")
    secret = identity.enrol_totp(context.session, user)
    context.session.commit()
    return MfaEnrolmentOut(
        secret=secret,
        otpauth_url=(
            f"otpauth://totp/{quote(context.actor)}?secret={secret}"
            f"&issuer={quote('ERP')}&digits={identity.TOTP_DIGITS}"
            f"&period={identity.TOTP_STEP_SECONDS}"
        ),
    )


@app.post(f"{BASE}/auth/mfa/confirm", response_model=MfaEnrolmentOut, tags=["identity"])
def confirm_second_factor(payload: MfaCodeIn, context: PendingContext) -> MfaEnrolmentOut:
    """Confirm enrolment with a live code, and receive the recovery codes once."""
    user = identity.user_by_login(context.session, context.actor)
    if user is None:
        raise ApiError(401, "unauthenticated", "no such user")
    codes = identity.confirm_totp(context.session, user, payload.code)
    context.session.commit()
    return MfaEnrolmentOut(secret="", otpauth_url="", recovery_codes=codes)


@app.post(f"{BASE}/auth/mfa/verify", response_model=SessionOut, tags=["identity"])
def verify_second_factor(payload: MfaCodeIn, context: PendingContext) -> SessionOut:
    """Finish a sign-in that owed a second factor."""
    session = context.session
    resolved = _resolved(context)
    if not resolved.mfa_pending:
        raise ApiError(409, "mfa_not_pending", "this session does not owe a second factor")
    state = identity.confirm_session_mfa(session, resolved, payload.code)
    session.commit()
    return _session_out(session, state)


# --- T-7.ADMIN.01 … T-7.ADMIN.03 — administration -----------------------------


class UserIn(BaseModel):
    login: str
    password: str | None = None
    email: str | None = None
    display_name: str | None = None


class UserOut(BaseModel):
    login: str
    email: str | None
    display_name: str | None
    status: str
    has_password: bool
    companies: list[str]
    roles: list[str]
    mfa_enrolled: bool


class UserPageOut(BaseModel):
    items: list[UserOut]
    limit: int
    offset: int
    total: int


class UserStatusIn(BaseModel):
    status: str


class PasswordSetIn(BaseModel):
    password: str
    # Optional **and nullable**, which is this contract's convention for "not stated"
    # (`tests/check_api_conventions.py` holds every documented optional field to it):
    # null and false mean the same thing here — the person is not made to change it.
    must_change: bool | None = None


class RoleIn(BaseModel):
    code: str
    name: str


class RoleOut(BaseModel):
    code: str
    name: str
    capabilities: list[str]


class RolePageOut(BaseModel):
    items: list[RoleOut]
    limit: int
    offset: int
    total: int


class GrantIn(BaseModel):
    capabilities: list[str]


class RestrictionIn(BaseModel):
    """One field of one entity, for one role. Both flags are optional and nullable:
    null means "not stated", and the two are independent — a field may be writable and
    unreadable, which is what a write-only column (a password, a bank account) is."""

    entity: str
    field: str
    can_read: bool | None = None
    can_write: bool | None = None


class AssignmentIn(BaseModel):
    subject: str
    role: str


class CompanyIn(BaseModel):
    """A tenant, as it is created. The market decides what it starts with."""

    code: str
    name: str
    market: str
    fiscal_year_start_month: int | None = None
    base_currency: str | None = None


class CompanyStatusIn(BaseModel):
    status: str


def _member_of(session: Session, *, login: str, company_id: uuid.UUID) -> identity.AppUser:
    """The user, if they are a member of this company — otherwise 404.

    Administering somebody who does not work here answers "no such user" rather than
    naming them: a company's administrator has no business learning another tenant's
    logins, and identity rows are installation-level (app/identity.py), so the
    membership check is the isolation.
    """
    user = identity.user_by_login(session, login)
    if user is None:
        raise ApiError(404, "unknown_user", f"no user {login!r} works in this company")
    member = any(
        str(company.id) == str(company_id)
        for company in identity.memberships_for(session, user)
    )
    if not member:
        raise ApiError(404, "unknown_user", f"no user {login!r} works in this company")
    return user


def _user_out(session: Session, *, user: identity.AppUser, company_id: uuid.UUID) -> UserOut:
    entry = session.get(identity.UserTotp, user.id)
    return UserOut(
        login=user.login,
        email=user.email,
        display_name=user.display_name,
        status=user.status,
        has_password=user.password_hash is not None,
        companies=[company.code for company in identity.memberships_for(session, user)],
        roles=[
            role.code
            for role in roles_for(session, company_id=company_id, subject=user.login)
        ],
        mfa_enrolled=bool(entry is not None and entry.confirmed_at is not None),
    )


@app.get(f"{BASE}/admin/users", response_model=UserPageOut, tags=["administration"])
def list_users(
    context: Context,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> UserPageOut:
    """The people who may work in this company. Only this company's — never others'."""
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="admin.users",
        entity="app_user",
    )
    everyone = [
        user
        for user in session.scalars(
            select(identity.AppUser)
            .join(
                identity.UserMembership,
                identity.UserMembership.user_id == identity.AppUser.id,
            )
            .where(identity.UserMembership.member_company_id == context.company_id)
            .order_by(identity.AppUser.login)
        )
    ]
    window = everyone[offset : offset + limit]
    return UserPageOut(
        items=[_user_out(session, user=user, company_id=context.company_id) for user in window],
        limit=limit,
        offset=offset,
        total=len(everyone),
    )


@app.post(f"{BASE}/admin/users", response_model=UserOut, status_code=201, tags=["administration"])
def create_user_route(payload: UserIn, context: Context) -> UserOut:
    """Add a person to this company, with a password they can sign in with.

    Creating the *identity* is installation-level, but creating it through this route
    always makes a member of the acting company: there is no way to make a user who
    belongs nowhere, which would be an account nothing could reach or administer.
    """
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="admin.users",
        entity="app_user",
    )
    user = identity.create_user(
        session,
        login=payload.login,
        password=payload.password,
        email=payload.email,
        display_name=payload.display_name,
        companies=[context.company_id],
    )
    session.commit()
    return _user_out(session, user=user, company_id=context.company_id)


@app.post(
    f"{BASE}/admin/users/{{login}}/password", response_model=UserOut, tags=["administration"]
)
def set_user_password(
    login: str, payload: PasswordSetIn, context: Context
) -> UserOut:
    """Set somebody's password. Their live sessions are revoked by it."""
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="admin.users",
        entity="app_user",
    )
    user = _member_of(session, login=login, company_id=context.company_id)
    identity.set_password(
        session, user, payload.password, must_change=bool(payload.must_change)
    )
    identity.revoke_sessions(session, user=user)
    session.commit()
    return _user_out(session, user=user, company_id=context.company_id)


@app.post(
    f"{BASE}/admin/users/{{login}}/status", response_model=UserOut, tags=["administration"]
)
def set_user_status(login: str, payload: UserStatusIn, context: Context) -> UserOut:
    """Disable or re-enable somebody. Disabling revokes every session they hold."""
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="admin.users",
        entity="app_user",
    )
    user = _member_of(session, login=login, company_id=context.company_id)
    identity.set_status(session, user, payload.status)
    session.commit()
    return _user_out(session, user=user, company_id=context.company_id)


@app.get(f"{BASE}/admin/roles", response_model=RolePageOut, tags=["administration"])
def list_roles(
    context: Context,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RolePageOut:
    """This company's roles and what each holds."""
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="admin.roles",
        entity="role",
    )
    everyone = list(
        session.scalars(
            select(Role).where(Role.company_id == context.company_id).order_by(Role.code)
        )
    )
    window = everyone[offset : offset + limit]
    return RolePageOut(
        items=[
            RoleOut(
                code=role.code,
                name=role.name,
                capabilities=sorted(
                    permission.capability for permission in role.permissions
                ),
            )
            for role in window
        ],
        limit=limit,
        offset=offset,
        total=len(everyone),
    )


@app.post(f"{BASE}/admin/roles", response_model=RoleOut, status_code=201, tags=["administration"])
def create_role(payload: RoleIn, context: Context) -> RoleOut:
    """Define a role. It holds nothing until something is granted to it."""
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="admin.roles",
        entity="role",
    )
    role = define_role(
        session, company_id=context.company_id, code=payload.code, name=payload.name
    )
    session.commit()
    return RoleOut(code=role.code, name=role.name, capabilities=[])


def _role_in_company(session: Session, *, code: str, company_id: uuid.UUID) -> Role:
    """This company's role with that code, or 404."""
    role = session.scalars(
        select(Role).where(Role.company_id == company_id, Role.code == code)
    ).one_or_none()
    if role is None:
        raise ApiError(404, "unknown_role", f"no role {code!r} in this company")
    return role


@app.post(
    f"{BASE}/admin/roles/{{code}}/grants", response_model=RoleOut, tags=["administration"]
)
def grant_capabilities(code: str, payload: GrantIn, context: Context) -> RoleOut:
    """Give a role capabilities. This is the only way anything is ever allowed."""
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="admin.roles",
        entity="role",
    )
    role = _role_in_company(session, code=code, company_id=context.company_id)
    grant(session, role, *payload.capabilities)
    session.commit()
    return RoleOut(
        code=role.code,
        name=role.name,
        capabilities=sorted(permission.capability for permission in role.permissions),
    )


@app.post(
    f"{BASE}/admin/roles/{{code}}/restrictions",
    response_model=RoleOut,
    status_code=201,
    tags=["administration"],
)
def restrict_field(code: str, payload: RestrictionIn, context: Context) -> RoleOut:
    """Restrict one field of one entity for a role — the field-level half of RBAC."""
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="admin.roles",
        entity="role",
    )
    role = _role_in_company(session, code=code, company_id=context.company_id)
    restrict(
        session,
        role,
        entity=payload.entity,
        field=payload.field,
        can_read=payload.can_read if payload.can_read is not None else True,
        can_write=payload.can_write if payload.can_write is not None else False,
    )
    session.commit()
    return RoleOut(
        code=role.code,
        name=role.name,
        capabilities=sorted(permission.capability for permission in role.permissions),
    )


@app.post(
    f"{BASE}/admin/assignments", response_model=UserOut, status_code=201, tags=["administration"]
)
def assign_role_route(payload: AssignmentIn, context: Context) -> UserOut:
    """Give a member one of this company's roles. Takes effect on their next request.

    Two refusals worth stating: the subject must be a **member** of this company, and a
    role the `mfa_required_roles` variable names cannot be assigned to somebody without a
    confirmed second factor — which is how "MFA is enforced for the administrator role"
    is true without a sign-in that could lock an install out of itself.
    """
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="admin.roles",
        entity="role",
    )
    user = _member_of(session, login=payload.subject, company_id=context.company_id)
    role = _role_in_company(session, code=payload.role, company_id=context.company_id)
    if identity.mfa_required_for_role(role.code):
        entry = session.get(identity.UserTotp, user.id)
        if entry is None or entry.confirmed_at is None:
            raise ApiError(
                422,
                "second_factor_required",
                f"role {role.code!r} requires a confirmed second factor;"
                f" {user.login!r} has not enrolled one",
            )
    assign_role(
        session, company_id=context.company_id, subject=user.login, role=role
    )
    session.commit()
    return _user_out(session, user=user, company_id=context.company_id)


@app.post(f"{BASE}/companies", response_model=CompanyOut, status_code=201, tags=["platform"])
def create_company_route(payload: CompanyIn, context: Context) -> CompanyOut:
    """Provision a tenant: the company, its chart of accounts, and its first administrator.

    The creator is made a member of the company they created and given its administrator
    role, because a tenant nobody can administer is not provisioned — it is stranded.
    Both writes are company-scoped, so the trail files them under the new company.

    # ponytail: no platform-level administrator. Every administration route is
    # scoped to the actor's own company, so this is how a tenant is created: by
    # somebody who already holds `company.configure` somewhere. Ceiling: an
    # operator with no company cannot create the first one — the install command is
    # still the only way in, which is the bootstrap module's job. Upgrade path: a
    # platform principal, when something asks for one.
    """
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="company.configure",
        entity="company",
    )
    company = installer(
        session,
        code=payload.code,
        name=payload.name,
        market=payload.market,
        fiscal_year_start_month=payload.fiscal_year_start_month,
        base_currency=payload.base_currency,
    )
    creator = identity.user_by_login(session, context.actor)
    if creator is not None:
        identity.add_membership(session, user=creator, company_id=company.id)
        role = define_role(
            session,
            company_id=company.id,
            code=ADMINISTRATOR_ROLE[0],
            name=ADMINISTRATOR_ROLE[1],
        )
        grant(session, role, *declared_capabilities())
        assign_role(
            session, company_id=company.id, subject=context.actor, role=role
        )
    session.commit()
    return _company_out(company)


@app.post(
    f"{BASE}/companies/{{code}}/status", response_model=CompanyOut, tags=["platform"]
)
def set_company_status_route(
    code: str, payload: CompanyStatusIn, context: SuspendedContext
) -> CompanyOut:
    """Suspend or reinstate a company. Nothing it owns is touched.

    A suspended company keeps every row — the ledger stays whole — and refuses
    sign-ins and every request except this one, which is why this route is reachable
    for a suspended company and nothing else is (app/auth.py `allow_suspended`). It
    still asks for `company.configure`, so lifting the suspension is as hard as
    suspending.
    """
    session = context.session
    require(
        session,
        company_id=context.company_id,
        subject=context.actor,
        capability="company.configure",
        entity="company",
    )
    target = session.scalars(
        select(Company).where(Company.code == code, Company.deleted_at.is_(None))
    ).one_or_none()
    if target is None:
        raise ApiError(404, "unknown_company", f"no company {code!r}")
    set_company_status(session, target, status=payload.status)
    session.commit()
    return _company_out(target)
