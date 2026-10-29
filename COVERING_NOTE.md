# Covering Note — FINN (ENAM Holdings AI Engineer Build Challenge)

## 1. What We Built

We built **FINN**, a mobile-first institutional investment agent for Indian public equities (NSE/BSE) designed around a single core principle: **high signal density, primary-source grounding, and zero AI slop**.

### Push Mode (Proactive Portfolio Intelligence)
1. **17-Category Filing Triage & Hygiene Guard (`backend/scrapers/filings.py`, `backend/push_engine.py`):**
   - Scrapes live corporate announcements from NSE (with session cookie warm-up) and BSE fallback across a 16-stock institutional watchlist (`RELIANCE`, `TCS`, `HDFCBANK`, `INFY`, `TATAMOTORS`, `SUNPHARMA`, `TRENT`, `LT`, `BAJFINANCE`, `MARUTI`, `ZOMATO`, `HAL`, `ITC`, `TITAN`, `KPITTECH`, `CIPLA`).
   - Enforces a deterministic + LLM hygiene layer (`enforce_filing_hygiene`) that guarantees routine paperwork—newspaper clippings, Regulation 74(5) depository certificates, loss of share certificates, trading window closures, and scheduled analyst meet notices—is automatically buried in a collapsible Routine drawer (`~55–75%` noise filtered).
   - Deduplicates multi-filing corporate events (`_deduplicate_material_filings`) so repeat exchange submissions for the same event (e.g., dual resignation intimations for `TRENT` or separate Board Outcome + Auditor Report uploads for `INFY`) render as a single canonical card.
2. **Deep-Enriched Hero Cards (`backend/llm_router.py`, `frontend/src/components/FilingCard.tsx`):**
   - **Quarterly Results (`results`):** Extracts Consolidated **Revenue, EBITDA, PBT, and PAT** (`₹ Cr`) with color-coded **QoQ %** and **YoY %** delta pills and a 1-line takeaway. To prevent PDF unit-conversion hallucinations (such as companies reporting in `₹ Million` or `₹ Billion` where an LLM shifts decimals inconsistently), FINN cross-checks extracted figures against Screener's standardized `₹ Cr` quarterly table (`_is_pdf_extraction_sane`).
   - **Management Changes (`management_change`):** Extracts the incoming/outgoing executive's name, role, and action (`Appointment` / `Resignation`) from the exchange PDF, queries DuckDuckGo (`ddgs`) for verified career history, and synthesizes a grounded **2-line executive bio** with source links.
3. **Live Configurable Threshold Alerts (`frontend/src/components/AlertsTab.tsx`):**
   - Tracks EOD price moves (`>= ±X%`) and volume spikes (`>= Yx 20d avg`) via `yfinance` (with automatic symbol alias resolution for renamed/demerged NSE tickers such as `ZOMATO` → `ETERNAL.NS` and `TATAMOTORS` → `TMPV.NS`) alongside watched material filing categories.

### Pull Mode (On-Demand Primary-Source Research for Any NSE/BSE Stock)
1. **Signature Worked Example — 4-Quarter "Narrative vs. Numbers" Audit (`frontend/src/components/PullTab.tsx`):**
   - Leads every Pull report by auditing management's stated guidance, margin commentary, and capex/expansion promises from primary Concall Transcripts, Investor Presentations, and Annual Reports against actual reported Revenue, OPM %, and PAT across the last 4 quarters (`Beat` / `Delivered` / `Mixed` / `Missed`).
2. **8 Institutional Research Modules + Banking/NBFC Domain Calibration:**
   - Covers `narrative_vs_numbers`, `financial_snapshot`, `business_snapshot`, `trajectory` (6Q and 5Y charts), `cash_quality` (CFO vs. PAT conversion), `balance_sheet`, `guidance`, and `bull_vs_bear`.
   - Includes explicit **Banking & NBFC domain rules** (`backend/pull_engine.py`) so lenders like `HDFCBANK` and `BAJFINANCE` are evaluated on Financing Margin / NIM, Capital Adequacy, and Asset Quality rather than misinterpreting deposit/borrowing funding or loan-book expansion cash flows as corporate leverage distress.
3. **Primary-Source Copilot Q&A (`POST /api/pull/{ticker}/ask`):**
   - Lets the investor ask ad-hoc follow-up questions grounded strictly in the company's 10Y financial tables and extracted concall/AR text, returning exact numbers and source citations.

