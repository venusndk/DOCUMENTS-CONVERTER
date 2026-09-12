"""
Tests for documents_converter/ai_intelligence.py -- Phase 16, master
directive numbering (AI Document Intelligence).

Every test that actually calls the real Gemini API is marked
@requires_gemini_key (same shape as @requires_tesseract/
@requires_libreoffice/@requires_redis) and costs a small amount of real
API quota -- kept to one real call per behavior rather than exhaustively
varying inputs, since this project pays for these calls for real, unlike
every other optional dependency's tests. The "AI features disabled"
path needs no key at all and is tested unconditionally.

Real, empirically-confirmed limitation, found while verifying this
file: Gemini's free tier caps gemini-3.6-flash at 20 requests/DAY per
project (the API's own 429 response names it explicitly --
quotaId=GenerateRequestsPerDayPerProjectPerModel-FreeTier,
quotaValue=20) -- not a per-minute burst limit, a hard daily ceiling.
This file alone makes 8 real calls per run, so re-running it more than
a couple of times in one day will start failing with
google.genai.errors.ClientError: 429 RESOURCE_EXHAUSTED on whichever
tests happen to run after the quota is spent -- every one of these 8
tests has been independently confirmed to pass for real; a 429 here is
the free tier's daily ceiling, not a regression. CI is unaffected: it
never sets a real GEMINI_API_KEY, so @requires_gemini_key skips all of
these there, same as Tesseract/LibreOffice/Redis being absent in some
environments.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw, ImageFont

from documents_converter import ai_intelligence
from conftest import requires_gemini_key


def _fabricated_text_image(text: str, width=700, height=150) -> bytes:
    img = Image.new("RGB", (width, height), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.text((20, 50), text, fill=(0, 0, 0), font=ImageFont.load_default(size=28))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# --------------------------------------------------------------------------
# is_ai_available / the disabled path -- no API key or quota needed
# --------------------------------------------------------------------------


def test_is_ai_available_reflects_config(monkeypatch):
    from documents_converter.api import config

    monkeypatch.setattr(config, "GEMINI_API_KEY", None)
    assert ai_intelligence.is_ai_available() is False

    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key-for-this-check-only")
    assert ai_intelligence.is_ai_available() is True


def test_every_function_raises_ai_unavailable_error_when_not_configured(monkeypatch):
    from documents_converter.api import config

    monkeypatch.setattr(config, "GEMINI_API_KEY", None)

    with pytest.raises(ai_intelligence.AIUnavailableError):
        ai_intelligence.vision_extract(b"not-really-an-image")
    with pytest.raises(ai_intelligence.AIUnavailableError):
        ai_intelligence.summarize("some text")
    with pytest.raises(ai_intelligence.AIUnavailableError):
        ai_intelligence.translate("some text", "French")
    with pytest.raises(ai_intelligence.AIUnavailableError):
        ai_intelligence.classify(text="some text")
    with pytest.raises(ai_intelligence.AIUnavailableError):
        ai_intelligence.extract_structured(text="some text", fields=["a"])


def test_extract_structured_requires_exactly_one_of_text_or_image():
    with pytest.raises(ValueError):
        ai_intelligence.extract_structured(fields=["x"])
    with pytest.raises(ValueError):
        ai_intelligence.extract_structured(text="a", image_bytes=b"b", fields=["x"])


def test_classify_requires_exactly_one_of_text_or_image():
    with pytest.raises(ValueError):
        ai_intelligence.classify()
    with pytest.raises(ValueError):
        ai_intelligence.classify(text="a", image_bytes=b"b")


# --------------------------------------------------------------------------
# Real API calls -- each one a genuine, verifiable result, not just "it
# didn't raise"
# --------------------------------------------------------------------------


@requires_gemini_key
def test_vision_extract_reads_real_text_from_a_fabricated_image():
    image_bytes = _fabricated_text_image("Fabricated Vision Test 84213")
    text = ai_intelligence.vision_extract(image_bytes)
    assert "84213" in text


@requires_gemini_key
def test_extract_structured_from_text():
    text = "Invoice #INV-7734, billed to Jane Doe, total due: $128.40."
    result = ai_intelligence.extract_structured(
        text=text, fields=["invoice_number", "total_amount"]
    )
    assert result["invoice_number"] == "INV-7734"
    assert "128.40" in result["total_amount"]


@requires_gemini_key
def test_extract_structured_from_image():
    image_bytes = _fabricated_text_image("Order Number: ORD-5591")
    result = ai_intelligence.extract_structured(image_bytes=image_bytes, fields=["order_number"])
    assert "5591" in result["order_number"]


@requires_gemini_key
def test_extract_structured_returns_null_for_a_genuinely_missing_field():
    text = "This document mentions no reference numbers of any kind, just prose."
    result = ai_intelligence.extract_structured(text=text, fields=["tracking_number"])
    assert result["tracking_number"] is None


@requires_gemini_key
def test_summarize_produces_a_real_bounded_summary():
    text = (
        "The quarterly report shows revenue grew 12% year over year, driven "
        "mainly by the new product line launched in March. Operating costs "
        "rose 8%, primarily from increased staffing in the support team. "
        "Net income improved by 15% compared to the same quarter last year. "
        "The board approved a plan to expand into two new regional markets "
        "next fiscal year, contingent on continued revenue growth."
    )
    summary = ai_intelligence.summarize(text, max_sentences=2)
    assert len(summary) > 0
    assert len(summary) < len(text)


@requires_gemini_key
def test_translate_produces_real_translated_text():
    result = ai_intelligence.translate("Good morning, how are you?", "French")
    # A real, correct translation contains a recognizable French greeting
    # word -- not asserting an exact string, since a model may phrase it
    # slightly differently, but a real translation must contain one of these.
    assert any(word in result.lower() for word in ("bonjour", "matin"))


@requires_gemini_key
def test_classify_text_with_fixed_categories():
    text = "Please find attached invoice #4471 for services rendered in March. Payment due in 30 days."
    result = ai_intelligence.classify(text=text, categories=["invoice", "resume", "poem"])
    assert result["category"] == "invoice"
    assert len(result["reasoning"]) > 0


@requires_gemini_key
def test_classify_image_without_fixed_categories():
    image_bytes = _fabricated_text_image("INVOICE #4471 - Amount Due: $500")
    result = ai_intelligence.classify(image_bytes=image_bytes)
    assert "invoice" in result["category"].lower()
