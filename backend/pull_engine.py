from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import re
import threading
from pathlib import Path
from typing import Any

from backend.db import get_conn, normalize_ticker, DB_PATH
from backend.pdf_utils import extract_pdf_text
from backend.scrapers.screener import fetch_screener_fundamentals
from backend.scrapers.filings import fetch_company_filings
from backend.llm_router import (
    _call_gemini_json_with_model,
    call_gemini_pro_json,
    MODEL_FLASH,
)

_LOCKS_GUARD = threading.Lock()
_TICKER_LOCKS: dict[str, threading.Lock] = {}

CONCALL_KEYWORDS = [
    "guidance",
    "outlook",
    "margin",
    "ebitda",
    "revenue",
    "growth",
    "capex",
    "order book",
    "demand",
    "quarter",
    "working capital",
    "cash flow",
    "store",
    "capacity",
    "target",
]

# Annual Report sections Pull must read. A page scores one point per distinct phrase it contains,
# so each section gets a few specific phrases and no single section hogs every page slot.
AR_KEYWORDS = [
    # MD&A
    "management discussion",
    "industry structure",
    "opportunities and threats",
    # Segment performance
    "segment revenue",
    "segment results",
    "operating segment",
    # Contingent liabilities
    "contingent liabilit",
    "not acknowledged as debt",
    # Related party transactions
    "related party",
    "key managerial personnel",
    # Auditor's report
    "independent auditor",
    "key audit matter",
    "basis for opinion",
]
# ponytail: one keyword-scored pass over the whole AR, not per-section selection. A section whose pages
# never use these phrases can be missed; upgrade path is one extract_pdf_text pass per section.
AR_MAX_PAGES = 13  # extract_pdf_text always keeps 3 cover pages, leaving 10 keyword pages
AR_MAX_CHARS_PER_PAGE = 3000
AR_MAX_CHARS = 40000  # hard ceiling, ~10k input tokens to the Flash map step
CONCALL_EXCERPT_CHARS = 12000


def _get_ticker_lock(ticker: str) -> threading.Lock:
    with _LOCKS_GUARD:
        if ticker not in _TICKER_LOCKS:
            _TICKER_LOCKS[ticker] = threading.Lock()
        return _TICKER_LOCKS[ticker]


def _extract_primary_documents(screener_data: dict[str, Any], max_concalls: int = 4) -> list[dict[str, str]]:
    """
    Download and extract high-signal pages in parallel from the last up to 4 Concall Transcripts / Investor PPTs
    plus the latest Annual Report (MD&A, segments, contingent liabilities, related parties, auditor's report).
    """
    concalls = (screener_data.get("concalls") or [])[:max_concalls]
    annual_reports = screener_data.get("annual_reports") or []

    def _fetch_one(cc: dict[str, str]) -> dict[str, str] | None:
        period = cc.get("period", "Recent Quarter")
        url = cc.get("transcript_url") or cc.get("ppt_url") or ""
        doc_type = "Concall Transcript" if cc.get("transcript_url") else "Investor Presentation"
        if not url:
            return None
        text = extract_pdf_text(url, max_pages=10, keywords=CONCALL_KEYWORDS, max_chars=16000)
        if len(text.strip()) > 300:
            return {"period": period, "doc_type": doc_type, "url": url, "text": text}
        return None

    def _fetch_annual_report(ar: dict[str, str]) -> dict[str, str] | None:
        ar_url = ar.get("url", "")
        if not ar_url:
            return None
        ar_text = extract_pdf_text(
            ar_url,
            max_pages=AR_MAX_PAGES,
            keywords=AR_KEYWORDS,
            max_chars=AR_MAX_CHARS,
            max_chars_per_page=AR_MAX_CHARS_PER_PAGE,
        )
        if len(ar_text.strip()) > 400:
            return {
                "period": ar.get("title", "Latest Annual Report"),
                "doc_type": "Annual Report",
                "url": ar_url,
                "text": ar_text,
            }
        return None

    # Always read the latest Annual Report alongside the concalls (Screener lists newest first)
    with ThreadPoolExecutor(max_workers=5) as pool:
        ar_future = pool.submit(_fetch_annual_report, annual_reports[0]) if annual_reports else None
        docs = [item for item in pool.map(_fetch_one, concalls) if item]
        ar_doc = ar_future.result() if ar_future else None
    if ar_doc:
        docs.append(ar_doc)

    return docs


def _get_material_filings(clean_ticker: str, db_path: Path = DB_PATH) -> list[dict[str, str]]:
    """
    Fetch recent material filings for `clean_ticker`, checking SQLite `announcements` first
    and refreshing from NSE/BSE if none are stored yet.
    """
    bse_code = ""
    with get_conn(db_path) as conn:
        p_row = conn.execute(
            "SELECT bse_code FROM portfolio WHERE ticker = ?", (clean_ticker,)
        ).fetchone()
        if p_row and p_row["bse_code"]:
            bse_code = str(p_row["bse_code"]).strip()

        existing = conn.execute(
            """
            SELECT published_at, category, title, pdf_url
            FROM announcements
            WHERE ticker = ? AND materiality = 1
            ORDER BY published_at DESC
            LIMIT 8
            """,
            (clean_ticker,),
        ).fetchall()

    if existing:
        return [
            {
                "date": r["published_at"],
                "category": r["category"],
                "title": r["title"],
                "pdf_url": r["pdf_url"] or "",
            }
            for r in existing
        ]

    recent_filings = fetch_company_filings(
        clean_ticker, bse_code=bse_code, days=30, db_path=db_path, save_to_db=True
    )
    return [
        {
            "date": f["published_at"],
            "category": f["category"],
            "title": f["title"],
            "pdf_url": f.get("pdf_url") or "",
        }
        for f in recent_filings
        if f.get("materiality") == 1
    ][:8]


