"""T-7.AUTH.01 … T-7.AUTH.03 — identity: the person, the credential, the session, the second factor.

**Identity is installation-level, and that is a schema decision rather than an
omission.** In this schema ``company_id`` means *this row is owned by that company*;
a user is not owned by a company — a person may act in several — and a request has
to know who it is **before** any company is known, because the session is what names
the company. So these tables carry no ``company_id``: they point at a company
through ``active_company_id`` / ``member_company_id`` and are declared in
:data:`app.db.UNSCOPED_TABLES`. The trail's own exclusion list names the same
tables, so the absent trail row is a stated decision, not a gap
(``tests/check_audit_coverage.py`` holds the two lists together).

What that costs, said plainly: an **identity** change is not on the audit trail,
because the trail files every row under a company and these rows have none. What
covers it instead: roles, capability grants, field restrictions and role
assignments *are* company-scoped and therefore audited, so "who may do what in
this company" is on the record; ``user_session`` is the sign-in record (who, from
where, when, for which company, and when it was revoked); and every row here
carries its own timestamps.

**Authorisation is unchanged by this module.** A session resolves to a *subject* —
the login string ``app/security.py`` has always keyed roles on — so capabilities,
field restrictions, ``require(...)``, the trail's actor and every existing endpoint
keep the meaning they had. What changes is only where that subject comes from: a
verified session, never a request header.

Passwords are Argon2id (the decided algorithm). A refused sign-in increments the
credential's failure count and locks it past the threshold; the counter is its own
unit of work, so a refusal is recorded even though the request is not committed.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import secrets
import struct
import uuid
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from app import config as _config
from app.company import Company
from app.db import Base

# --- The variables this module reads (§9 of the plan) -------------------------
# Declared once in app/config.py (T-8.CFG.01), which validates them at startup; the
# names below are the ones this module and the checks already use.

PASSWORD_MIN_LENGTH = _config.SETTINGS.password_min_length
SESSION_TTL = dt.timedelta(minutes=_config.SETTINGS.session_ttl_minutes)
SESSION_IDLE_TIMEOUT = dt.timedelta(minutes=_config.SETTINGS.session_idle_timeout_minutes)
LOCKOUT_THRESHOLD = _config.SETTINGS.lockout_threshold
LOCKOUT_WINDOW = dt.timedelta(minutes=_config.SETTINGS.lockout_window_minutes)
RECOVERY_CODE_COUNT = _config.SETTINGS.recovery_code_count
# The roles whose holders must present a second factor (`mfa_required_roles`).
MFA_REQUIRED_ROLES = list(_config.SETTINGS.mfa_required_roles)

# Argon2id, with the parameters stated rather than inherited silently.
_PASSWORDS = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)

USER_STATUSES = ("active", "disabled")

# A short refusal list. The policy is a floor, not a strength meter (T-7.AUTH.01).
WEAK_PASSWORDS = frozenset(
    {
        "password",
        "password1",
        "password123",
        "administrator",
        "letmein",
        "welcome",
        "qwerty",
        "123456789012",
        "changeme",
    }
)


class IdentityError(ValueError):
    """Something about identity was asked for that cannot be granted."""


class UserExistsError(IdentityError):
    """That login already belongs to somebody."""


class UnknownUserError(IdentityError):
    """No user holds that login."""


class WeakPasswordError(IdentityError):
    """The password does not meet the stated policy."""


class RefusedSignIn(IdentityError):
    """A sign-in that did not succeed. The message is deliberately uninformative.

    It never says whether the login exists, whether the password was wrong or
    whether the account is locked: a caller learns "no", and the difference is not
    readable from the answer.
    """


class NotAMemberError(IdentityError):
    """The user does not belong to that company."""


class CompanySuspended(IdentityError):
    """The company that would be signed into is not trading.

    Refused at the door rather than after it: a suspended company could otherwise hand
    out a session whose every request is refused, which is a worse answer than "no".
    """


class UnknownCompanyCode(IdentityError):
    """No company in this installation carries that code."""


# --- The tables --------------------------------------------------------------


class AppUser(Base):
    """One person who may sign in. Installation-level (see the module docstring).

    ``app_user`` rather than ``user``: ``user`` is reserved in PostgreSQL, and a
    quoted reserved word in every query is a footgun waiting for the first hand-written
    statement.
    """

    __tablename__ = "app_user"
    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{status}'" for status in USER_STATUSES) + ")",
            name="ck_app_user_status",
        ),
        CheckConstraint(
            "failed_attempts >= 0", name="ck_app_user_failed_attempts_not_negative"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Lower-cased, unique across the installation. This string is the **subject**
    # every other table in this platform already keys on — roles, the trail, the
    # posting attribution — so nothing downstream had to change when identity
    # arrived.
    login: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    email: Mapped[str | None] = mapped_column(String(254))
    display_name: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    # Null means "no password set yet": such a user cannot sign in, and an install
    # that names an administrator gives it one in the same transaction
    # (app/bootstrap.py). Never a plaintext password, and never reversible.
    password_hash: Mapped[str | None] = mapped_column(String(255))
    password_changed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    must_change_password: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="false"
    )
    failed_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    locked_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class UserSession(Base):
    """One live sign-in: a row, so signing out is real and revocation is immediate.

    The token itself is **never stored** — only its SHA-256, which is what makes a
    stolen database dump useless for impersonation. ``active_company_id`` is the
    company this session is bound to; a caller cannot change it by stating a
    header, only by the audited switch in :func:`switch_company`.
    """

    __tablename__ = "user_session"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_user.id"), nullable=False, index=True
    )
    # Deliberately not `company_id`: in this schema that name means "owned by that
    # company", and a session *points at* a company rather than being owned by one
    # (``app/db.py`` UNSCOPED_TABLES).
    active_company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    issued_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    # When the holder passed the second factor, if they had one to pass. A session
    # with a pending factor is refused by the boundary (api.py `context`) until this
    # is set, so a half-finished sign-in reaches nothing but the MFA route.
    mfa_confirmed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str | None] = mapped_column(String(256))
    address: Mapped[str | None] = mapped_column(String(64))


class UserMembership(Base):
    """Which companies a person may act in — the record a sign-in binds to.

    Not the permissions: what a subject may *do* stays where it was, in the
    company-scoped ``role_assignment`` (:mod:`app.security`), which is audited.
    This row only answers "may this person work here at all".
    """

    __tablename__ = "user_membership"
    __table_args__ = (
        UniqueConstraint("user_id", "member_company_id", name="uq_user_membership_once"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_user.id"), nullable=False, index=True
    )
    member_company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class UserTotp(Base):
    """A confirmed time-based second factor for one user.

    The secret is stored as issued. # ponytail: not encrypted at rest yet — the
    envelope encryption of T-9.CRYPTO.01 owns that, and it lands with the field
    encryption of payroll bank details, which is the same mechanism.
    """

    __tablename__ = "user_totp"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_user.id"), primary_key=True
    )
    secret: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class UserRecoveryCode(Base):
    """A single-use second-factor recovery code, stored hashed."""

    __tablename__ = "user_recovery_code"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_user.id"), nullable=False, index=True
    )
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# --- Users and credentials ---------------------------------------------------


def normalise_login(login: str) -> str:
    """The canonical form of a login: trimmed, lower-cased, non-empty."""
    canonical = login.strip().lower()
    if not canonical:
        raise IdentityError("a login cannot be empty")
    if len(canonical) > 64:
        raise IdentityError("a login is at most 64 characters")
    return canonical


def check_password_policy(password: str) -> None:
    """Refuse a password below the stated floor, before it is ever hashed."""
    if len(password) < PASSWORD_MIN_LENGTH:
        raise WeakPasswordError(
            f"a password is at least {PASSWORD_MIN_LENGTH} characters"
        )
    if password.lower() in WEAK_PASSWORDS:
        raise WeakPasswordError("that password is on the refused list")


def hash_password(password: str) -> str:
    """The Argon2id hash of a password, with a fresh salt."""
    check_password_policy(password)
    return _PASSWORDS.hash(password)


def user_by_login(session: Session, login: str) -> AppUser | None:
    """The user holding that login, or None."""
    try:
        canonical = normalise_login(login)
    except IdentityError:
        return None
    return session.scalars(select(AppUser).where(AppUser.login == canonical)).one_or_none()


def user_by_id(session: Session, user_id: uuid.UUID) -> AppUser | None:
    """The user with that id, or None."""
    return session.get(AppUser, user_id)


def create_user(
    session: Session,
    *,
    login: str,
    password: str | None = None,
    email: str | None = None,
    display_name: str | None = None,
    companies: list[uuid.UUID] | None = None,
) -> AppUser:
    """Create a person, optionally with a password and company memberships.

    A user with no password cannot sign in; that is the state an administrator
    clears with :func:`set_password`.
    """
    canonical = normalise_login(login)
    if user_by_login(session, canonical) is not None:
        raise UserExistsError(f"{canonical!r} already belongs to a user")
    user = AppUser(
        login=canonical,
        email=email,
        display_name=display_name,
        password_hash=hash_password(password) if password is not None else None,
        password_changed_at=dt.datetime.now(dt.timezone.utc) if password is not None else None,
    )
    session.add(user)
    session.flush()
    for company_id in companies or []:
        add_membership(session, user=user, company_id=company_id)
    return user


def set_password(
    session: Session, user: AppUser, password: str, *, must_change: bool = False
) -> AppUser:
    """Set (or replace) a user's password and clear any lockout."""
    user.password_hash = hash_password(password)
    user.password_changed_at = dt.datetime.now(dt.timezone.utc)
    user.must_change_password = must_change
    user.failed_attempts = 0
    user.locked_until = None
    session.flush()
    return user


