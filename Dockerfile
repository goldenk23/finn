# FINN: one container serves the static Next.js export and the FastAPI API from the same origin.

# --- Stage 1: build the static frontend (frontend/out) ---
FROM node:24-slim AS web
WORKDIR /app/frontend
ENV NEXT_TELEMETRY_DISABLED=1
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN FINN_STATIC_EXPORT=1 npx next build

# --- Stage 2: Python runtime ---
FROM python:3.13-slim
# FINN_PROXY_HOPS=1: Caddy / load balancer appends the real client IP to X-Forwarded-For.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FINN_PROXY_HOPS=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install -r requirements.txt

RUN useradd --create-home --uid 10001 finn
# backend/ must be writable: SQLite (finn.db + WAL) and the PDF cache live there.
COPY --chown=finn:finn backend/ backend/
COPY --from=web --chown=finn:finn /app/frontend/out frontend/out
# Volume mount points must exist and be owned by finn, or Docker creates them as root and every PDF/DB write fails.
RUN mkdir -p backend/cache_pdfs /app/data && chown finn:finn backend/cache_pdfs /app/data
USER finn
# The live DB lives on the finn_data volume (docker-compose.yml) so rebuilds don't wipe refreshes and the ledger.
# On first start it is seeded from the pre-warmed backend/finn.db baked into the image.
ENV FINN_DB_PATH=/app/data/finn.db

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:${PORT:-8000}/api/health || exit 1
# Secrets (GEMINI_API_KEY, FINN_ADMIN_TOKEN) come from the platform's env settings, never from the image.
CMD ["sh", "-c", "[ -f \"$FINN_DB_PATH\" ] || cp backend/finn.db \"$FINN_DB_PATH\"; exec uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000} --timeout-keep-alive 75"]
