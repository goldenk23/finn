# FINN: covering note

ENAM Holdings AI Engineer Build Challenge. This note covers what I built, the main decisions and trade-offs, what I left out, and what I'd do next. [ARCHITECTURE.md](ARCHITECTURE.md) has the details with file and line references. Every cost and token number below comes from the app's own ledger in `backend/finn.db`.

## What I built

FINN is a mobile-first web app for following a portfolio of Indian listed companies. It has two modes.

**Push is the daily triage.** I open it in the morning to see what happened at my companies without reading every exchange filing.
- Material events come first, in a fixed order: results, M&A, management changes, fund raises, litigation, capex, credit ratings, and so on.
- Routine paperwork (newspaper notices, trading-window closures, Reg 74(5) certificates, ESOP allotments, analyst-meet schedules) is collapsed into a drawer under the feed.
- The same event filed twice becomes one card, with the other filings listed as "Also filed as".
- Results filings get a card with Revenue, EBITDA, PBT and PAT and their QoQ and YoY change. Management changes get a short factual bio of the person.
- The Alerts tab checks end-of-day price moves, volume spikes and watched filing categories against thresholds I set (defaults 3% and 2x, as in the brief). While the page is open, it polls every 60 s, shows a badge with the number of unseen alerts, and can raise a browser notification.

**Pull is on-demand due diligence.** I type a ticker and get a report built from primary sources only: the latest Annual Report, the last 4 concall transcripts or presentations, and Screener's financial tables. There are 8 modules. The one I care most about is narrative vs numbers: for each of the last 4 quarters it compares what management guided with what they reported. Where management held no call before a quarter ended, it says "No guidance" instead of stretching older guidance to fit. A Copilot answers follow-up questions from the same sources and says so when the answer isn't in them.

The demo database has 16 portfolio stocks across 13 sectors and 21 cached Pull reports, 5 of them for companies outside the portfolio (DIXON, INDHOTEL, PERSISTENT, POLYCAB, WIPRO). All 269 stored announcements have been triaged. The research isn't tied to the demo set; any NSE symbol that Screener covers works.

## Key decisions and trade-offs

### Model routing

| Tier | Model | Job | Fallbacks |
| --- | --- | --- | --- |
| 1 | `gemini-3.1-flash-lite` | Triage filings, 15 per call | `gemini-3.5-flash-lite`, `gemini-2.5-flash-lite`, `gemini-2.5-flash` |
| 2 | `gemini-2.5-flash` | Copy results line items, write takeaways, extract management changes and bios, Pull map step, Copilot | `gemini-3.8-flash`, `gemini-3-flash-preview` |
| 3 | `gemini-3.1-pro-preview` | Pull reduce step (the 8-module report) | `gemini-4-argon`, `gemini-2.5-pro`, `gemini-2.5-flash` |

Source: `backend/llm_router.py:36-62`. I started on the 2.5 family. A live probe showed `gemini-2.5-flash-lite` and `gemini-2.5-pro` return 404 for my Vertex project, so triage had been silently running on Flash and some reports were written by Flash. The router now logs every fallback, and the ledger's `model` column shows which model really answered. Before that fix, 12 Pull reports and 15 triage batches were served by `gemini-2.5-flash`. Since the fix, every call used the intended model.

The cheap model does the bulk work. Pro only runs once per Pull report, on the step that has to reason across tables and four quarters of commentary.

### What it actually costs

The numbers below are the app's estimate from its own price table (`MODEL_PRICING_PER_1M`, `llm_router.py:45`). They are not a Google bill. For averages I use the 2026-10-31 warmup run (ledger ids 202-352). It ran the current code and price table, and it is what produced every card and report in the demo database. Earlier rows mix older prompts, re-runs while I was fixing bugs, model tests and an older price table, so I list them separately instead of averaging them in.