def set_status(session: Session, user: AppUser, status: str) -> AppUser:
    """Enable or disable a user. A disabled user's live sessions are revoked."""
    if status not in USER_STATUSES:
        raise IdentityError(f"unknown user status {status!r}")
    user.status = status
    if status == "disabled":
        revoke_sessions(session, user=user)
    session.flush()
    return user


def is_locked(user: AppUser, *, now: dt.datetime | None = None) -> bool:
    """Whether the credential is currently locked out."""
    if user.locked_until is None:
        return False
    return user.locked_until > (now or dt.datetime.now(dt.timezone.utc))


def verify_password(
    session: Session, user: AppUser, password: str, *, now: dt.datetime | None = None
) -> bool:
    """Check a password, counting the failure and locking the credential at the threshold.

    The counter is committed here rather than left to the caller: a refused sign-in
    is a fact about the credential, and it must survive the request it happened in.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    if user.password_hash is None or is_locked(user, now=now):
        return False
    try:
        _PASSWORDS.verify(user.password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        user.failed_attempts += 1
        if user.failed_attempts >= LOCKOUT_THRESHOLD:
            user.locked_until = now + LOCKOUT_WINDOW
            user.failed_attempts = 0
        session.commit()
        return False
    # `needs_rehash` is the reason the parameters are a value and not a constant:
    # raising them later re-hashes on the next successful sign-in rather than
    # locking anybody out.
    if _PASSWORDS.check_needs_rehash(user.password_hash):
        user.password_hash = _PASSWORDS.hash(password)
    user.failed_attempts = 0
    user.locked_until = None
    session.flush()
    return True


# --- Memberships -------------------------------------------------------------


def add_membership(session: Session, *, user: AppUser, company_id: uuid.UUID) -> UserMembership:
    """Let a person act in a company. Idempotent."""
    existing = session.scalars(
        select(UserMembership).where(
            UserMembership.user_id == user.id,
            UserMembership.member_company_id == company_id,
        )
    ).one_or_none()
    if existing is not None:
        return existing
    membership = UserMembership(user_id=user.id, member_company_id=company_id)
    session.add(membership)
    session.flush()
    return membership


def memberships_for(session: Session, user: AppUser) -> list[Company]:
    """The companies this person may act in, by code."""
    return list(
        session.scalars(
            select(Company)
            .join(UserMembership, UserMembership.member_company_id == Company.id)
            .where(UserMembership.user_id == user.id, Company.deleted_at.is_(None))
            .order_by(Company.code)
        )
    )


# --- Sessions ----------------------------------------------------------------


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class SignIn:
    """A resolved session: who, for which company, and the token that carries it."""

    user: AppUser
    company: Company
    token: str
    session_row: UserSession

    @property
    def subject(self) -> str:
        """The login string — the subject every other table keys on."""
        return self.user.login

    @property
    def mfa_pending(self) -> bool:
        """Whether a second factor is still owed before this session may act."""
        return self.session_row.mfa_confirmed_at is None


def _choose_company(
    session: Session, user: AppUser, company_code: str | None
) -> Company:
    """The company a sign-in binds to: the one named, or the only active one there is.

    A person who belongs to several companies and names none is bound to the first
    **active** one by code rather than refused: the shell lists the others and
    :func:`switch_company` moves between them. Refusing instead would put a company
    prompt in front of the password prompt for a case the switcher already covers.

    A suspended company is refused by name when it is asked for, and skipped when it
    is merely the first alphabetically — otherwise suspending one tenant would lock
    its staff out of the others they work in.
    """
    available = memberships_for(session, user)
    if not available:
        raise NotAMemberError("this account belongs to no company")
    if company_code is None:
        for company in available:
            if company.status == "active":
                return company
        raise CompanySuspended(f"{available[0].code} is suspended")
    for company in available:
        if company.code.lower() == company_code.strip().lower():
            if company.status != "active":
                raise CompanySuspended(f"{company.code} is suspended")
            return company
    raise UnknownCompanyCode(
        f"{user.login!r} does not work in a company called {company_code!r}"
    )


def open_session(
    session: Session,
    *,
    user: AppUser,
    company_id: uuid.UUID,
    user_agent: str | None = None,
    address: str | None = None,
    mfa_confirmed: bool = False,
    now: dt.datetime | None = None,
) -> SignIn:
    """Mint a session row and return the token that carries it (shown once)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    token = secrets.token_urlsafe(32)
    company = session.get(Company, company_id)
    if company is None:
        raise UnknownCompanyCode(f"no company {company_id}")
    row = UserSession(
        token_hash=_hash_token(token),
        user_id=user.id,
        active_company_id=company_id,
        expires_at=now + SESSION_TTL,
        last_seen_at=now,
        mfa_confirmed_at=now if mfa_confirmed else None,
        user_agent=(user_agent or None),
        address=(address or None),
    )
    session.add(row)
    session.flush()
    return SignIn(user=user, company=company, token=token, session_row=row)


