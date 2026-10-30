import json
import logging
import os
import re
from pathlib import Path
from typing import Any
from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from ddgs import DDGS

from backend.db import get_conn, DB_PATH
from backend.pdf_utils import extract_pdf_text, extract_results_pdf_text
from backend.scrapers.screener import fetch_screener_fundamentals
from backend.scrapers.filings import enforce_filing_hygiene

# Load .env from project root
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

logger = logging.getLogger("finn.llm")


def _sanitize_log_message(msg: str) -> str:
    """Strip any potential API key or bearer token substrings before logging."""
    s = re.sub(r"(key|token|access_token|secret)=[^&\s\"']+", r"\1=[REDACTED]", msg, flags=re.IGNORECASE)
    s = re.sub(r"Bearer\s+[0-9A-Za-z\-._~+/]+=*", "Bearer [REDACTED]", s, flags=re.IGNORECASE)
    s = re.sub(r"(AIza|AQ\.|ya29\.)[0-9A-Za-z\-_]{16,}", "[REDACTED_KEY]", s)
    return s


# Model routing, verified by a live probe on 2026-10-31 (Vertex AI, location=global).
# gemini-2.5-flash-lite and gemini-2.5-pro return 404 for this project (global and us-central1), which is why
# triage silently ran on 2.5-flash and Pull synthesis dropped to 2.5-flash.
MODEL_FLASH_LITE = "gemini-3.1-flash-lite"  # Tier 1: filing triage (cheapest working Lite)
MODEL_FLASH = "gemini-2.5-flash"  # Tier 2: PDF extraction, exec bios, concall map, Copilot
MODEL_PRO = "gemini-3.1-pro-preview"  # Tier 3: Pull 8-module synthesis

# USD per 1M tokens: Standard paid tier, text input, prompts <= 200k tokens. Output price includes thinking tokens.
# Source: https://ai.google.dev/gemini-api/docs/pricing (Vertex global rates match:
# https://cloud.google.com/vertex-ai/generative-ai/pricing), checked 2026-10-31.
# ponytail: ignores the >200k-token long-context rates of the Pro models (our prompts are capped far below) and the
# scheduled increases (gemini-4-argon -> $4/$20 on 2026-12-01; 3.6/3.8 Flash -> $1.50/$7.50 on 2027-01-01).
MODEL_PRICING_PER_1M: dict[str, tuple[float, float]] = {
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-3-flash-preview": (0.50, 3.00),
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-3.1-pro-preview": (2.00, 12.00),
    "gemini-4-argon": (2.00, 10.00),
}

# Tried in order. Pull synthesis only drops to Flash once every Pro-class model has failed.
MODEL_FALLBACKS: dict[str, list[str]] = {
    MODEL_FLASH_LITE: [MODEL_FLASH_LITE, "gemini-3.5-flash-lite", "gemini-2.5-flash-lite", MODEL_FLASH],
    MODEL_FLASH: [MODEL_FLASH, "gemini-3.8-flash", "gemini-3-flash-preview"],
    MODEL_PRO: [MODEL_PRO, "gemini-4-argon", "gemini-2.5-pro", MODEL_FLASH],
}

ROUTING_TIERS = [
    {"tier": 1, "model": MODEL_FLASH_LITE, "purpose": "Batch-triages 15 NSE/BSE filings per call into 17 categories + 1-line digest."},
    {"tier": 2, "model": MODEL_FLASH, "purpose": "Results PDF tables, exec bios, Concall Map notes and Copilot Q&A."},
    {"tier": 3, "model": MODEL_PRO, "purpose": "8-module Pull synthesis and the 4-quarter Narrative vs Numbers audit."},
]


