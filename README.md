# FINN — Institutional Financial Investment Agent (NSE / BSE)

**FINN** is a mobile-first equity intelligence agent built for Indian public-markets investors (ENAM Holdings AI Engineer Build Challenge). It operates in two complementary modes—**Push (Proactive Signal Triage & Anomaly Alerts)** and **Pull (On-Demand Primary-Source Deep Research)**—grounded strictly in exchange filings, 10Y financial statements, concall transcripts, investor presentations, and annual reports.

---

## Quick Start (Local Demo)

The repository ships with a pre-warmed SQLite database (`backend/finn.db`) containing **16 NSE portfolio stocks** with live EOD price/volume metrics, **150+ classified NSE/BSE corporate filings** (with deep-enriched Quarterly Results tables and Executive Change biographies), and **4 pre-cached Pull Research reports** (`TRENT`, `KPITTECH`, `DIXON`, `POLYCAB`) that load in **<15ms at $0.00 marginal cost**.

### 1. Environment Setup
Create a `.env` file in the project root (see `.env.example`):
```ini
# Option A: Standard Gemini API Key
GEMINI_API_KEY=your_gemini_api_key

# Option B: Google Cloud Vertex AI (Application Default Credentials)
GOOGLE_GENAI_USE_VERTEXAI=true
GOOGLE_CLOUD_PROJECT=your_gcp_project_id
GOOGLE_CLOUD_LOCATION=global
```

### 2. Start the FastAPI Backend (`:8000`)
```bash
pip install -r requirements.txt
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
```

### 3. Start the Next.js Mobile UI (`:3000`)
```bash
cd frontend
npm install
npm run dev
```
Open **`http://localhost:3000`** in your browser. Next.js automatically proxies `/api/*` requests to `http://127.0.0.1:8000/api/*`.

---

## Core Capabilities

### 1. Push Mode — Proactive Digest & Configurable Alerts
- **Windowed Corporate Digest (`24h` default, `48h`, `7d`, `14d`, `30d`):**
  - **Noise Filter:** Automatically buries routine exchange paperwork (newspaper advertisements, Regulation 74(5) depository certificates, loss of share certificates, trading window closures, routine analyst meet schedule notices) behind a single collapsible counter while surfacing high-conviction material signals at the top.
  - **Event Deduplication:** Collapses duplicate exchange announcements for the same stock and event (e.g., dual resignation intimations or board outcome + auditor report filings) into a single canonical card.
  - **Hero Card — Quarterly Results (`results`):** Extracts Consolidated **Revenue, EBITDA, PBT, and PAT** (`₹ Cr`) with color-coded **QoQ %** and **YoY %** delta pills, cross-checked against Screener's standardized quarterly table to prevent PDF unit-conversion errors.
  - **Hero Card — Executive Changes (`management_change`):** Extracts executive name, designation, action badge (`Appointment` / `Resignation`), and synthesizes a grounded **2-line background/bio** from the exchange PDF and DuckDuckGo web search (`ddgs`).
- **Configurable Threshold Alerts:**
  - Interactive sliders for **EOD Price Move (`±X%`)** and **Volume Spike (`Yx 20d avg`)** plus toggleable **Watched Material Categories** (`results`, `M&A`, `management_change`, `fund_raise`, `litigation`, `capex`, etc.) that persist to SQLite and re-evaluate portfolio anomalies live.

### 2. Pull Mode — On-Demand Primary-Source Deep Research
Works on **any NSE/BSE ticker** (portfolio stocks + ad-hoc search for any listed company such as `DIXON`, `POLYCAB`, `INDHOTEL`, `PERSISTENT`):
- **Hero Worked Example — Narrative vs. Numbers (4-Quarter Audit):**
  - Audits management's stated guidance, margin expectations, and expansion promises from recent Concall Transcripts / Investor Presentations / Annual Reports against actual reported Revenue, OPM %, and PAT across the last 4 quarters, assigning a `Beat`, `Delivered`, `Mixed`, or `Missed` verdict and analytical note per quarter.