def requires_mfa(session: Session, user: AppUser) -> bool:
    """Whether this user owes a second factor on every sign-in.

    A user who has **enrolled** one always owes it: the factor is not decorative. The
    role side of the requirement — `mfa_required_roles` — is enforced where access is
    granted instead, by :func:`mfa_required_for_role` at the role-assignment route, so
    a subject cannot hold an administrator role without a second factor and an install
    with no enrolled factor yet is not locked out of itself.
    """
    entry = session.get(UserTotp, user.id)
    return entry is not None and entry.confirmed_at is not None


def sign_in(
    session: Session,
    *,
    login: str,
    password: str,
    company_code: str | None = None,
    user_agent: str | None = None,
    address: str | None = None,
) -> SignIn:
    """Verify a login and password and open a session, or refuse.

    Every refusal is the same :class:`RefusedSignIn` with the same shape of message.
    """
    user = user_by_login(session, login)
    if user is None:
        # Hash anyway: a caller cannot tell "no such user" from "wrong password" by
        # how long the answer took, which is the timing oracle the identical
        # message is meant to close.
        _PASSWORDS.hash(password or "")
        raise RefusedSignIn("those credentials were not accepted")
    if user.status != "active":
        _PASSWORDS.hash(password or "")
        raise RefusedSignIn("those credentials were not accepted")
    if not verify_password(session, user, password):
        raise RefusedSignIn("those credentials were not accepted")
    company = _choose_company(session, user, company_code)
    return open_session(
        session,
        user=user,
        company_id=company.id,
        user_agent=user_agent,
        address=address,
        mfa_confirmed=not requires_mfa(session, user),
    )


