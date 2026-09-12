"""
AI Document Intelligence (master directive Phase 16) -- vision
fallback, structured extraction, summarization, translation, and
intelligent classification. Explicitly "optional/configurable" per the
directive: every function here needs config.GEMINI_API_KEY set, and
is_ai_available() is what every /api/v1/ai/* endpoint (app.py) checks
first, returning a clear 503 rather than failing unpredictably deep
inside a request when it isn't configured -- the same shape as
check_tesseract_available() gating the OCR pipeline.

Google Gemini, not Anthropic/Claude -- a deliberate, disclosed
departure from this environment's own "default to Claude" guidance, for
a concrete, real reason: Gemini's free tier needs no billing/credit
card at all, while a working Anthropic API key with zero credits
purchased genuinely cannot make a single request (confirmed directly
against the real API before any of this module existed: a valid key,
correctly authenticated, still failed with "Your credit balance is too
low to access the Anthropic API"). See config.py's own note on
GEMINI_API_KEY/AI_MODEL for the same story in more detail, including
why the model defaults to gemini-3.6-flash rather than the more
obvious first guess (gemini-2.5-flash, retired for new accounts by the
time this was written -- confirmed against the real API's own error
message, not assumed from a cached model list).

Every function takes plain Python values (bytes, str) and returns
plain Python values -- no FastAPI/HTTP concern here at all, mirroring
document_preview.py and document_analysis.py's own separation between
"what this actually does" and "how app.py exposes it over HTTP".

Deliberately thin wrappers, not a bigger abstraction: this project has
exactly one AI provider today, so a provider-agnostic interface would
be indirection with nothing yet to plug into it -- the same
"provider/capability split" restraint README.md already documents for
the OCR pipeline (Phase 2's provider registry, not built until there's
a second real provider).
"""

from __future__ import annotations

import json
import logging

from google import genai
from google.genai import types

from .api import config

# The SDK's own "you're calling generate_content directly instead of
# through Chat" advisory -- correct advice for a chat/tool-use app,
# irrelevant here (every call in this module is a single, stateless
# request with no tools and nothing to loop over), and noisy enough
# that every real request in this project's own testing logged it.
# Scoped to just this one namespace so a genuine, unexpected warning
# from elsewhere in the SDK is never silenced by this.
logging.getLogger("google_genai.models").setLevel(logging.ERROR)


class AIUnavailableError(RuntimeError):
    """Raised by every function in this module when GEMINI_API_KEY
    isn't configured -- callers map this to a 503, not a 500; the
    service is working correctly, an optional feature just isn't
    turned on."""


def is_ai_available() -> bool:
    return config.GEMINI_API_KEY is not None


def _client() -> genai.Client:
    if not is_ai_available():
        raise AIUnavailableError(
            "AI features are not configured (GEMINI_API_KEY is unset)."
        )
    return genai.Client(api_key=config.GEMINI_API_KEY)


# Real bug found and fixed while building this module: google-genai's
# Client closes its underlying httpx transport once the Client object
# itself is garbage-collected. Writing `_client().models.generate_content(...)`
# -- a bare temporary, no name holding it -- let CPython's refcounting
# collect (and finalize/close) the Client between the `.models` attribute
# access and the actual request, raising, on every single real call:
#   RuntimeError: Cannot send a request, as the client has been closed.
# Reproduced outside pytest entirely (a standalone script hit the same
# traceback), which ruled out test-fixture/pytest interference before
# this was found. Fix: every function below assigns `_client()` to a
# local `client` variable first, so a real reference outlives the call.


def _truncate_for_ai(text: str) -> str:
    """Shared bound for every text-based feature below -- see
    config.AI_MAX_TEXT_CHARS's own docstring for why."""
    return text[: config.AI_MAX_TEXT_CHARS]


def vision_extract(image_bytes: bytes, mime_type: str = "image/png") -> str:
    """
    Vision fallback: reads an image directly with a vision-capable
    model and returns the text it can see -- for a page Tesseract's own
    OCR pipeline (documents_converter/ocr_excel.py, pdf_to_text.py)
    reads poorly (unusual fonts, handwriting, low-contrast scans), not
    a replacement for that pipeline's normal, faster, free path. Plain
    text out, not structured table data -- extract_structured() is the
    function for that, given a real schema.
    """
    client = _client()
    response = client.models.generate_content(
        model=config.AI_MODEL,
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            "Read every word of visible text in this image, in reading order. "
            "Reply with only the text itself -- no commentary, no markdown formatting.",
        ],
    )
    return (response.text or "").strip()