---

## 2. Key Architectural, Model Routing & Cost Choices

| Decision | Choice Made | Why / Trade-Off |
| :--- | :--- | :--- |
| **3-Tier Gemini Routing** | **Tier 1:** `gemini-2.5-flash-lite` (`$0.10/1M`)<br/>**Tier 2:** `gemini-2.5-flash` (`$0.30/1M`)<br/>**Tier 3:** `gemini-2.5-pro` (`$1.25/1M`) | Using `Pro` for bulk filing triage would cost 12.5x more and add 5x latency. Batching 15 filings per `Flash-Lite` call keeps triage under `$0.001`, while `Flash` handles structured PDF extraction/Map steps and `Pro` is reserved strictly for the final 8-module Reduce synthesis (`~$0.06` per cold stock report). |
| **Map-Reduce over Keyword-Dense PDF Pages** | `PyMuPDF` (`fitz`) page scoring + `Flash` Map → `Pro` Reduce | Annual Reports run 250–400 pages. Dumping raw PDFs into a single prompt wastes tokens and degrades attention. Scoring pages by financial/guidance keyword density (`backend/pdf_utils.py`) and caching extracted text by `SHA256(url)` cuts input tokens by >80% while preserving exact verbatim figures. |
| **SQLite (WAL Mode) + Quarter Cache** | Single-file `backend/finn.db` with `PRAGMA journal_mode=WAL` | Zero external database ops overhead on a single AWS `t3.small` VM (`2 vCPU, 2GB RAM`). Repeat Pull lookups hit `pull_reports_cache` in **<15ms at $0.00**, supporting 3,500+ concurrent cached readers (see `ARCHITECTURE.md`). |
| **Mobile-First Institutional UI (`430px` shell)** | Next.js 14 App Router + Tailwind + native SVG/Tailwind financial charts | Constrained to a sharp `430px` mobile viewport on desktop and 100% width on phones. Using lightweight native SVG/Tailwind combo charts avoids 45+ transitive chart packages and eliminates SSR hydration layout shift. |

---

## 3. What We Intentionally Skipped (Thin & Working Discipline)

To ship an end-to-end, reliable system with real NSE/BSE filings and primary documents rather than brittle scaffolding:
- **No Broker Reports, News Portals, or Social Sentiment:** Strictly excluded per challenge rules so every claim traces back to an exchange filing, Screener financial table, Concall transcript, or Annual Report.
- **No Heavy Vector DB / RAG Chunking Pipeline:** For single-company Pull research, structured 10Y financial tables + keyword-dense Concall/AR page extraction in a long-context Map-Reduce pipeline outperforms fragmented vector chunks (which frequently sever financial table headers from their rows).
- **No Intraday Tick-by-Tick WebSocket Streaming:** Long-only fundamental investors act on EOD price/volume anomalies (`>= ±X%` move or `>= Yx` 20d volume) and material corporate disclosures rather than sub-second tick noise.
- **No Multi-Tenant Auth / Push Notification Plumbing:** Focused 100% of engineering budget on financial accuracy, unit cross-checking, filing deduplication, and mobile UX density.

---

## 4. What We Would Build Next (Production Roadmap)

1. **Automated NSE/BSE Webhook & Background Poller (`asyncio` cron + Web Push / WhatsApp delivery):**
   - Run `refresh_portfolio_push_data` every 15 minutes during market hours (`09:00–18:00 IST`) and push instant mobile notifications when a watched category (`results`, `M&A`, `management_change`) or volume anomaly triggers.
2. **Table-Aware OCR Fallback (Document AI / Gemini Vision on Scanned PDFs):**
   - When an older BSE/NSE filing is a scanned image PDF without an embedded text layer, pass only the 2–3 financial table pages as images to `gemini-2.5-flash` multimodal vision instead of falling back to Screener's quarterly table.
3. **Peer-Group Cross-Comparison in Pull Mode:**
   - Pull standardized KPIs (OPM %, ROCE %, Working Capital Days, EV/EBITDA) for the top 3 sector peers alongside the target company to contextualize whether margin compression is company-specific or sector-wide.
4. **Historical Filing Diff ("What Changed in the Annual Report"):**
   - Side-by-side semantic diff of Contingent Liabilities, Related Party Transactions, and Auditor Qualifications between the latest and prior year's Annual Reports.