def resolve_session(
    session: Session, token: str | None, *, now: dt.datetime | None = None
) -> SignIn | None:
    """The live session that token carries, or None if there is none.

    A session is live when it is not revoked, has not expired, and has not been idle
    past the idle timeout. Reading it touches ``last_seen_at``, which is what makes
    the idle timeout more than a column.
    """
    if not token:
        return None
    now = now or dt.datetime.now(dt.timezone.utc)
    row = session.scalars(
        select(UserSession).where(UserSession.token_hash == _hash_token(token))
    ).one_or_none()
    if row is None or row.revoked_at is not None or row.expires_at <= now:
        return None
    if row.last_seen_at + SESSION_IDLE_TIMEOUT <= now:
        row.revoked_at = now
        session.commit()
        return None
    user = session.get(AppUser, row.user_id)
    company = session.get(Company, row.active_company_id)
    if user is None or company is None or user.status != "active":
        return None
    row.last_seen_at = now
    session.flush()
    return SignIn(user=user, company=company, token=token, session_row=row)


def switch_company(session: Session, sign_in_state: SignIn, company_code: str) -> SignIn:
    """Move a session to another company the user belongs to. Audited by the caller."""
    company = _choose_company(session, sign_in_state.user, company_code)
    sign_in_state.session_row.active_company_id = company.id
    session.flush()
    return SignIn(
        user=sign_in_state.user,
        company=company,
        token=sign_in_state.token,
        session_row=sign_in_state.session_row,
    )


