import json
import os
from pathlib import Path
from typing import Any
from dotenv import load_dotenv
from google import genai
from google.genai import types
from ddgs import DDGS

from backend.db import get_conn, DB_PATH
from backend.pdf_utils import extract_pdf_text, extract_results_pdf_text
from backend.scrapers.screener import fetch_screener_fundamentals
from backend.scrapers.filings import enforce_filing_hygiene

# Load .env from project root
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

MODEL_FLASH_LITE = "gemini-2.5-flash-lite"
MODEL_FLASH = "gemini-2.5-flash"
MODEL_PRO = "gemini-2.5-pro"

# Pricing per 1M tokens (USD) for cost ledger tracking
MODEL_PRICING_PER_1M: dict[str, tuple[float, float]] = {
    MODEL_FLASH_LITE: (0.10, 0.40),
    MODEL_FLASH: (0.30, 2.50),
    MODEL_PRO: (1.25, 10.00),
    "gemini-3.1-pro-preview": (1.25, 10.00),
}

MODEL_FALLBACKS: dict[str, list[str]] = {
    MODEL_FLASH_LITE: [MODEL_FLASH_LITE, MODEL_FLASH],
    MODEL_FLASH: [MODEL_FLASH],
    MODEL_PRO: [MODEL_PRO, "gemini-3.1-pro-preview", MODEL_FLASH],
}

VALID_CATEGORIES = [
    "results",
    "M&A",
    "fund_raise",
    "management_change",
    "litigation",
    "insider_trading",
    "earnings_call",
    "investor_meetings",
    "credit_rating",
    "dividend",
    "bonus_split",
    "capex",
    "press_release",
    "postal_ballot",
    "agm_egm",
    "pledging",
    "unclassified",
]


def _get_client() -> genai.Client:
    api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    project = (os.getenv("GOOGLE_CLOUD_PROJECT") or "").strip()
    location = (os.getenv("GOOGLE_CLOUD_LOCATION") or "global").strip()
    use_vertex = (os.getenv("GOOGLE_GENAI_USE_VERTEXAI") or "").lower() == "true"

    # Standard Gemini Developer API key (AIza...)
    if api_key.startswith("AIza") and not use_vertex:
        return genai.Client(vertexai=False, api_key=api_key)

    # Vertex AI with GCP project (uses Application Default Credentials + GCP credits)
    if project:
        return genai.Client(vertexai=True, project=project, location=location)

    if api_key:
        return genai.Client(api_key=api_key)

    raise RuntimeError("Neither GEMINI_API_KEY nor GOOGLE_CLOUD_PROJECT is set in .env")


def log_llm_usage(
    workflow: str,
    model: str,
    tokens_in: int,
    tokens_out: int,
    ticker: str | None = None,
    db_path: Path = DB_PATH,
) -> float:
    in_rate, out_rate = MODEL_PRICING_PER_1M.get(model, (0.30, 2.50))
    cost_usd = round((tokens_in * in_rate + tokens_out * out_rate) / 1_000_000.0, 6)
    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO llm_cost_ledger (workflow, ticker, model, tokens_in, tokens_out, cost_usd)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (workflow, ticker, model, tokens_in, tokens_out, cost_usd),
        )
    return cost_usd