def _thinking_config(model: str) -> types.ThinkingConfig:
    """Per-family thinking settings, from the probe:
    - Gemini 2.x accepts only thinking_budget (thinking_level -> 400). 2.5 Pro can't turn thinking off (min 128).
    - gemini-4-argon accepts only thinking_level (any thinking_budget -> 400).
    - Gemini 3.x accepts both; thinking_budget=0 turns thinking off (0 thought tokens) on the Flash/Lite models.
    """
    if model.startswith("gemini-2."):
        return types.ThinkingConfig(thinking_budget=512 if "pro" in model else 0)
    if "pro" in model or model.startswith("gemini-4"):
        return types.ThinkingConfig(thinking_level="LOW")  # bounded reasoning for synthesis
    return types.ThinkingConfig(thinking_budget=0)


def llm_cost_usd(model: str, tokens_in: int, tokens_out: int, tokens_thinking: int = 0) -> float:
    # Unknown models are priced at the most expensive known rates so the budget cap never undercounts.
    in_rate, out_rate = MODEL_PRICING_PER_1M.get(
        model, (max(p[0] for p in MODEL_PRICING_PER_1M.values()), max(p[1] for p in MODEL_PRICING_PER_1M.values()))
    )
    return round((tokens_in * in_rate + (tokens_out + tokens_thinking) * out_rate) / 1_000_000.0, 6)

# Cache models that return 404 NOT_FOUND in the active GCP project to avoid repeated 404 roundtrips
_UNAVAILABLE_MODELS: set[str] = set()

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

    # Auto-discover ADC GCP project if GOOGLE_CLOUD_PROJECT is not in os.environ / .env
    if not project:
        try:
            import google.auth
            _, adc_project = google.auth.default()
            if adc_project:
                project = str(adc_project).strip()
        except Exception:
            pass

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
    tokens_thinking: int = 0,
) -> float:
    cost_usd = llm_cost_usd(model, tokens_in, tokens_out, tokens_thinking)
    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO llm_cost_ledger (workflow, ticker, model, tokens_in, tokens_out, cost_usd)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            # tokens_out is billed output: response + thinking tokens
            (workflow, ticker, model, tokens_in, tokens_out + tokens_thinking, cost_usd),
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
        "routing": [
            {**t, "fallbacks": MODEL_FALLBACKS[t["model"]][1:], "price_per_1m": MODEL_PRICING_PER_1M[t["model"]]}
            for t in ROUTING_TIERS
        ],
    }


