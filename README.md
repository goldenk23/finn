# FINN: Financial Investment Agent (NSE)

FINN is a mobile-first web app I built for the ENAM Holdings AI Engineer Build Challenge. It has two modes:

- **Push:** a daily triage of exchange filings for a portfolio. Material events come first, routine paperwork is collapsed, duplicates are merged, and results and management changes get their own cards. Price, volume and filing alerts are checked against thresholds you set.
- **Pull:** an on-demand research report on any NSE company Screener covers, built from its Annual Report, recent concall transcripts and Screener's tables, plus a Copilot for follow-up questions.

[COVERING_NOTE.md](COVERING_NOTE.md) explains the choices, real costs and what I left out. [ARCHITECTURE.md](ARCHITECTURE.md) explains how each part works, with file and line references. [docs/ai-build-history/](docs/ai-build-history/) contains the complete AI coding-agent conversation transcripts (`chat1.md`–`chat4.md`).

## What's in the demo database

`backend/finn.db` is committed and already filled, so the app runs without an API key:
- 16 portfolio stocks with end-of-day prices
- 269 announcements, all triaged, with results and management-change cards
- 21 cached Pull reports, 5 of them for companies outside the portfolio (DIXON, INDHOTEL, PERSISTENT, POLYCAB, WIPRO)
- alert rules at 3% price move and 2x volume

Cached reports and cards cost nothing to open. You only need Gemini credentials for **Refresh Live**, **Deep Enrich**, **Re-run**, a report for a new ticker, or Copilot questions.

## Setup

You need Python 3.13 (the version in the `Dockerfile`; I developed on 3.13.3) and Node.js with npm for the frontend.

### 1. Environment

Copy `.env.example` to `.env` in the repo root and fill in one of the two Gemini options:

```ini
# Option 1: Gemini Developer API key
GEMINI_API_KEY=your_api_key_here

# Option 2: Vertex AI with Application Default Credentials (gcloud auth application-default login)
# GOOGLE_GENAI_USE_VERTEXAI=true
# GOOGLE_CLOUD_PROJECT=your_gcp_project_id
# GOOGLE_CLOUD_LOCATION=global
```

Optional settings, all from `.env.example`:

| Variable | Default | What it does |
| --- | --- | --- |
| `FINN_DAILY_BUDGET_USD` | `5.00` | Paid endpoints return 429 once today's (UTC) spend in `llm_cost_ledger` reaches this |
| `FINN_ADMIN_TOKEN` | unset | If set, `?force=true`, `/api/push/refresh` and `/api/push/enrich` need an `X-Admin-Token` header. The UI doesn't send it, so leave it unset locally or those buttons get 401 |
| `FINN_REFRESH_MINUTES` | unset (off) | Run a Push refresh (prices, filings, triage, up to 6 cards; this makes paid LLM calls) every N minutes while the server runs. Use one uvicorn worker, since each worker starts its own loop |
| `FINN_PROXY_HOPS` | `0` (`1` in the Docker image) | How many trusted proxies add to `X-Forwarded-For`, for the per-IP rate limit |
| `FINN_CORS_ORIGINS` | `*` | Only needed if the frontend is served from another origin |
| `NEXT_PUBLIC_API_URL` | empty (same origin) | Backend URL for the browser and the dev proxy |

### 2. Backend (port 8000)

```bash
pip install -r requirements.txt
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Add `--reload` while editing code. Check it with `http://127.0.0.1:8000/api/health`.

### 3. Frontend (port 3000)

The scripts come from `frontend/package.json`:

```bash
cd frontend
npm ci            # or npm install
npm run dev       # next dev on http://localhost:3000
```

The dev server forwards `/api/*` to `http://127.0.0.1:8000` (or `NEXT_PUBLIC_API_URL`), with a 180 s proxy timeout for cold Pull reports (`frontend/next.config.mjs`). For a production build, run `npm run build` and then `npm run start`.

### 4. One process instead (static export)

FastAPI serves the frontend itself if `frontend/out` exists (`backend/main.py:524-526`):

PowerShell:

```powershell
cd frontend
$env:FINN_STATIC_EXPORT = "1"
npx next build
cd ..
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

In bash, the build line is `FINN_STATIC_EXPORT=1 npx next build`.

Then open `http://127.0.0.1:8000`.

### 5. Docker & AWS EC2 Deployment

The `Dockerfile` builds the static frontend and runs everything in one container on `$PORT` (default 8000). The demo database is copied into the image.

```bash
docker build -t finn .
docker run --rm -p 8000:8000 --env-file .env finn
```

For AWS EC2 production deployment, `docker-compose.yml` pairs the app container (`restart: always`, `/api/health` healthcheck) with a **Caddy** reverse proxy (`Caddyfile`, 180 s upstream timeout for cold Pull synthesis, automatic Let's Encrypt HTTPS when `FINN_DOMAIN` is set):

```bash
cp .env.example .env      # fill in GEMINI_API_KEY (and optional FINN_DOMAIN)
docker compose up -d --build
```

## Tests and scripts

```bash
python backend/test_offline.py
```

This runs the offline suite: no network, no LLM calls, and it never writes `finn.db`. It prints `34 offline test(s) passed`.

`backend/test_plumbing.py`, `backend/test_push.py` and `backend/test_pull.py` are live smoke tests. They scrape NSE and Screener, some call Gemini (paid), and they write to `finn.db`. Don't run them unless you mean to.

`backend/warmup_demo.py` rebuilds the demo data with the current code. Start with `python backend/warmup_demo.py --dry-run`, which prints the plan and makes no LLM calls. The full run makes paid calls and stops once the ledger total reaches `--stop-at-usd` (default 4.50).

## Repository layout

```text
├── README.md, COVERING_NOTE.md, ARCHITECTURE.md
├── docs/diagrams/            # architecture PNGs + Mermaid sources (render_diagrams.py)
├── Dockerfile, docker-compose.yml, Caddyfile, .dockerignore, .env.example, requirements.txt
├── backend/
│   ├── main.py               # FastAPI routes, budget cap, rate limits, admin token, background refresh
│   ├── db.py                 # SQLite schema (WAL), 16-stock seed, ticker renames
│   ├── llm_router.py         # model tiers, fallbacks, price table, cost ledger, results and management cards
│   ├── push_engine.py        # refresh, triage, enrichment, digest, dedup, alerts
│   ├── pull_engine.py        # Pull report (map + reduce), unit checks, staleness, Copilot
│   ├── pdf_utils.py          # PDF download + cache, keyword page selection
│   ├── warmup_demo.py        # rebuilds the demo data
│   ├── test_offline.py       # offline test suite
│   ├── test_plumbing.py, test_push.py, test_pull.py   # live smoke tests
│   ├── finn.db               # committed demo database
│   └── scrapers/             # filings.py (NSE/BSE), market_data.py (yfinance), screener.py
└── frontend/
    ├── next.config.mjs, package.json, tailwind.config.ts
    └── src/
        ├── app/              # layout.tsx, page.tsx (tabs, polling, unseen alerts)
        ├── components/       # PushTab, FilingCard, AlertsTab, PullTab, PortfolioTab, CostDrawer
        ├── lib/api.ts        # API client
        └── types/finn.ts     # response types
```