| What | n | Avg tokens in | Avg tokens out | Avg cost |
| --- | --- | --- | --- | --- |
| Pull report, map + reduce (warmup) | 21 reports | map 22,394, reduce 17,775 | map 5,449, reduce 3,267 | $0.0951 per report (min $0.0783, max $0.1182) |
| Pull report, earlier runs with Pro reduce | 9 | map 15,437, reduce 16,745 | map 5,009, reduce 3,100 | $0.0736 (older, lower Pro price) |
| Pull report, earlier runs where reduce fell back to Flash | 12 | map 13,124, reduce 15,672 | map 3,466, reduce 3,987 | $0.0273 |
| Copilot question (all ledger rows) | 13 | 13,726 | 222 | $0.0047 |
| Push triage batch, current Tier 1 model | 12 | 3,308 | 910 | $0.0022 |
| Results card (PDF line items + takeaway) | 33 cards, 60 calls | 9,325 (PDF call), 252 (takeaway) | 360, 97 | $0.0039 per card |
| Management card (extract + bio) | 22 cards, 44 calls | 1,757 (extract), 855 (bio) | 364, 140 | $0.0020 per card |

Output tokens include the model's thinking tokens, because they are billed as output (`llm_router.py:162-163`).

How I separated the runs:
- A Pull report is one `pull_deep_synthesis` row paired with the latest earlier `pull_concall_map` row for the same ticker. All 42 reduce rows pair up.
- The 21 warmup pairs match `pull_reports_cache` one for one, same ticker, timestamp and cost.
- Card costs divide the warmup run's enrichment spend by the number of cards it enriched: 55 of 55 cards were enriched with 0 errors, and the database has 33 results cards and 22 management cards.
- The 30 `probe` rows were model-availability tests ($0.01392). They are excluded from every average.
- I can't tell my own test questions from demo questions in the Copilot rows, so that average includes both.

**A full Push refresh.** The ledger has no refresh id, so I can't add up one refresh in general. The one I can pin down is the warmup refresh: it checked 122 filings for 16 stocks and found nothing new to triage. Enrichment was switched off for that refresh, so it made 0 LLM calls and cost $0. Scraping is free. A refresh costs LLM money only for new filings (about $0.0022 per batch of up to 15) and new cards (Refresh Live enriches up to 4, about $0.002-$0.004 each).

**Total spend.** The ledger total is $3.475335 over 352 calls, which leaves $1.524665 of the $5 budget. The warmup run was $2.173714 of that. Rows logged before the pricing fix (ids 1-122, $0.986407) used a lower Pro price and didn't bill thinking tokens, so the ledger undercounts those rows. The app also has a daily cap: paid endpoints return 429 once a UTC day's spend reaches `FINN_DAILY_BUDGET_USD`, $5.00 by default (`main.py:151-162`).

### Primary sources, but only the pages that matter

Annual Reports are long. The 21 cached reports used ARs of 187 to 756 pages, 438 on average. Instead of sending them whole, `pdf_utils.py` keeps the first 3 pages and fills the rest with the pages that contain the most distinct keywords. The AR keywords target MD&A, segment results, contingent liabilities, related parties and the auditor's report. The AR caps are 13 pages, 3,000 characters per page and 40,000 characters in total (`pull_engine.py:64-66`). Concall transcripts are capped at 10 pages and cut to 12,000 characters.

I re-ran the selection offline on the cached PDFs:

| | n | Avg pages | Avg pages sent | Share of characters sent |
| --- | --- | --- | --- | --- |
| Annual Report | 21 | 438 | 12.7 | 2.4% |
| Concall / presentation | 79 | 27.5 | 6.5 | 21.9% |

These are character counts, not tokens. Each page keeps a `[Page N of M]` marker so the map step can cite it. The trade-off: it is one keyword pass over the whole AR, so a section that doesn't use the expected phrases can be missed.

I skipped a vector database. For one company at a time, the Screener tables plus a few selected pages fit in one prompt, and table rows stay attached to their headers.

### Keeping the numbers honest

