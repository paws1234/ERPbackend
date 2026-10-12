"""T-7.AUTH.02 — the session boundary: one cookie in, one verified identity out.

This is the only place in the application that reads who a request is. It replaces
the two request headers (``X-Company-Id`` and ``X-Actor``) that ``T-0.API.01``
documented as a placeholder with a ceiling — "any caller may claim any actor" — and
it closes that ceiling rather than restating it: company and actor now come from a
session row that was minted against a verified credential.

Three refusals, each with its own meaning to a client, because the shell treats them
differently (``lib/api.ts`` already distinguishes them):

* :class:`NotAuthenticated` — no cookie, an unknown token, an expired or revoked
  session, an idle session, or a disabled user. ``401``.
* :class:`MfaPending` — a real session that still owes its second factor. ``401``
  with its own code, and only the MFA route accepts such a session.
* :class:`SuspendedCompany` — a session for a company that has been suspended.
  ``403``: the caller is who they say they are, and the company is not trading.

They are raised here and answered in :mod:`app.api`, which is where every other
domain refusal in this tree is turned into the platform's one error shape.

The cookie carries a random 256-bit token; only its SHA-256 is stored
(:mod:`app.identity`). ``HttpOnly`` always, so a script cannot read it; ``Secure``
whenever the request arrived over HTTPS (which is every request once the reverse
proxy of T-9.EDGE.01 is in front of the application); ``SameSite=Lax``, which is
what lets the browser send it on a top-level navigation to the shell without
sending it on a cross-site form post.
"""

from __future__ import annotations

from fastapi import Request, Response
from sqlalchemy.orm import Session

from app import identity

# The cookie's name. Versioned like the API path, so a future scheme can be run
# alongside this one for a release rather than over it.
SESSION_COOKIE = "erpv1_session"

# The two shapes a client is answered with, stated here as the codes the one error
# shape carries.
UNAUTHENTICATED_CODE = "unauthenticated"
MFA_REQUIRED_CODE = "mfa_required"
SUSPENDED_COMPANY_CODE = "company_suspended"


class NotAuthenticated(Exception):
    """The request carries no usable session."""

    def __init__(self, message: str = "this endpoint requires a signed-in session"):
        super().__init__(message)


class MfaPending(Exception):
    """The session is real but its second factor has not been presented yet."""

    def __init__(self, message: str = "a second factor is required for this session"):
        super().__init__(message)


class SuspendedCompany(Exception):
    """The session names a company that has been suspended."""

    def __init__(self, message: str = "this company is suspended"):
        super().__init__(message)


def request_token(request: Request) -> str | None:
    """The session token on the request, or None. Never logs it, never echoes it."""
    return request.cookies.get(SESSION_COOKIE)


def current_identity(
    session: Session,
    request: Request,
    *,
    allow_pending_mfa: bool = False,
    allow_suspended: bool = False,
) -> identity.SignIn:
    """Resolve the request's session, or refuse.

    `allow_pending_mfa` is what the MFA route itself needs: a session that owes a
    second factor must be able to present one, and nothing else may run until it
    has.

    `allow_suspended` is what the reinstatement route needs, and it is the only
    escape from suspension. Without it a company's suspension would be a one-way
    door: the operator who suspended a tenant could not reach the route that
    reinstates it, because that route is behind the same check. It grants nothing
    else — the route still asks for `company.configure` like any other.
    """
    resolved = identity.resolve_session(session, request_token(request))
    if resolved is None:
        raise NotAuthenticated()
    if resolved.company.status != "active" and not allow_suspended:
        raise SuspendedCompany(f"{resolved.company.code} is suspended")
    if resolved.mfa_pending and not allow_pending_mfa:
        raise MfaPending()
    return resolved


def set_session_cookie(response: Response, token: str, *, over_https: bool) -> None:
    """Hand a freshly minted session to the client.

    `max_age` matches the session's own TTL, so the browser stops carrying a cookie
    the server would refuse anyway; the authoritative timestamps are the row's.
    """
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(identity.SESSION_TTL.total_seconds()),
        httponly=True,
        secure=over_https,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response, *, over_https: bool) -> None:
    """Remove the cookie, so a signed-out browser stops carrying it."""
    response.delete_cookie(
        SESSION_COOKIE, httponly=True, secure=over_https, samesite="lax", path="/"
    )
