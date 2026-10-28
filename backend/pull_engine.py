from concurrent.futures import ThreadPoolExecutor
import json
import threading
from pathlib import Path
from typing import Any

from backend.db import get_conn, DB_PATH
from backend.pdf_utils import extract_pdf_text
from backend.scrapers.screener import fetch_screener_fundamentals
from backend.scrapers.filings import fetch_company_filings
from backend.llm_router import (
    _call_gemini_json,
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
    clean_ticker = ticker.upper().replace(".NS", "").replace(".BO", "").strip()
    if not clean_ticker:
        raise ValueError("Ticker cannot be empty")

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

        # 2. Fetch recent NSE/BSE filings to include any fresh quarterly disclosures
        recent_filings = fetch_company_filings(clean_ticker, days=30, db_path=db_path, save_to_db=True)
        material_filings = [
            {"date": f["published_at"], "category": f["category"], "title": f["title"], "pdf_url": f["pdf_url"]}
            for f in recent_filings
            if f.get("materiality") == 1
        ][:8]

        # 3. Extract text from primary Concalls / Investor PPTs / Annual Reports
        primary_docs = _extract_primary_documents(fundamentals, max_concalls=4)

        # 4. Map Step (Tier 2: gemini-2.5-flash): Distill management promises & commentary per document
        distilled_notes: list[dict[str, Any]] = []
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
                mapped = _call_gemini_json(
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
        report = call_gemini_pro_json(
            prompt=reduce_prompt,
            workflow="pull_deep_synthesis",
            ticker=clean_ticker,
            db_path=db_path,
        )

        # Attach source provenance metadata to report
        report["sources_used"] = {
            "screener_url": fundamentals["screener_url"],
            "documents_parsed": [
                {"period": d["period"], "doc_type": d["doc_type"], "url": d["url"]} for d in primary_docs
            ],
            "concalls_available": fundamentals["concalls"][:4],
            "annual_reports_available": fundamentals["annual_reports"][:3],
            "material_filings_count": len(material_filings),
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


def ask_pull_copilot(
    ticker: str,
    question: str,
    db_path: Path = DB_PATH,
) -> dict[str, Any]:
    """
    On-demand Q&A grounded strictly in the company's primary fundamentals and Pull report.
    Uses Tier-2 (gemini-2.5-flash) for fast, low-cost interactive responses.
    """
    clean_ticker = ticker.upper().replace(".NS", "").replace(".BO", "").strip()
    pull_data = get_or_generate_pull_report(clean_ticker, force_refresh=False, db_path=db_path)
    fundamentals = pull_data["fundamentals"]
    report = pull_data["report"]

    context = {
        "company_name": fundamentals.get("company_name"),
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
- "citations": list of 1-3 short source tags (e.g. ["Screener Q1 FY27 Table", "Jul 2026 Concall", "FY26 Balance Sheet"]).

Primary Data Context:
{json.dumps(context, ensure_ascii=False)[:40000]}
"""
    res = _call_gemini_json(
        prompt=prompt,
        model=MODEL_FLASH,
        workflow="pull_copilot_qa",
        ticker=clean_ticker,
        db_path=db_path,
    )
    return {
        "ticker": clean_ticker,
        "question": question,
        "answer": res.get("answer", ""),
        "citations": res.get("citations", []),
    }
