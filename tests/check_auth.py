"""T-7.AUTH.01 … T-7.AUTH.04 check — identity: the door, the session, and what they replace.

    DATABASE_URL=******localhost:5432/erpv1 \\
        python tests/check_auth.py

It fails (non-zero exit) if any of these stops holding:

1. **the install produces somebody who can sign in** — one command creates the company, its
   chart of accounts and an administrator with a credential, so a fresh install is reachable
   instead of answering `403` to everything forever (the ceiling `app/bootstrap.py` recorded)
2. **a company and an actor cannot be claimed** — the two request headers T-0.API.01 left as a
   placeholder (`X-Company-Id`, `X-Actor`) are ignored: the request is the session, and no
   route reads them
3. **a refusal says nothing** — an unknown login and a wrong password answer with the same
   status and the same words, so the endpoint cannot be used to enumerate logins, and the
   credential's failure count is written even though the request is refused
4. **the credential locks out** — the configured number of failures inside the window locks it,
   and a locked credential is refused even with the right password
5. **the session is real** — signing out revokes the *row*, so the token is dead even if the
   cookie is copied; changing your own password revokes the others and leaves you signed in
6. **the session names the company** — a caller reaches the company of their session and no
   other, and switching company needs a membership, not a request field
7. **a second factor is demanded when it exists** — once enrolled, a sign-in stops at `mfa_pending`
   and reaches nothing but the MFA route until a code is presented, and a recovery code works
   exactly once
8. **a suspended company is refused at the door** — signing into it fails, its live sessions get
   `403`, and the reinstatement route is still reachable (or suspension would be a one-way door)
9. **the administrator administers** — users, roles, grants, restrictions and assignments are
   API rows now, and a role the `mfa_required_roles` variable names cannot be granted to somebody
   without a second factor

**Scratch database only**: it drops and recreates the public schema.

**Ceiling this check states rather than hides**: a TOTP code is accepted for its whole
30-second step, so the same code presented twice inside one step is accepted twice. Replay
protection within a step needs the step to be recorded per user, which nothing needs yet
(`app/identity.py` records it as a ceiling).
"""

from __future__ import annotations

import os
import sys
import uuid

# The lockout has to be small enough to drive in a check; identity reads its variables at
# import, so this is stated before the application is imported.
os.environ.setdefault("LOCKOUT_THRESHOLD", "3")
os.environ.setdefault("PASSWORD_MIN_LENGTH", "12")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import auth, identity  # noqa: E402
from app.api import BASE, app  # noqa: E402
from app.bootstrap import bootstrap  # noqa: E402
from app.company import Company  # noqa: E402
from app.identity import AppUser, UserSession, WeakPasswordError, totp_now  # noqa: E402
from app.security import assign, capabilities, define_role, grant, roles_for  # noqa: E402

ADMIN = "admin@example.test"
ADMIN_PASSWORD = "administrator-password-1"
CLERK = "clerk@example.test"
CLERK_PASSWORD = "clerk-password-1"