def latest_model_for(workflow: str, db_path: Path = DB_PATH) -> str | None:
    """Model that served the most recent call of a workflow, per the ledger."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT model FROM llm_cost_ledger WHERE workflow = ? ORDER BY id DESC LIMIT 1", (workflow,)
        ).fetchone()
    return row["model"] if row else None


def _is_model_fallback_error(exc: Exception) -> bool:
    """Errors specific to one model (not found, unsupported config, quota, overload, bad JSON) -> try the next model.
    Auth failures and network errors would hit every model the same way, so they propagate."""
    if isinstance(exc, json.JSONDecodeError):
        return True
    return isinstance(exc, genai_errors.APIError) and exc.code not in (401, 403)


def _call_gemini_json_with_model(
    prompt: str,
    model: str,
    workflow: str,
    ticker: str | None = None,
    temperature: float = 0.1,
    db_path: Path = DB_PATH,
) -> tuple[Any, str]:
    """Returns (parsed JSON, model that actually served it), walking MODEL_FALLBACKS[model] on model-specific errors."""
    client = _get_client()
    raw_candidates = MODEL_FALLBACKS.get(model, [model])
    candidates = [m for m in raw_candidates if m not in _UNAVAILABLE_MODELS] or raw_candidates
    last_err: Exception | None = None

    for i, candidate_model in enumerate(candidates):
        try:
            resp = client.models.generate_content(
                model=candidate_model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=temperature,
                    response_mime_type="application/json",
                    thinking_config=_thinking_config(candidate_model),
                ),
            )
            usage = resp.usage_metadata
            log_llm_usage(
                workflow=workflow,
                model=candidate_model,
                tokens_in=int(getattr(usage, "prompt_token_count", 0) or 0),
                tokens_out=int(getattr(usage, "candidates_token_count", 0) or 0),
                tokens_thinking=int(getattr(usage, "thoughts_token_count", 0) or 0),
                ticker=ticker,
                db_path=db_path,
            )
            return json.loads((resp.text or "{}").strip()), candidate_model
        except Exception as exc:
            if not _is_model_fallback_error(exc):
                raise
            last_err = exc
            if isinstance(exc, genai_errors.APIError) and exc.code == 404:
                _UNAVAILABLE_MODELS.add(candidate_model)
            nxt = candidates[i + 1] if i + 1 < len(candidates) else None
            logger.warning(
                "[llm] %s (%s): %s failed, %s. Reason: %s",
                workflow,
                ticker or "-",
                candidate_model,
                f"falling back to {nxt}" if nxt else "no fallbacks left",
                _sanitize_log_message(" ".join(f"{type(exc).__name__}: {exc}".split()))[:300],
            )
    raise RuntimeError(f"All candidate models {candidates} failed: {last_err}")


def _call_gemini_json(*args: Any, **kwargs: Any) -> Any:
    return _call_gemini_json_with_model(*args, **kwargs)[0]


def classify_and_summarize_batch(
    filings: list[dict[str, Any]], db_path: Path = DB_PATH, batch_size: int = 15
) -> dict[str, dict[str, Any]]:
    """
    Tier 1 (MODEL_FLASH_LITE):
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

    # Implausible margin ratio (<2.0% EBITDA margin or >95% EBITDA margin on large revenue, or negative EBITDA with positive PAT)
    if (
        (ebitda <= 0 and pat > 0)
        or abs(ebitda) / rev < 0.02
        or abs(ebitda) / rev > 0.95
        or abs(pat) / rev < 0.01
        or abs(pat) / rev > 0.80
    ):
        return False

    # Cross-check order-of-magnitude scale against Screener's standardized ₹ Cr Revenue & PAT
    # (allow up to ~40% variance across adjacent quarters / standalone vs consolidated, while catching 10x Million/Billion shifts)
    if sc_ref:
        sc_m = sc_ref.get("metrics") or {}
        sc_rev = _parse_num((sc_m.get("revenue") or {}).get("value"))
        sc_pat = _parse_num((sc_m.get("pat") or {}).get("value"))
        for pdf_v, sc_v in ((rev, sc_rev), (pat, sc_pat)):
            if pdf_v is not None and sc_v is not None and sc_v > 0:
                ratio = pdf_v / sc_v
                if ratio < 0.40 or ratio > 2.50:
                    return False

    return True


def enrich_quarterly_results(
    ticker: str,
    filing_title: str,
    pdf_url: str = "",
    db_path: Path = DB_PATH,
) -> dict[str, Any]:
    """
    Tier 2 (MODEL_FLASH):
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
        sc_context = ""
        if sc_ref and sc_ref.get("raw_table"):
            headers = (sc_ref["raw_table"].get("headers") or [])[-3:]
            rows_preview = [
                {k: v for k, v in r.items() if k == "metric" or k in headers}
                for r in (sc_ref["raw_table"].get("rows") or [])[:8]
            ]
            sc_context = (
                f"\nReference Standardized Quarterly Table (₹ Cr) for {ticker} (use to verify ₹ Cr unit scale "
                f"or compute QoQ % if the PDF press release only prints Q2 and H1 where Q1 = H1 - Q2):\n"
                f"{json.dumps(rows_preview, ensure_ascii=False)}\n"
            )

        prompt_pdf = f"""You are a financial analyst extracting headline quarterly results for {ticker}.
Filing title: {filing_title}