def revoke_session(session: Session, token: str) -> bool:
    """Sign one session out. True when a live session was actually revoked."""
    row = session.scalars(
        select(UserSession).where(UserSession.token_hash == _hash_token(token))
    ).one_or_none()
    if row is None or row.revoked_at is not None:
        return False
    row.revoked_at = dt.datetime.now(dt.timezone.utc)
    session.flush()
    return True


def revoke_sessions(session: Session, *, user: AppUser) -> int:
    """Revoke every live session a user holds. Returns how many were live."""
    now = dt.datetime.now(dt.timezone.utc)
    live = list(
        session.scalars(
            select(UserSession).where(
                UserSession.user_id == user.id, UserSession.revoked_at.is_(None)
            )
        )
    )
    for row in live:
        row.revoked_at = now
    session.flush()
    return len(live)


def sessions_for(session: Session, *, user: AppUser) -> list[UserSession]:
    """A user's sessions, newest first."""
    return list(
        session.scalars(
            select(UserSession)
            .where(UserSession.user_id == user.id)
            .order_by(UserSession.issued_at.desc())
        )
    )


# --- The second factor (TOTP, RFC 6238) --------------------------------------

TOTP_DIGITS = 6
TOTP_STEP_SECONDS = 30
# How many steps either side of now are accepted — clocks drift, and a code that
# expires between the prompt and the keystroke is a support call.
TOTP_WINDOW = 1


class MfaError(IdentityError):
    """A second factor that did not verify."""


def _totp_at(secret: str, counter: int) -> str:
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10**TOTP_DIGITS)).zfill(TOTP_DIGITS)