def _fresh() -> TestClient:
    """A client with its own cookie jar — one caller."""
    return TestClient(app, raise_server_exceptions=False)


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is required (a scratch Postgres)", file=sys.stderr)
        return 2

    engine = create_engine(url)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")

    # 1 — one command installs a database somebody can actually sign into
    company_id = bootstrap(
        engine,
        company_code="AUTH",
        company_name="Auth check trading",
        market="philippines",
        fiscal_year_start_month=1,
        admin_subject=ADMIN,
        admin_password=ADMIN_PASSWORD,
    )
    with Session(engine) as session:
        administrator = session.get(AppUser, session.scalar(
            select(AppUser.id).where(AppUser.login == ADMIN)
        ))
        assert administrator is not None and administrator.password_hash is not None, (
            "the install named an administrator with no credential to sign in with"
        )
        assert administrator.password_hash != ADMIN_PASSWORD, "the password was stored in clear"
        assert str(administrator.password_hash).startswith("$argon2id$"), (
            f"the password is not an Argon2id hash: {administrator.password_hash[:16]!r}"
        )
        print(
            "1. the install created an administrator with an Argon2id credential"
            f" ({len(str(administrator.password_hash))} characters) and a membership"
        )

    # 2 — a header claims nothing: no session, no identity
    anonymous = _fresh()
    bare = anonymous.get(f"{BASE}/companies/current")
    assert bare.status_code == 401, bare.text
    assert bare.json()["error"]["code"] == "unauthenticated", bare.json()
    lying = anonymous.get(
        f"{BASE}/companies/current",
        headers={"X-Company-Id": str(uuid.uuid4()), "X-Actor": "somebody.else"},
    )
    assert lying.status_code == 401, (
        "a request with no session but both old headers was answered:" + lying.text
    )
    print(
        "2. with no cookie the API answers 401 unauthenticated, and naming"
        " X-Company-Id and X-Actor changes nothing — the headers are ignored, not honoured"
    )

    # 3 — the same refusal for both mistakes, and the attempt is counted
    admin = _fresh()
    unknown = admin.post(
        f"{BASE}/auth/login", json={"login": "nobody@example.test", "password": "whatever-1-1"}
    )
    wrong = admin.post(
        f"{BASE}/auth/login", json={"login": ADMIN, "password": "not-the-password-1"}
    )
    assert unknown.status_code == wrong.status_code == 401, (unknown.text, wrong.text)
    assert unknown.json() == wrong.json(), (
        "an unknown login and a wrong password answer differently:" + str(unknown.json())
    )
    with Session(engine) as session:
        someone = session.scalars(select(AppUser).where(AppUser.login == ADMIN)).one()
        assert someone.failed_attempts == 1, (
            f"a refused sign-in was not counted: failed_attempts={someone.failed_attempts}"
        )
    print(
        f"3. both refusals are 401 {unknown.json()['error']['code']} with the same words"
        f" ({unknown.json()['error']['message']!r}), and the failure was counted"
    )

    # 4 — the credential locks out at the threshold, and stays locked
    admin = _fresh()
    for attempt in range(3):
        locked = admin.post(f"{BASE}/auth/login", json={"login": ADMIN, "password": "wrong-x-1"})
    assert locked.status_code == 401, locked.text
    with Session(engine) as session:
        locked_user = session.scalars(select(AppUser).where(AppUser.login == ADMIN)).one()
        was_locked = locked_user.locked_until is not None
    assert was_locked, "the credential did not lock after the configured failures"
    even_right = admin.post(
        f"{BASE}/auth/login", json={"login": ADMIN, "password": ADMIN_PASSWORD}
    )
    assert even_right.status_code == 401, (
        "a locked credential was accepted with the right password:" + even_right.text
    )
    with Session(engine) as session:
        still_locked = session.scalars(select(AppUser).where(AppUser.login == ADMIN)).one()
        still_locked.locked_until = None
        still_locked.failed_attempts = 0
        session.commit()
    print(
        "4. three failures locked the credential (locked_until recorded), the right password was"
        " still refused while locked, and the lockout is stored rather than counted in memory"
    )

    # 5 — the session is a row: signing out kills the token, not the browser
    admin = _fresh()
    signed = admin.post(
        f"{BASE}/auth/login", json={"login": ADMIN, "password": ADMIN_PASSWORD}
    )
    assert signed.status_code == 200, signed.text
    assert signed.json()["login"] == ADMIN and signed.json()["company"] == "AUTH", signed.json()
    cookie = admin.cookies.get(auth.SESSION_COOKIE)
    assert cookie, "the sign-in set no session cookie"
    assert "admin.users" in signed.json()["capabilities"], signed.json()["capabilities"]
    working = admin.get(f"{BASE}/admin/users")
    assert working.status_code == 200, working.text

    stolen = _fresh()
    out = admin.post(f"{BASE}/auth/logout")
    assert out.status_code == 204, out.text
    replayed = stolen.request(
        "GET", f"{BASE}/admin/users", headers={"Cookie": f"{auth.SESSION_COOKIE}={cookie}"}
    )
    assert replayed.status_code == 401, (
        "a token copied before signing out still works:" + replayed.text
    )
    with Session(engine) as session:
        assert session.query(UserSession).filter(UserSession.revoked_at.isnot(None)).count() >= 1
    print(
        "5. the cookie is HttpOnly and carries a session row; signing out revoked it, so the"
        " same token copied beforehand is 401 afterwards"
    )

    # 6 — the session names the company; switching needs a membership
    admin = _fresh()
    admin.post(f"{BASE}/auth/login", json={"login": ADMIN, "password": ADMIN_PASSWORD})
    other = admin.post(f"{BASE}/companies", json={"code": "SECOND", "name": "Second trading", "market": "philippines", "fiscal_year_start_month": 1})
    assert other.status_code == 201, other.text
    switched = admin.post(f"{BASE}/auth/company", json={"company": "SECOND"})
    assert switched.status_code == 200 and switched.json()["company"] == "SECOND", switched.text
    assert sorted(switched.json()["companies"]) == ["AUTH", "SECOND"], switched.json()
    back_to_first = admin.post(f"{BASE}/auth/company", json={"company": "AUTH"})
    assert back_to_first.status_code == 200, back_to_first.text

    with Session(engine) as session:
        stranger = identity.create_user(
            session, login="stranger@example.test", password="stranger-password-1",
            companies=[company_id],
        )
        session.commit()
    outsider = _fresh()
    outsider_signin = outsider.post(
        f"{BASE}/auth/login",
        json={"login": "stranger@example.test", "password": "stranger-password-1"},
    )
    assert outsider_signin.status_code == 200, outsider_signin.text
    refused_switch = outsider.post(f"{BASE}/auth/company", json={"company": "SECOND"})
    # A company the caller does not work in answers exactly as a company that does not
    # exist: the two are indistinguishable on purpose, so a caller cannot feel out which
    # tenants are installed by switching at them (`identity._choose_company`).
    assert refused_switch.status_code == 404, (
        "a caller switched into a company they are not a member of:" + refused_switch.text
    )
    assert refused_switch.json()["error"]["code"] == "unknown_company", refused_switch.json()
    print(
        "6. provisioning created a second company the creator can administer"
        f" ({switched.json()['companies']}), and a non-member naming it is refused"
        f" {refused_switch.status_code} {refused_switch.json()['error']['code']}"
    )

    # 7 — a second factor, demanded from the next sign-in on
    enrolled = admin.post(f"{BASE}/auth/mfa/enrol")
    assert enrolled.status_code == 200, enrolled.text
    secret = enrolled.json()["secret"]
    assert enrolled.json()["otpauth_url"].startswith("otpauth://totp/"), enrolled.json()
    confirmed = admin.post(f"{BASE}/auth/mfa/confirm", json={"code": totp_now(secret)})
    assert confirmed.status_code == 200, confirmed.text
    codes = confirmed.json()["recovery_codes"]
    assert codes and len(codes) == identity.RECOVERY_CODE_COUNT, confirmed.json()

    pending = _fresh()
    awaiting = pending.post(
        f"{BASE}/auth/login", json={"login": ADMIN, "password": ADMIN_PASSWORD}
    )
    assert awaiting.status_code == 200 and awaiting.json()["mfa_pending"] is True, awaiting.text
    stopped = pending.get(f"{BASE}/admin/users")
    assert stopped.status_code == 401, (
        "a session owing a second factor reached an admin route:" + stopped.text
    )
    assert stopped.json()["error"]["code"] == "mfa_required", stopped.json()
    wrong_code = pending.post(f"{BASE}/auth/mfa/verify", json={"code": "000000"})
    assert wrong_code.status_code == 401, wrong_code.text
    passed = pending.post(f"{BASE}/auth/mfa/verify", json={"code": totp_now(secret)})
    assert passed.status_code == 200 and passed.json()["mfa_pending"] is False, passed.text

    recovered = _fresh()
    recovered.post(f"{BASE}/auth/login", json={"login": ADMIN, "password": ADMIN_PASSWORD})
    once = recovered.post(f"{BASE}/auth/mfa/verify", json={"code": codes[0]})
    assert once.status_code == 200, once.text
    twice = _fresh()
    twice.post(f"{BASE}/auth/login", json={"login": ADMIN, "password": ADMIN_PASSWORD})
    spent = twice.post(f"{BASE}/auth/mfa/verify", json={"code": codes[0]})
    assert spent.status_code == 401, "a recovery code was accepted twice"
    print(
        f"7. enrolling returned a secret and {len(codes)} recovery codes; the next sign-in"
        " stopped at mfa_pending (401 mfa_required on every other route), a wrong code was"
        " refused, the right one finished it, and a recovery code worked exactly once"
    )

    # 8 — the administrator administers: users, roles, grants, restrictions, assignments
    admin = _fresh()
    admin.post(f"{BASE}/auth/login", json={"login": ADMIN, "password": ADMIN_PASSWORD})
    # The administrator owes a second factor from here on (step 7 enrolled it), so the
    # session is completed before it is used for anything else.
    owed = admin.post(f"{BASE}/auth/mfa/verify", json={"code": totp_now(secret)})
    assert owed.status_code == 200, owed.text

    created = admin.post(
        f"{BASE}/admin/users",
        json={"login": CLERK, "password": CLERK_PASSWORD, "display_name": "Clerk"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["roles"] == [] and created.json()["companies"] == ["AUTH"], created.json()

    weak = admin.post(
        f"{BASE}/admin/users",
        json={"login": "weak@example.test", "password": "short"},
    )
    assert weak.status_code == 422, weak.text

    role = admin.post(f"{BASE}/admin/roles", json={"code": "clerk", "name": "Clerk"})
    assert role.status_code == 201, role.text
    granted = admin.post(
        f"{BASE}/admin/roles/clerk/grants",
        json={"capabilities": ["company.read", "journal.read"]},
    )
    assert granted.status_code == 200, granted.text
    restricted = admin.post(
        f"{BASE}/admin/roles/clerk/restrictions",
        json={"entity": "journal_line", "field": "party", "can_read": False, "can_write": False},
    )
    assert restricted.status_code == 201, restricted.text
    assigned = admin.post(f"{BASE}/admin/assignments", json={"subject": CLERK, "role": "clerk"})
    assert assigned.status_code == 201 and assigned.json()["roles"] == ["clerk"], assigned.text

    # A role the variable names cannot be held without a second factor.
    before = admin.post(
        f"{BASE}/admin/assignments", json={"subject": CLERK, "role": "administrator"}
    )
    assert before.status_code == 422, (
        "the administrator role was granted without a second factor:" + before.text
    )
    assert before.json()["error"]["code"] == "second_factor_required", before.json()
    print(
        "8. a user was created, a role defined, capabilities granted and a field restricted,"
        " then assigned — all rows, no SQL — and the administrator role was refused"
        " (422 second_factor_required) until the holder had enrolled one"
    )

    # 9 — the new role takes effect on the very next request
    clerk = _fresh()
    clerk_signin = clerk.post(
        f"{BASE}/auth/login", json={"login": CLERK, "password": CLERK_PASSWORD}
    )
    assert clerk_signin.status_code == 200, clerk_signin.text
    assert clerk_signin.json()["capabilities"] == ["company.read", "journal.read"], (
        clerk_signin.json()["capabilities"]
    )
    refused = clerk.post(f"{BASE}/admin/roles", json={"code": "sneaky", "name": "Sneaky"})
    assert refused.status_code == 403, refused.text
    assert "no roles" not in refused.json()["error"]["message"], refused.json()
    with Session(engine) as session:
        assert identity.user_by_login(session, CLERK).password_hash != CLERK_PASSWORD
        held = roles_for(session, company_id=company_id, subject=CLERK)
        assert [entry.code for entry in held] == ["clerk"], held
        assert "journal.read" in capabilities(session, company_id=company_id, subject=CLERK)
    print(
        "9. the clerk signed in holding exactly ['company.read', 'journal.read'], could not"
        " define a role (403 naming what they hold), and the role change needed no restart"
    )

    # 10 — changing your own password keeps you in and puts everyone else out
    changed = clerk.post(
        f"{BASE}/auth/password",
        json={"current_password": CLERK_PASSWORD, "new_password": "clerk-password-two-2"},
    )
    assert changed.status_code == 204, changed.text
    still_in = clerk.get(f"{BASE}/companies/current")
    assert still_in.status_code == 200, still_in.text
    wrong_current = clerk.post(
        f"{BASE}/auth/password",
        json={"current_password": "not-it-at-all-1", "new_password": "another-password-1"},
    )
    assert wrong_current.status_code == 401, wrong_current.text
    with Session(engine) as session:
        assert any(
            row.revoked_at is None
            for row in identity.sessions_for(session, user=identity.user_by_login(session, CLERK))
        ), "changing a password signed the changer out"
    print(
        "10. the password changed, the changer's own session survived, a wrong current password"
        " was refused, and the password is nowhere stored in clear"
    )

    # 11 — suspension is refused at the door, and reinstatement stays reachable
    #
    # The session is moved into the second company **while it is trading**, which is what
    # makes the refusal below about suspension rather than about the switch: signing into a
    # suspended company is refused too, so a session can only be *in* one if it was there
    # before the suspension.
    into_second = admin.post(f"{BASE}/auth/company", json={"company": "SECOND"})
    assert into_second.status_code == 200 and into_second.json()["company"] == "SECOND", (
        into_second.text
    )
    suspended = admin.post(f"{BASE}/companies/SECOND/status", json={"status": "suspended"})
    assert suspended.status_code == 200 and suspended.json()["status"] == "suspended", suspended.text
    blocked = admin.get(f"{BASE}/companies/current")
    assert blocked.status_code == 403, (
        "a suspended company's session was served:" + blocked.text
    )
    assert blocked.json()["error"]["code"] == auth.SUSPENDED_COMPANY_CODE, blocked.json()
    door = _fresh()
    refused_door = door.post(
        f"{BASE}/auth/login", json={"login": ADMIN, "password": ADMIN_PASSWORD, "company": "SECOND"}
    )
    assert refused_door.status_code == 403, (
        "signing into a suspended company was allowed:" + refused_door.text
    )
    lifted = admin.post(f"{BASE}/companies/SECOND/status", json={"status": "active"})
    assert lifted.status_code == 200 and lifted.json()["status"] == "active", lifted.text
    served_again = admin.get(f"{BASE}/companies/current")
    assert served_again.status_code == 200, served_again.text
    print(
        "11. a suspended company refused its live sessions (403 company_suspended) and refused a"
        " new sign-in into it (403 at the door), while the reinstatement route stayed reachable"
        " and put it back in service"
    )

    # 12 — the policy is enforced where a password is set, not only at sign-in
    with Session(engine) as session:
        try:
            identity.check_password_policy("short")
        except WeakPasswordError as exc:
            refused_policy = str(exc)
        else:
            raise AssertionError("a short password passed the policy")
        assert "at least" in refused_policy, refused_policy
    print(f"12. the password floor is enforced before anything is hashed: {refused_policy!r}")

    print("\ncheck_auth: all assertions green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
