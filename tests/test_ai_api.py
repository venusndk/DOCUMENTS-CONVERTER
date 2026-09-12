"""
Tests for the Phase 16 (master directive numbering: AI Document
Intelligence) endpoints: POST /api/v1/ai/vision-extract, /extract,
/summarize, /translate, /classify.

The "AI features disabled" (503), validation (400/413), and auth (401)
paths need no real GEMINI_API_KEY and run everywhere this project's
suite does -- they're checking this service's own behavior, not
Gemini's. The real happy-path tests are marked @requires_gemini_key
(same shape as tests/test_ai_intelligence.py) and each make exactly one
real call -- see that file's own docstring for the real, confirmed
20-requests/day free-tier ceiling these share: a 429 from one of these
tests on a day the quota is already spent is that ceiling, not a
regression.
"""

from __future__ import annotations

import io

import fitz
import pytest
from PIL import Image, ImageDraw, ImageFont
from fastapi.testclient import TestClient

from documents_converter.api.app import _rate_limiter, app

from conftest import requires_gemini_key

client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """See tests/test_api.py's identical fixture."""
    _rate_limiter._counts.clear()
    yield


def _fabricated_text_image(text: str, width=700, height=150) -> bytes:
    img = Image.new("RGB", (width, height), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.text((20, 50), text, fill=(0, 0, 0), font=ImageFont.load_default(size=28))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _build_pdf_bytes(page_texts: list[str], width=400, height=150) -> bytes:
    doc = fitz.open()
    for text in page_texts:
        page = doc.new_page(width=width, height=height)
        page.insert_text((30, 75), text)
    data = doc.tobytes()
    doc.close()
    return data


# --------------------------------------------------------------------------
# Disabled path -- no key needed, exercised unconditionally
# --------------------------------------------------------------------------


def test_vision_extract_503_when_ai_not_configured(monkeypatch):
    from documents_converter.api import config

    monkeypatch.setattr(config, "GEMINI_API_KEY", None)
    image_bytes = _fabricated_text_image("Anything")
    resp = client.post(
        "/api/v1/ai/vision-extract", files={"file": ("p.png", io.BytesIO(image_bytes), "image/png")}
    )
    assert resp.status_code == 503


def test_extract_503_when_ai_not_configured(monkeypatch):
    from documents_converter.api import config

    monkeypatch.setattr(config, "GEMINI_API_KEY", None)
    resp = client.post(
        "/api/v1/ai/extract", data={"text": "some text", "fields": "a,b"}
    )
    assert resp.status_code == 503


def test_summarize_503_when_ai_not_configured(monkeypatch):
    from documents_converter.api import config

    monkeypatch.setattr(config, "GEMINI_API_KEY", None)
    resp = client.post("/api/v1/ai/summarize", data={"text": "some text"})
    assert resp.status_code == 503


def test_translate_503_when_ai_not_configured(monkeypatch):
    from documents_converter.api import config

    monkeypatch.setattr(config, "GEMINI_API_KEY", None)
    resp = client.post(
        "/api/v1/ai/translate", data={"text": "some text", "target_language": "French"}
    )
    assert resp.status_code == 503


def test_classify_503_when_ai_not_configured(monkeypatch):
    from documents_converter.api import config

    monkeypatch.setattr(config, "GEMINI_API_KEY", None)
    resp = client.post("/api/v1/ai/classify", data={"text": "some text"})
    assert resp.status_code == 503


# --------------------------------------------------------------------------
# Validation -- also no key needed: these fail before any real API call
# --------------------------------------------------------------------------


def test_extract_requires_exactly_one_of_text_or_file(monkeypatch):
    from documents_converter.api import config

    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key-for-this-check-only")
    resp = client.post("/api/v1/ai/extract", data={"fields": "a"})
    assert resp.status_code == 400

    image_bytes = _fabricated_text_image("x")
    resp = client.post(
        "/api/v1/ai/extract",
        data={"text": "a", "fields": "b"},
        files={"file": ("p.png", io.BytesIO(image_bytes), "image/png")},
    )
    assert resp.status_code == 400


def test_extract_requires_a_non_empty_fields_list(monkeypatch):
    from documents_converter.api import config

    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key-for-this-check-only")
    resp = client.post("/api/v1/ai/extract", data={"text": "a", "fields": "  , ,"})
    assert resp.status_code == 400


def test_classify_requires_exactly_one_of_text_or_file(monkeypatch):
    from documents_converter.api import config

    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key-for-this-check-only")
    resp = client.post("/api/v1/ai/classify", data={})
    assert resp.status_code == 400

    image_bytes = _fabricated_text_image("x")
    resp = client.post(
        "/api/v1/ai/classify",
        data={"text": "a"},
        files={"file": ("p.png", io.BytesIO(image_bytes), "image/png")},
    )
    assert resp.status_code == 400


def test_vision_extract_rejects_an_unsupported_extension(monkeypatch):
    from documents_converter.api import config

    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key-for-this-check-only")
    resp = client.post(
        "/api/v1/ai/vision-extract", files={"file": ("notes.txt", io.BytesIO(b"hello"), "text/plain")}
    )
    assert resp.status_code == 400


def test_summarize_rejects_text_over_the_configured_limit(monkeypatch):
    from documents_converter.api import config

    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key-for-this-check-only")
    monkeypatch.setattr(config, "AI_MAX_TEXT_CHARS", 10)
    resp = client.post("/api/v1/ai/summarize", data={"text": "x" * 11})
    assert resp.status_code == 413


def test_translate_rejects_text_over_the_configured_limit(monkeypatch):
    from documents_converter.api import config

    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key-for-this-check-only")
    monkeypatch.setattr(config, "AI_MAX_TEXT_CHARS", 10)
    resp = client.post(
        "/api/v1/ai/translate", data={"text": "x" * 11, "target_language": "French"}
    )
    assert resp.status_code == 413


# --------------------------------------------------------------------------
# Auth -- also no key needed
# --------------------------------------------------------------------------


def test_ai_endpoints_require_auth_when_configured(monkeypatch):
    from documents_converter.api import config

    monkeypatch.setattr(config, "API_KEYS", ("secret-key-1",))
    resp = client.post("/api/v1/ai/summarize", data={"text": "some text"})
    assert resp.status_code == 401


# --------------------------------------------------------------------------
# Real API calls -- one real call per endpoint's happy path
# --------------------------------------------------------------------------


@requires_gemini_key
def test_vision_extract_endpoint_reads_real_text_from_an_uploaded_image():
    image_bytes = _fabricated_text_image("Fabricated Endpoint Test 55210")
    resp = client.post(
        "/api/v1/ai/vision-extract", files={"file": ("p.png", io.BytesIO(image_bytes), "image/png")}
    )
    assert resp.status_code == 200
    assert "55210" in resp.json()["text"]


@requires_gemini_key
def test_extract_endpoint_from_text():
    resp = client.post(
        "/api/v1/ai/extract",
        data={
            "text": "Invoice #INV-9012, billed to John Smith, total due: $75.00.",
            "fields": "invoice_number,total_amount",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["invoice_number"] == "INV-9012"


@requires_gemini_key
def test_extract_endpoint_from_a_pdf_page():
    data = _build_pdf_bytes(["Order Number: ORD-6602"])
    resp = client.post(
        "/api/v1/ai/extract",
        data={"fields": "order_number"},
        files={"file": ("doc.pdf", io.BytesIO(data), "application/pdf")},
    )
    assert resp.status_code == 200
    assert "6602" in resp.json()["order_number"]


@requires_gemini_key
def test_summarize_endpoint_produces_a_real_bounded_summary():
    text = (
        "The annual report shows customer retention improved 9% this year, "
        "driven by a redesigned onboarding flow. Support ticket volume fell "
        "14% over the same period. The team plans to extend the same "
        "onboarding redesign to the enterprise product line next year."
    )
    resp = client.post("/api/v1/ai/summarize", data={"text": text, "max_sentences": 2})
    assert resp.status_code == 200
    summary = resp.json()["summary"]
    assert 0 < len(summary) < len(text)


@requires_gemini_key
def test_translate_endpoint_produces_real_translated_text():
    resp = client.post(
        "/api/v1/ai/translate", data={"text": "Good evening", "target_language": "Spanish"}
    )
    assert resp.status_code == 200
    result = resp.json()["translation"].lower()
    assert any(word in result for word in ("buenas", "noches", "tardes"))


@requires_gemini_key
def test_classify_endpoint_with_fixed_categories():
    text = "Please find attached invoice #8821 for consulting services. Payment due in 15 days."
    resp = client.post(
        "/api/v1/ai/classify",
        data={"text": text, "categories": "invoice,resume,poem"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["category"] == "invoice"
    assert len(body["reasoning"]) > 0