def get_cost_summary(db_path: Path = DB_PATH) -> dict[str, Any]:
    with get_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT workflow, model, COUNT(*) as calls,
                   SUM(tokens_in) as total_tokens_in,
                   SUM(tokens_out) as total_tokens_out,
                   ROUND(SUM(cost_usd), 6) as total_cost_usd
            FROM llm_cost_ledger
            GROUP BY workflow, model
            ORDER BY total_cost_usd DESC
            """
        ).fetchall()
        totals = conn.execute(
            """
            SELECT COUNT(*) as total_calls,
                   COALESCE(SUM(tokens_in), 0) as tokens_in,
                   COALESCE(SUM(tokens_out), 0) as tokens_out,
                   ROUND(COALESCE(SUM(cost_usd), 0.0), 6) as cost_usd
            FROM llm_cost_ledger
            """
        ).fetchone()
    return {
        "totals": dict(totals) if totals else {},
        "by_workflow": [dict(r) for r in rows],
    }


def _call_gemini_json(
    prompt: str,
    model: str,
    workflow: str,
    ticker: str | None = None,
    temperature: float = 0.1,
    db_path: Path = DB_PATH,
) -> Any:
    client = _get_client()
    candidates = MODEL_FALLBACKS.get(model, [model])
    last_err: Exception | None = None

    for candidate_model in candidates:
        try:
            resp = client.models.generate_content(
                model=candidate_model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=temperature,
                    response_mime_type="application/json",
                ),
            )
            usage = resp.usage_metadata
            tokens_in = int(getattr(usage, "prompt_token_count", 0) or 0)
            tokens_out = int(getattr(usage, "candidates_token_count", 0) or 0)
            log_llm_usage(
                workflow=workflow,
                model=candidate_model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                ticker=ticker,
                db_path=db_path,
            )
            raw_text = (resp.text or "{}").strip()
            return json.loads(raw_text)
        except Exception as exc:
            last_err = exc
            if "404" in str(exc) or "NOT_FOUND" in str(exc):
                continue
            raise
    raise RuntimeError(f"All candidate models {candidates} failed: {last_err}")


def classify_and_summarize_batch(
    filings: list[dict[str, Any]], db_path: Path = DB_PATH, batch_size: int = 15
) -> dict[str, dict[str, Any]]:
    """
    Tier 1 (gemini-2.5-flash-lite):
    Batch-classifies corporate announcements into the 17 categories, assigns materiality (0 or 1),
    and generates a sharp 1-line investor summary.
    """
    if not filings:
        return {}

    results: dict[str, dict[str, Any]] = {}
    for i in range(0, len(filings), batch_size):
        batch = filings[i : i + batch_size]
        payload = [
            {
                "filing_id": f["filing_id"],
                "ticker": f["ticker"],
                "title": f["title"],
                "heuristic_category": f.get("category", "unclassified"),
            }
            for f in batch
        ]
        prompt = f"""You are a senior Indian public-markets equity analyst filtering NSE/BSE corporate filings.
For each filing in the input JSON array, classify it and return a JSON array of objects with exact keys:
- "filing_id": string (matching input)
- "category": one of {json.dumps(VALID_CATEGORIES)}
- "materiality": integer 1 or 0.
  Set materiality=1 ONLY if the event can materially impact stock valuation, operations, or governance (e.g. quarterly/annual financial results, M&A/divestment/stake sale, CEO/CFO/MD/Director appointment or resignation, fund raise, major capex, credit rating change, significant litigation/fine, dividend/bonus/split, insider trading, promoter pledging, or actual earnings call transcript/recording).
  Set materiality=0 for routine noise (newspaper advertisements, loss of share certificates, duplicate share issuance, ESOP allotments, trading window closures, Regulation 74(5) certificates, scheduled 1-on-1 investor/analyst meet notices, routine AGM/postal ballot notices).
- "summary": A crisp, no-fluff 1-line summary (max 18 words) stating WHAT happened and WHY an investor cares. No filler phrases like "The company informed the exchange that".

