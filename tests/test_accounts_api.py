"""
Tests for the Phase 17 (master directive numbering: Authentication &
User Workspace) endpoints: POST /api/v1/account/signup, /login,
/logout, GET/PUT /api/v1/account/preferences, GET .../usage,
GET .../history, and POST /api/v1/jobs/{id}/save[/unsave].

Every test uses a fresh, randomly-generated email -- this project's
local dev database is a real, persistent SQLite file (not wiped between
test runs), and User.email is UNIQUE, so a fixed literal email would
collide with itself on a second run, the same reason job/batch ids are
random uuids rather than fixed strings.
"""

from __future__ import annotations

import io
import uuid

import fitz
import pytest
from fastapi.testclient import TestClient

from documents_converter.api.accounts import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from documents_converter.api.app import _rate_limiter, app

from conftest import requires_redis

client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """See tests/test_api.py's identical fixture."""
    _rate_limiter._counts.clear()
    yield


def _unique_email() -> str:
    return f"user-{uuid.uuid4().hex}@example.com"


def _signup(email: str | None = None, password: str = "correct horse battery"):
    return client.post(
        "/api/v1/account/signup", json={"email": email or _unique_email(), "password": password}
    )


def _csrf_headers() -> dict:
    """The signed double-submit CSRF cookie is deliberately JS-readable
    (see accounts.py's module docstring) -- tests read it the same way
    real frontend JS would, then echo it back as a header."""
    token = client.cookies.get(CSRF_COOKIE_NAME)
    return {CSRF_HEADER_NAME: token} if token else {}


def _build_pdf_bytes(text="Some Text", width=400, height=150) -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=width, height=height)
    page.insert_text((30, 75), text)
    data = doc.tobytes()
    doc.close()
    return data


# --------------------------------------------------------------------------
# Signup / login / logout
# --------------------------------------------------------------------------


def test_signup_creates_an_account_and_logs_it_in():
    resp = _signup()
    assert resp.status_code == 200
    body = resp.json()
    assert "id" in body and "email" in body
    assert "session_id" in resp.cookies
    assert "csrf_token" in resp.cookies

    me = client.get("/api/v1/account/me")
    assert me.status_code == 200
    assert me.json()["id"] == body["id"]


def test_signup_rejects_a_duplicate_email():
    email = _unique_email()
    assert _signup(email).status_code == 200
    resp = _signup(email)
    assert resp.status_code == 409


def test_signup_rejects_an_invalid_email():
    resp = _signup(email="not-an-email")
    assert resp.status_code == 400


def test_signup_rejects_a_weak_password():
    resp = _signup(password="short")
    assert resp.status_code == 400


def test_login_with_correct_credentials():
    email = _unique_email()
    _signup(email, password="a real password 123")
    resp = client.post("/api/v1/account/login", json={"email": email, "password": "a real password 123"})
    assert resp.status_code == 200
    assert resp.json()["email"] == email


def test_login_with_wrong_password_is_rejected():
    email = _unique_email()
    _signup(email, password="a real password 123")
    resp = client.post("/api/v1/account/login", json={"email": email, "password": "totally wrong"})
    assert resp.status_code == 401


def test_login_with_unknown_email_gives_the_same_error_as_wrong_password():
    resp = client.post(
        "/api/v1/account/login", json={"email": _unique_email(), "password": "whatever it is"}
    )
    assert resp.status_code == 401
    assert "incorrect" in resp.json()["detail"].lower()


def test_me_requires_a_session():
    fresh_client = TestClient(app)
    resp = fresh_client.get("/api/v1/account/me")
    assert resp.status_code == 401


def test_logout_invalidates_the_session():
    fresh_client = TestClient(app)
    fresh_client.post(
        "/api/v1/account/signup", json={"email": _unique_email(), "password": "a real password 123"}
    )
    assert fresh_client.get("/api/v1/account/me").status_code == 200

    csrf = fresh_client.cookies.get(CSRF_COOKIE_NAME)
    logout_resp = fresh_client.post("/api/v1/account/logout", headers={CSRF_HEADER_NAME: csrf})
    assert logout_resp.status_code == 200
    assert fresh_client.get("/api/v1/account/me").status_code == 401


# --------------------------------------------------------------------------
# CSRF enforcement
# --------------------------------------------------------------------------


def test_mutating_account_request_without_csrf_header_is_rejected():
    fresh_client = TestClient(app)
    fresh_client.post(
        "/api/v1/account/signup", json={"email": _unique_email(), "password": "a real password 123"}
    )
    resp = fresh_client.put("/api/v1/account/preferences", json={"theme": "dark"})
    assert resp.status_code == 403


def test_mutating_account_request_with_wrong_csrf_header_is_rejected():
    fresh_client = TestClient(app)
    fresh_client.post(
        "/api/v1/account/signup", json={"email": _unique_email(), "password": "a real password 123"}
    )
    resp = fresh_client.put(
        "/api/v1/account/preferences", json={"theme": "dark"}, headers={CSRF_HEADER_NAME: "wrong-value"}
    )
    assert resp.status_code == 403


def test_mutating_account_request_with_correct_csrf_header_succeeds():
    fresh_client = TestClient(app)
    fresh_client.post(
        "/api/v1/account/signup", json={"email": _unique_email(), "password": "a real password 123"}
    )
    csrf = fresh_client.cookies.get(CSRF_COOKIE_NAME)
    resp = fresh_client.put(
        "/api/v1/account/preferences", json={"theme": "dark"}, headers={CSRF_HEADER_NAME: csrf}
    )
    assert resp.status_code == 200


# --------------------------------------------------------------------------
# Preferences
# --------------------------------------------------------------------------


