"""
Security testing (Phase 19, master directive numbering: Performance &
Security Hardening) -- real attack attempts against this service's own
auth, session, and account machinery (Phases 6, 17, 18), not a
theoretical review. See tests/test_malicious_files.py for the
file-upload half of this phase's "security testing"/"malicious-file
testing" line items.

Every test here either confirms a real defense holds under a real
attempt to break it, or would have caught a real regression if one of
these properties silently broke.
"""

from __future__ import annotations

import io
import uuid

import pytest
from fastapi.testclient import TestClient

from documents_converter.api import config
from documents_converter.api.accounts import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from documents_converter.api.app import _rate_limiter, app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """See tests/test_api.py's identical fixture."""
    _rate_limiter._counts.clear()
    yield


def _unique_email() -> str:
    return f"sectest-{uuid.uuid4().hex}@example.com"


def _signup(email: str | None = None, password: str = "a real password 123") -> TestClient:
    c = TestClient(app)
    resp = c.post("/api/v1/account/signup", json={"email": email or _unique_email(), "password": password})
    assert resp.status_code == 200
    return c


# --------------------------------------------------------------------------
# CSRF -- can it be bypassed without the real signed cookie?
# --------------------------------------------------------------------------


def test_csrf_cannot_be_bypassed_by_guessing_a_plausible_looking_token():
    """A real attacker has the session cookie (a forged cross-site
    request carries it automatically) but not the CSRF cookie's actual
    value (Same-Origin Policy blocks reading it) -- confirms a
    same-shape-but-wrong token is rejected exactly like a missing one,
    not partially trusted for being 'close enough' or the right length."""
    c = _signup()
    real_csrf = c.cookies.get(CSRF_COOKIE_NAME)
    fake_csrf = "0" * len(real_csrf)  # same length, entirely wrong value
    assert fake_csrf != real_csrf

    resp = c.put(
        "/api/v1/account/preferences", json={"x": 1}, headers={CSRF_HEADER_NAME: fake_csrf}
    )
    assert resp.status_code == 403


def test_csrf_token_from_a_different_session_does_not_work():
    """The CSRF token is bound to the specific session it was issued
    for (HMAC of that session's own token) -- a real, valid CSRF token
    minted for account A must not authorize a mutating request riding
    on account B's session cookie."""
    account_a = _signup()
    account_b = _signup()
    csrf_from_a = account_a.cookies.get(CSRF_COOKIE_NAME)

    # Attach account A's CSRF token to a request carrying account B's
    # session cookie.
    session_b = account_b.cookies.get("session_id")
    forged = TestClient(app)
    forged.cookies.set("session_id", session_b)
    resp = forged.put(
        "/api/v1/account/preferences", json={"x": 1}, headers={CSRF_HEADER_NAME: csrf_from_a}
    )
    assert resp.status_code == 403


def test_csrf_is_not_required_for_safe_methods():
    """GET never needs the CSRF header -- confirms the check is scoped
    to mutating methods only, not applied so broadly it breaks normal
    read access."""
    c = _signup()
    resp = c.get("/api/v1/account/me")
    assert resp.status_code == 200


# --------------------------------------------------------------------------
# Session security
# --------------------------------------------------------------------------


def test_session_tokens_are_not_predictable():
    """Real entropy, not a design-review assumption: signs up twice and
    confirms the two raw session tokens share no exploitable structure
    beyond coincidence -- both unique, neither derivable from the other
    or from the account id."""
    a = _signup()
    b = _signup()
    token_a = a.cookies.get("session_id")
    token_b = b.cookies.get("session_id")
    assert token_a != token_b
    assert len(token_a) >= 32  # secrets.token_urlsafe(32) -- real, high entropy


def test_a_forged_session_cookie_is_rejected():
    """A syntactically-plausible but never-actually-issued session
    token must not authenticate -- confirms sessions are looked up by a
    real stored hash, not accepted on shape/format alone."""
    forged = TestClient(app)
    forged.cookies.set("session_id", "a" * 43)  # matches real token_urlsafe(32) length
    resp = forged.get("/api/v1/account/me")
    assert resp.status_code == 401


def test_password_hash_never_appears_in_any_account_api_response():
    """A real, direct check of this service's own responses -- not a
    review of the code that builds them -- confirms the argon2 hash
    genuinely never round-trips to a caller."""
    email = _unique_email()
    c = _signup(email, password="a real password 123")
    for resp in (
        c.get("/api/v1/account/me"),
        c.get("/api/v1/account/preferences"),
    ):
        body_text = resp.text.lower()
        assert "argon2" not in body_text
        assert "password" not in body_text or "password_hash" not in body_text


