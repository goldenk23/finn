# FINN: System Architecture & Capacity Plan

## 1. End-to-End System Architecture Diagram

```mermaid
flowchart TB
    subgraph Client["Mobile Client (Browser / PWA)"]
        UI["Next.js App Router + Tailwind + shadcn/ui<br/>(Mobile Viewport Frame, Push Feed, Pull Research View)"]
    end

    subgraph Sources["External Primary Data Sources"]
        YF["Yahoo Finance (yfinance)<br/>EOD Price & Volume (.NS / .BO)"]
        EX["NSE / BSE Filings API<br/>Corporate Announcements & PDFs"]
        SC["Screener.in<br/>10Y Financials, AR & Concall PDF Links"]
        WEB["DuckDuckGo (ddgs) / Search Grounding<br/>Executive Bio Enrichment"]
    end

    subgraph GCP["Google Cloud Platform (Vertex AI)"]
        FLASH_LITE["Gemini 2.5 Flash-Lite<br/>Bulk Filing Classifier (17 Categories) & 1-Line Digest"]
        FLASH["Gemini 2.5 Flash<br/>PDF Extraction: QoQ/YoY Results, Concall Guidance Chunks"]
        PRO["Gemini 2.5 Pro<br/>Deep Pull Synthesis: 'Narrative vs. Numbers' & Bull/Bear"]
    end

    subgraph AWS["AWS EC2 VM (ap-south-1 Mumbai | t3.small: 2 vCPU, 2GB RAM, 20GB gp3 SSD)"]
        CADDY["Caddy Reverse Proxy<br/>(Auto-HTTPS, HTTP/2, Gzip/Zstd, Static Cache)"]
        NEXT["Next.js Server (Node.js)<br/>SSR + UI Shell (:3000)"]
        
        subgraph Backend["Python FastAPI Backend (:8000 | Async Uvicorn Workers)"]
            API["FastAPI Router<br/>(/api/push, /api/pull, /api/alerts, /api/portfolio)"]
            SCHED["Background Poller (Asyncio Task)<br/>EOD Price/Volume Anomaly Detector & Filing Watcher"]
            SCRAPE["Scraper & PDF Engine<br/>httpx (Async) + BeautifulSoup4 + PyMuPDF (fitz)"]
            ROUTER["Smart LLM Router & Cost Tracker<br/>Token Counter + Cache-First Guard + Prompt Chunker"]
        end

        subgraph Storage["Persistent EBS Storage (gp3 SSD)"]
            SQLITE[("SQLite DB (WAL Mode)<br/>• portfolios & alert_rules<br/>• announcements (classified)<br/>• eod_prices<br/>• pull_reports_cache (JSON)<br/>• llm_cost_ledger")]
            PDF_CACHE[("Local PDF & Text Store<br/>Extracted Concall & AR Text<br/>(Content-Addressed by SHA256)")]
        end
    end

    UI <-->|"HTTPS (JSON / SSE Stream)"| CADDY
    CADDY -->|"/"| NEXT
    CADDY -->|"/api/*"| API

    API <--> SQLITE
    SCHED --> SCRAPE
    API --> SCRAPE
    SCRAPE <--> PDF_CACHE
    SCRAPE --> YF & EX & SC & WEB

    API --> ROUTER
    SCHED --> ROUTER
    ROUTER <--> SQLITE
    ROUTER -->|"Tier 1: Bulk Triage"| FLASH_LITE
    ROUTER -->|"Tier 2: Structured Extraction"| FLASH
    ROUTER -->|"Tier 3: Deep Reasoning"| PRO
```

---

## 2. Push vs. Pull Execution Flows

### A. Push Flow (Proactive Digest & Threshold Alerts)
```mermaid
sequenceDiagram
    autonumber
    participant Poller as Async Background Poller
    participant Source as BSE/NSE + yfinance
    participant FitZ as PyMuPDF (fitz)
    participant Flash as Vertex AI (Flash-Lite / Flash)
    participant DB as SQLite (WAL Mode)
    participant User as Mobile UI

    Poller->>Source: Fetch new filings & EOD price/volume across portfolio
    Source-->>Poller: Raw filings metadata + EOD OHLCV
    Poller->>DB: Deduplicate against seen filing_ids
    Poller->>Poller: Check EOD Volume > 2x 20d-avg or Price > ±X%
    Poller->>Flash: Batch classify new filings into 17 categories + 1-line summary (Flash-Lite)
    alt Material Filing = 'results'
        Poller->>FitZ: Download & extract P&L table pages from Results PDF
        Poller->>Flash: Extract Revenue, EBITDA, PBT, PAT (QoQ & YoY %)
    else Material Filing = 'management_change'
        Poller->>Flash: Enrich incoming/outgoing exec bio via ddgs / Search Grounding
    end
    Poller->>DB: Save enriched items & trigger matching user alert rules
    User->>DB: Open App -> Instant read (<15ms) of prioritized Windowed Digest
```