def test_preferences_round_trip():
    fresh_client = TestClient(app)
    fresh_client.post(
        "/api/v1/account/signup", json={"email": _unique_email(), "password": "a real password 123"}
    )
    csrf = fresh_client.cookies.get(CSRF_COOKIE_NAME)

    assert fresh_client.get("/api/v1/account/preferences").json() == {}

    put_resp = fresh_client.put(
        "/api/v1/account/preferences",
        json={"default_target": "text", "theme": "dark"},
        headers={CSRF_HEADER_NAME: csrf},
    )
    assert put_resp.status_code == 200

    get_resp = fresh_client.get("/api/v1/account/preferences")
    assert get_resp.json() == {"default_target": "text", "theme": "dark"}


# --------------------------------------------------------------------------
# Usage / history / saved jobs
# --------------------------------------------------------------------------


@requires_redis
def test_job_submitted_while_logged_in_is_tagged_and_shows_in_history():
    fresh_client = TestClient(app)
    fresh_client.post(
        "/api/v1/account/signup", json={"email": _unique_email(), "password": "a real password 123"}
    )

    data = _build_pdf_bytes("History Test")
    resp = fresh_client.post(
        "/api/v1/jobs",
        data={"target": "text"},
        files={"file": ("doc.pdf", io.BytesIO(data), "application/pdf")},
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    history = fresh_client.get("/api/v1/account/history")
    assert history.status_code == 200
    job_ids = [j["job_id"] for j in history.json()["jobs"]]
    assert job_id in job_ids

    usage = fresh_client.get("/api/v1/account/usage")
    assert usage.status_code == 200
    assert usage.json()["total_jobs"] >= 1


@requires_redis
def test_job_submitted_anonymously_does_not_appear_in_someone_elses_history():
    data = _build_pdf_bytes("Anonymous Job")
    anon_client = TestClient(app)
    resp = anon_client.post(
        "/api/v1/jobs",
        data={"target": "text"},
        files={"file": ("doc.pdf", io.BytesIO(data), "application/pdf")},
    )
    job_id = resp.json()["job_id"]

    logged_in_client = TestClient(app)
    logged_in_client.post(
        "/api/v1/account/signup", json={"email": _unique_email(), "password": "a real password 123"}
    )
    history = logged_in_client.get("/api/v1/account/history")
    job_ids = [j["job_id"] for j in history.json()["jobs"]]
    assert job_id not in job_ids


@requires_redis
def test_save_and_unsave_a_job():
    fresh_client = TestClient(app)
    fresh_client.post(
        "/api/v1/account/signup", json={"email": _unique_email(), "password": "a real password 123"}
    )
    csrf = fresh_client.cookies.get(CSRF_COOKIE_NAME)

    data = _build_pdf_bytes("Save Test")
    resp = fresh_client.post(
        "/api/v1/jobs",
        data={"target": "text"},
        files={"file": ("doc.pdf", io.BytesIO(data), "application/pdf")},
    )
    job_id = resp.json()["job_id"]

    save_resp = fresh_client.post(f"/api/v1/jobs/{job_id}/save", headers={CSRF_HEADER_NAME: csrf})
    assert save_resp.status_code == 200
    assert save_resp.json()["saved"] is True

    unsave_resp = fresh_client.post(f"/api/v1/jobs/{job_id}/unsave", headers={CSRF_HEADER_NAME: csrf})
    assert unsave_resp.status_code == 200
    assert unsave_resp.json()["saved"] is False


@requires_redis
def test_cannot_save_someone_elses_job():
    owner_client = TestClient(app)
    owner_client.post(
        "/api/v1/account/signup", json={"email": _unique_email(), "password": "a real password 123"}
    )
    data = _build_pdf_bytes("Not Yours")
    resp = owner_client.post(
        "/api/v1/jobs",
        data={"target": "text"},
        files={"file": ("doc.pdf", io.BytesIO(data), "application/pdf")},
    )
    job_id = resp.json()["job_id"]

    other_client = TestClient(app)
    other_client.post(
        "/api/v1/account/signup", json={"email": _unique_email(), "password": "a real password 123"}
    )
    other_csrf = other_client.cookies.get(CSRF_COOKIE_NAME)
    save_resp = other_client.post(f"/api/v1/jobs/{job_id}/save", headers={CSRF_HEADER_NAME: other_csrf})
    assert save_resp.status_code == 403


@requires_redis
def test_cannot_save_an_anonymous_job():
    anon_client = TestClient(app)
    data = _build_pdf_bytes("Anonymous")
    resp = anon_client.post(
        "/api/v1/jobs",
        data={"target": "text"},
        files={"file": ("doc.pdf", io.BytesIO(data), "application/pdf")},
    )
    job_id = resp.json()["job_id"]

    logged_in_client = TestClient(app)
    logged_in_client.post(
        "/api/v1/account/signup", json={"email": _unique_email(), "password": "a real password 123"}
    )
    csrf = logged_in_client.cookies.get(CSRF_COOKIE_NAME)
    save_resp = logged_in_client.post(f"/api/v1/jobs/{job_id}/save", headers={CSRF_HEADER_NAME: csrf})
    assert save_resp.status_code == 403


@requires_redis
def test_save_requires_login():
    anon_client = TestClient(app)
    data = _build_pdf_bytes("No Login")
    resp = anon_client.post(
        "/api/v1/jobs",
        data={"target": "text"},
        files={"file": ("doc.pdf", io.BytesIO(data), "application/pdf")},
    )
    job_id = resp.json()["job_id"]
    save_resp = anon_client.post(f"/api/v1/jobs/{job_id}/save")
    assert save_resp.status_code == 401
