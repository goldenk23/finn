# FINN architecture: from company information to an explainable answer

FINN helps you follow Indian listed companies (companies whose shares trade on a stock exchange). It collects announcements, financial tables (records of sales, profits, assets, debts, and cash), and company documents, then turns them into a filtered feed, alerts, and AI research.

**How to read this:** start with the main diagram, follow the four UI tabs, then use the finance dictionary to understand the numbers. Bold labels match the UI. Meanings in parentheses explain finance terms without changing those labels. The diagrams are embedded PNG images, so Markdown preview does not need Mermaid support. Open any diagram at full size to read it comfortably.

This describes the current code, rather than a proposed cloud deployment.

## 1. The whole platform in one diagram

Read from top to bottom. Arrows show requests and information moving between components. Results return to the screen that requested them.

![FINN: end-to-end architecture](docs/diagrams/architecture-overview.png)

[Open full-size diagram](docs/diagrams/architecture-overview.png) · [Editable Mermaid source](docs/diagrams/architecture-overview.mmd)

**How to explain it aloud:** “The browser sends my action to the Python backend. The backend reads saved data or collects company information. It uses AI for classification, extraction, and explanation, saves reusable results, and sends structured data back to the screen.”

| Part | Job in plain language | Main files |
| --- | --- | --- |
| Browser UI | Displays tabs, cards, charts, sources, loading states, and errors. | `frontend/src/app/page.tsx`, `frontend/src/components/` |
| API (the connection between UI and backend) | Accepts requests and returns results or errors. | `backend/main.py` |
| Collection | Reads announcements, market data, financial tables, and PDFs. | `backend/scrapers/`, `backend/pdf_utils.py` |
| Workflow engines | Build the feed, evaluate alerts, assemble research, and answer questions. | `backend/push_engine.py`, `backend/pull_engine.py` |
| AI routing | Calls Gemini and logs usage. | `backend/llm_router.py` |
| Storage | Keeps information for reuse. | `backend/db.py`, `backend/finn.db`, `backend/cache_pdfs/` |

## 2. Start with the four tabs you see in the UI

| Exact UI tab | Question it answers | What happens |
| --- | --- | --- |
| **Push Digest** (filtered summary of announcements) | “What happened to companies I follow?” | Reads saved announcements, filters by company and time, prioritizes important categories, and collapses routine paperwork. **Refresh Live** collects updated data. |
| **Alerts** (events meeting your chosen rules) | “What needs my attention?” | Compares saved prices, volume (shares traded), and announcements with your rules. |
| **Pull Deep** (detailed company research requested by you) | “Explain this company and its performance.” | **Analyze** opens a saved report or generates one. Copilot answers follow-up questions. |
| **Portfolio** (the companies you track here) | “Which companies am I following?” | **Add** stores a company and fetches data; removing it updates the watchlist. This is a watchlist, not a record of brokerage holdings or trades. |

**Push** means information is prepared for you to browse. **Pull** means you request research. Here, “Push” is an in-app feed; it does not mean automatic phone notifications.

## 3. Push Digest: Refresh Live to Material Corporate Signals

![Push Digest: Refresh Live to the feed](docs/diagrams/push-digest.png)

[Open full-size diagram](docs/diagrams/push-digest.png) · [Editable Mermaid source](docs/diagrams/push-digest.mmd)

**Refresh Live** updates prices and announcements. Changing **24h**, **48h**, **7d**, **14d**, or **30d** changes which saved announcements you see. **All Portfolio** includes every tracked company; a company chip narrows the feed.

| UI label | Meaning and source |
| --- | --- |
| **Material Corporate Signals** (company events likely to matter to an investor) | Important announcements after classification, paperwork filtering, and deduplication (merging repeated submissions about the same event). |
| **Ranked by Materiality** (ordered by importance) | Fixed category order: results first, M&A (mergers and acquisitions: businesses combining or buying businesses) next, then leadership changes, and other categories. Newer items come first within a category. This is not a personalized AI score. |
| **Quarterly Results** (performance over three months) | Extracts Revenue (sales income), EBITDA (earnings before interest, tax, depreciation, and amortization), PBT (profit before tax), and PAT (profit after tax) from the filing PDF. Screener tables help check units and provide a fallback. |
| **Key Management** (senior leadership) | Extracts the person, role, Appointment (joining a role), or Resignation (leaving a role). Background search can supplement the filing with a short biography. |
| **Routine / Low-Relevance Filings** (administrative announcements) | Collapses paperwork such as newspaper notices and share-certificate notices. Items remain in storage. |

