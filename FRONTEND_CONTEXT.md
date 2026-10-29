# FINN — Mobile-First Frontend Handoff & Codebase Context

## 1. Project Overview (ENAM Holdings AI Engineer Build Challenge)
We are building **FINN**, a **mobile-first financial investment agent** for Indian public-markets investors (NSE/BSE).
The backend (`backend/`) is **100% built, tested with live NSE/BSE/Screener/Vertex AI Gemini data, and committed to Git**.
The next step is building the **Mobile-First Next.js Frontend (`frontend/`)** and pre-warming the remaining portfolio stocks for a fast demo.

### Core Product Bar ("No AI Slop")
- **Mobile-first viewport:** Even when opened on a desktop browser by an evaluator, constrain the main shell to a crisp mobile viewport (`max-w-[430px] mx-auto min-h-screen`) with bottom tab navigation, high information density, and zero filler prose.
- **Two Modes of Interaction:**
  1. **Push Mode (Proactive Intelligence):**
     - **Windowed Digest (`24h` / `48h` / `7d` / `14d` / `30d`):** Prioritizes **Material** corporate filings at the top while collapsing **Routine** paperwork (`unclassified`, newspaper ads, trading window closures) behind a single expandable counter (showing `% noise filtered`).
     - **Special Push Cards:**
       - `results`: Displays a 4-row mini table (**Revenue, EBITDA, PBT, PAT**) with **QoQ %** and **YoY %** pills + 1-line takeaway.
       - `management_change`: Displays executive name, role, action badge (`Appointment` / `Resignation`), and a grounded **2-line background/bio** enriched via DuckDuckGo + filing PDF.
     - **Triggered Alerts + Configurable Rules Drawer:** Shows EOD unusual price moves (`>= ±X%`) and volume spikes (`>= Yx 20d avg`) + watched material filing categories, with sliders/toggles allowing the investor to customize thresholds live.
  2. **Pull Mode (On-Demand Primary-Source Deep Research):**
     - Works on **any NSE/BSE ticker** (portfolio stocks + ad-hoc search bar for any NSE symbol like `ZOMATO`, `TRENT`, `RELIANCE`, `KPITTECH`, `DIXON`, `HAL`).
     - Grounded strictly in primary sources (Screener 10Y financials, Quarterly tables, Concall Transcripts, Investor Presentations, Annual Reports).
     - Displays the **8 Research Modules**:
       1. `business_snapshot` (Overview, Segments, Key Operating Levers)
       2. `financial_snapshot` (Market Cap, Price, P/E, ROCE, ROE, Revenue, PAT, OPM %)
       3. `trajectory` (What's growing vs. compressing + multi-year `annual_series` & `quarterly_series` charts)
       4. `balance_sheet` (Status badge, Borrowings, Reserves, D/E, Working Capital & Cash Conversion Cycle days)
       5. `cash_quality` (Verdict badge, CFO vs. PAT conversion %, `annual_cash_vs_pat` chart)
       6. **`narrative_vs_numbers` (Signature Worked Example):** 4-Quarter Audit comparing `management_narrative` (what was promised/guided) vs. `actual_numbers` (what was delivered), with `Beat` / `Delivered` / `Mixed` / `Missed` badge and `audit_note`.
       7. `guidance` (Management tone badge + stated forward targets with primary source tags)
       8. `bull_vs_bear` (3 data-backed Bull points vs. 3 Bear points)
     - **Primary-Source Copilot Q&A:** Interactive input box at the bottom of any Pull report (`POST /api/pull/{ticker}/ask`) returning cited answers grounded strictly in that company's filings.
     - **Cost & Cache Telemetry Badge:** Shows token count, USD cost, and `Cached (<25ms · $0.00)` status on Pull reports and in a global cost drawer (`GET /api/cost`).

---

## 2. Backend Codebase & Git Status
- **Git Binary Path (if not on PATH):** `C:\Users\golde\AppData\Roaming\Accio\pre-install\ab1f8a6ee51b\git\cmd\git.exe`
- **Commits Completed:**
  - `2f0920d` — `feat(plumbing): add sqlite schema and market/filings/screener scrapers`
  - `69c089e` — `feat(push): add PDF extractor, Gemini router with cost ledger, and Push digest/alerts engine`
  - `c063afd` — `feat(pull): add primary-source Pull research engine, copilot Q&A, and FastAPI endpoints`

### Backend Directory Structure (`backend/`)
- `backend/db.py` — SQLite (WAL mode) at `backend/finn.db`. Seeded with 16 NSE portfolio stocks (`RELIANCE`, `TCS`, `HDFCBANK`, `INFY`, `TATAMOTORS`, `SUNPHARMA`, `TRENT`, `LT`, `BAJFINANCE`, `MARUTI`, `ZOMATO`, `HAL`, `ITC`, `TITAN`, `KPITTECH`, `CIPLA`).
- `backend/scrapers/market_data.py` — `yfinance` EOD price, 1d % change, volume, and 20d avg volume ratio.
- `backend/scrapers/filings.py` — NSE corporate announcements scraper (with cookie warm-up & BSE fallback) + heuristic pre-classifier.
- `backend/scrapers/screener.py` — Screener.in consolidated/standalone scraper (Top ratios, Quarters, 10Y P&L, Balance Sheet, Cash Flow, Ratios, Annual Report & Concall PDF links).
- `backend/pdf_utils.py` — SHA256 content-addressed PDF cache (`backend/cache_pdfs/`), unpacks NSE `.zip` archives and BSE `AnnPdfOpen.aspx` links, extracts keyword-dense pages via `PyMuPDF` (`fitz`).
- `backend/llm_router.py` — Vertex AI / Gemini router (`gemini-2.5-flash-lite`, `gemini-2.5-flash`, `gemini-2.5-pro` / `gemini-3.1-pro-preview`) + `llm_cost_ledger` logging.
- `backend/push_engine.py` — `refresh_portfolio_push_data`, `get_windowed_digest`, `get_triggered_alerts`, `enrich_single_filing`.
- `backend/pull_engine.py` — `get_or_generate_pull_report` (Map-Reduce over Concall/PPT/AR PDFs + Screener tables, cached in `pull_reports_cache`) and `ask_pull_copilot`.
- `backend/main.py` — FastAPI server exposing all `/api/*` endpoints.

### Running the Backend Server
```powershell
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
```

---

## 3. FastAPI Endpoints & Response Shapes (`backend/main.py`)

### Portfolio
- `GET /api/portfolio`
  - Returns `{ "portfolio": [ { "ticker", "bse_code", "company_name", "sector", "cap_tier", "trade_date", "close_price", "pct_change", "volume", "avg_volume_20d", "volume_ratio", "price_alert", "volume_alert", "material_filings_count", "has_cached_pull" } ], "count": 16, "alert_rules": { ... } }`
- `POST /api/portfolio` — Body: `{ "ticker": "DIXON", "sector": "EMS" }`
- `DELETE /api/portfolio/{ticker}`

### Push (Digest & Alerts)
- `GET /api/push/digest?window=24h|48h|7d|14d|30d&ticker=OPTIONAL`
  - Returns:
    ```json
    {
      "window": "14d",
      "stats": {
        "total_filings": 27,
        "material_count": 14,
        "routine_collapsed_count": 13,
        "noise_filtered_pct": 48.1
      },
      "material": [
        {
          "filing_id": "...",
          "ticker": "TRENT",
          "company_name": "Trent Limited",
          "title": "...",
          "pdf_url": "...",
          "published_at": "2026-10-19 17:25:08",
          "exchange": "NSE",
          "category": "management_change",
          "materiality": 1,
          "summary": "1-line investor summary",
          "priority_rank": 3,
          "enriched": {
            // For 'results': { "period", "unit", "source", "metrics": { "revenue": {"value","qoq_pct","yoy_pct"}, "ebitda": ..., "pbt": ..., "pat": ... }, "takeaway" }
            // For 'management_change': { "person_name", "role", "action", "effective_date", "two_line_bio", "source_url" }
          }
        }
      ],
      "routine": [ ... ]
    }
    ```
- `POST /api/push/refresh` — Body: `{ "tickers": ["RELIANCE", "TRENT"], "days": 14, "max_enrich": 5 }`
- `POST /api/push/enrich/{filing_id}` — Triggers on-demand deep enrichment for a specific filing.
- `GET /api/push/alerts?window=14d`
  - Returns `{ "rules": { "price_move_pct": 3.0, "volume_multiplier": 2.0, "watched_categories": [...] }, "total_alerts": 14, "market_alerts": [...], "filing_alerts": [...] }`
- `PUT /api/push/alerts/rules` — Body: `{ "price_move_pct": 2.5, "volume_multiplier": 1.8, "watched_categories": ["results", "M&A", "management_change"] }`

### Pull (On-Demand Primary-Source Research)
- `GET /api/pull-cached` — Lists tickers already cached in `pull_reports_cache` for instant 20ms loading.
- `GET /api/pull/{ticker}?force=false`
  - Returns `{ "ticker", "quarter_key", "cached", "tokens_in", "tokens_out", "cost_usd", "created_at", "eod", "fundamentals", "report": { "business_snapshot", "financial_snapshot", "trajectory", "balance_sheet", "cash_quality", "narrative_vs_numbers", "guidance", "bull_vs_bear", "sources_used" } }`
- `POST /api/pull/{ticker}/ask` — Body: `{ "question": "..." }` -> Returns `{ "ticker", "question", "answer", "citations": [...] }`

### Cost Telemetry
- `GET /api/cost` — Returns `{ "totals": { "total_calls", "tokens_in", "tokens_out", "cost_usd" }, "by_workflow": [...] }`

