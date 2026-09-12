"""
Real browser-level tests for the Phase 8 frontend
(documents_converter/api/static/index.html).

Uses Playwright to actually load the page in a headless browser and
interact with it the way a real person would -- choose a file, click
Convert, wait for the real download -- rather than only asserting the
HTML contains expected markup. The whole point of Phase 8 is that a
non-technical person can use this without curl; a test that doesn't
drive a real browser wouldn't actually prove that.
"""

from __future__ import annotations

import threading
import time

import openpyxl
import pytest
import requests
import uvicorn

from documents_converter.api import config
from documents_converter.api.app import app

from conftest import requires_redis, requires_tesseract

_PORT = 8765


@pytest.fixture(scope="module")
def running_app_server(tesseract_cmd):
    """
    Playwright drives a real browser against a real URL -- it can't talk
    to FastAPI's in-process TestClient the way the other test files do.
    Runs uvicorn in a background thread for the module's test session.
    """
    config.TESSERACT_CMD = tesseract_cmd
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=_PORT, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10
    base_url = f"http://127.0.0.1:{_PORT}"
    while time.monotonic() < deadline:
        try:
            if requests.get(f"{base_url}/health", timeout=1).status_code == 200:
                break
        except requests.exceptions.ConnectionError:
            time.sleep(0.2)
    else:
        raise RuntimeError("live server did not start in time")

    yield base_url
    server.should_exit = True