Only a limited number of result/leadership cards are deeply enriched during each refresh. Individual cards can request enrichment separately. Reading a saved feed does not itself require a new Gemini call.

**Freshness detail:** the backend anchors the feed window to the earlier of current time and the latest stored portfolio announcement. If collection is out of date, “24h” can cover old stored data. Check announcement dates and use **Refresh Live**.

## 4. Alerts: saved data plus your rules

![Alerts: saved data plus your rules](docs/diagrams/alerts.png)

[Open full-size diagram](docs/diagrams/alerts.png) · [Editable Mermaid source](docs/diagrams/alerts.mmd)

These checks use calculations and category matches; AI does not decide whether a numerical threshold was crossed.

| Exact UI control | Meaning | Example |
| --- | --- | --- |
| **EOD Price Move** (end-of-day price change against the previous trading close) | Alerts when the absolute percentage move reaches your threshold; either direction counts. | At ±3%, +4% or −4% triggers; +2% does not. |
| **Volume Spike** (unusually many shares traded) | Compares the latest session with the average of previous available trading sessions, up to 20. | **2.0x 20d** means twice that average. |
| **Watched Material Filing Categories** (important announcement types you follow) | Matches announcements to enabled categories. | **Results** (company performance), **Mgmt Change** (leadership changes). |

Other toggles are **M&A** (business combinations or purchases), **Fund Raise** (obtaining money through shares or borrowing), **Litigation** (legal disputes), **Capex** (spending on long-lived assets such as stores or factories), **Credit Rating** (assessment of ability to repay debt), **Dividend** (payment to shareholders), **Insider Trade** (a disclosed trade by an insider, not automatically illegal), and **Pledging** (using shares as security for a loan).

Changing rules rechecks existing data; it does not fetch new prices. The market section uses the latest stored session; the filing section uses a time window.

## 5. Pull Deep: Analyze to a company report

![Pull Deep: Analyze to a company report](docs/diagrams/pull-deep.png)

[Open full-size diagram](docs/diagrams/pull-deep.png) · [Editable Mermaid source](docs/diagrams/pull-deep.mmd)

**Map** means “extract useful evidence from documents.” **Reduce** means “combine tables and evidence into one report.” These are processing steps, not finance terms. The router can try fallback models when a call fails; the tier describes intended routing, not a guarantee that one model always runs.

The collector attempts up to four recent Concall Transcripts (written records of management discussing results and answering questions), using Investor Presentations (company briefing slides) where available. If fewer than two documents yield enough text, it also attempts the latest Annual Report (yearly business and financial report). PyMuPDF selects pages using keywords and keeps page markers. Downloaded PDFs are cached on disk; extracted text is prepared for the request.

### Eight backend modules, grouped into six UI sections

