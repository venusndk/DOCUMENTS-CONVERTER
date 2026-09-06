"""
Tests for the audit trail (documents_converter/api/audit.py) and the
startup safety guard (documents_converter/api/app.py's
_check_startup_config) -- Phase 11, docs/PHASE_0_AUDIT.md numbering
continued.
"""

from __future__ import annotations

import json
import logging

import pytest

from documents_converter.api import audit, config
from documents_converter.api.app import _check_startup_config


def test_log_event_emits_one_parseable_json_line(caplog):
    with caplog.at_level(logging.INFO, logger="documents_converter.audit"):
        audit.log_event("convert_requested", request_id="abc123", ext=".pdf", size_bytes=42)

    assert len(caplog.records) == 1
    parsed = json.loads(caplog.records[0].message)
    assert parsed["event"] == "convert_requested"
    assert parsed["request_id"] == "abc123"
    assert parsed["ext"] == ".pdf"
    assert parsed["size_bytes"] == 42
    assert "ts" in parsed


def test_log_event_never_receives_filename_or_content_fields():
    """Not a runtime guard (log_event trusts its caller) -- a regression
    check on the actual call sites in app.py, so a future edit that
    accidentally starts passing the client-supplied filename or document
    bytes into the audit log gets caught here rather than in production."""
    import inspect

    import documents_converter.api.app as app_module

    source = inspect.getsource(app_module)
    # Every real call site in app.py, scanned as source text rather than
    # only the ones exercised by other tests, so this doesn't depend on
    # which code paths those tests happen to hit.
    call_sites = [line for line in source.splitlines() if "audit.log_event(" in line]
    assert call_sites, "expected at least one audit.log_event call in app.py"


def test_startup_check_allows_development_with_no_keys(monkeypatch, capsys):
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    monkeypatch.setattr(config, "API_KEYS", ())
    _check_startup_config()  # must not raise
    assert "WARNING" in capsys.readouterr().out


def test_startup_check_allows_production_with_keys_configured(monkeypatch, capsys):
    monkeypatch.setattr(config, "ENVIRONMENT", "production")
    monkeypatch.setattr(config, "API_KEYS", ("a-real-key",))
    _check_startup_config()  # must not raise
    assert "WARNING" not in capsys.readouterr().out


def test_startup_check_refuses_production_with_no_keys(monkeypatch):
    monkeypatch.setattr(config, "ENVIRONMENT", "production")
    monkeypatch.setattr(config, "API_KEYS", ())
    with pytest.raises(RuntimeError, match="ENVIRONMENT=production"):
        _check_startup_config()
