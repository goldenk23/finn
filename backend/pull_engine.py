from concurrent.futures import ThreadPoolExecutor
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


def _get_ticker_lock(ticker: str) -> threading.Lock:
    with _LOCKS_GUARD:
        if ticker not in _TICKER_LOCKS:
            _TICKER_LOCKS[ticker] = threading.Lock()
        return _TICKER_LOCKS[ticker]


def _extract_primary_documents(screener_data: dict[str, Any], max_concalls: int = 4) -> list[dict[str, str]]:
    """
    Download and extract high-signal pages in parallel from the last up to 4 Concall Transcripts / Investor PPTs,
    falling back to the latest Annual Report if concall PDFs are unavailable.
    """
    concalls = (screener_data.get("concalls") or [])[:max_concalls]

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

    docs: list[dict[str, str]] = []
    if concalls:
        with ThreadPoolExecutor(max_workers=4) as pool:
            for item in pool.map(_fetch_one, concalls):
                if item:
                    docs.append(item)

    # Supplement with latest Annual Report MD&A if fewer than 2 concall docs extracted
    annual_reports = screener_data.get("annual_reports") or []
    if len(docs) < 2 and annual_reports:
        ar = annual_reports[0]
        ar_url = ar.get("url", "")
        if ar_url:
            ar_text = extract_pdf_text(
                ar_url,
                max_pages=10,
                keywords=["management discussion", "outlook", "segment", "performance", "strategy", "risk", "capex"],
                max_chars=16000,
            )
            if len(ar_text.strip()) > 400:
                docs.append(
                    {
                        "period": ar.get("title", "Latest Annual Report"),
                        "doc_type": "Annual Report",
                        "url": ar_url,
                        "text": ar_text,
                    }
                )

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
                return {
                    "ticker": clean_ticker,
                    "quarter_key": cached["quarter_key"],
                    "cached": True,
                    "tokens_in": cached["tokens_in"],
                    "tokens_out": cached["tokens_out"],
                    "cost_usd": cached["cost_usd"],
                    "created_at": cached["created_at"],
                    "fundamentals": json.loads(cached["fundamentals_json"]),
                    "report": json.loads(cached["report_json"]),
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
        if primary_docs:
            docs_payload = [
                {
                    "period": d["period"],
                    "doc_type": d["doc_type"],
                    "url": d["url"],
                    "excerpt": d["text"][:12000],
                }
                for d in primary_docs
            ]
            map_prompt = f"""You are a primary-source financial analyst extracting management commentary for {fundamentals['company_name']} ({clean_ticker}).
Analyze the following primary documents (Concall Transcripts, Investor Presentations, Annual Report) and extract ONLY explicit statements, guidance, growth/margin commentary, capex plans, and segment drivers stated by management.
Do NOT use outside knowledge or broker opinions.

Return a JSON array of objects, one per document, with keys:
- "period": string (matching document period)
- "doc_type": string
- "stated_guidance_and_promises": list of specific forward-looking claims, growth targets, margin expectations, or execution milestones management stated
- "segment_and_demand_commentary": list of key operational highlights or headwinds discussed
- "notable_quotes_or_facts": list of 1-2 verbatim or near-verbatim factual statements with page reference if visible

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

        reduce_prompt = f"""You are FINN, a high-conviction institutional investment analyst for ENAM Holdings.
Generate a complete, deeply analytical Pull Research Report for {fundamentals['company_name']} ({clean_ticker}).

STRICT DISCIPLINE:
- Ground EVERY number, ratio, trajectory claim, and narrative comparison strictly in the provided Primary Source Bundle (Screener 10Y audited financials, quarterly exchange results, concall transcripts, investor presentations, and annual reports).
- NO broker notes, NO news commentary, NO ungrounded outside assumptions.
- Zero AI slop: write with high information density, sharp institutional judgment, and exact numbers (with ₹ Cr and % units).
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
) -> dict[str, Any]:
    """
    On-demand Q&A grounded strictly in the company's primary fundamentals and Pull report.
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
    sources_used = report.get("sources_used") or {}
    for d in sources_used.get("documents_parsed") or []:
        if d.get("url"):
            available_source_tags.append(f"{d.get('period', 'Recent')} {d.get('doc_type', 'Concall')}".strip())
    for c in (fundamentals.get("concalls") or [])[:4]:
        p = c.get("period", "")
        if c.get("transcript_url") and f"{p} Concall Transcript" not in available_source_tags:
            available_source_tags.append(f"{p} Concall Transcript".strip())
        elif c.get("ppt_url") and f"{p} Investor Presentation" not in available_source_tags:
            available_source_tags.append(f"{p} Investor Presentation".strip())
    for ar in (fundamentals.get("annual_reports") or [])[:3]:
        if ar.get("url") and ar.get("title"):
            available_source_tags.append(ar["title"])
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
        "synthesized_report": report,
    }

    prompt = f"""You are FINN, an institutional equity research copilot grounded STRICTLY in primary company filings for {fundamentals.get('company_name')} ({clean_ticker}).
Answer the investor's question concisely using ONLY the primary financial tables and concall/AR synthesis below.
Cite exact figures (₹ Cr, %, YoY/QoQ) from the data. If the primary data does not contain the answer, state that clearly instead of guessing.

Investor Question: {question}

Return a JSON object with:
- "answer": Concise, high-signal markdown response (2-4 bullet points or short paragraph with bold numbers).
- "citations": list of 1-3 exact source tags chosen from `available_sources` in the context that directly support your answer (do NOT invent citations outside `available_sources`).

Primary Data Context:
{json.dumps(context, ensure_ascii=False)[:40000]}
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
    citation_links = resolve_copilot_citations(raw_citations, fundamentals, report, db_path=db_path)
    return {
        "ticker": clean_ticker,
        "question": question,
        "answer": answer_text,
        "citations": [c["label"] for c in citation_links],
        "citation_links": citation_links,
        "model": served_by,
    }