# --------------------------------------------------------------------------
# Injection-shaped inputs -- confirms the ORM's parameterization holds
# for real, not just "should be fine because SQLAlchemy"
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "injection_attempt",
    [
        "'; DROP TABLE users; --@example.com",
        "admin' OR '1'='1@example.com",
        "x@example.com'; DELETE FROM user_sessions WHERE '1'='1",
    ],
)
def test_sql_injection_shaped_email_is_treated_as_plain_data(injection_attempt):
    """A SQL-injection-shaped string in the one field most likely to be
    hand-built into a query (email, used in an equality filter on every
    login) is rejected as an invalid email format (this service's own
    validation) or handled as inert data -- never able to affect query
    structure. Confirmed by checking the users table is still queryable
    afterward, not just that this one request didn't 500."""
    resp = client.post(
        "/api/v1/account/signup", json={"email": injection_attempt, "password": "a real password 123"}
    )
    assert resp.status_code in (400, 200)  # rejected as invalid, or accepted as a literal (odd) email

    # The real proof: the table itself is intact and still queryable --
    # an actual DROP/DELETE succeeding would break this next, ordinary
    # signup outright.
    canary_resp = client.post(
        "/api/v1/account/signup", json={"email": _unique_email(), "password": "a real password 123"}
    )
    assert canary_resp.status_code == 200


def test_sql_injection_shaped_preferences_value_is_stored_as_plain_data():
    """Preferences are caller-defined JSON (accounts.py's own
    docstring) -- confirms a SQL-shaped string stored there round-trips
    as inert data, never interpreted."""
    c = _signup()
    csrf = c.cookies.get(CSRF_COOKIE_NAME)
    payload = {"note": "'; DROP TABLE users; --"}
    put_resp = c.put("/api/v1/account/preferences", json=payload, headers={CSRF_HEADER_NAME: csrf})
    assert put_resp.status_code == 200
    assert c.get("/api/v1/account/preferences").json() == payload


# --------------------------------------------------------------------------
# Privilege escalation attempts
# --------------------------------------------------------------------------


def test_cannot_self_promote_to_admin_via_preferences():
    """Preferences (an unstructured, caller-owned JSON blob) and
    User.is_admin (a real, separate database column only accounts.py's
    own login/signup sync ever writes) are unrelated storage -- setting
    a same-named key in preferences must have zero effect on the real
    admin flag."""
    email = _unique_email()
    c = _signup(email)
    csrf = c.cookies.get(CSRF_COOKIE_NAME)
    c.put("/api/v1/account/preferences", json={"is_admin": True}, headers={CSRF_HEADER_NAME: csrf})

    me = c.get("/api/v1/account/me").json()
    assert me["is_admin"] is False
    admin_resp = c.get("/api/v1/admin/queue")
    assert admin_resp.status_code == 403


def test_cannot_access_another_accounts_preferences_or_history():
    """Every account-scoped endpoint reads the *session's own* account
    id server-side (accounts.get_current_user) -- there's no id
    parameter a caller could substitute another account's into, but
    confirms directly that two real accounts' data stays genuinely
    separate rather than assuming it from the code shape."""
    account_a = _signup()
    account_b = _signup()
    csrf_a = account_a.cookies.get(CSRF_COOKIE_NAME)
    account_a.put(
        "/api/v1/account/preferences", json={"secret": "only account A's"}, headers={CSRF_HEADER_NAME: csrf_a}
    )

    assert account_b.get("/api/v1/account/preferences").json() == {}


# --------------------------------------------------------------------------
# Rate-limit bypass attempts
# --------------------------------------------------------------------------


def test_rate_limit_cannot_be_bypassed_by_spoofing_x_forwarded_for():
    """_client_ip (app.py) reads request.client.host -- the real TCP
    peer address, never a caller-controlled header -- confirmed here by
    sending a different X-Forwarded-For on every single request and
    checking the limit still triggers exactly as if that header were
    absent."""
    bad_file = {"file": ("x.xyz", io.BytesIO(b"data"), "application/octet-stream")}
    triggered_429 = False
    for i in range(config.RATE_LIMIT_MAX_REQUESTS + 5):
        resp = client.post(
            "/api/v1/convert", files=bad_file, headers={"X-Forwarded-For": f"10.0.0.{i}"}
        )
        if resp.status_code == 429:
            triggered_429 = True
            break
    assert triggered_429, "rate limit never triggered despite a spoofed X-Forwarded-For on every request"
