"""
Tests for the Phase 18 (master directive numbering: Administration &
Observability) endpoints: GET /metrics, GET /api/v1/admin/queue,
/providers, /analytics, /audit-log, and the ADMIN_EMAILS
promotion/demotion behavior (accounts.py) that gates them.

Every test uses a fresh, randomly-generated email -- see
tests/test_accounts_api.py's own module docstring for why (this
project's local dev database is real and persistent; User.email is
UNIQUE).
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from documents_converter.api import config
from documents_converter.api.app import _rate_limiter, app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """See tests/test_api.py's identical fixture."""
    _rate_limiter._counts.clear()
    yield


def _unique_email() -> str:
    return f"admin-test-{uuid.uuid4().hex}@example.com"


def _signup(email: str, password: str = "a real password 123") -> TestClient:
    c = TestClient(app)
    resp = c.post("/api/v1/account/signup", json={"email": email, "password": password})
    assert resp.status_code == 200
    return c


# --------------------------------------------------------------------------
# GET /metrics -- unauthenticated, like GET /health
# --------------------------------------------------------------------------


def test_metrics_endpoint_is_unauthenticated_and_real_prometheus_format():
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "documents_converter_http_requests_total" in resp.text
    assert "documents_converter_jobs_current" in resp.text


def test_metrics_reflects_a_real_request_that_just_happened():
    client.get("/health")
    resp = client.get("/metrics")
    assert 'path="/health"' in resp.text


# --------------------------------------------------------------------------
# ADMIN_EMAILS promotion/demotion
# --------------------------------------------------------------------------


def test_signup_promotes_an_email_already_in_admin_emails(monkeypatch):
    email = _unique_email()
    monkeypatch.setattr(config, "ADMIN_EMAILS", (email,))
    c = _signup(email)
    me = c.get("/api/v1/account/me")
    assert me.json()["is_admin"] is True


def test_signup_does_not_promote_an_email_not_in_admin_emails(monkeypatch):
    email = _unique_email()
    monkeypatch.setattr(config, "ADMIN_EMAILS", ("someone-else@example.com",))
    c = _signup(email)
    me = c.get("/api/v1/account/me")
    assert me.json()["is_admin"] is False


def test_login_promotes_an_existing_account_added_to_admin_emails_later(monkeypatch):
    email = _unique_email()
    monkeypatch.setattr(config, "ADMIN_EMAILS", ())
    _signup(email)

    monkeypatch.setattr(config, "ADMIN_EMAILS", (email,))
    c = TestClient(app)
    login_resp = c.post("/api/v1/account/login", json={"email": email, "password": "a real password 123"})
    assert login_resp.status_code == 200
    me = c.get("/api/v1/account/me")
    assert me.json()["is_admin"] is True


def test_login_demotes_an_admin_removed_from_admin_emails(monkeypatch):
    email = _unique_email()
    monkeypatch.setattr(config, "ADMIN_EMAILS", (email,))
    _signup(email)

    monkeypatch.setattr(config, "ADMIN_EMAILS", ())
    c = TestClient(app)
    c.post("/api/v1/account/login", json={"email": email, "password": "a real password 123"})
    me = c.get("/api/v1/account/me")
    assert me.json()["is_admin"] is False


# --------------------------------------------------------------------------
# Admin endpoint gating
# --------------------------------------------------------------------------


def test_admin_endpoints_require_login():
    anon_client = TestClient(app)
    for path in (
        "/api/v1/admin/queue",
        "/api/v1/admin/providers",
        "/api/v1/admin/analytics",
        "/api/v1/admin/audit-log",
    ):
        resp = anon_client.get(path)
        assert resp.status_code == 401, path


def test_admin_endpoints_reject_a_non_admin_account(monkeypatch):
    email = _unique_email()
    monkeypatch.setattr(config, "ADMIN_EMAILS", ())
    c = _signup(email)
    for path in (
        "/api/v1/admin/queue",
        "/api/v1/admin/providers",
        "/api/v1/admin/analytics",
        "/api/v1/admin/audit-log",
    ):
        resp = c.get(path)
        assert resp.status_code == 403, path


def test_admin_endpoints_accept_a_real_admin_account(monkeypatch):
    email = _unique_email()
    monkeypatch.setattr(config, "ADMIN_EMAILS", (email,))
    c = _signup(email)
    for path in (
        "/api/v1/admin/queue",
        "/api/v1/admin/providers",
        "/api/v1/admin/analytics",
        "/api/v1/admin/audit-log",
    ):
        resp = c.get(path)
        assert resp.status_code == 200, path


# --------------------------------------------------------------------------
# Response shape / real data
# --------------------------------------------------------------------------


def test_provider_status_reports_every_real_dependency(monkeypatch):
    email = _unique_email()
    monkeypatch.setattr(config, "ADMIN_EMAILS", (email,))
    c = _signup(email)
    body = c.get("/api/v1/admin/providers").json()
    assert set(body.keys()) == {"tesseract", "libreoffice", "redis", "database", "ai_gemini_configured"}
    assert isinstance(body["database"], bool)


def test_job_analytics_reflects_real_job_counts(monkeypatch):
    email = _unique_email()
    monkeypatch.setattr(config, "ADMIN_EMAILS", (email,))
    c = _signup(email)
    body = c.get("/api/v1/admin/analytics").json()
    assert "total_jobs" in body
    assert "jobs_by_status" in body
    assert "jobs_by_target" in body


def test_audit_log_shows_this_session_own_recent_events(monkeypatch):
    email = _unique_email()
    monkeypatch.setattr(config, "ADMIN_EMAILS", (email,))
    c = _signup(email)
    body = c.get("/api/v1/admin/audit-log?limit=50").json()
    events = [e["event"] for e in body["events"]]
    assert "account_signup" in events


def test_audit_log_can_filter_by_event_type(monkeypatch):
    email = _unique_email()
    monkeypatch.setattr(config, "ADMIN_EMAILS", (email,))
    c = _signup(email)
    body = c.get("/api/v1/admin/audit-log?event=account_signup&limit=50").json()
    assert body["events"]
    assert all(e["event"] == "account_signup" for e in body["events"])


def test_audit_log_limit_is_capped(monkeypatch):
    from documents_converter.api import admin as admin_module

    email = _unique_email()
    monkeypatch.setattr(config, "ADMIN_EMAILS", (email,))
    c = _signup(email)
    resp = c.get(f"/api/v1/admin/audit-log?limit={admin_module._MAX_AUDIT_LOG_PAGE + 500}")
    assert resp.status_code == 200
    assert len(resp.json()["events"]) <= admin_module._MAX_AUDIT_LOG_PAGE