def _get_ledger_snapshot(ticker: str, db_path: Path = DB_PATH) -> tuple[int, int, float]:
    with get_conn(db_path) as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(tokens_in), 0) as tin,
                   COALESCE(SUM(tokens_out), 0) as tout,
                   COALESCE(SUM(cost_usd), 0.0) as cost
            FROM llm_cost_ledger
            WHERE ticker = ?
            """,
            (ticker,),
        ).fetchone()
    return int(row["tin"]), int(row["tout"]), float(row["cost"])


def _quarters_without_prior_call(quarter_headers: list[str], concalls: list[dict[str, str]]) -> list[str]:
    """
    Quarter headers (e.g. 'Sep 2025') with no concall/PPT dated in that quarter's 3 months. A call in those months
    discusses the previous quarter's results, so it is the latest guidance management gave before this quarter's.
    """
    def _month_index(label: str) -> int | None:
        try:
            d = datetime.strptime(str(label).strip()[:8], "%b %Y")
        except ValueError:
            return None
        return d.year * 12 + d.month

    call_months = {m for m in (_month_index(c.get("period", "")) for c in concalls) if m is not None}
    out = []
    for q in quarter_headers:
        end = _month_index(q)
        if end is not None and not any(end - 2 <= m <= end for m in call_months):
            out.append(q)
    return out


STALE_AFTER_DAYS = 7
IST_OFFSET = timedelta(hours=5, minutes=30)  # NSE filing timestamps are IST; cache created_at is SQLite UTC
_QUARTER_END_MONTHS = {"mar": 3, "jun": 6, "sep": 9, "dec": 12}


def _filing_quarter(text: str) -> int | None:
    """Quarter-end month index (year*12 + month) named in a results filing: 'Q2 FY27', 'Q2FY2027', 'quarter ended September 30, 2026'."""
    low = (text or "").lower()
    m = re.search(r"\bq([1-4])\s*fy\s*'?(\d{4}|\d{2})\b", low)
    if m:
        q, fy = int(m.group(1)), int(m.group(2)) % 100 + 2000
        return fy * 12 + 3 if q == 4 else (fy - 1) * 12 + 3 + 3 * q  # Q1=Jun, Q2=Sep, Q3=Dec of fy-1; Q4=Mar of fy
    m = re.search(r"ended\s+(?:on\s+)?(?:\d{1,2}(?:st|nd|rd|th)?\s+)?(mar|jun|sep|dec)[a-z]*\.?\s+(?:\d{1,2}(?:st|nd|rd|th)?,?\s+)?(\d{4})", low)
    return int(m.group(2)) * 12 + _QUARTER_END_MONTHS[m.group(1)] if m else None


def _fiscal_label(idx: int) -> str:
    """Quarter-end month index -> Indian fiscal label: Jun 2026 -> 'Q1 FY27', Mar 2027 -> 'Q4 FY27'."""
    year, month = divmod(idx - 1, 12)
    month += 1
    fiscal_q = {6: 1, 9: 2, 12: 3, 3: 4}[month]
    return f"Q{fiscal_q} FY{(year + (month >= 4)) % 100:02d}"


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).strip().replace("T", " ")[:19])
    except ValueError:
        return None


def _report_staleness(
    ticker: str, quarter_key: str, generated_at: str | None, db_path: Path = DB_PATH, now: datetime | None = None
) -> dict[str, Any]:
    """stale / stale_reason / screener_lag for a cached report. Read-only, $0: SQLite + regex, no network or LLM."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    generated = _parse_ts(generated_at) if generated_at else now
    try:
        d = datetime.strptime(str(quarter_key).strip()[:8], "%b %Y")
        report_q = d.year * 12 + d.month
    except ValueError:
        report_q = None  # "Latest": Screener had no quarter headers, nothing to compare against

    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT title, summary, published_at FROM announcements WHERE ticker = ? AND category = 'results'",
            (ticker,),
        ).fetchall()

    newer_quarters, filed_since, latest_q = [], False, None
    for r in rows:
        q = _filing_quarter(f"{r['title']} {r['summary'] or ''}")
        published = _parse_ts(r["published_at"])
        if generated and published and published - IST_OFFSET > generated:
            filed_since = True
            if q:
                newer_quarters.append(q)
        if q and (latest_q is None or q > latest_q):
            latest_q = q

    out = {"stale": False, "stale_reason": "", "screener_lag": False}
    if filed_since:
        label = f"{_fiscal_label(max(newer_quarters))} " if newer_quarters else ""
        out.update(stale=True, stale_reason=f"New {label}results filed since this report was generated")
    elif latest_q and report_q and latest_q > report_q:
        out.update(
            stale=True,
            screener_lag=True,
            stale_reason=f"{_fiscal_label(latest_q)} results are filed, but Screener's tables still end at {quarter_key}",
        )
    elif generated and now - generated > timedelta(days=STALE_AFTER_DAYS):
        out.update(stale=True, stale_reason=f"Report is {(now - generated).days} days old (refreshed every {STALE_AFTER_DAYS} days)")
    return out


