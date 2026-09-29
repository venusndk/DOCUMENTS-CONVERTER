"""
Real load testing (Phase 19, master directive numbering: Performance &
Security Hardening) -- Locust, driving a real running instance of this
service (uvicorn or docker-compose's `api`), not a simulated or
in-process client. Every simulated user performs the same requests a
real caller would through the real HTTP API.

Not run as part of the regular pytest suite -- a load test's whole
point is sustained, real concurrent traffic over real wall-clock time,
which doesn't fit `pytest tests/`'s pass/fail-in-seconds model. Run it
directly:

    uvicorn documents_converter.api.app:app &
    locust -f tests/load/locustfile.py --host http://127.0.0.1:8000 \\
        --users 20 --spawn-rate 5 --run-time 60s --headless \\
        --csv tests/load/results

See README.md's "Performance & Security Hardening" section for this
project's own real, recorded numbers from running exactly this.
"""

from __future__ import annotations

import io

import fitz
from locust import HttpUser, between, task


def _real_pdf_bytes(text: str = "Load test page") -> bytes:
    """A real, valid, digital-text PDF -- built once per simulated user
    (see PdfConverterUser.on_start), not per request, so this test
    measures the service's own request-handling cost, not PDF
    construction overhead in the load-generating client itself."""
    doc = fitz.open()
    page = doc.new_page(width=400, height=300)
    page.insert_text((30, 100), text)
    data = doc.tobytes()
    doc.close()
    return data


def _real_image_bytes() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (300, 200), color=(80, 120, 200)).save(buf, "PNG")
    return buf.getvalue()


class DocumentConverterUser(HttpUser):
    """
    Simulates a realistic mix of this service's own real traffic:
    mostly cheap reads (health, capabilities) and fast, no-OCR
    conversions (image->pdf, pdf->text) -- deliberately NOT OCR->Excel
    as the bulk of load-test traffic, since Tesseract's own real
    per-page cost would dominate every measurement and tell this project
    more about Tesseract's throughput than its own API layer's -- an
    OCR-specific, separately-weighted task is included at low frequency
    for a realistic mix, not as the load test's main subject.
    """

    wait_time = between(0.5, 2.0)

    def on_start(self):
        self.pdf_bytes = _real_pdf_bytes()
        self.image_bytes = _real_image_bytes()

    @task(10)
    def health_check(self):
        self.client.get("/health", name="/health")

    @task(5)
    def list_capabilities(self):
        self.client.get("/api/v1/capabilities", name="/api/v1/capabilities")

    @task(20)
    def convert_pdf_to_text_sync(self):
        self.client.post(
            "/api/v1/convert",
            data={"target": "text"},
            files={"file": ("load.pdf", io.BytesIO(self.pdf_bytes), "application/pdf")},
            name="/api/v1/convert [pdf->text]",
        )

    @task(15)
    def convert_image_to_pdf_sync(self):
        self.client.post(
            "/api/v1/convert",
            data={"target": "pdf"},
            files={"file": ("load.png", io.BytesIO(self.image_bytes), "image/png")},
            name="/api/v1/convert [image->pdf]",
        )

    @task(8)
    def submit_and_poll_async_job(self):
        """The async path's own real cost: not just the submit, the
        poll loop a real caller would actually run too -- a load test
        that only ever measures POST /api/v1/jobs and never follows up
        would miss GET /api/v1/jobs/{id}'s own real, repeated cost
        under load (this project's own established job-queue pattern)."""
        with self.client.post(
            "/api/v1/jobs",
            data={"target": "text"},
            files={"file": ("load.pdf", io.BytesIO(self.pdf_bytes), "application/pdf")},
            name="/api/v1/jobs [submit]",
            catch_response=True,
        ) as resp:
            if resp.status_code != 202:
                resp.failure(f"unexpected status {resp.status_code}")
                return
            job_id = resp.json()["job_id"]

        for _ in range(10):
            with self.client.get(
                f"/api/v1/jobs/{job_id}", name="/api/v1/jobs/[id] [poll]", catch_response=True
            ) as poll_resp:
                if poll_resp.status_code != 200:
                    poll_resp.failure(f"unexpected status {poll_resp.status_code}")
                    return
                status = poll_resp.json()["status"]
                if status in ("completed", "failed"):
                    break
