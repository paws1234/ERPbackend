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
from app.company import Company
from app.db import Base, scope_to_company
from app.ledger.posting import (
    JournalEntry,
    UnbalancedEntryError,
    post_journal_entry,
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
    lines: list[JournalLineIn]


class JournalLineOut(BaseModel):
    line_no: int
    account: str
    debit: str
    credit: str
    party: str | None = None


class JournalEntryOut(BaseModel):
    id: str
    company_id: str
    posting_date: date
    currency: str
    memo: str | None = None
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


def _entry_out(entry: JournalEntry) -> JournalEntryOut:
    return JournalEntryOut(
        id=str(entry.id),
        company_id=str(entry.company_id),
        posting_date=entry.posting_date,
        currency=entry.currency,
        memo=entry.memo,
        lines=[
            JournalLineOut(
                line_no=line.line_no,
                account=line.account,
                debit=_money(line.debit),
                credit=_money(line.credit),
                party=line.party,
            )
            for line in entry.lines
        ],
    )


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
        return JSONResponse(
            status_code=seen.status_code,
            content=json.loads(seen.body),
            headers={"Idempotent-Replay": "true"},
        )

    try:
        entry = post_journal_entry(
            session,
            company_id=context.company_id,
            posting_date=payload.posting_date,
            currency=payload.currency,
            memo=payload.memo,
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
    return JSONResponse(status_code=201, content=body)


@app.get(f"{BASE}/journal-entries", response_model=PageOut, tags=["ledger"])
def list_entries(
    context: Context,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PageOut:
    """One page of the company's ledger, in posting order."""
    session = context.session
    total = session.scalar(
        select(func.count()).select_from(JournalEntry).where(
            JournalEntry.company_id == context.company_id
        )
    )
    entries = session.scalars(
        select(JournalEntry)
        .where(JournalEntry.company_id == context.company_id)
        .order_by(JournalEntry.posting_date, JournalEntry.created_at)
        .limit(limit)
        .offset(offset)
    ).all()
    return PageOut(
        items=[_entry_out(entry) for entry in entries],
        limit=limit,
        offset=offset,
        total=total or 0,
    )