- **Results cards:** the model only copies raw line items from the filing. Python converts the unit to ₹ Cr and computes EBITDA (or Financing Profit for lenders), PBT, PAT and every QoQ/YoY %. The definitions follow Screener's: EBITDA excludes other income and exceptional items, and Financing Profit is after provisions, so it is not PPOP (`llm_router.py:414-425`, `:460`). When Screener's latest column is the same quarter and basis, every line must match within 1% or ₹1 Cr (`llm_router.py:435-436`).
- **Takeaways are checked against the numbers.** A takeaway can't say "strong", "robust", "solid" and similar words if PAT or EBITDA fell QoQ or YoY. It must mention exceptional items of ₹0.5 Cr or more with the amount, and it may only quote percentages Python computed. Otherwise a template sentence built from the numbers is used (`llm_router.py:581-612`).
- **Guidance vs delivery:** for each of the last 4 quarters, Python looks for a concall or presentation dated in that quarter's three months, which means it was held before the quarter ended. Quarters without one get "No guidance", written into the report by Python after the model answers (`pull_engine.py:192`, `:621-623`). Since 2023, Screener lists one HAL call a year, in May. So Sep 2025, Dec 2025 and Mar 2026 say "No guidance", and Jun 2026 is checked against the May 2026 call.
- **Units in Annual Reports:** amounts in lakhs, millions or thousands get their ₹ Cr value appended by Python before the reduce step. After it, any "₹X Cr" that is really a raw lakh figure is corrected (`pull_engine.py:289-349`).
- **Copilot** only sees the notes of documents that were actually read. It is told to say when the answer isn't in the loaded sources, and Python drops any citation to a document it wasn't given (`pull_engine.py:1047-1083`).
- **Management bios** are facts only. A bio with praise words is replaced by a plain record built from the filing (`llm_router.py:868`, `:995`).

The gap: inside the Pull report, the 4-quarter QoQ/YoY figures, the 3-year CFO/PAT conversion and the debt-to-equity ratio are still written by the reduce model from the Screener tables. Nothing recomputes them afterwards.

### SQLite and a committed demo database

One SQLite file in WAL mode, committed with the repo, so the app works with no API key and no setup beyond `pip install`. Cached reports and cards are free to read. The cost is a single-writer database and a single-process deployment (see below).

### UI

Next.js 14 and Tailwind, constrained to a phone-width column with bottom navigation. Charts are plain SVG/Tailwind, so there is no chart library to install. Text inputs use `text-base` (16 px) so iOS doesn't zoom, pinch-zoom is allowed, and buttons use `min-h-11` (44 px), for example `PullTab.tsx:1002` and `PullTab.tsx:1008`.

## What I left out, and why

- **Web Push to a closed browser.** It needs a Service Worker, VAPID keys and a push server. In 72 hours I chose the optional `FINN_REFRESH_MINUTES` background refresh on the server, plus in-app toasts and browser notifications while the page is open. Android Chrome doesn't allow page notifications without a service worker, so there you get the toast only.
- **More than one process.** The background loop starts once per uvicorn worker and the rate limiter lives in memory (`ponytail:` notes at `main.py:213-214` and `main.py:111-113`). The per-ticker Pull lock is in-process too, and SQLite allows only one writer at a time. So it runs as one worker and one instance.
- **BSE.** `api.bseindia.com` still returns 403 after the cookie warm-up. The app runs on NSE, and the header shows "BSE blocked" when BSE is tried. All 269 stored filings came from NSE.
- **Screener lag.** Screener updates its tables some time after a results filing; I didn't measure how long. Until then the results card uses the filing's own numbers without the Screener cross-check, and the Pull staleness banner explains that a re-run won't help until Screener catches up.
- **Lender detection is name-based.** It misses lenders without "bank", "finance" and similar words in their name (`filings.py:99`).
- **Praise words in Pull prose.** The facts-only rule covers management cards only. 20 of the 21 cached reports use "robust" or "stellar" in their own text.
- **Unit-check false positives.** The lakh check can't tell a company total from a per-unit amount. It flags TITAN's "₹1.9 lakhs" average ticket, which is correct as written.
- **No broker notes, news or social data** on the Pull side, as the brief requires. Web search is used only for management bios on the Push side, which the brief allows.
- **No login, no brokerage link, no intraday data.** Everything is end of day.
- **No OCR.** A scanned results PDF with no text layer falls back to Screener's table, but only when Screener already has that quarter.

## Where I'd take it next

1. Compute the Pull report's QoQ/YoY, CFO/PAT and debt-to-equity in Python, the way the results card already does, and let the model only explain them.
2. Web Push with a Service Worker, so alerts reach a phone with the browser closed.
3. A scheduler outside the web process (cron calling `/api/push/refresh`), so the app can run more than one worker, and Postgres if writes grow.
4. Per-section Annual Report page selection, and a facts-only check on Pull prose like the one on management cards.
5. Peer comparison in Pull, and a year-on-year diff of contingent liabilities, related-party transactions and auditor remarks.

## AI build history

The brief asks for the coding-agent and LLM conversations used to build this. I'm sending them with the submission email, together with the repository and prototype links.