Extract the Consolidated financial numbers (or Standalone if Consolidated is not present) from the filing text below.
CRITICAL UNIT RULE: Convert all values to ₹ Crores (₹ Cr) consistently across all 4 rows (1 Billion = 100 Crores, 10 Million = 1 Crore, 100 Lakhs = 1 Crore).
BANK / NBFC RULE: For Banks or NBFCs (e.g. HDFCBANK, BAJFINANCE), use "Operating Profit before provisions and contingencies (PPOP)" for the "ebitda" field, and "Total Income" for "revenue".
PRESS RELEASE RULE: If the PDF reports Q2 and H1 (Half-Year) numbers without explicitly printing Q1, derive Q1 = H1 - Q2 (or use the Reference Standardized Quarterly Table below) to compute QoQ %; if PBT is not in the Press Release, use the Reference Table or derive it if available.
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
{sc_context}
Filing PDF Text:
{pdf_text[:30000]}
"""
        try:
            res, served_by = _call_gemini_json_with_model(
                prompt=prompt_pdf,
                model=MODEL_FLASH,
                workflow="push_enrich_results_pdf",
                ticker=ticker,
                db_path=db_path,
            )
            if isinstance(res, dict) and res.get("has_numbers") is True and _is_pdf_extraction_sane(res, sc_ref):
                res["source"] = "Exchange Filing PDF"
                res["model"] = served_by
                return _normalize_results_enrichment(res)
        except Exception as exc:
            print(f"[llm_router] PDF results extraction failed for {ticker}, using Screener table: {exc}")

    if not sc_ref:
        return {"has_numbers": False, "error": f"No usable quarterly table for {ticker}"}

    prompt_takeaway = f"""Write a 1-sentence institutional takeaway for {ticker}'s {sc_ref['period']} quarterly results (in ₹ Cr):
Metrics: {json.dumps(sc_ref['metrics'], ensure_ascii=False)}
Return JSON: {{"takeaway": "1 concise sentence highlighting Revenue & PAT QoQ/YoY growth and operating margin direction."}}"""
    takeaway = f"{ticker} reported {sc_ref['period']} Revenue of ₹{sc_ref['metrics']['revenue']['value']} Cr ({sc_ref['metrics']['revenue']['qoq_pct']} QoQ, {sc_ref['metrics']['revenue']['yoy_pct']} YoY) and PAT of ₹{sc_ref['metrics']['pat']['value']} Cr ({sc_ref['metrics']['pat']['qoq_pct']} QoQ, {sc_ref['metrics']['pat']['yoy_pct']} YoY)."
    takeaway_model: str | None = None  # None = numbers and takeaway are deterministic, no LLM involved
    try:
        t_res, served_by = _call_gemini_json_with_model(
            prompt=prompt_takeaway,
            model=MODEL_FLASH,
            workflow="push_enrich_results_fallback",
            ticker=ticker,
            db_path=db_path,
        )
        if isinstance(t_res, dict) and t_res.get("takeaway"):
            takeaway = t_res["takeaway"].strip()
            takeaway_model = served_by
    except Exception as exc:
        logger.warning("[llm] results takeaway for %s fell back to template: %s", ticker, _sanitize_log_message(str(exc))[:200])

    return _normalize_results_enrichment(
        {
            "has_numbers": True,
            "period": sc_ref["period"],
            "unit": "₹ Cr",
            "source": sc_ref["source"],
            "metrics": sc_ref["metrics"],
            "takeaway": takeaway,
            "model": takeaway_model,
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
    Tier 2 (MODEL_FLASH) + DuckDuckGo Search (ddgs):
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
    enriched, served_by = _call_gemini_json_with_model(
        prompt=bio_prompt,
        model=MODEL_FLASH,
        workflow="push_mgmt_bio",
        ticker=ticker,
        db_path=db_path,
    )
    if isinstance(enriched, list):
        enriched = enriched[0] if enriched and isinstance(enriched[0], dict) else {}
    return {**enriched, "model": served_by} if isinstance(enriched, dict) else {}


def call_gemini_pro_json(
    prompt: str,
    workflow: str = "pull_deep_synthesis",
    ticker: str | None = None,
    db_path: Path = DB_PATH,
) -> tuple[Any, str]:
    """
    Tier 3 (MODEL_PRO): Reserved for complex Pull multi-quarter reasoning ('Numbers vs. Narrative').
    Returns (report JSON, model that served it).
    """
    return _call_gemini_json_with_model(
        prompt=prompt,
        model=MODEL_PRO,
        workflow=workflow,
        ticker=ticker,
        temperature=0.15,
        db_path=db_path,
    )

