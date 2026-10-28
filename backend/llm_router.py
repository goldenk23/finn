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
                if cat == "unclassified":
                    mat = 0
                results[fid] = {
                    "category": cat,
                    "materiality": mat,
                    "summary": (item.get("summary") or "").strip(),
                }
        except Exception as exc:
            print(f"[llm_router] Batch triage error: {exc}")

    return results


def enrich_quarterly_results(
    ticker: str,
    filing_title: str,
    pdf_url: str = "",
    db_path: Path = DB_PATH,
) -> dict[str, Any]:
    """
    Tier 2 (gemini-2.5-flash):
    Extracts Revenue, EBITDA, PBT, and PAT with QoQ and YoY % changes.
    First tries the actual results filing PDF; if scanned/missing numbers, falls back to Screener's quarterly table.
    """
    pdf_text = extract_results_pdf_text(pdf_url, max_pages=10) if pdf_url else ""
    has_usable_pdf = len(pdf_text.strip()) > 600

    screener_quarters_json = ""
    source_label = "Exchange Filing PDF"

    if has_usable_pdf:
        prompt_pdf = f"""You are a financial analyst extracting headline quarterly results for {ticker}.
Filing title: {filing_title}

Extract the Consolidated financial numbers (or Standalone if Consolidated is not present) from the filing text below.
Determine whether the filing actually contains the quarterly financial results numbers (Revenue, EBITDA, PBT, PAT).
Return a JSON object with:
- "has_numbers": boolean (true ONLY if actual numeric Revenue and PAT for the current quarter are present in the text)
- "period": string (e.g. "Q1 FY27 (Jun 2026)")
- "unit": string (normalized to "₹ Cr" if possible)
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
            if isinstance(res, dict) and res.get("has_numbers") is True:
                res["source"] = source_label
                return res
        except Exception as exc:
            print(f"[llm_router] PDF results extraction failed for {ticker}, trying Screener fallback: {exc}")

    # Fallback to Screener quarterly table when PDF is scanned, a board notice, or lacks full table
    try:
        sc_data = fetch_screener_fundamentals(ticker)
        q_table = sc_data.get("quarters", {})
        screener_quarters_json = json.dumps(q_table, ensure_ascii=False)
        source_label = "Screener Primary Filing Data (Fallback)"
    except Exception as exc:
        return {"has_numbers": False, "error": f"Failed to fetch fallback data: {exc}"}

    prompt_fallback = f"""You are a financial analyst extracting headline quarterly results for {ticker}.
The exchange filing PDF was either a notice or scanned image, so use the company's primary quarterly filing table below.
Take the latest quarter column, the immediately preceding quarter column (for QoQ %), and the same quarter a year ago (4 quarters back, for YoY %).
Map:
- Revenue = Sales / Revenue
- EBITDA = Operating Profit
- PBT = Profit before tax
- PAT = Net Profit

Compute exact QoQ % and YoY % changes (formatted with sign like "+5.8%" or "-2.1%").
Return a JSON object with:
- "has_numbers": true
- "period": string (the latest quarter column header, e.g. "Jun 2026")
- "unit": "₹ Cr"
- "source": "{source_label}"
- "metrics": {{
    "revenue": {{"value": string, "qoq_pct": string, "yoy_pct": string}},
    "ebitda":  {{"value": string, "qoq_pct": string, "yoy_pct": string}},
    "pbt":     {{"value": string, "qoq_pct": string, "yoy_pct": string}},
    "pat":     {{"value": string, "qoq_pct": string, "yoy_pct": string}}
  }}
- "takeaway": 1 concise sentence highlighting Revenue & PAT QoQ/YoY growth and operating margin direction.

Quarterly Data Table:
{screener_quarters_json}
"""
    res = _call_gemini_json(
        prompt=prompt_fallback,
        model=MODEL_FLASH,
        workflow="push_enrich_results_fallback",
        ticker=ticker,
        db_path=db_path,
    )
    if isinstance(res, dict):
        res["source"] = source_label
    return res


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
    return enriched


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

