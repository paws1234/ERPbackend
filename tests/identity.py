"""Shared identity for the checks — **not** a check itself.

The pipeline runs `tests/check_*.py`; this file is a helper they import.

Before T-7.AUTH.02 a check said who it was with two request headers
(``X-Company-Id`` and ``X-Actor``), because that is where the boundary read identity
from. The boundary now reads it from a **session** (:mod:`app.auth`), so a check that
wants to be somebody has to be somebody: this helper gives a subject a user, a
password and a membership of the company it is acting in, opens a real session, and
returns the cookie that carries it.

It mints the session directly rather than going through ``POST /auth/login``, on
purpose. That route is exercised by ``tests/check_auth.py`` against the real thing;
every other check wants *a caller*, not another test of the door, and a direct
session keeps those checks about their own rule — the same reason they insert rows
with the domain functions instead of the API.

The `actor` a check names is still the **subject** — the login string. Roles,
capabilities, field restrictions and the audit trail key on it exactly as they did
before, so a check that assigns a role to ``"maria"`` and then acts as ``"maria"``
needs no other change.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app import auth, identity

# The password every check's caller signs in with. Long enough for the policy of
# T-7.AUTH.01, and stated once so a change to the floor lands in one place.
CHECK_PASSWORD = "check-password-1"


def sign_in(engine, *, company_id: uuid.UUID, actor: str) -> dict[str, str]:
    """Headers carrying a real session for `actor` in `company_id`.

    Idempotent: the user, the credential and the membership are created once and
    reused, so a check may call this for the same actor as often as it likes. Call it
    once per request set rather than once per request — it writes rows.
    """
    with Session(engine) as session:
        user = identity.user_by_login(session, actor)
        if user is None:
            user = identity.create_user(
                session,
                login=actor,
                password=CHECK_PASSWORD,
                display_name=actor,
                companies=[company_id],
            )
        else:
            identity.add_membership(session, user=user, company_id=company_id)
            if user.password_hash is None:
                identity.set_password(session, user, CHECK_PASSWORD)
        state = identity.open_session(
            session,
            user=user,
            company_id=company_id,
            user_agent="check",
            # A check's caller has no second factor to present: T-7.AUTH.03's own
            # check is the one that goes through the pending-factor state.
            mfa_confirmed=True,
        )
        session.commit()
        return {"Cookie": f"{auth.SESSION_COOKIE}={state.token}"}