def get_or_generate_pull_report(
    ticker: str,
    force_refresh: bool = False,
    db_path: Path = DB_PATH,
) -> dict[str, Any]:
    """
    On-Demand Deep Research (Pull Mode) for ANY NSE/BSE ticker:
    1. Checks SQLite `pull_reports_cache` first (<15ms, $0.00).
    2. Scrapes Screener 10Y financials, quarterly tables, concall transcripts, and annual reports.
    3. Map step (gemini-2.5-flash): Distills management promises, guidance, and commentary across primary PDFs.
    4. Reduce step (gemini-2.5-pro / fallback): Synthesizes the 8 research modules including the
       4-quarter 'Numbers vs. Narrative' comparison grounded strictly in primary data.
    """
    clean_ticker = normalize_ticker(ticker)

    lock = _get_ticker_lock(clean_ticker)
    with lock:
        if not force_refresh:
            with get_conn(db_path) as conn:
                cached = conn.execute(
                    "SELECT * FROM pull_reports_cache WHERE ticker = ?",
                    (clean_ticker,),
                ).fetchone()
            if cached:
                report = json.loads(cached["report_json"])
                report.setdefault("document_notes", [])  # reports cached before notes were persisted
                return {
                    "ticker": clean_ticker,
                    "quarter_key": cached["quarter_key"],
                    "cached": True,
                    "tokens_in": cached["tokens_in"],
                    "tokens_out": cached["tokens_out"],
                    "cost_usd": cached["cost_usd"],
                    "created_at": cached["created_at"],
                    **_report_staleness(clean_ticker, cached["quarter_key"], cached["created_at"], db_path),
                    "fundamentals": json.loads(cached["fundamentals_json"]),
                    "report": report,
                }

        tin_before, tout_before, cost_before = _get_ledger_snapshot(clean_ticker, db_path=db_path)

        # 1. Scrape primary fundamentals & document links from Screener
        fundamentals = fetch_screener_fundamentals(clean_ticker)
        q_headers = fundamentals.get("quarters", {}).get("headers") or []
        quarter_key = q_headers[-1] if q_headers else "Latest"

        # 2 & 3. Fetch recent NSE/BSE filings and extract Concall/AR PDFs concurrently
        with ThreadPoolExecutor(max_workers=2) as io_pool:
            filings_future = io_pool.submit(_get_material_filings, clean_ticker, db_path)
            docs_future = io_pool.submit(_extract_primary_documents, fundamentals, 4)
            material_filings = filings_future.result()
            primary_docs = docs_future.result()

        # 4. Map Step (Tier 2: gemini-2.5-flash): Distill management promises & commentary per document
        distilled_notes: list[dict[str, Any]] = []
        map_model: str | None = None  # stays None if there were no PDFs or the map step failed
        docs_by_id = {f"D{i}": d for i, d in enumerate(primary_docs, 1)}
        if primary_docs:
            docs_payload = [
                {
                    "doc_id": doc_id,
                    "period": d["period"],
                    "doc_type": d["doc_type"],
                    "url": d["url"],
                    "excerpt": d["text"][: AR_MAX_CHARS if d["doc_type"] == "Annual Report" else CONCALL_EXCERPT_CHARS],
                }
                for doc_id, d in docs_by_id.items()
            ]
            map_prompt = f"""You are a primary-source financial analyst extracting management commentary for {fundamentals['company_name']} ({clean_ticker}).
Analyze the following primary documents (Concall Transcripts, Investor Presentations, Annual Report) and extract ONLY explicit statements, guidance, growth/margin commentary, capex plans, and segment drivers stated by management, plus the audited disclosures listed below for the Annual Report.
Do NOT use outside knowledge or broker opinions.

Return a JSON array of objects, one per document, with keys:
- "doc_id": string (copy the document's doc_id exactly)
- "period": string (matching document period)
- "doc_type": string
- "stated_guidance_and_promises": list of specific forward-looking claims, growth targets, margin expectations, or execution milestones management stated
- "segment_and_demand_commentary": list of key operational highlights or headwinds discussed
- "notable_quotes_or_facts": list of 1-2 verbatim or near-verbatim factual statements with page reference if visible
For the doc_type "Annual Report" document ONLY, also include these keys (each a list of short factual strings with ₹ amounts and page reference if visible; use [] if the excerpt does not contain it, never guess):
- "mdna_highlights": business model, industry position, strategy and outlook from Management Discussion & Analysis
- "segment_performance": each reported segment with its revenue / result numbers
- "contingent_liabilities": nature and amount of contingent liabilities and claims not acknowledged as debt
- "related_party_transactions": material related party transactions with counterparty and amount
- "auditor_report": audit opinion type (unmodified / qualified), key audit matters, emphasis of matter or qualifications

Primary Documents:
{json.dumps(docs_payload, ensure_ascii=False)}
"""
            try:
                mapped, map_model = _call_gemini_json_with_model(
                    prompt=map_prompt,
                    model=MODEL_FLASH,
                    workflow="pull_concall_map",
                    ticker=clean_ticker,
                    db_path=db_path,
                )
                distilled_notes = mapped if isinstance(mapped, list) else mapped.get("documents", [])
            except Exception as exc:
                print(f"[pull_engine] Map step warning for {clean_ticker}: {exc}")

        # Tie each distilled note to the real document it came from (ID, period, URL) so it can be cached and cited.
        # Falls back to period + doc_type if the model dropped doc_id; notes matching no parsed document are dropped.
        document_notes: list[dict[str, Any]] = []
        for note in distilled_notes:
            if not isinstance(note, dict):
                continue
            doc_id = str(note.get("doc_id") or "")
            if doc_id not in docs_by_id:
                doc_id = next(
                    (k for k, d in docs_by_id.items() if (d["period"], d["doc_type"]) == (note.get("period"), note.get("doc_type"))),
                    "",
                )
            if doc_id:
                d = docs_by_id[doc_id]
                document_notes.append({**note, "doc_id": doc_id, "period": d["period"], "doc_type": d["doc_type"], "url": d["url"]})

        # 5. Reduce Step (Tier 3: gemini-2.5-pro / fallback): Synthesize the 8 Pull Research Modules
        primary_bundle = {
            "ticker": clean_ticker,
            "company_name": fundamentals["company_name"],
            "about": fundamentals["about"],
            "top_ratios": fundamentals["top_ratios"],
            "quarterly_results_table": fundamentals["quarters"],
            "annual_profit_and_loss": fundamentals["profit_loss"],
            "balance_sheet": fundamentals["balance_sheet"],
            "cash_flow": fundamentals["cash_flow"],
            "ratios_table": fundamentals["ratios"],
            "distilled_concall_and_ar_evidence": distilled_notes,
            "recent_material_exchange_filings": material_filings,
            "primary_document_links": {
                "screener_url": fundamentals["screener_url"],
                "concalls": fundamentals["concalls"][:4],
                "annual_reports": fundamentals["annual_reports"][:3],
            },
        }

        no_call_quarters = _quarters_without_prior_call(q_headers[-4:], fundamentals.get("concalls") or [])

        reduce_prompt = f"""You are FINN, a high-conviction institutional investment analyst for ENAM Holdings.
Generate a complete, deeply analytical Pull Research Report for {fundamentals['company_name']} ({clean_ticker}).

STRICT DISCIPLINE:
- Ground EVERY number, ratio, trajectory claim, and narrative comparison strictly in the provided Primary Source Bundle (Screener 10Y audited financials, quarterly exchange results, concall transcripts, investor presentations, and annual reports).
- NO broker notes, NO news commentary, NO ungrounded outside assumptions.
- Zero AI slop: write with high information density, sharp institutional judgment, and exact numbers (with ₹ Cr and % units).
- ANNUAL REPORT EVIDENCE: the `distilled_concall_and_ar_evidence` entry with doc_type "Annual Report" holds audited disclosures. Use its `mdna_highlights` and `segment_performance` for `business_snapshot.overview` and `business_snapshot.segments` (prefer reported segment numbers over inference). In `balance_sheet.summary`, state the contingent liabilities, material related party transactions and the auditor's opinion / key audit matters from it; if no Annual Report entry or field is present, say they were not found in the loaded Annual Report pages instead of guessing.
- BANKING & NBFC DOMAIN RULE (e.g., HDFCBANK, BAJFINANCE, banks/lenders where Screener reports 'Financing Profit', 'Financing Margin %', or 'Interest' instead of standard manufacturing/services EBITDA):
  1) Map 'Financing Margin %' (or NIM / PPOP margin) into `latest_opm_pct` and `opm_pct` chart fields, and label it clearly in prose.
  2) Do NOT flag high Borrowings / Deposits or high Debt-to-Equity as balance-sheet distress for a Bank or NBFC—deposits and borrowings are their core lending raw material. Instead evaluate Balance Sheet health on Capital Adequacy (CRAR/Tier-1), Asset Quality (GNPA/NNPA, credit costs), Reserves compounding, and ROE/ROA.
  3) For Bank/NBFC Cash Quality, recognize that negative CFO during high loan-book growth reflects lending disbursements (working capital of a lender), not poor earnings quality; judge quality via ROE/ROA consistency, provisioning discipline, and PAT compounding.

Return a single valid JSON object with the exact 8 modules below:
{{
  "business_snapshot": {{
    "overview": "2-3 crisp sentences on what the company does, core business model, and market position based on primary disclosures.",
    "segments": [
      {{"name": "Segment / Vertical Name", "contribution": "Revenue share or scale from filings", "driver": "Key growth driver or trend"}}
    ],
    "key_operating_levers": ["Lever 1", "Lever 2", "Lever 3"]
  }},
  "financial_snapshot": {{
    "latest_quarter_label": "{quarter_key}",
    "kpis": {{
      "market_cap": "string (from top_ratios)",
      "current_price": "string",
      "pe_ratio": "string",
      "roce_pct": "string",
      "roe_pct": "string",
      "ttm_or_latest_revenue": "string (₹ Cr)",
      "ttm_or_latest_pat": "string (₹ Cr)",
      "latest_opm_pct": "string (%)"
    }},
    "headline_summary": "1-2 sentence institutional read on current valuation vs. return profile (ROCE/ROE)."
  }},
  "trajectory": {{
    "summary": "2-3 sentences analyzing multi-year direction: what is growing/accelerating vs. what is compressing.",
    "whats_growing": ["Specific metric/line item 1 with multi-year numbers", "Specific metric 2"],
    "whats_compressing": ["Specific metric/margin/cost line with numbers (or 'None material — margins expanding' with numbers)"],
    "annual_series": [
      {{"period": "Mar 2022", "revenue": 0.0, "pat": 0.0, "opm_pct": 0.0}}
    ],
    "quarterly_series": [
      {{"period": "Sep 2025", "revenue": 0.0, "pat": 0.0, "opm_pct": 0.0}}
    ]
  }},
  "balance_sheet": {{
    "status_badge": "Fortress | Healthy | Moderate Leverage | Stretched",
    "summary": "2-3 sentences on borrowings/debt trajectory, reserves growth, fixed assets/CWIP expansion, and working capital cycle.",
    "metrics": {{
      "latest_borrowings": "string (₹ Cr)",
      "latest_reserves": "string (₹ Cr)",
      "debt_to_equity": "string (computed Borrowings / (Equity + Reserves))",
      "working_capital_days": "string (from ratios table)",
      "cash_conversion_cycle_days": "string (from ratios table)"
    }}
  }},
  "cash_quality": {{
    "verdict_badge": "High Cash Conversion | Solid Conversion | Working Capital Heavy | Red Flag",
    "summary": "2-3 sentences comparing Cash from Operating Activity (CFO) against Net Profit (PAT) over the last 3-5 years.",
    "cfo_to_pat_conversion_pct": "string (e.g. '98% (3Y cumulative CFO / PAT)')",
    "latest_annual_cfo": "string (₹ Cr)",
    "latest_annual_pat": "string (₹ Cr)",
    "annual_cash_vs_pat": [
      {{"period": "Mar 2024", "cfo": 0.0, "pat": 0.0}}
    ]
  }},
  "narrative_vs_numbers": {{
    "credibility_badge": "High Alignment | Mostly Aligned | Mixed Execution | Divergence Alert",
    "synthesis": "2-3 sentences comparing management's stated guidance/commentary across recent quarters against actual reported Revenue, OPM %, and PAT in the quarterly table.",
    "quarters_audit": [
      {{
        "quarter": "e.g. Jun 2026 (or latest 4 quarters in chronological or reverse-chronological order)",
        "management_narrative": "What management guided, emphasized, or targeted for this period (citing Concall/PPT/AR or operational focus).",
        "actual_numbers": "Exact reported Revenue (₹ Cr, YoY/QoQ %), Operating Margin (OPM %), and Net Profit (₹ Cr, YoY/QoQ %).",
        "verdict": "Beat | Delivered | Mixed | Missed",
        "audit_note": "1-sentence analytical judgment on narrative vs. actual delivery."
      }}
    ]
  }},
  "guidance": {{
    "management_tone": "Bullish | Confident | Measured | Cautious",
    "summary": "1-2 sentences on management's forward posture on growth, margins, and capital allocation.",
    "stated_targets": [
      {{
        "metric": "Revenue / Margin / Capex / Expansion",
        "target": "Explicit forward guidance or commentary from primary docs",
        "source": "e.g. Concall / Investor PPT / Annual Report"
      }}
    ]
  }},
  "bull_vs_bear": {{
    "bull_case": [
      "Tight, number-backed bull argument 1",
      "Tight, number-backed bull argument 2",
      "Tight, number-backed bull argument 3"
    ],
    "bear_case": [
      "Tight, number-backed bear/risk argument 1",
      "Tight, number-backed bear/risk argument 2",
      "Tight, number-backed bear/risk argument 3"
    ]
  }}
}}

Note on `annual_series` (last 5 years), `quarterly_series` (last 6 quarters), and `annual_cash_vs_pat` (last 4 years): populate numeric floats (strip commas) directly from the Screener tables so charts render accurately.
Note on `quarters_audit`: MUST include 4 entries corresponding to the last 4 reported quarters in `quarterly_results_table`.
Quarters with no prior concall/presentation: {json.dumps(no_call_quarters)}. For each of these, set `management_narrative` and `verdict` to exactly "No guidance" (do not carry older guidance forward) and use `audit_note` for the reported numbers only.

Primary Source Bundle:
{json.dumps(primary_bundle, ensure_ascii=False)}
"""
        report, reduce_model = call_gemini_pro_json(
            prompt=reduce_prompt,
            workflow="pull_deep_synthesis",
            ticker=clean_ticker,
            db_path=db_path,
        )
        report["models_used"] = {"map": map_model, "reduce": reduce_model}
        report["document_notes"] = document_notes
        # Enforce the prompt rule: a quarter with no prior call has no guidance to audit against.
        for row in (report.get("narrative_vs_numbers") or {}).get("quarters_audit") or []:
            if isinstance(row, dict) and any(q in str(row.get("quarter", "")) for q in no_call_quarters):
                row["management_narrative"] = row["verdict"] = "No guidance"

        # Attach source provenance metadata to report
        report["sources_used"] = {
            "screener_url": fundamentals["screener_url"],
            "documents_parsed": [
                {"period": d["period"], "doc_type": d["doc_type"], "url": d["url"]} for d in primary_docs
            ],
            "concalls_available": fundamentals["concalls"][:4],
            "annual_reports_available": fundamentals["annual_reports"][:3],
            "material_filings_count": len(material_filings),
            "material_filings": material_filings,
        }

        tin_after, tout_after, cost_after = _get_ledger_snapshot(clean_ticker, db_path=db_path)
        run_tin = max(0, tin_after - tin_before)
        run_tout = max(0, tout_after - tout_before)
        run_cost = round(max(0.0, cost_after - cost_before), 6)

        # Persist to SQLite cache
        with get_conn(db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO pull_reports_cache
                (ticker, quarter_key, fundamentals_json, report_json, tokens_in, tokens_out, cost_usd, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                (
                    clean_ticker,
                    quarter_key,
                    json.dumps(fundamentals, ensure_ascii=False),
                    json.dumps(report, ensure_ascii=False),
                    run_tin,
                    run_tout,
                    run_cost,
                ),
            )

        return {
            "ticker": clean_ticker,
            "quarter_key": quarter_key,
            "cached": False,
            "tokens_in": run_tin,
            "tokens_out": run_tout,
            "cost_usd": run_cost,
            **_report_staleness(clean_ticker, quarter_key, None, db_path),  # Screener lag shows up right after a re-run
            "fundamentals": fundamentals,
            "report": report,
        }


_MONTH_TOKENS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def _quarter_to_concall_months(low: str) -> tuple[list[str], str | None]:
    """
    Map Indian fiscal quarter references (e.g. 'Q1 FY27', 'Q4 FY26') to the calendar months
    and year in which that quarter's concall/presentation is published on Screener.
    """
    m = re.search(r"\bq([1-4])\s*(?:fy)?\s*'?(\d{2,4})\b", low)
    if not m:
        return [], None
    q_num = int(m.group(1))
    yr_raw = m.group(2)
    fy_year = int(yr_raw) + 2000 if len(yr_raw) == 2 else int(yr_raw)
    if q_num == 1:
        return ["jul", "aug", "jun"], str(fy_year - 1)
    if q_num == 2:
        return ["oct", "nov", "sep"], str(fy_year - 1)
    if q_num == 3:
        return ["jan", "feb", "dec"], str(fy_year)
    return ["apr", "may", "mar"], str(fy_year)


def resolve_copilot_citations(
    citations: list[Any],
    fundamentals: dict[str, Any],
    report: dict[str, Any],
    db_path: Path = DB_PATH,
) -> list[dict[str, Any]]:
    """
    Resolves Copilot citation tags against real company sources (Screener tables, Concall/PPT PDFs,
    Annual Reports, Exchange Filings, and internal Synthesized Report sections).
    Never invents URLs; marks unverified or missing sources as `available=False`.
    """
    sources_used = report.get("sources_used") or {}
    raw_sc = (sources_used.get("screener_url") or fundamentals.get("screener_url") or "").strip()
    base_sc = raw_sc.split("#")[0] if raw_sc.startswith(("http://", "https://")) else ""

    docs_parsed = sources_used.get("documents_parsed") or []
    concalls = fundamentals.get("concalls") or sources_used.get("concalls_available") or []
    annual_reports = fundamentals.get("annual_reports") or sources_used.get("annual_reports_available") or []
    filings = sources_used.get("material_filings") or []
    ticker = (fundamentals.get("ticker") or "").strip().upper()
    if not filings and ticker:
        try:
            with get_conn(db_path) as conn:
                rows = conn.execute(
                    "SELECT published_at as date, category, title, pdf_url FROM announcements WHERE ticker = ? AND pdf_url != '' ORDER BY published_at DESC LIMIT 8",
                    (ticker,),
                ).fetchall()
                filings = [dict(r) for r in rows]
        except Exception:
            filings = []

    resolved: list[dict[str, Any]] = []
    for item in citations or []:
        label = str(item.get("label") if isinstance(item, dict) else item or "").strip()
        if not label:
            continue
        low = label.lower()

        if any(u in low for u in ("unavailable", "not available", "n/a", "none", "missing", "unspecified")):
            resolved.append({"label": label, "kind": "unavailable", "url": None, "section_id": None, "available": False})
            continue

        # 1. Internal Synthesized Report sections
        if any(
            k in low
            for k in (
                "synthesized report",
                "pull report",
                "research report",
                "narrative vs",
                "4q audit",
                "quarters audit",
                "bull vs",
                "bull/bear",
                "business snapshot",
                "financial snapshot",
                "cash quality",
                "forward guidance",
                "trajectory",
            )
        ):
            if any(k in low for k in ("narrative", "4q", "audit", "promise")):
                sec_id = "pull-module-narrative"
            elif any(k in low for k in ("trajectory", "growing", "compressing", "series")):
                sec_id = "pull-module-trajectory"
            elif any(k in low for k in ("cash", "cfo", "balance sheet", "leverage", "borrowing")):
                sec_id = "pull-module-cash-bs"
            elif any(k in low for k in ("guidance", "target", "outlook", "tone")):
                sec_id = "pull-module-guidance"
            elif any(k in low for k in ("bull", "bear", "risk", "debate")):
                sec_id = "pull-module-bull-bear"
            elif any(k in low for k in ("snapshot", "segment", "kpi", "overview", "business")):
                sec_id = "pull-module-snapshot"
            else:
                sec_id = "pull-report-overview"
            resolved.append(
                {
                    "label": label,
                    "kind": "internal_report",
                    "url": f"#{sec_id}",
                    "section_id": sec_id,
                    "available": True,
                }
            )
            continue

        # 2. Primary Documents: Annual Reports
        if "annual report" in low or re.search(r"\bar\b", low):
            ar_candidates: list[tuple[str, str]] = []
            for ar in annual_reports:
                u = (ar.get("url") or "").strip()
                if u.startswith(("http://", "https://")):
                    ar_candidates.append(((ar.get("title") or "").lower(), u))
            for d in docs_parsed:
                if "annual" in (d.get("doc_type") or "").lower():
                    u = (d.get("url") or "").strip()
                    if u.startswith(("http://", "https://")):
                        ar_candidates.append(((d.get("period") or "").lower(), u))

            yr_match = re.search(r"\b(20\d{2})\b", low)
            fy_match = re.search(r"\bfy\s*'?(\d{2})\b", low)
            target_year = yr_match.group(1) if yr_match else (f"20{fy_match.group(1)}" if fy_match else None)

            matched_url: str | None = None
            if target_year:
                for title_low, u in ar_candidates:
                    if target_year in title_low or f"fy{target_year[-2:]}" in title_low:
                        matched_url = u
                        break
            elif ar_candidates:
                matched_url = ar_candidates[0][1]

            resolved.append(
                {
                    "label": label,
                    "kind": "document" if matched_url else "unavailable",
                    "url": matched_url,
                    "section_id": None,
                    "available": bool(matched_url),
                }
            )
            continue

        # 3. Primary Documents: Concalls / Transcripts / Investor Presentations / PPTs
        if any(
            k in low
            for k in ("concall", "transcript", "earnings call", "conference call", "presentation", "ppt", "investor deck")
        ):
            wants_ppt = any(k in low for k in ("ppt", "presentation", "deck"))
            cc_entries: list[dict[str, str]] = []
            for c in concalls:
                cc_entries.append(c)
            for d in docs_parsed:
                if "annual" not in (d.get("doc_type") or "").lower():
                    cc_entries.append(
                        {
                            "period": d.get("period", ""),
                            "transcript_url": d.get("url", "") if "transcript" in (d.get("doc_type") or "").lower() else "",
                            "ppt_url": d.get("url", "") if "presentation" in (d.get("doc_type") or "").lower() else "",
                            "url": d.get("url", ""),
                        }
                    )

            def _pick_cc_url(entry: dict[str, str]) -> str | None:
                order = (
                    ("ppt_url", "url", "transcript_url", "audio_url")
                    if wants_ppt
                    else ("transcript_url", "url", "ppt_url", "audio_url")
                )
                for k in order:
                    v = (entry.get(k) or "").strip()
                    if v.startswith(("http://", "https://")):
                        return v
                return None

            explicit_months = [m for m in _MONTH_TOKENS if re.search(rf"\b{m}[a-z]*\b", low)]
            explicit_year_m = re.search(r"\b(20\d{2})\b", low)
            explicit_year = explicit_year_m.group(1) if explicit_year_m else None
            q_months, q_year = _quarter_to_concall_months(low)

            matched_cc_url: str | None = None
            has_specific_period = bool(explicit_months or explicit_year or q_months)

            if explicit_months or explicit_year:
                for entry in cc_entries:
                    p_low = (entry.get("period") or "").lower()
                    month_ok = all(m in p_low for m in explicit_months) if explicit_months else True
                    year_ok = (explicit_year in p_low) if explicit_year else True
                    if month_ok and year_ok:
                        matched_cc_url = _pick_cc_url(entry)
                        if matched_cc_url:
                            break

            if not matched_cc_url and q_months:
                for entry in cc_entries:
                    p_low = (entry.get("period") or "").lower()
                    if any(m in p_low for m in q_months) and (not q_year or q_year in p_low):
                        matched_cc_url = _pick_cc_url(entry)
                        if matched_cc_url:
                            break

            if not has_specific_period and not matched_cc_url:
                for entry in cc_entries:
                    matched_cc_url = _pick_cc_url(entry)
                    if matched_cc_url:
                        break

            resolved.append(
                {
                    "label": label,
                    "kind": "document" if matched_cc_url else "unavailable",
                    "url": matched_cc_url,
                    "section_id": None,
                    "available": bool(matched_cc_url),
                }
            )
            continue

        # 4. Exchange Filings / Press Releases
        if any(k in low for k in ("filing", "announcement", "exchange", "press release", "bse", "nse")):
            f_url: str | None = None
            for f in filings:
                u = (f.get("pdf_url") or "").strip()
                if u.startswith(("http://", "https://")):
                    f_url = u
                    break
            resolved.append(
                {
                    "label": label,
                    "kind": "document" if f_url else "unavailable",
                    "url": f_url,
                    "section_id": None,
                    "available": bool(f_url),
                }
            )
            continue

        # 5. Screener Financial Tables & Ratios
        if base_sc and any(
            k in low
            for k in (
                "screener",
                "table",
                "quarter",
                "q1",
                "q2",
                "q3",
                "q4",
                "balance sheet",
                "cash flow",
                "cfo",
                "profit & loss",
                "profit and loss",
                "p&l",
                "income statement",
                "ratio",
                "roce",
                "roe",
                "working capital",
                "10y",
                "financial",
                "fy2",
                "mar 20",
                "jun 20",
                "sep 20",
                "dec 20",
            )
        ):
            if "balance sheet" in low:
                sc_url = f"{base_sc}#balance-sheet"
            elif "cash flow" in low or "cfo" in low:
                sc_url = f"{base_sc}#cash-flow"
            elif any(k in low for k in ("profit & loss", "profit and loss", "p&l", "income statement", "10y", "annual p&l")):
                sc_url = f"{base_sc}#profit-loss"
            elif "top ratio" in low or "market cap" in low or "valuation" in low:
                sc_url = f"{base_sc}#top"
            elif any(k in low for k in ("ratio", "working capital", "roce", "roe", "cash conversion")):
                sc_url = f"{base_sc}#ratios"
            elif any(k in low for k in ("quarter", "q1", "q2", "q3", "q4", "table", "jun ", "sep ", "dec ", "mar ")):
                sc_url = f"{base_sc}#quarters"
            else:
                sc_url = base_sc

            resolved.append(
                {
                    "label": label,
                    "kind": "webpage",
                    "url": sc_url,
                    "section_id": None,
                    "available": True,
                }
            )
            continue

        # 6. Unrecognized / unverified source -> mark unavailable, never invent a URL
        resolved.append(
            {
                "label": label,
                "kind": "unavailable",
                "url": None,
                "section_id": None,
                "available": False,
            }
        )

    return resolved


def ask_pull_copilot(
    ticker: str,
    question: str,
    db_path: Path = DB_PATH,
    history: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """
    On-demand Q&A grounded strictly in the company's primary fundamentals, Pull report and the distilled
    document notes cached with it. `history` is the last Q&A turns (oldest first); only the last 2 are used.
    Uses Tier-2 (gemini-2.5-flash) for fast, low-cost interactive responses.
    """
    clean_ticker = normalize_ticker(ticker)
    pull_data = get_or_generate_pull_report(clean_ticker, force_refresh=False, db_path=db_path)
    fundamentals = pull_data["fundamentals"]
    report = pull_data["report"]

    q_headers = (fundamentals.get("quarters") or {}).get("headers") or []
    latest_q = q_headers[-1] if q_headers else "Latest"

    available_source_tags: list[str] = [
        f"Screener Quarterly Table ({latest_q})",
        "Screener Annual P&L Table",
        "Screener Balance Sheet",
        "Screener Cash Flow Table",
        "Screener Ratios Table",
    ]
    # Only documents whose distilled notes are in this prompt are citable; unread PDFs on Screener are not.
    document_notes = [n for n in report.get("document_notes") or [] if isinstance(n, dict)]
    doc_tags = [f"{n.get('period', '')} {n.get('doc_type', '')}".strip() for n in document_notes]
    available_source_tags.extend(doc_tags)
    available_source_tags.extend(
        [
            "Synthesized Report (Narrative vs. Numbers)",
            "Synthesized Report (Financial Trajectory)",
            "Synthesized Report (Cash & Balance Sheet)",
            "Synthesized Report (Forward Guidance)",
        ]
    )

    context = {
        "company_name": fundamentals.get("company_name"),
        "available_sources": available_source_tags,
        "top_ratios": fundamentals.get("top_ratios"),
        "quarters": fundamentals.get("quarters"),
        "profit_loss": fundamentals.get("profit_loss"),
        "balance_sheet": fundamentals.get("balance_sheet"),
        "cash_flow": fundamentals.get("cash_flow"),
        "ratios": fundamentals.get("ratios"),
        # Notes go before the report so the char cap below trims the report, not the citable documents.
        "document_notes": [
            {"source_tag": tag, **{k: v for k, v in n.items() if k != "url"}} for tag, n in zip(doc_tags, document_notes)
        ],
        "synthesized_report": {k: v for k, v in report.items() if k != "document_notes"},
    }
    recent_turns = [
        {"question": str(t.get("question", ""))[:1000], "answer": str(t.get("answer", ""))[:4000]}
        for t in (history or [])[-2:]
        if isinstance(t, dict)
    ]

    prompt = f"""You are FINN, an institutional equity research copilot grounded STRICTLY in primary company filings for {fundamentals.get('company_name')} ({clean_ticker}).
Answer the investor's question concisely using ONLY the Screener tables, the distilled document notes (`document_notes`) and the synthesized report below.
You have NOT read any concall transcript, presentation or annual report beyond what `document_notes` contains. You may only cite documents present in this context.
Cite exact figures (₹ Cr, %, YoY/QoQ) from the data. If the provided sources do not contain the answer, say clearly that it is not in the loaded filings/transcripts instead of guessing, and return an empty `citations` list.
`recent_conversation` holds the last Q&A turns only to resolve follow-up questions. It is not a source: never cite it.

Investor Question: {question}

Return a JSON object with:
- "answer": Concise, high-signal markdown response (2-4 bullet points or short paragraph with bold numbers).
- "citations": list of 0-3 exact source tags chosen from `available_sources` (a document's tag is its `source_tag` in `document_notes`) that directly support your answer. Never cite anything outside `available_sources`.

Recent Conversation:
{json.dumps({"recent_conversation": recent_turns}, ensure_ascii=False)}

Primary Data Context:
{json.dumps(context, ensure_ascii=False)[:60000]}
"""
    res, served_by = _call_gemini_json_with_model(
        prompt=prompt,
        model=MODEL_FLASH,
        workflow="pull_copilot_qa",
        ticker=clean_ticker,
        db_path=db_path,
    )
    answer_text = res.get("answer", "") if isinstance(res, dict) else ""
    raw_citations = list(res.get("citations", [])) if isinstance(res, dict) else []
    if (
        any(k in answer_text.lower() for k in ("synthesized report", "narrative vs"))
        and not any("report" in str(c).lower() or "narrative" in str(c).lower() for c in raw_citations)
    ):
        raw_citations.append("Synthesized Report")
    # Drop document citations the model was never shown (e.g. an older transcript listed on Screener but not loaded).
    doc_words = ("concall", "transcript", "earnings call", "conference call", "presentation", "ppt", "annual report")
    loaded = [t.lower() for t in doc_tags if t]
    raw_citations = [
        c
        for c in raw_citations
        if not any(w in str(c).lower() for w in doc_words) or any(t in str(c).lower() for t in loaded)
    ]
    citation_links = resolve_copilot_citations(raw_citations, fundamentals, report, db_path=db_path)
    return {
        "ticker": clean_ticker,
        "question": question,
        "answer": answer_text,
        "citations": [c["label"] for c in citation_links],
        "citation_links": citation_links,
        "model": served_by,
    }