Input filings:
{json.dumps(payload, ensure_ascii=False)}
"""
        try:
            title_by_id = {f["filing_id"]: f.get("title", "") for f in batch}
            parsed = _call_gemini_json(
                prompt=prompt,
                model=MODEL_FLASH_LITE,
                workflow="push_batch_triage",
                ticker="BATCH",
                db_path=db_path,
            )
            items = parsed if isinstance(parsed, list) else parsed.get("items", [])
            for item in items:
                fid = item.get("filing_id")
                if not fid:
                    continue
                cat = item.get("category", "unclassified")
                if cat not in VALID_CATEGORIES:
                    cat = "unclassified"
                mat = 1 if int(item.get("materiality", 0)) == 1 else 0
                cat, mat = enforce_filing_hygiene(title_by_id.get(fid, ""), cat, mat)
                results[fid] = {
                    "category": cat,
                    "materiality": mat,
                    "summary": (item.get("summary") or "").strip(),
                }
        except Exception as exc:
            print(f"[llm_router] Batch triage error: {exc}")

    return results


def _parse_num(val: Any) -> float | None:
    if val is None:
        return None
    s = str(val).replace(",", "").replace("₹", "").replace("Cr", "").replace("%", "").strip()
    if not s or s.lower() in ("n/a", "-", "none", "null"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _format_inr(val: Any) -> str:
    num = _parse_num(val)
    if num is None:
        return str(val or "—")
    if abs(num) >= 100 or num == int(num):
        return f"{int(round(num)):,}"
    return f"{num:,.1f}"


def _format_pct(val: Any) -> str:
    num = _parse_num(val)
    if num is None:
        return "—"
    return f"{num:+.1f}%"


def _normalize_results_enrichment(res: dict[str, Any]) -> dict[str, Any]:
    metrics = res.get("metrics") or {}
    for k in ("revenue", "ebitda", "pbt", "pat"):
        m = metrics.get(k) or {}
        if isinstance(m, dict):
            m["value"] = _format_inr(m.get("value"))
            m["qoq_pct"] = _format_pct(m.get("qoq_pct"))
            m["yoy_pct"] = _format_pct(m.get("yoy_pct"))
            metrics[k] = m
    res["metrics"] = metrics
    res["unit"] = "₹ Cr"
    return res


def _compute_screener_quarterly_metrics(ticker: str) -> dict[str, Any] | None:
    """
    Deterministically computes latest quarter Revenue, EBITDA (or Financing Profit),
    PBT, and PAT with exact QoQ % and YoY % from Screener's normalized ₹ Cr table.
    """
    sc_data = fetch_screener_fundamentals(ticker)
    q_table = sc_data.get("quarters", {})
    headers = q_table.get("headers") or []
    rows = q_table.get("rows") or []
    if len(headers) < 2 or not rows:
        return None

    q_curr = headers[-1]
    q_prev = headers[-2]
    q_yoy = headers[-5] if len(headers) >= 5 else None

    def _find_row(names: tuple[str, ...]) -> dict[str, Any] | None:
        for r in rows:
            m_name = (r.get("metric") or "").lower().strip()
            if any(m_name.startswith(n) for n in names):
                return r
        return None

    rev_row = _find_row(("sales", "revenue"))
    ebitda_row = _find_row(("operating profit", "financing profit"))
    pbt_row = _find_row(("profit before tax",))
    pat_row = _find_row(("net profit",))

    def _build_metric(row: dict[str, Any] | None) -> dict[str, str]:
        if not row:
            return {"value": "—", "qoq_pct": "—", "yoy_pct": "—"}
        v_curr = _parse_num(row.get(q_curr))
        v_prev = _parse_num(row.get(q_prev))
        v_yoy = _parse_num(row.get(q_yoy)) if q_yoy else None

        qoq_str = "—"
        if v_curr is not None and v_prev is not None and v_prev != 0:
            qoq_str = f"{((v_curr - v_prev) / abs(v_prev)) * 100.0:+.1f}%"

        yoy_str = "—"
        if v_curr is not None and v_yoy is not None and v_yoy != 0:
            yoy_str = f"{((v_curr - v_yoy) / abs(v_yoy)) * 100.0:+.1f}%"

        return {
            "value": _format_inr(v_curr) if v_curr is not None else "—",
            "qoq_pct": qoq_str,
            "yoy_pct": yoy_str,
        }

    return {
        "has_numbers": True,
        "period": q_curr,
        "unit": "₹ Cr",
        "source": "Screener Primary Filing Data (Consolidated ₹ Cr)",
        "metrics": {
            "revenue": _build_metric(rev_row),
            "ebitda": _build_metric(ebitda_row),
            "pbt": _build_metric(pbt_row),
            "pat": _build_metric(pat_row),
        },
        "raw_table": q_table,
    }


def _is_pdf_extraction_sane(pdf_res: dict[str, Any], sc_ref: dict[str, Any] | None) -> bool:
    """
    Guards against PDF unit-shift bugs (e.g. Revenue in ₹ Cr but EBITDA/PAT shifted 10x from ₹ Billion/Million).
    """
    m = pdf_res.get("metrics") or {}
    rev = _parse_num((m.get("revenue") or {}).get("value"))
    ebitda = _parse_num((m.get("ebitda") or {}).get("value"))
    pat = _parse_num((m.get("pat") or {}).get("value"))
    if rev is None or rev <= 0 or ebitda is None or pat is None:
        return False

    # Implausible margin ratio (<2.5% EBITDA margin or >95% EBITDA margin on large revenue)
    if abs(ebitda) / rev < 0.025 or abs(ebitda) / rev > 0.95 or abs(pat) / rev < 0.015:
        return False

    # Cross-check against Screener's standardized ₹ Cr numbers if available
    if sc_ref:
        sc_m = sc_ref.get("metrics") or {}
        sc_rev = _parse_num((sc_m.get("revenue") or {}).get("value"))
        sc_ebitda = _parse_num((sc_m.get("ebitda") or {}).get("value"))
        sc_pat = _parse_num((sc_m.get("pat") or {}).get("value"))
        for pdf_v, sc_v in ((rev, sc_rev), (ebitda, sc_ebitda), (pat, sc_pat)):
            if pdf_v is not None and sc_v is not None and sc_v != 0:
                rel_diff = abs(pdf_v - sc_v) / abs(sc_v)
                if rel_diff > 0.25:
                    return False

    return True


def enrich_quarterly_results(
    ticker: str,
    filing_title: str,
    pdf_url: str = "",
    db_path: Path = DB_PATH,
) -> dict[str, Any]:
    """
    Tier 2 (gemini-2.5-flash):
    Extracts Revenue, EBITDA, PBT, and PAT with QoQ and YoY % changes.
    Cross-checks PDF extraction against Screener's normalized ₹ Cr quarterly table
    to prevent PDF unit-conversion anomalies (Million/Billion vs Crore), and formats all % deltas consistently.
    """
    sc_ref: dict[str, Any] | None = None
    try:
        sc_ref = _compute_screener_quarterly_metrics(ticker)
    except Exception as exc:
        print(f"[llm_router] Screener reference fetch warning for {ticker}: {exc}")

    pdf_text = extract_results_pdf_text(pdf_url, max_pages=10) if pdf_url else ""
    has_usable_pdf = len(pdf_text.strip()) > 600

    if has_usable_pdf:
        prompt_pdf = f"""You are a financial analyst extracting headline quarterly results for {ticker}.