- **8 Structured Institutional Modules:**
  1. `narrative_vs_numbers` (4-Quarter Guidance vs. Delivery Audit)
  2. `financial_snapshot` (8-KPI institutional grid + valuation vs. ROCE/ROE read)
  3. `business_snapshot` (Core business model, vertical revenue mix, key operating levers)
  4. `trajectory` (6-Quarter & 5-Year Revenue, PAT & OPM % charts + expanding vs. compressing drivers)
  5. `cash_quality` (CFO vs. PAT conversion badge + multi-year comparative chart)
  6. `balance_sheet` (Leverage posture, Borrowings, Reserves, D/E, Working Capital & Cash Conversion Cycle days, with specialized **Banking/NBFC domain rules** for `HDFCBANK` / `BAJFINANCE`)
  7. `guidance` (Management tone badge + stated forward targets with source tags)
  8. `bull_vs_bear` (3 number-backed Bull arguments vs. 3 Bear/Risk arguments)
- **Primary-Source Copilot Q&A (`POST /api/pull/{ticker}/ask`):**
  - Interactive Q&A grounded strictly in the company's primary financial tables and extracted filing text, returning number-cited answers and source tags.

### 3. 3-Tier Gemini Model Routing & Cost Telemetry
Click the **`$0.XXX • 3-Tier`** pill in the top header to inspect live token consumption and USD spend across workflows:
- **Tier 1 (`gemini-2.5-flash-lite` — `$0.10 / 1M`):** Bulk classification & 1-line digest generation across 17 filing categories.
- **Tier 2 (`gemini-2.5-flash` — `$0.30 / 1M`):** PDF results extraction, executive bio synthesis, Concall Map distillation, and interactive Copilot Q&A.
- **Tier 3 (`gemini-2.5-pro` — `$1.25 / 1M`):** Reserved strictly for the 8-module Pull synthesis and 4-Quarter Narrative vs. Numbers audit.

---

## Repository Structure

```text
├── ARCHITECTURE.md            # System architecture diagram, Push/Pull sequence flows & EC2 capacity math
├── COVERING_NOTE.md           # Design choices, model routing trade-offs, what we skipped & roadmap
├── FRONTEND_CONTEXT.md        # API contract reference & frontend handoff specification
├── requirements.txt           # Python backend dependencies
├── backend/
│   ├── main.py                # FastAPI server & REST endpoints (/api/push, /api/pull, /api/portfolio, /api/cost)
│   ├── db.py                  # SQLite (WAL mode) schema, 16-stock seed & alert rules
│   ├── llm_router.py          # 3-Tier Vertex AI / Gemini router, cost ledger & results sanity checker
│   ├── push_engine.py         # Filing hygiene, event deduplication, digest & anomaly evaluation
│   ├── pull_engine.py         # Map-Reduce primary-source synthesis, Bank/NBFC rules & Copilot Q&A
│   ├── pdf_utils.py           # SHA256 content-addressed PDF cache & PyMuPDF keyword page extractor
│   ├── warmup_demo.py         # Portfolio EOD, filing hygiene & Pull cache pre-warming script
│   └── scrapers/
│       ├── market_data.py     # yfinance EOD price, 1D % change & 20d volume ratio fetcher
│       ├── filings.py         # NSE & BSE corporate announcements scraper + hygiene rules
│       └── screener.py        # Screener.in 10Y consolidated financials, Concall & AR link scraper
└── frontend/
    └── src/
        ├── app/               # Next.js 14 App Router shell (max-w-[430px] institutional mobile frame)
        ├── components/        # PushTab, FilingCard, AlertsTab, PullTab, PortfolioTab, CostDrawer
        ├── lib/api.ts         # Typed FastAPI client
        └── types/finn.ts      # End-to-end TypeScript interfaces
```