def extract_structured(
    text: str | None = None,
    image_bytes: bytes | None = None,
    mime_type: str = "image/png",
    *,
    fields: list[str],
) -> dict:
    """
    Structured extraction: given plain text or an image, and a list of
    field names to pull out, returns a flat {field: value} JSON object
    -- every requested field present, `null` for one genuinely not
    found in the source rather than an omitted key or a guessed value.
    Exactly one of `text`/`image_bytes` must be given.
    """
    if (text is None) == (image_bytes is None):
        raise ValueError("Provide exactly one of `text` or `image_bytes`.")

    schema = {
        "type": "OBJECT",
        "properties": {field: {"type": "STRING", "nullable": True} for field in fields},
        "required": fields,
    }
    instruction = (
        f"Extract these fields from the document: {', '.join(fields)}. "
        "Use null for any field genuinely not present -- never guess or invent a value."
    )
    contents: list = (
        [types.Part.from_bytes(data=image_bytes, mime_type=mime_type), instruction]
        if image_bytes is not None
        else [instruction, "\n\nDocument text:\n", _truncate_for_ai(text)]
    )
    client = _client()
    response = client.models.generate_content(
        model=config.AI_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            response_mime_type="application/json", response_schema=schema
        ),
    )
    return json.loads(response.text)


def summarize(text: str, max_sentences: int = 5) -> str:
    """A plain-language summary of `text`, bounded to at most
    `max_sentences` sentences -- a real bound the model is asked to
    respect, not a truncation of its own output after the fact."""
    client = _client()
    response = client.models.generate_content(
        model=config.AI_MODEL,
        contents=(
            f"Summarize the following document in at most {max_sentences} sentences. "
            "Reply with only the summary -- no preamble, no heading.\n\n"
            f"{_truncate_for_ai(text)}"
        ),
    )
    return (response.text or "").strip()


def translate(text: str, target_language: str) -> str:
    """Translates `text` into `target_language` (a plain language name
    or code, e.g. "French" or "fr" -- passed straight through to the
    model rather than validated against a fixed list, since the model
    already handles far more languages than this project could
    usefully enumerate)."""
    client = _client()
    response = client.models.generate_content(
        model=config.AI_MODEL,
        contents=(
            f"Translate the following document into {target_language}. "
            "Reply with only the translation -- no commentary, no original text.\n\n"
            f"{_truncate_for_ai(text)}"
        ),
    )
    return (response.text or "").strip()


def classify(
    text: str | None = None,
    image_bytes: bytes | None = None,
    mime_type: str = "image/png",
    *,
    categories: list[str] | None = None,
) -> dict:
    """
    Intelligent classification: what kind of document is this
    (invoice, transcript, contract, letter, ...) -- a real semantic
    read, unlike document_analysis.py's own classification (Phase 4
    completion), which only ever answers "digital vs. scanned vs.
    mixed" from the PDF's own structure and never looks at what the
    document actually says. Returns {"category": str, "reasoning": str}.

    `categories`, when given, constrains the answer to exactly one of
    that list (returned verbatim) rather than an open-ended label --
    useful when a caller's downstream logic only knows how to handle a
    fixed set of outcomes.
    """
    if (text is None) == (image_bytes is None):
        raise ValueError("Provide exactly one of `text` or `image_bytes`.")

    if categories:
        instruction = (
            f"Classify this document into exactly one of these categories: "
            f"{', '.join(categories)}. Reply with that exact category string, "
            "plus a one-sentence reason."
        )
        schema_categories = {"type": "STRING", "enum": categories}
    else:
        instruction = (
            "Classify what kind of document this is (e.g. invoice, academic "
            "transcript, contract, letter, form, report -- your own best label "
            "if none of those fit), plus a one-sentence reason."
        )
        schema_categories = {"type": "STRING"}

    schema = {
        "type": "OBJECT",
        "properties": {"category": schema_categories, "reasoning": {"type": "STRING"}},
        "required": ["category", "reasoning"],
    }
    contents: list = (
        [types.Part.from_bytes(data=image_bytes, mime_type=mime_type), instruction]
        if image_bytes is not None
        else [instruction, "\n\nDocument text:\n", _truncate_for_ai(text)]
    )
    client = _client()
    response = client.models.generate_content(
        model=config.AI_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            response_mime_type="application/json", response_schema=schema
        ),
    )
    return json.loads(response.text)