Filing title: {filing_title}

Extract the Consolidated financial numbers (or Standalone if Consolidated is not present) from the filing text below.
CRITICAL UNIT RULE: Convert all values to ₹ Crores (₹ Cr) consistently across all 4 rows (1 Billion = 100 Crores, 10 Million = 1 Crore, 100 Lakhs = 1 Crore).
Format QoQ % and YoY % with an explicit leading '+' or '-' sign and trailing '%' (e.g. "+5.8%", "-2.1%").
Return a JSON object with:
- "has_numbers": boolean (true ONLY if actual numeric Revenue and PAT for the current quarter are present in the text)
- "period": string (e.g. "Q2 FY27 (Sep 2026)")
- "unit": "₹ Cr"
- "metrics": {{
    "revenue": {{"value": string, "qoq_pct": string, "yoy_pct": string}},
    "ebitda":  {{"value": string, "qoq_pct": string, "yoy_pct": string}},
    "pbt":     {{"value": string, "qoq_pct": string, "yoy_pct": string}},
    "pat":     {{"value": string, "qoq_pct": string, "yoy_pct": string}}
  }}
- "takeaway": 1 concise sentence summarizing topline & bottomline performance (QoQ and YoY).

Filing PDF Text:
{pdf_text[:30000]}
"""
        try:
            res = _call_gemini_json(
                prompt=prompt_pdf,
                model=MODEL_FLASH,
                workflow="push_enrich_results_pdf",
                ticker=ticker,
                db_path=db_path,
            )
            if isinstance(res, dict) and res.get("has_numbers") is True and _is_pdf_extraction_sane(res, sc_ref):
                res["source"] = "Exchange Filing PDF"
                return _normalize_results_enrichment(res)
        except Exception as exc:
            print(f"[llm_router] PDF results extraction failed for {ticker}, using Screener table: {exc}")

    if not sc_ref:
        return {"has_numbers": False, "error": f"No usable quarterly table for {ticker}"}

    prompt_takeaway = f"""Write a 1-sentence institutional takeaway for {ticker}'s {sc_ref['period']} quarterly results (in ₹ Cr):