def test_index_page_loads_with_expected_elements(running_app_server):
    """Fast, non-Playwright sanity check -- the full browser test below is
    slower and OCR-dependent, this one just confirms the route itself is
    wired correctly."""
    resp = requests.get(running_app_server + "/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert 'id="submit-btn"' in resp.text
    assert 'id="file-input"' in resp.text


@requires_tesseract
@requires_redis
def test_frontend_full_upload_to_download_flow(running_app_server, synthetic_pdf, tmp_path):
    """
    The real end-to-end proof: a real headless browser loads the actual
    page, selects the actual synthetic fixture through the actual file
    input, clicks the actual Convert button, and the actual polling/
    review/download JavaScript in index.html produces a real file --
    checked against the same expected values as every other end-to-end
    test in this project.

    Phase 7 completion (master directive numbering) means an OCR->Excel
    job always has review data (see documents_converter/api/jobs.py's
    has_review), so the page shows the review step before downloading --
    this test confirms/dismisses it with no edits, matching a user who
    checks the data and finds nothing to fix. See
    test_frontend_review_lets_a_person_correct_a_cell_before_downloading
    below for the actual edit-a-cell path.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(running_app_server + "/")
            assert page.title() == "Documents Converter"

            page.set_input_files("#file-input", str(synthetic_pdf))
            submit = page.locator("#submit-btn")
            assert not submit.is_disabled()
            submit.click()

            confirm_btn = page.locator("#confirm-download-btn")
            page.wait_for_function(
                "document.getElementById('review-field').classList.contains('visible')",
                timeout=60_000,
            )
            assert page.locator(".review-table td").count() > 0

            with page.expect_download(timeout=30_000) as download_info:
                confirm_btn.click()
            download = download_info.value

            saved_path = tmp_path / "downloaded.xlsx"
            download.save_as(str(saved_path))
        finally:
            browser.close()

    wb = openpyxl.load_workbook(saved_path)
    ws = wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True))
    assert rows[1][:5] == ("1", "100000001", "SMITH", "JOHN", "M")


@requires_tesseract
@requires_redis
def test_frontend_review_lets_a_person_correct_a_cell_before_downloading(
    running_app_server, synthetic_pdf, tmp_path
):
    """
    The actual point of Phase 7 completion: edit a cell in the rendered
    review table (not just accept it as-is), confirm, and check the
    correction reached the downloaded file -- through the real
    contenteditable UI, not by calling the API directly.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(running_app_server + "/")
            page.set_input_files("#file-input", str(synthetic_pdf))
            page.locator("#submit-btn").click()

            page.wait_for_function(
                "document.getElementById('review-field').classList.contains('visible')",
                timeout=60_000,
            )

            # Phase 15 (master directive numbering): the source page
            # image should actually render alongside the editable table,
            # not just exist as an unused endpoint -- confirmed via a
            # real browser, not just that the img tag was created.
            page.wait_for_selector(".review-page-image", timeout=10_000)
            preview_src = page.locator(".review-page-image").first.get_attribute("src")
            assert preview_src and preview_src.startswith("blob:")
            preview_natural_width = page.locator(".review-page-image").first.evaluate(
                "el => el.naturalWidth"
            )
            assert preview_natural_width > 0, "page preview image failed to actually load"

            first_cell = page.locator(".review-table td").first
            assert first_cell.inner_text() == "S/N"
            first_cell.click()
            page.keyboard.press("Control+A")
            page.keyboard.type("EDITED")
            # Blur the cell so the browser commits the contenteditable
            # edit before collectCorrections() reads it.
            page.locator("#review-help").click()

            with page.expect_download(timeout=30_000) as download_info:
                page.locator("#confirm-download-btn").click()
            download = download_info.value

            saved_path = tmp_path / "downloaded.xlsx"
            download.save_as(str(saved_path))
        finally:
            browser.close()

    wb = openpyxl.load_workbook(saved_path)
    ws = wb[wb.sheetnames[0]]
    assert ws.cell(row=1, column=1).value == "EDITED"


def test_frontend_rejects_unsupported_file_type_client_side(running_app_server, tmp_path):
    """
    Phase 2 completion (master directive numbering): frontend capability
    discovery. An unsupported file type is now caught client-side, from
    the real GET /api/v1/capabilities response -- before any network
    request to /api/v1/jobs -- rather than only being caught by the
    server's 400 after a real upload. Confirmed via a real browser: pick
    a .exe, expect a visible error and a disabled Convert button.
    """
    from playwright.sync_api import sync_playwright

    bad_file = tmp_path / "not_a_document.exe"
    bad_file.write_bytes(b"whatever")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(running_app_server + "/")
            page.set_input_files("#file-input", str(bad_file))
            status = page.locator("#status")
            page.wait_for_function(
                "document.getElementById('status').className === 'error'", timeout=10_000
            )
            assert "No conversion available" in status.inner_text()
            assert page.locator("#submit-btn").is_disabled()
        finally:
            browser.close()


def test_frontend_offers_a_target_choice_for_a_convertible_file(running_app_server, tmp_path):
    """
    A PNG genuinely matches multiple registered capabilities -- it could
    be a photographed table (source_format=scanned_document, target=xlsx
    or target=searchable_pdf) or a plain image someone wants
    containerized as a PDF (source_format=image, target=pdf) -- so the
    dropdown, driven by the real GET /api/v1/capabilities response,
    should offer all of them, defaulting to xlsx (this page's original,
    pre-registry behavior) rather than silently picking one and hiding
    the rest. Deliberately asserts the exact set rather than just "more
    than one option": this page adding a new capability (as Phase 5
    completion's searchable_pdf did, with zero frontend code changes)
    should show up here automatically -- that's the point of driving
    this from the registry instead of a hardcoded list.
    """
    from playwright.sync_api import sync_playwright
    from PIL import Image

    png_path = tmp_path / "photo.png"
    Image.new("RGB", (50, 40), color=(10, 90, 200)).save(png_path)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(running_app_server + "/")
            page.set_input_files("#file-input", str(png_path))
            page.wait_for_function(
                "document.getElementById('target-select').options.length > 0", timeout=10_000
            )
            options = page.locator("#target-select option").all_inner_texts()
            assert set(options) == {"XLSX", "PDF", "SEARCHABLE_PDF"}
            assert page.locator("#target-select").input_value() == "xlsx"
            assert not page.locator("#submit-btn").is_disabled()
        finally:
            browser.close()


@requires_redis
def test_frontend_image_to_pdf_end_to_end(running_app_server, tmp_path):
    """
    The real proof this feature exists for: pick an image, explicitly
    choose the "pdf" target from the capability-driven dropdown (not the
    xlsx default), click Convert, and get back a real, valid,
    correctly-sized PDF -- through the actual browser UI, not a direct
    API call. No OCR/Tesseract involved, so this doesn't need
    @requires_tesseract -- it does need @requires_redis, though (Phase
    14, master directive numbering): the frontend's Convert button goes
    through POST /api/v1/jobs, which is now a real Redis/RQ queue.
    """
    from playwright.sync_api import sync_playwright
    from PIL import Image
    import fitz

    png_path = tmp_path / "photo.png"
    Image.new("RGB", (120, 80), color=(200, 60, 60)).save(png_path)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(running_app_server + "/")
            page.set_input_files("#file-input", str(png_path))
            page.wait_for_function(
                "document.getElementById('target-select').options.length > 0", timeout=10_000
            )
            page.select_option("#target-select", "pdf")
            submit = page.locator("#submit-btn")
            assert not submit.is_disabled()

            with page.expect_download(timeout=30_000) as download_info:
                submit.click()
            download = download_info.value

            saved_path = tmp_path / "downloaded.pdf"
            download.save_as(str(saved_path))
        finally:
            browser.close()

    doc = fitz.open(str(saved_path))
    try:
        assert doc.page_count == 1
        assert doc[0].rect.width == 120
        assert doc[0].rect.height == 80
    finally:
        doc.close()


@requires_redis
def test_frontend_signup_submit_job_and_save_it(running_app_server, tmp_path):
    """
    Phase 17 (master directive numbering): a real browser signs up
    through the actual account UI, submits a real job while logged in,
    opens History, and saves it -- end to end through the real page's
    own JavaScript (including reading the CSRF cookie and echoing it
    back, accounts.py's own docstring), not a direct API call. Uses
    image->pdf (no Tesseract needed), same reasoning as
    test_frontend_image_to_pdf_end_to_end above -- this test's only
    real dependency should be Redis, not also Tesseract.
    """
    import uuid

    from playwright.sync_api import sync_playwright
    from PIL import Image

    png_path = tmp_path / "photo.png"
    Image.new("RGB", (100, 80), color=(50, 120, 200)).save(png_path)
    email = f"frontend-{uuid.uuid4().hex}@example.com"

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(running_app_server + "/")

            page.click("#account-toggle-link")
            page.click("#auth-mode-toggle")  # switch from Log in to Sign up
            page.fill("#auth-email", email)
            page.fill("#auth-password", "a real password 123")
            page.click("#auth-submit-btn")
            page.wait_for_function(
                "document.getElementById('account-status').textContent.includes('Signed in')",
                timeout=10_000,
            )

            page.set_input_files("#file-input", str(png_path))
            page.wait_for_function(
                "document.getElementById('target-select').options.length > 0", timeout=10_000
            )
            page.select_option("#target-select", "pdf")

            with page.expect_download(timeout=30_000):
                page.click("#submit-btn")

            page.click("#history-toggle-link")
            page.wait_for_selector(".history-row", timeout=10_000)
            assert page.locator(".history-row").count() >= 1

            save_btn = page.locator(".history-row button").first
            assert save_btn.inner_text() == "Save"
            save_btn.click()
            # Optional chaining, not a bare property read -- toggleSaved()
            # re-renders the whole list (historyListEl.innerHTML = "" then
            # rebuilds it), so a poll landing in that brief empty window
            # would otherwise throw on a null querySelector result instead
            # of just evaluating falsy and being polled past.
            page.wait_for_function(
                "document.querySelector('.history-row button')?.innerText === 'Unsave'", timeout=10_000
            )
        finally:
            browser.close()