| Exact UI section | Question it answers and finance meanings | Backend module |
| --- | --- | --- |
| **Narrative vs. Numbers (4Q Audit)** (compare management's statements with four quarters of reported performance) | “Did management deliver?” Narrative (explanations or promises) is compared with figures. **Beat** (exceeded expectations), **Delivered** (met them), **Mixed** (some met and some missed), **Missed** (fell short). This is an AI assessment, not an independent financial audit. | `narrative_vs_numbers` |
| **Financial & Business Snapshot** (compact view of performance and how the company makes money) | “What does it do, how large is it, and what are its key numbers?” Includes segments (business divisions) and operating levers (factors influencing results). | `financial_snapshot`, `business_snapshot` |
| **Financial Trajectory** (how performance changes over time) | “Are sales, profits, and operating margins improving?” Charts show annual and quarterly series. **What's Growing** means increasing measures; **What's Compressing** means shrinking measures. | `trajectory` |
| **Cash Flow Quality & Balance Sheet** (cash generation plus what the company owns, owes, and retains for owners) | “Do reported profits become cash, and how much financial pressure exists?” **Balance Sheet Posture** means assessment of financial strength and debt burden. | `cash_quality`, `balance_sheet` |
| **Forward Guidance & Stated Targets** (management's expectations and future goals) | “What does management plan to achieve?” **Tone** summarizes its expressed outlook. Plans are not guaranteed outcomes. | `guidance` |
| **Bull vs. Bear Primary-Source Case** (positive arguments versus risks, based on source material) | “What supports a favorable view, and what could go wrong?” **Bull Case (Data-Backed)** means reasons for optimism; **Bear Case / Key Risks** means reasons for caution. | `bull_vs_bear` |

**Banks and NBFCs:** NBFC (non-banking financial company: a lender that is not a bank) reports use special prompt instructions. Borrowings (money owed to lenders) and deposits (customer money held by a bank) fund lending and need different interpretation from a retailer's debt. AI is asked to consider capital adequacy (financial cushion against losses) and asset quality (how reliably borrowers repay). These are interpretation instructions, not a separately validated banking calculation engine.

### Saved reports, Re-run, and the default company

- **Cached** means a previous report is reused without a new report-generation call. It can be older than the latest results.
- Lookup is by ticker. `quarter_key` records the period, but lookup has no automatic quarter-expiry check. **Re-run** forces fresh collection and synthesis.
- A saved report can appear with a more recent stored price; read the report period and price date separately.
- **Cached (<25ms • $0.00)** describes the intended fast, no-new-generation path; it is not a measured response-time guarantee. Original generation usage remains stored.
- A per-company lock prevents duplicate generation within one Python process; it does not coordinate separate processes.
- The UI initializes research to **TRENT** and requests its report at startup. This is a hardcoded default, not an AI choice or a restriction to TRENT.
- On a new analysis, the UI clears the old report and ignores superseded responses. Cancelling a browser request does not necessarily stop backend work already running.

## 6. Ask [company] Primary-Source Copilot: answer plus sources

![Primary-Source Copilot: question to cited answer](docs/diagrams/copilot.png)

[Open full-size diagram](docs/diagrams/copilot.png) · [Editable Mermaid source](docs/diagrams/copilot.mmd)

**Primary Sources** (original company documents and disclosures) include exchange filings, Concall Transcripts (written earnings discussions), Investor Presentations (company briefing slides), and Annual Reports (yearly reports). Screener is a secondary website organizing company-reported figures and document links; Yahoo Finance supplies market data. **Synthesized Report** (FINN's AI-combined explanation) is derived analysis, not an original company disclosure.

Copilot uses financial tables and report synthesis; it does not reread every original PDF for every question. Its prompt asks for available citations and acknowledgement of missing information. Resolving a citation finds a destination for a label; it does not independently prove every claim. Available history varies by company, even where the UI mentions 10Y (ten years).

## 7. Which AI does what?

Open **AI Cost & 3-Tier Model Routing** from the header's cost pill.

| Intended tier | Default model in code | Job |
| --- | --- | --- |
| **Tier 1** | `gemini-2.5-flash-lite` | Classifies announcements into 17 categories and writes feed summaries. |
| **Tier 2** | `gemini-2.5-flash` | Extracts result tables and executive details, summarizes document evidence, and answers Copilot questions. |
| **Tier 3** | `gemini-2.5-pro` | Combines evidence into the eight research modules. |

Fallbacks include Flash and preview models, so actual model use can differ from the intended tier. `llm_cost_ledger` stores model, workflow, input tokens (pieces of text sent), output tokens (pieces of text returned), and estimated USD cost. **Total Spend**, **Tokens Processed**, and **Cached Pulls** appear in the drawer. Costs use rates configured in code; this is estimation, not a cloud billing statement.

Google access uses `.env`: a Gemini API key or Vertex AI (Google Cloud's AI service) with project configuration and Application Default Credentials (Google's local application login). A successful cached read does not verify Google connectivity; fresh synthesis or Copilot needs it.

## 8. What is stored, and why?

SQLite is a local database in `backend/finn.db`. WAL mode (a setting that lets readers continue during many writes) helps ordinary database access; writes still have concurrency limits.

| Table or folder | What it remembers | Used by |
| --- | --- | --- |
| `portfolio` | Tickers, exchange codes, names, and sectors (business categories). | All four tabs |
| `eod_prices` | Dated closes, previous closes, volume, and volume comparisons. | Portfolio, Alerts, report price header |
| `announcements` | IDs, dates, categories, summaries, document URLs, and enriched card data. | Push Digest, Alerts, research evidence |
| `alert_rules` | Thresholds and watched categories; one shared rules row. | Alerts |
| `pull_reports_cache` | One report per ticker, financial tables, period, source references, and generation usage. | Pull Deep, Copilot |
| `llm_cost_ledger` | AI calls and estimated costs by workflow and model. | Cost drawer |
| `backend/cache_pdfs/` | PDFs named using a hash of the source URL. | Result-card extraction and research |

**Portfolio → Add** saves a company and fetches prices and announcements; it does not automatically generate full research. **Warm Pull Cache** means some tracked companies already have saved reports. Removing a company from the watchlist does not erase all its historical data or research.

## 9. Finance dictionary: understand the numbers on screen

The first column preserves UI wording or abbreviations. Read the meaning in parentheses directly alongside each label.

The building blocks are **shares** (small units of company ownership), **shareholders** (people or organizations owning shares), **assets** (resources a company owns or controls), **liabilities** (amounts or obligations it owes), and **equity** (owners' accounting interest after subtracting liabilities from assets). **Profit** (income minus expenses under accounting rules) can differ from **cash flow** (actual cash moving in and out).

### Performance and comparisons

| UI term | Meaning |
| --- | --- |
| **Revenue** | (Sales income before deducting expenses.) |
| **EBITDA** | (Earnings before interest, tax, depreciation, and amortization. Interest is the cost of borrowing; depreciation and amortization spread asset costs over time. This profit measure excludes those charges and tax. It is not cash flow.) |
| **PBT** | (Profit before tax: profit after relevant expenses but before income tax.) |
| **PAT / Net Profit** | (Profit after tax: accounting profit remaining after expenses and tax.) |
| **OPM %** | (Operating profit margin: operating profit as a percentage of revenue. At 20%, ₹100 of revenue produces ₹20 of operating profit; bank reports can use another margin measure, explained in their text.) |
| **QoQ %** | (Quarter-on-quarter percentage change: compare with the immediately preceding three-month period.) |
| **YoY %** | (Year-on-year percentage change: compare with the same period one year earlier.) |
| **₹ Cr** | (Indian rupees in crores; ₹1 Cr = ₹10,000,000 = ₹10 million.) |
| **FY / Q1–Q4 / 4Q** | (Financial year / its four three-month quarters / four quarters. In the usual Indian April–March year, FY27 ends in March 2027; Q1 is April–June 2026.) |
| **TTM** | (Trailing twelve months: totals for the most recent twelve months.) |
| **Consolidated / Standalone** | (Company plus subsidiaries as a group / company on its own. Check the source basis before comparing figures.) |
| **Capex / Capacity** | (Spending on long-lived assets / how much a business can produce or serve.) |

### Price and valuation

| UI term | Meaning |
| --- | --- |
| **Ticker** | (Company trading symbol, such as TRENT or HDFCBANK.) |
| **NSE / BSE** | (National Stock Exchange / BSE, two Indian exchanges where shares trade.) |
| **CMP** | (Current market price: share price reported by the source; check its date because this app uses stored or end-of-day data.) |
| **Mkt Cap** | (Market capitalization: share price multiplied by outstanding shares; the market value of company equity.) |
| **Stock P/E** | (Price-to-earnings ratio: share price divided by earnings per share; usefulness depends on the earnings figure and period.) |
| **ROCE %** | (Return on capital employed: operating earnings relative to capital used by the business; measures capital efficiency.) |
| **ROE %** | (Return on equity: net profit relative to shareholders' equity; measures returns on owners' capital.) |
| **EOD / 1D** | (End of day / one trading day's change, normally against the previous trading close.) |
| **Volume / 20d / 2x** | (Shares traded / up to twenty prior trading sessions for the average / twice that average.) |

### Cash and financial strength

| UI term | Meaning |
| --- | --- |
| **Operating Cash Flow (CFO)** | (Cash generated or used by operating activities. Here CFO means cash from operations, not Chief Financial Officer.) |
| **CFO vs PAT / Cash Conversion** | (Compare operating cash with accounting profit to see how much profit becomes cash. Timing and losses can distort a period's ratio.) |
| **Balance Sheet Posture** | (Assessment of financial strength based on assets, liabilities, owners' funds, and related measures.) |
| **Borrowings** | (Money owed to lenders.) |
| **Reserves** | (Accumulated retained profits and other accounting reserves within owners' equity; not necessarily cash in a bank account.) |
| **Debt / Equity** | (Debt compared with shareholders' funds; indicates borrowing relative to owners' capital.) |
| **Work Cap Days** | (Working capital days: operating funds tied up in running the business, expressed in days of sales; follow the source's definition.) |
| **Cash Conv Cycle** | (Cash conversion cycle: inventory days plus customer-payment days minus supplier-payment days; estimates how long operating cash stays tied up.) |
| **Guidance / Stated Targets** | (Management's future expectations or goals, rather than achieved results.) |
| **Bull / Bear** | (Reasons for an optimistic view / reasons for caution or a negative view.) |

## 10. Request map for explanation or debugging

The browser normally sends requests to its Next.js server. `frontend/next.config.mjs` forwards `/api/*` to FastAPI at `127.0.0.1:8000`; the destination is configurable. Responses are ordinary JSON, not progress streams. The current proxy timeout is 180 seconds for long-running generation.

| UI action | Backend request |
| --- | --- |
| Read watchlist | `GET /api/portfolio` |
| Add / remove company | `POST /api/portfolio` / `DELETE /api/portfolio/{ticker}` |
| Read filtered feed | `GET /api/push/digest?window=24h&ticker=...` |
| **Refresh Live** | `POST /api/push/refresh` |
| Enrich one card | `POST /api/push/enrich/{filing_id}` |
| Read alerts / change rules | `GET /api/push/alerts` / `PUT /api/push/alerts/rules` |
| **Analyze** / **Re-run** | `GET /api/pull/{ticker}` / same route with `?force=true` |
| Ask Copilot | `POST /api/pull/{ticker}/ask` |
| List saved reports / AI usage | `GET /api/pull-cached` / `GET /api/cost` |
| Check backend availability | `GET /api/health` |

**When something fails:** identify the action first. An empty feed may reflect filters or old data. Fresh research can fail during collection, Google authentication, model calls, or response parsing. Research/Copilot routes log technical details and return shorter UI errors. A healthy `/api/health` confirms that FastAPI responds; it does not test external services.

## 11. Current operating boundaries

Next.js and FastAPI run as two services with local SQLite/PDF storage. AI computation runs at Google. Collection and generation are triggered by requests or a manually run warm-up script; current server startup has no automatic background scheduler. The UI shows loading states while requests run.

There is no implemented brokerage connection, trade execution, individual-user login system, phone-notification delivery, or configured cloud deployment in this repository. AWS hosting, Caddy, capacity estimates, and automatic polling belong to future deployment work rather than the current architecture.

### A short walkthrough you can use to explain FINN

“I add a company in **Portfolio** (my watchlist). **Refresh Live** collects prices and official announcements. **Push Digest** filters paperwork and surfaces important events. **Alerts** applies my rules. In **Pull Deep**, **Analyze** retrieves or builds a report from financial tables and company documents. I ask questions through **Ask [company] Primary-Source Copilot** and open its sources. Saved reports and PDFs reduce repeated work, while the cost drawer shows logged AI usage.”

