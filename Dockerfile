# Runs the HTTP API (documents_converter/api/app.py). See
# docs/PHASE_0_AUDIT.md Phase 5: Phases 3-4 built an API with nothing to
# actually deploy it in until now.
FROM python:3.12-slim

# opencv-contrib-python needs libGL/libglib even headless on a minimal
# Linux image -- a common gotcha, not optional here.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first (better layer caching -- code changes far more often
# than dependencies do).
COPY requirements.txt requirements-api.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-api.txt

# Only what the API actually needs at runtime -- not tests/, docs/, or the
# CLI's own dev-only files.
COPY documents_converter/ documents_converter/

# Phase 1 completion (master directive numbering): Alembic migration
# files, run automatically at startup (see api/app.py's _run_migrations,
# called from the FastAPI lifespan handler).
COPY alembic.ini alembic.ini
COPY migrations/ migrations/

# Phase 11 (docs/PHASE_0_AUDIT.md numbering continued): run as an
# unprivileged user rather than the container default (root). The app
# never needs root -- it only reads its own code and writes to per-request
# temp directories (Python's tempfile module defaults to a world-writable
# /tmp, which appuser can use without owning /app) and, as of Phase 1
# completion, to ./data/ for the default local SQLite database -- created
# and chowned here since appuser can't create new directories under /app
# itself (owned by root, since WORKDIR ran before this point).
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /app/data \
    && chown appuser:appuser /app/data
USER appuser

EXPOSE 8000

# Lets an orchestrator (docker compose, k8s, etc.) detect a wedged
# container instead of only a crashed one -- hits the same unauthenticated
# liveness endpoint real monitoring would (see documents_converter/api/app.py).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" || exit 1

CMD ["uvicorn", "documents_converter.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