Metrics: {json.dumps(sc_ref['metrics'], ensure_ascii=False)}
Return JSON: {{"takeaway": "1 concise sentence highlighting Revenue & PAT QoQ/YoY growth and operating margin direction."}}"""
    takeaway = f"{ticker} reported {sc_ref['period']} Revenue of ₹{sc_ref['metrics']['revenue']['value']} Cr ({sc_ref['metrics']['revenue']['qoq_pct']} QoQ, {sc_ref['metrics']['revenue']['yoy_pct']} YoY) and PAT of ₹{sc_ref['metrics']['pat']['value']} Cr ({sc_ref['metrics']['pat']['qoq_pct']} QoQ, {sc_ref['metrics']['pat']['yoy_pct']} YoY)."
    try:
        t_res = _call_gemini_json(
            prompt=prompt_takeaway,
            model=MODEL_FLASH,
            workflow="push_enrich_results_fallback",
            ticker=ticker,
            db_path=db_path,
        )
        if isinstance(t_res, dict) and t_res.get("takeaway"):
            takeaway = t_res["takeaway"].strip()
    except Exception:
        pass

    return _normalize_results_enrichment(
        {
            "has_numbers": True,
            "period": sc_ref["period"],
            "unit": "₹ Cr",
            "source": sc_ref["source"],
            "metrics": sc_ref["metrics"],
            "takeaway": takeaway,
        }
    )


def _search_executive_web(person_name: str, company_name: str) -> list[dict[str, str]]:
    if not person_name or person_name.lower() in ("unknown", "n/a", "none"):
        return []
    query = f'"{person_name}" {company_name} executive profile OR LinkedIn OR biography'
    snippets: list[dict[str, str]] = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=5):
                snippets.append(
                    {
                        "title": r.get("title", ""),
                        "url": r.get("href", ""),
                        "snippet": r.get("body", ""),
                    }
                )
    except Exception as exc:
        print(f"[llm_router:ddgs] Search warning for {person_name}: {exc}")
    return snippets


def enrich_management_change(
    ticker: str,
    company_name: str,
    filing_title: str,
    pdf_url: str = "",
    db_path: Path = DB_PATH,
) -> dict[str, Any]:
    """
    Tier 2 (gemini-2.5-flash) + DuckDuckGo Search (ddgs):
    1. Extracts person name, role, and change type from the filing PDF (first 4 pages) + title.
    2. Runs a DuckDuckGo search for the executive's prior background / LinkedIn.
    3. Synthesizes a grounded 2-line bio without hallucinating.
    """
    pdf_text = extract_pdf_text(pdf_url, max_pages=4, max_chars=12000) if pdf_url else ""

    extract_prompt = f"""Extract the key executive/director details from this corporate announcement for {company_name} ({ticker}).
Filing Title: {filing_title}
Filing Text:
{pdf_text[:10000]}

Return a JSON object with:
- "person_name": Full name of the primary person joining, resigning, or changing role (or "Unknown" if not named)
- "role": Their designation/role (e.g., "Chief Financial Officer", "Independent Director", "Managing Director")
- "action": One of ["Appointment", "Resignation", "Retirement", "Re-appointment", "Change in Directorate"]
- "effective_date": string (if stated, else "")
- "filing_profile_notes": Any career history, qualifications, or reason for change explicitly mentioned in the filing text.
"""
    info = _call_gemini_json(
        prompt=extract_prompt,
        model=MODEL_FLASH,
        workflow="push_mgmt_extract",
        ticker=ticker,
        db_path=db_path,
    )
    if isinstance(info, list):
        info = info[0] if info and isinstance(info[0], dict) else {}
    elif not isinstance(info, dict):
        info = {}
    person_name = (info.get("person_name") or "Unknown").strip()
    role = (info.get("role") or "").strip()
    action = (info.get("action") or "Management Change").strip()

    web_results = _search_executive_web(person_name, company_name or ticker)

    bio_prompt = f"""You are preparing an executive briefing card for an institutional investor on a management change at {company_name} ({ticker}).

Extracted Filing Info:
{json.dumps(info, ensure_ascii=False)}

Web Search Snippets (DuckDuckGo):
{json.dumps(web_results, ensure_ascii=False)}

Write a factual, grounded executive summary. Do NOT make up or guess career history if neither the filing nor search results mention it.
Return a JSON object with:
- "person_name": "{person_name}"
- "role": "{role}"
- "action": "{action}"
- "effective_date": "{info.get('effective_date', '')}"
- "two_line_bio": A sharp 2-line background/bio covering who they are, where they came from (previous companies/roles/education), or the context of their departure.
- "source_url": Best reference URL from the search snippets (e.g. LinkedIn or profile page), or "" if only from the exchange filing.
"""
    enriched = _call_gemini_json(
        prompt=bio_prompt,
        model=MODEL_FLASH,
        workflow="push_mgmt_bio",
        ticker=ticker,
        db_path=db_path,
    )
    if isinstance(enriched, list):
        enriched = enriched[0] if enriched and isinstance(enriched[0], dict) else {}
    return enriched if isinstance(enriched, dict) else {}


def call_gemini_pro_json(
    prompt: str,
    workflow: str = "pull_deep_synthesis",
    ticker: str | None = None,
    db_path: Path = DB_PATH,
) -> Any:
    """
    Tier 3 (gemini-2.5-pro): Reserved for complex Pull multi-quarter reasoning ('Numbers vs. Narrative').
    """
    return _call_gemini_json(
        prompt=prompt,
        model=MODEL_PRO,
        workflow=workflow,
        ticker=ticker,
        temperature=0.15,
        db_path=db_path,
    )

