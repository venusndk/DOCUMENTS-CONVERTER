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

EXPOSE 8000

CMD ["uvicorn", "documents_converter.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