### B. Pull Flow (On-Demand Primary-Source Deep Research)
```mermaid
sequenceDiagram
    autonumber
    participant User as Mobile UI
    participant API as FastAPI (/api/pull/{ticker})
    participant DB as SQLite + PDF Text Cache
    participant Screener as Screener.in
    participant FitZ as PyMuPDF (fitz)
    participant Vertex as Vertex AI (Flash -> Pro)

    User->>API: Request research on any NSE/BSE ticker (e.g., TRENT.NS)
    API->>DB: Check pull_reports_cache (valid for current quarter)
    alt Cache Hit
        DB-->>User: Return full interactive report + Cost Badge in <15ms ($0.00)
    else Cache Miss (Uncached Ticker)
        API-->>User: Stream progress via SSE ("Fetching Screener financials...")
        API->>Screener: Scrape 10Y P&L, Balance Sheet, Cash Flow, Ratios + Concall/AR links
        API->>FitZ: Parallel download & extract last 4 Concall PDFs + latest AR MD&A
        API->>DB: Cache extracted raw text on disk
        API->>Vertex: [Map Step - Gemini 2.5 Flash] Extract explicit guidance & promises per quarter
        API->>Vertex: [Reduce Step - Gemini 2.5 Pro] Compare 4Q Promises vs Actuals + 8 Research Modules
        Vertex-->>API: Structured JSON (grounded with exact transcript citations)
        API->>DB: Save to pull_reports_cache & record token cost
        API-->>User: Stream completed report to Mobile UI
    end
```

---

## 3. Concurrent Users & Capacity Calculation

Assuming an **AWS EC2 `t3.small`** instance in `ap-south-1` (Mumbai):
* **Hardware:** 2 vCPUs, 2 GB RAM, 20 GB gp3 NVMe SSD (3,000 IOPS, 125 MB/s), 5 Gbps burst network.
* **Database Configuration:** SQLite with `PRAGMA journal_mode=WAL;` and `PRAGMA synchronous=NORMAL;` (allows concurrent reads while a write is occurring).

### Bottleneck Analysis by Resource

#### 1. RAM Footprint (2,048 MB total)
| Component | Memory Used | Notes |
| :--- | :--- | :--- |
| Ubuntu 24.04 LTS OS + Kernel + Caddy | ~220 MB | Minimal footprint |
| Next.js Production Server (`next start`) | ~200 MB | Serves mobile shell |
| FastAPI (2 Uvicorn workers) | ~250 MB | `~125 MB` per async Python worker |
| SQLite Page Cache (`cache_size = -64000`) | ~64 MB | Keeps hot DB pages in RAM |
| Peak `PyMuPDF` + `httpx` buffer (concurrent PDF parsing) | ~350 MB | ~30–50 MB per active 200-page Annual Report parse |
| **Total Used at Peak** | **~1,084 MB** | **~960 MB free headroom** |

#### 2. Workload Latency & Throughput Math
Traffic falls into two distinct categories:
1. **Cached Reads & Push Digests (95% of traffic):**
   - Viewing the Push digest, toggling time windows (24h / 7d / 30d), filtering categories, checking triggered alerts, or opening an already-analyzed Pull company report.
   - **Latency:** SQLite WAL read + FastAPI serialization takes **3 ms – 8 ms** of CPU time per request.
   - **Throughput:** With 2 vCPUs, FastAPI + SQLite WAL comfortably serves **250 – 400 requests/second**.

2. **Uncached "Cold" Pull Research Generation (5% of traffic):**
   - When a user types a brand-new NSE ticker that isn't in `pull_reports_cache` yet.
   - **I/O bound (Network + Vertex AI):** Scraping Screener + downloading 4 Concall PDFs (`~2s` via async `httpx`) + `PyMuPDF` text extraction (`~0.4s` CPU time) + Vertex AI Flash/Pro synthesis (`~12–18s` network wait).
   - Because FastAPI uses `asyncio`, waiting on Vertex AI consumes **zero CPU**.
   - **Hard limits on Cold Pulls:**
     - **CPU (`PyMuPDF` parsing):** ~0.4s CPU per cold ticker -> max **~5 cold tickers parsed/second**.
     - **Vertex AI Quota (Default GCP Tier 1):** 300–1,000 RPM for Gemini 2.5 Flash/Pro -> **~5–15 cold ticker analyses/minute** before hitting GCP rate limits.
     - **In-flight deduplication (`asyncio.Lock` per ticker):** If 50 users request the same uncached ticker (`ZOMATO.NS`) simultaneously, only **1** scrape/LLM job runs and all 50 receive the result.

---

### Final Concurrency Numbers (`t3.small` @ $15/mo)

Taking an active investor session where a user clicks/interacts **once every 15 seconds** (1/15 req/s per active user):

| Scenario | Max Concurrent Active Users | Requests / Sec (RPS) | Limiting Factor |
| :--- | :--- | :--- | :--- |
| **1. Push Digest, Alerts & Cached Pull Reports** *(Normal daily workflow)* | **3,500 – 5,000 concurrent users** | ~250 – 350 RPS | vCPU serialization & Caddy/Uvicorn workers |
| **2. Mixed Workload** *(95% cached reads + 5% brand-new uncached tickers)* | **400 – 600 concurrent users** | ~30 – 40 RPS *(including ~10–15 cold LLM analyses/min)* | Vertex AI RPM quota & `PyMuPDF` RAM buffer |
| **3. Worst-Case Cold Stampede** *(100% of users requesting distinct uncached tickers at the exact same second)* | **15 – 25 simultaneous cold analyses** | ~15–25 concurrent PDF+LLM pipelines | 2 GB RAM (`PyMuPDF` buffers) & Vertex AI RPM quota |

> [!TIP]
> **Vertical Scaling Ceiling (Zero Code Changes):**
> Upgrading the EC2 instance type from `t3.small` ($15/mo) to `t3.medium` (4 GB RAM, $30/mo) or `c7g.large` (4 GB RAM, dedicated CPU, $50/mo) immediately doubles/triples the in-memory PDF parsing concurrency without touching a single line of code or database config.