def totp_now(secret: str, *, at: dt.datetime | None = None) -> str:
    """The code for the step that `at` falls in."""
    at = at or dt.datetime.now(dt.timezone.utc)
    return _totp_at(secret, int(at.timestamp()) // TOTP_STEP_SECONDS)


def verify_totp(secret: str, code: str, *, at: dt.datetime | None = None) -> bool:
    """Whether `code` is the code for now, or for a step either side of it."""
    at = at or dt.datetime.now(dt.timezone.utc)
    step = int(at.timestamp()) // TOTP_STEP_SECONDS
    candidate = (code or "").strip().replace(" ", "")
    return any(
        hmac.compare_digest(_totp_at(secret, step + drift), candidate)
        for drift in range(-TOTP_WINDOW, TOTP_WINDOW + 1)
    )


def enrol_totp(session: Session, user: AppUser) -> str:
    """Issue (or re-issue) a second-factor secret. Unconfirmed until a code verifies."""
    secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
    row = session.get(UserTotp, user.id)
    if row is None:
        row = UserTotp(user_id=user.id, secret=secret)
        session.add(row)
    else:
        row.secret = secret
        row.confirmed_at = None
    session.flush()
    return secret


def confirm_totp(session: Session, user: AppUser, code: str) -> list[str]:
    """Confirm enrolment with a code, and return the recovery codes (shown once)."""
    row = session.get(UserTotp, user.id)
    if row is None:
        raise MfaError("no second factor has been enrolled")
    if not verify_totp(row.secret, code):
        raise MfaError("that code is not valid")
    row.confirmed_at = dt.datetime.now(dt.timezone.utc)
    session.flush()
    return issue_recovery_codes(session, user)


def issue_recovery_codes(session: Session, user: AppUser) -> list[str]:
    """Replace a user's recovery codes, returning the plaintext once."""
    for old in session.scalars(
        select(UserRecoveryCode).where(UserRecoveryCode.user_id == user.id)
    ):
        session.delete(old)
    codes: list[str] = []
    for _ in range(RECOVERY_CODE_COUNT):
        code = "-".join(secrets.token_hex(2) for _ in range(2))
        codes.append(code)
        session.add(
            UserRecoveryCode(user_id=user.id, code_hash=hashlib.sha256(code.encode()).hexdigest())
        )
    session.flush()
    return codes


def redeem_recovery_code(session: Session, user: AppUser, code: str) -> bool:
    """Spend a recovery code. A code works once and only once."""
    digest = hashlib.sha256((code or "").strip().lower().encode()).hexdigest()
    row = session.scalars(
        select(UserRecoveryCode).where(
            UserRecoveryCode.user_id == user.id, UserRecoveryCode.code_hash == digest
        )
    ).one_or_none()
    if row is None or row.used_at is not None:
        return False
    row.used_at = dt.datetime.now(dt.timezone.utc)
    session.flush()
    return True


def confirm_session_mfa(session: Session, sign_in_state: SignIn, code: str) -> SignIn:
    """Pass the second factor for a pending session, by code or recovery code."""
    row = session.get(UserTotp, sign_in_state.user.id)
    accepted = False
    if row is not None and row.confirmed_at is not None:
        accepted = verify_totp(row.secret, code)
    if not accepted:
        accepted = redeem_recovery_code(session, sign_in_state.user, code)
    if not accepted:
        raise MfaError("that code is not valid")
    sign_in_state.session_row.mfa_confirmed_at = dt.datetime.now(dt.timezone.utc)
    session.flush()
    return sign_in_state


def purge_expired_sessions(session: Session, *, now: dt.datetime | None = None) -> int:
    """Delete sessions that expired or were revoked more than a day ago.

    Housekeeping, not security — a revoked row is already inert; this only stops the
    table growing without bound. T-8.WORK.01 schedules it.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=1)
    dead = list(
        session.scalars(
            select(UserSession).where(
                (UserSession.expires_at < cutoff)
                | (
                    UserSession.revoked_at.is_not(None)
                    & (UserSession.revoked_at < cutoff)
                )
            )
        )
    )
    for row in dead:
        session.delete(row)
    session.flush()
    return len(dead)


def mfa_required_for_role(role_code: str) -> bool:
    """Whether holding this role obliges its holder to present a second factor.

    Read by the role-assignment route: a subject cannot be given a role the
    `mfa_required_roles` variable names until it has a confirmed second factor, so
    the requirement is enforced at the moment access is granted rather than
    discovered at the next sign-in.
    """
    return role_code in {name.strip() for name in MFA_REQUIRED_ROLES if name.strip()}
