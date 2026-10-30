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
# FINN_PROXY_HOPS=1: the platform load balancer appends the real client IP to X-Forwarded-For (verify per platform).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FINN_PROXY_HOPS=1
WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

RUN useradd --create-home --uid 10001 finn
# backend/ must be writable: SQLite (finn.db + WAL) and the PDF cache live there.
COPY --chown=finn:finn backend/ backend/
COPY --from=web --chown=finn:finn /app/frontend/out frontend/out
USER finn

EXPOSE 8000
# Secrets (GEMINI_API_KEY, FINN_ADMIN_TOKEN) come from the platform's env settings, never from the image.
CMD ["sh", "-c", "exec uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
