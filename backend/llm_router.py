import json
import logging
import os
import re
from datetime import datetime
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
from backend.scrapers.filings import enforce_filing_hygiene, is_bank_or_nbfc, is_vague_title

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


NO_DETAIL_SUMMARY = "Filing does not specify details; see attached PDF"
# ponytail: page 1 only, capped, so a 15-filing batch adds at most ~22k chars (~6k Flash-Lite tokens)
PDF_EXCERPT_CHARS = 1500


def _pdf_page1_excerpt(pdf_url: str) -> str:
    """First PDF_EXCERPT_CHARS of page 1, whitespace collapsed. Reuses backend/cache_pdfs/ via download_pdf."""
    if not pdf_url:
        return ""
    text = extract_pdf_text(pdf_url, max_pages=1, max_chars_per_page=PDF_EXCERPT_CHARS)
    text = re.sub(r"^\s*--- \[Page \d+ of \d+\] ---", "", text)
    return " ".join(text.split())[:PDF_EXCERPT_CHARS]


def classify_and_summarize_batch(
    filings: list[dict[str, Any]], db_path: Path = DB_PATH, batch_size: int = 15
) -> dict[str, dict[str, Any]]:
    """
    Tier 1 (MODEL_FLASH_LITE):
    Batch-classifies corporate announcements into the 17 categories, assigns materiality (0 or 1),
    and generates a sharp 1-line investor summary.
    Vague titles ("Change in Director(s)") carry the start of their PDF; with no PDF text they get NO_DETAIL_SUMMARY.
    Filings missing from the returned map were not triaged and must stay blank.
    """
    if not filings:
        return {}

    results: dict[str, dict[str, Any]] = {}
    for i in range(0, len(filings), batch_size):
        batch = filings[i : i + batch_size]
        payload = []
        for f in batch:
            entry = {
                "filing_id": f["filing_id"],
                "ticker": f["ticker"],
                "title": f["title"],
                "heuristic_category": f.get("category", "unclassified"),
            }
            if is_vague_title(f["title"]):
                excerpt = _pdf_page1_excerpt(f.get("pdf_url") or "")
                if not excerpt:
                    # Nothing to read beyond a generic title: keep the regex category, don't let the model guess
                    cat, mat = enforce_filing_hygiene(
                        f["title"], f.get("category") or "unclassified", int(f.get("materiality") or 0),
                        is_lender=is_bank_or_nbfc(f.get("company_name") or ""),
                    )
                    results[f["filing_id"]] = {"category": cat, "materiality": mat, "summary": NO_DETAIL_SUMMARY}
                    continue
                entry["pdf_page1_excerpt"] = excerpt
            payload.append(entry)
        if not payload:
            continue
        prompt = f"""You are a senior Indian public-markets equity analyst filtering NSE/BSE corporate filings.
For each filing in the input JSON array, classify it and return a JSON array of objects with exact keys:
- "filing_id": string (matching input)
- "category": one of {json.dumps(VALID_CATEGORIES)}
- "materiality": integer 1 or 0.
  Set materiality=1 ONLY if the event can materially impact stock valuation, operations, or governance (e.g. quarterly/annual financial results, M&A/divestment/stake sale, CEO/CFO/MD/Director appointment or resignation, fund raise, major capex, credit rating change, significant litigation/fine, dividend/bonus/split, insider trading, promoter pledging, or actual earnings call transcript/recording).
  Set materiality=0 for routine noise (newspaper advertisements, loss of share certificates, duplicate share issuance, ESOP allotments, trading window closures, Regulation 74(5) certificates, scheduled 1-on-1 investor/analyst meet notices, routine AGM/postal ballot notices).
- "summary": A crisp, no-fluff 1-line summary (max 18 words) stating WHAT happened and WHY an investor cares. No filler phrases like "The company informed the exchange that".
  Some filings carry "pdf_page1_excerpt", the start of the attached PDF. Use it to state the actual event (who, what role, amount, date).
  Only state facts present in the title or excerpt. Never guess ("likely concerning ...") and never write generic filler such as "impacts leadership and strategic direction".
  If neither the title nor the excerpt says what happened, the summary must be exactly: "{NO_DETAIL_SUMMARY}"

Input filings:
{json.dumps(payload, ensure_ascii=False)}
"""
        try:
            title_by_id = {e["filing_id"]: e["title"] for e in payload}
            lender_by_id = {f["filing_id"]: is_bank_or_nbfc(f.get("company_name") or "") for f in batch}
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
                summary = (item.get("summary") or "").strip()
                # No summary (or an id we didn't send) = not triaged; the row stays blank and is retried next refresh
                if fid not in title_by_id or not summary:
                    continue
                cat = item.get("category", "unclassified")
                if cat not in VALID_CATEGORIES:
                    cat = "unclassified"
                mat = 1 if int(item.get("materiality", 0)) == 1 else 0
                cat, mat = enforce_filing_hygiene(title_by_id[fid], cat, mat, is_lender=lender_by_id[fid])
                results[fid] = {"category": cat, "materiality": mat, "summary": summary}
        except Exception as exc:
            print(f"[llm_router] Batch triage error: {exc}")

    return results


def _parse_num(val: Any) -> float | None:
    if val is None:
        return None
    s = str(val).replace(",", "").replace("₹", "").replace("Cr", "").replace("%", "").strip()
    if not s or s.lower() in ("n/a", "-", "none", "null"):
        return None
    if s.startswith("(") and s.endswith(")"):  # accounting negative, e.g. "(793)"
        s = "-" + s[1:-1]
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
        return "n/a" if str(val).strip().lower() == "n/a" else "—"
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


# Results-card definitions, shared by the PDF path and the Screener path so both show the same numbers.
# They follow Screener's quarterly table:
# - Revenue = revenue from operations (banks: total interest earned). Other income is never revenue.
# - Non-financials: EBITDA = revenue - total expenses + finance costs + D&A, i.e. Screener's "Operating Profit".
#   Other income and exceptional items are excluded.
# - Banks/NBFCs: Financing Profit = revenue - interest expended - operating expenses (ex-D&A) - provisions,
#   i.e. Screener's "Financing Profit". It is after provisions, so it is NOT PPOP, and it excludes other income.
#   Ind AS NBFC tables (BAJFINANCE) carry finance costs and impairment inside total expenses, so there it is
#   revenue - total expenses + D&A. RBI-format bank tables report D&A inside operating expenses, and Screener
#   shows bank depreciation as 0, so nothing is added back there.
# - PAT = net profit for the period before the non-controlling-interest split (Screener's "Net Profit").
UNIT_TO_CR = {"cr": 1.0, "lakh": 0.01, "million": 0.1, "billion": 100.0, "thousand": 0.0001}
_BANK_KEYS = ("interest_earned", "other_income", "interest_expended", "operating_expenses", "provisions")
_STANDARD_KEYS = ("revenue_from_operations", "other_income", "total_expenses")

# Cross-check tolerance once the PDF and Screener are on the same quarter and basis. Q2 FY27 (Sep 2026) filings vs
# Screener, same definitions: CIPLA consolidated revenue 7,403.93 vs 7,404, EBITDA 1,387.78 vs 1,388, PBT 1,177.34
# vs 1,177, PAT 851.51 vs 852; BAJFINANCE consolidated revenue 24,546.37 vs 24,546, Financing Profit 8,825.09 vs
# 8,825, PBT 8,537.20 vs 8,537, PAT 6,366.57 vs 6,367. The worst gap is 0.06% (0.49 Cr), which is Screener rounding
# to whole crores. 1% (or 1 Cr on small numbers) still rejects the old definition errors: CIPLA EBITDA incl. other
# income +11%, BAJFINANCE PPOP vs Financing Profit +21%, CIPLA PAT attributable to owners +3%.
SCREENER_MATCH_REL_TOL = 0.01
SCREENER_MATCH_ABS_TOL_CR = 1.0

# Words a results takeaway may not use when PAT or the operating line fell QoQ or YoY
_HYPE_WORDS = re.compile(r"\b(strong|robust|stellar|impressive|healthy|solid|excellent)\b", re.IGNORECASE)


def _pct_change(curr: float | None, base: float | None) -> str:
    """'—' when a number is missing, 'n/a' when the base is zero or negative (a % off it would mislead)."""
    if curr is None or base is None:
        return "—"
    if base <= 0:
        return "n/a"
    return f"{(curr - base) / base * 100.0:+.1f}%"


def _metric(series: tuple[float | None, float | None, float | None]) -> dict[str, str]:
    curr, prev, year_ago = series
    return {
        "value": _format_inr(curr) if curr is not None else "—",
        "qoq_pct": _pct_change(curr, prev),
        "yoy_pct": _pct_change(curr, year_ago),
    }


def _operating_profit(items: dict[str, float], bank: bool, financial: bool) -> float | None:
    """EBITDA (non-financials) or Financing Profit (banks/NBFCs) from raw ₹ Cr line items, other income excluded."""
    if bank:
        parts = [items.get(k) for k in ("interest_earned", "interest_expended", "operating_expenses", "provisions")]
        return None if None in parts else parts[0] - parts[1] - parts[2] - parts[3]
    rev, total_exp, fin, da = (
        items.get(k) for k in ("revenue_from_operations", "total_expenses", "finance_costs", "depreciation_amortisation")
    )
    if rev is None or total_exp is None or da is None or (fin is None and not financial):
        return None
    return rev - total_exp + da + (0.0 if financial else fin)


def _signed_exceptional(items: dict[str, float], bank: bool) -> float | None:
    """
    Filings print exceptional items either signed or as a positive amount that is then subtracted
    (SUNPHARMA: 'PBT (V-VI)'). Pick the sign that reconciles profit before exceptional items to the printed PBT.
    ponytail: a share of associates booked above PBT shifts both candidates equally, so this only picks the wrong
    sign if that share is bigger than the exceptional item itself.
    """
    exc, pbt = items.get("exceptional_items"), items.get("pbt")
    vals = [items.get(k) for k in (_BANK_KEYS if bank else _STANDARD_KEYS)]
    if not exc or pbt is None or None in vals:
        return exc
    pre = vals[0] + vals[1] - sum(vals[2:])
    return -abs(exc) if abs(pre - abs(exc) - pbt) < abs(pre + abs(exc) - pbt) else abs(exc)


def _month_key(text: Any) -> int | None:
    """'Sep 2026' / 'Q2 FY27 (Sep 2026)' / 'September 2026' -> year*12 + month (same index as pull_engine)."""
    if isinstance(text, int):
        return text
    m = re.search(r"\b([A-Za-z]{3})[a-z]*\.?\s+(\d{4})\b", str(text or ""))
    if not m:
        return None
    try:
        d = datetime.strptime(f"{m.group(1).title()} {m.group(2)}", "%b %Y")
    except ValueError:
        return None
    return d.year * 12 + d.month


def _period_label(quarter_end: str) -> str:
    from backend.pull_engine import _fiscal_label  # lazy: pull_engine imports this module

    key = _month_key(quarter_end)
    return f"{_fiscal_label(key)} ({quarter_end})" if key and key % 3 == 0 else (quarter_end or "")


def _basis_note(basis: str, financial: bool) -> str:
    definition = "Financing Profit ex-other income, after provisions" if financial else "EBITDA ex-other income"
    return " • ".join(p for p in (basis, definition) if p)


def _results_from_pdf_items(
    raw: dict[str, Any], filing_title: str, is_financial: bool | None = None
) -> tuple[dict[str, Any], dict[str, tuple]] | None:
    """
    Turn the LLM's raw line items (3 quarter columns, printed unit) into card metrics. All arithmetic is here.
    `is_financial` comes from Screener when available (its table says "Financing Profit"); else the LLM's flag.
    Returns (result, raw ₹ Cr series per metric) or None when the unit is unknown.
    """
    from backend.pull_engine import _filing_quarter  # lazy: pull_engine imports this module

    unit = str(raw.get("unit_as_printed") or "").lower()
    factor = next((f for u, f in UNIT_TO_CR.items() if u in unit), None)
    cols = raw.get("columns") if isinstance(raw.get("columns"), dict) else {}
    if factor is None or not cols:
        return None
    per: list[dict[str, float]] = []
    for key in ("current", "previous", "year_ago"):
        col = cols.get(key) if isinstance(cols.get(key), dict) else {}
        per.append({k: n * factor for k, n in ((k, _parse_num(v)) for k, v in col.items()) if n is not None})

    bank = per[0].get("interest_earned") is not None
    financial = bank or (is_financial if is_financial is not None else bool(raw.get("is_bank_or_nbfc")))
    rev_key = "interest_earned" if bank else "revenue_from_operations"
    series = {
        "revenue": tuple(i.get(rev_key) for i in per),
        "ebitda": tuple(_operating_profit(i, bank, financial) for i in per),
        "pbt": tuple(i.get("pbt") for i in per),
        "pat": tuple(i.get("pat") for i in per),
    }
    q = _month_key(raw.get("quarter_end")) or _filing_quarter(filing_title)
    quarter_end = datetime((q - 1) // 12, (q - 1) % 12 + 1, 1).strftime("%b %Y") if q else ""
    basis_raw = str(raw.get("basis") or "").lower()
    basis = "Consolidated" if "consol" in basis_raw else "Standalone" if "standalone" in basis_raw else ""
    exc = _signed_exceptional(per[0], bank) or 0.0
    return (
        {
            "has_numbers": True,
            "quarter_end": quarter_end,
            "period": _period_label(quarter_end),
            "unit": "₹ Cr",
            "basis": basis,
            "is_financial": financial,
            "operating_label": "Financing Profit" if financial else "EBITDA",
            "basis_note": _basis_note(basis, financial),
            "exceptional_items_cr": round(exc, 2),
            "metrics": {k: _metric(v) for k, v in series.items()},
        },
        series,
    )


def _screener_gate(quarter: Any, basis: str | None, sc_ref: dict[str, Any] | None) -> str:
    """'match' only when Screener's latest column is the filing's quarter (and basis, when known)."""
    if not sc_ref:
        return "pdf_only_no_screener"
    pq, sq = _month_key(quarter), _month_key(sc_ref.get("quarter_end") or sc_ref.get("period"))
    if pq is None or sq is None:
        return "pdf_only_quarter_unknown"
    if pq > sq:
        return "pdf_only_screener_behind"
    if pq < sq:
        return "pdf_only_screener_ahead"
    if basis is not None and basis.lower() != (sc_ref.get("basis") or "").lower():
        return "pdf_only_basis_mismatch"
    return "match"


def _takeaway_ok(text: str, res: dict[str, Any], series: dict[str, tuple]) -> bool:
    """An LLM takeaway must not hype a falling quarter, must name exceptional items, and may only quote computed %s."""
    if not text:
        return False
    fell = any(
        s[0] is not None and base is not None and s[0] < base
        for s in (series.get("ebitda") or (None,) * 3, series.get("pat") or (None,) * 3)
        for base in s[1:]
    )
    if fell and _HYPE_WORDS.search(text):
        return False
    exc = res.get("exceptional_items_cr") or 0.0
    if abs(exc) >= 0.5 and not ("exceptional" in text.lower() and _format_inr(abs(exc)) in text):
        return False
    allowed = {p.lstrip("+-") for m in res["metrics"].values() for p in (m["qoq_pct"], m["yoy_pct"])}
    return all(f"{p}%" in allowed for p in re.findall(r"(\d+(?:\.\d+)?)\s*%", text))


def _deterministic_takeaway(ticker: str, res: dict[str, Any]) -> str:
    m = res["metrics"]

    def part(name: str, key: str) -> str:
        return f"{name} ₹{m[key]['value']} Cr ({m[key]['qoq_pct']} QoQ, {m[key]['yoy_pct']} YoY)"

    text = (
        f"{ticker} {res['period']}: {part('revenue', 'revenue')}, "
        f"{part(res.get('operating_label') or 'EBITDA', 'ebitda')}, {part('PAT', 'pat')}."
    )
    exc = res.get("exceptional_items_cr") or 0.0
    if abs(exc) >= 0.5:
        text += f" PAT includes an exceptional {'charge' if exc < 0 else 'gain'} of ₹{_format_inr(abs(exc))} Cr."
    return text


def _results_summary(res: dict[str, Any]) -> str:
    m = res["metrics"]
    basis = f" {res['basis'].lower()}" if res.get("basis") else ""
    return (
        f"{res['period']}{basis} results: revenue ₹{m['revenue']['value']} Cr "
        f"({m['revenue']['qoq_pct']} QoQ, {m['revenue']['yoy_pct']} YoY), PAT ₹{m['pat']['value']} Cr "
        f"({m['pat']['qoq_pct']} QoQ, {m['pat']['yoy_pct']} YoY)."
    )


def _results_takeaway(
    ticker: str, res: dict[str, Any], series: dict[str, tuple], db_path: Path = DB_PATH
) -> tuple[str, str | None]:
    """LLM takeaway from the computed numbers, kept only if `_takeaway_ok`; else a sentence built from the numbers."""
    label = res.get("operating_label") or "EBITDA"
    m = res["metrics"]
    exc = res.get("exceptional_items_cr") or 0.0
    exc_line = (
        f"Exceptional items this quarter: ₹{_format_inr(abs(exc))} Cr {'charge' if exc < 0 else 'gain'}. Mention them with this amount."
        if abs(exc) >= 0.5
        else "No exceptional items this quarter."
    )
    prompt = f"""Write a 1-sentence institutional takeaway for {ticker}'s {res['period']} {res.get('basis', '').lower()} results.
Use only these computed numbers (₹ Cr; {label} excludes other income):
{json.dumps({"Revenue": m["revenue"], label: m["ebitda"], "PBT": m["pbt"], "PAT": m["pat"]}, ensure_ascii=False)}
{exc_line}
Rules: quote percentages exactly as given; if PAT or {label} fell QoQ or YoY, do not call the quarter strong, robust, stellar, impressive, healthy, solid or excellent.
Return JSON: {{"takeaway": "..."}}"""
    try:
        t_res, served_by = _call_gemini_json_with_model(
            prompt=prompt, model=MODEL_FLASH, workflow="push_enrich_results_takeaway", ticker=ticker, db_path=db_path
        )
        text = (t_res.get("takeaway") or "").strip() if isinstance(t_res, dict) else ""
        if _takeaway_ok(text, res, series):
            return text, served_by
        logger.info("[llm] results takeaway for %s rejected by number checks, using template", ticker)
    except Exception as exc_:
        logger.warning("[llm] results takeaway for %s fell back to template: %s", ticker, _sanitize_log_message(str(exc_))[:200])
    return _deterministic_takeaway(ticker, res), None


def _finish_results(ticker: str, res: dict[str, Any], series: dict[str, tuple], db_path: Path) -> dict[str, Any]:
    res["takeaway"], res["takeaway_model"] = _results_takeaway(ticker, res, series, db_path)
    res["summary"] = _results_summary(res)
    return _normalize_results_enrichment(res)


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

    op_row = _find_row(("operating profit", "financing profit"))
    is_financial = bool(op_row) and op_row["metric"].lower().startswith("financing")
    cols = (q_curr, q_prev, q_yoy)
    series = {
        k: tuple(_parse_num(row.get(c)) if row and c else None for c in cols)
        for k, row in (
            ("revenue", _find_row(("sales", "revenue"))),
            ("ebitda", op_row),
            ("pbt", _find_row(("profit before tax",))),
            ("pat", _find_row(("net profit",))),
        )
    }
    # fetch_screener_fundamentals tries /consolidated/ first and only drops to standalone when it is empty
    basis = "Consolidated" if "/consolidated" in (sc_data.get("screener_url") or "") else "Standalone"
    return {
        "has_numbers": True,
        "quarter_end": q_curr,
        "period": _period_label(q_curr),
        "unit": "₹ Cr",
        "basis": basis,
        "is_financial": is_financial,
        "operating_label": "Financing Profit" if is_financial else "EBITDA",
        "basis_note": _basis_note(basis, is_financial),
        "source": f"Screener ({basis}, ₹ Cr)",
        "metrics": {k: _metric(v) for k, v in series.items()},
        "series": series,
        "raw_table": q_table,
    }


def _is_pdf_extraction_sane(pdf_res: dict[str, Any], sc_ref: dict[str, Any] | None) -> bool:
    """
    Decides whether to trust the PDF numbers and records why in pdf_res["verification_status"].
    1. Plausibility: revenue > 0, operating line and PAT present, PAT and (non-financial) EBITDA not above revenue.
    2. Screener cross-check, only when Screener's latest column is the filing's quarter on the same basis.
       Right after a filing Screener usually still shows the previous quarter: the PDF is kept as
       "pdf_only_screener_behind". On a match every metric must agree within SCREENER_MATCH_* tolerance.
    Returns False only for implausible numbers or a same-quarter mismatch with Screener.
    """
    m = pdf_res.get("metrics") or {}
    vals = {k: _parse_num((m.get(k) or {}).get("value")) for k in ("revenue", "ebitda", "pbt", "pat")}
    rev, op, pat = vals["revenue"], vals["ebitda"], vals["pat"]
    if (
        rev is None
        or rev <= 0
        or op is None
        or pat is None
        or abs(pat) / rev > 0.80
        or (not pdf_res.get("is_financial") and abs(op) / rev > 0.95)
    ):
        pdf_res["verification_status"] = "pdf_implausible"
        return False

    # A card without a basis (cached before basis was extracted) can't be compared like-for-like
    status = _screener_gate(pdf_res.get("quarter_end") or pdf_res.get("period"), pdf_res.get("basis") or "unknown", sc_ref)
    if status == "match":
        sc_m = sc_ref.get("metrics") or {}
        agrees = all(
            abs(v - sc_v) <= max(SCREENER_MATCH_ABS_TOL_CR, SCREENER_MATCH_REL_TOL * abs(sc_v))
            for k, v in vals.items()
            if v is not None and (sc_v := _parse_num((sc_m.get(k) or {}).get("value"))) is not None
        )
        status = "verified_screener" if agrees else "screener_mismatch"
    pdf_res["verification_status"] = status
    return status != "screener_mismatch"


def enrich_quarterly_results(
    ticker: str,
    filing_title: str,
    pdf_url: str = "",
    db_path: Path = DB_PATH,
) -> dict[str, Any]:
    """
    Tier 2 (MODEL_FLASH):
    The LLM only copies raw P&L line items from the filing's 3 quarter columns. Python converts units, computes
    Revenue / EBITDA (or Financing Profit) / PBT / PAT and every QoQ/YoY %, gates the Screener cross-check on
    quarter + basis (`_is_pdf_extraction_sane`), and checks the takeaway against the computed numbers.
    Screener's table is the fallback only when its latest column is this filing's quarter.
    """
    from backend.pull_engine import _filing_quarter  # lazy: pull_engine imports this module

    sc_ref: dict[str, Any] | None = None
    try:
        sc_ref = _compute_screener_quarterly_metrics(ticker)
    except Exception as exc:
        print(f"[llm_router] Screener reference fetch warning for {ticker}: {exc}")

    pdf_text = extract_results_pdf_text(pdf_url, max_pages=10) if pdf_url else ""
    pdf_res: dict[str, Any] | None = None

    if len(pdf_text.strip()) > 600:
        prompt_pdf = f"""You are copying raw line items from {ticker}'s quarterly financial results filing. Do NOT calculate anything: no EBITDA, no growth %, no unit conversion.
Filing title: {filing_title}

1. Use the Statement of Profit and Loss. Prefer the CONSOLIDATED statement when the filing has both; use Standalone only if there is no consolidated statement.
2. Use only the three quarter columns: "current" (latest quarter ended), "previous" (the quarter before it) and "year_ago" (same quarter last year). Ignore half-year, nine-month and full-year columns.
3. Copy each number exactly as printed, in the table's own unit. Numbers in brackets are negative. Use 0 for a line printed as "-" or blank, and null only when the line does not exist.
4. Pick the format from the statement's first income line. If it is "Revenue from operations", use the standard keys, even for NBFCs whose revenue lists "Interest income" (e.g. Bajaj Finance). Use the bank keys only when it is "Interest earned".
   Standard / NBFC format (Revenue from operations ... Total expenses) items:
   revenue_from_operations (the "Total revenue from operations" line including other operating revenues, not a sub-line such as "Revenue from contracts with customers" or "Interest income"; excluding other income), other_income,
   total_expenses (the "Total expenses" line), finance_costs, depreciation_amortisation,
   exceptional_items (signed effect on profit: negative for a charge/loss), pbt (profit before tax, after exceptional items),
   pat (net profit for the period after tax and share of associates/JVs, BEFORE the non-controlling-interest split, not "attributable to owners").
   Bank format (Interest earned ... Interest expended ... Operating expenses ... Provisions and contingencies) items:
   interest_earned (total), other_income, interest_expended, operating_expenses (total), provisions (other than tax), exceptional_items, pbt, pat.
Return a JSON object:
{{
  "has_numbers": true only if current-quarter revenue and PAT are in the text,
  "basis": "Consolidated" or "Standalone",
  "is_bank_or_nbfc": boolean,
  "unit_as_printed": one of "crore", "lakh", "million", "billion", "thousand",
  "quarter_end": month and year the current column ends, e.g. "Sep 2026",
  "columns": {{"current": {{item: number}}, "previous": {{item: number}}, "year_ago": {{item: number}}}}
}}

Filing PDF Text:
{pdf_text[:30000]}
"""
        try:
            raw, served_by = _call_gemini_json_with_model(
                prompt=prompt_pdf,
                model=MODEL_FLASH,
                workflow="push_enrich_results_pdf",
                ticker=ticker,
                db_path=db_path,
            )
            built = (
                _results_from_pdf_items(raw, filing_title, sc_ref.get("is_financial") if sc_ref else None)
                if isinstance(raw, dict) and raw.get("has_numbers") is True
                else None
            )
            if built:
                pdf_res, series = built
                if _is_pdf_extraction_sane(pdf_res, sc_ref):
                    pdf_res.update(source="Exchange Filing PDF", model=served_by)
                    return _finish_results(ticker, pdf_res, series, db_path)
        except Exception as exc:
            print(f"[llm_router] PDF results extraction failed for {ticker}: {exc}")

    if not sc_ref:
        return {"has_numbers": False, "error": f"No usable quarterly table for {ticker}"}
    # Screener's numbers only stand in for this filing when they are for the same quarter (and basis, if known)
    quarter = (pdf_res or {}).get("quarter_end") or _filing_quarter(filing_title)
    gate = _screener_gate(quarter, (pdf_res or {}).get("basis") or None, sc_ref)
    if gate != "match":
        return {
            "has_numbers": False,
            "verification_status": gate,
            "error": f"PDF numbers unusable and Screener's latest quarter ({sc_ref['quarter_end']}) is not this filing's",
        }
    res = {k: sc_ref[k] for k in ("has_numbers", "quarter_end", "period", "unit", "basis", "is_financial",
                                  "operating_label", "basis_note", "source", "metrics")}
    res["verification_status"] = "screener_fallback"
    # ponytail: Screener has no exceptional-items row, so the filing's own (same quarter + basis) line is used even
    # when its other numbers failed the cross-check. A misread exceptional line would show up in the takeaway only.
    res["exceptional_items_cr"] = (pdf_res or {}).get("exceptional_items_cr") or 0.0
    out = _finish_results(ticker, res, sc_ref["series"], db_path)
    out["model"] = out.get("takeaway_model")  # None = numbers and takeaway are deterministic, no LLM involved
    return out


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


# Praise / impact words a management-change bio may not use (the TRENT card said "exemplary leadership ... significant growth")
_PRAISE_WORDS = re.compile(
    r"\b(exemplary|visionary|veteran|seasoned|experienced|extensive|distinguished|eminent|illustrious|renowned|"
    r"stellar|outstanding|remarkable|exceptional|strong|robust|significant(ly)?|instrumental|credited|pivotal|"
    r"dynamic|accomplished|esteemed|rich experience)\b",
    re.IGNORECASE,
)


def _factual_mgmt_bio(info: dict[str, Any]) -> str:
    """'Noel N Tata: Retirement as Chairman, effective 11th November 2026.' from the extracted fields only."""
    who = (info.get("person_name") or "Unknown").strip()
    action = (info.get("action") or "Management change").strip()
    role = (info.get("role") or "").strip()
    date = (info.get("effective_date") or "").strip()
    return f"{who}: {action}{f' as {role}' if role else ''}{f', effective {date}' if date else ''}."


_TITLE_PERSON_RE = re.compile(r"\b(?:Mr|Mrs|Ms|Miss|Dr|Shri|Smt|Prof)\.?\s+((?:[A-Z][\w.'’]*\s?)+)")


def _title_person_info(filing_title: str) -> dict[str, str]:
    """Facts from a title that names someone, e.g. 'Appointment of Mr Rajiv Dube as Non- Executive Director ... w.e.f. October 30, 2026.'"""
    # skip "Dr. Reddy's Laboratories Limited": a company, not a person
    m = next((m for m in _TITLE_PERSON_RE.finditer(filing_title or "") if not re.search(r"\b(Limited|Ltd)\b", m.group(1))), None)
    if not m:
        return {}
    rest = re.sub(r"-\s+", "-", filing_title[m.end():])
    role = re.match(r"\s*(?:-\s*)?as (?:an? |the )?(.+?)(?= of the [Cc]ompany| w\.e\.f| on |[,(]|\.?$)", rest)
    date = re.search(r"w\.e\.f\.?\s*([A-Z][a-z]+ \d{1,2}, \d{4})", filing_title)
    prefix = filing_title.split(":", 1)[0].strip()
    return {
        "person_name": m.group(1).strip(),
        "role": role.group(1).strip() if role else "",
        "action": prefix if ":" in filing_title and len(prefix.split()) <= 2 else "Change in Directorate",
        "effective_date": date.group(1) if date else "",
        "filing_profile_notes": "",
    }


def _title_person_mismatch(filing_title: str, person_name: str) -> dict[str, str]:
    """The title's facts if the title names someone whose surname is not in `person_name`, else {}."""
    titled = _title_person_info(filing_title)
    if titled and titled["person_name"].split()[-1].strip(".").lower() not in (person_name or "").lower():
        return titled
    return {}


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
If the Filing Title names a person, that person is the subject: report only their change, even when the filing text describes other directors first.
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
    if titled := _title_person_mismatch(filing_title, info.get("person_name") or ""):
        # A board-outcome PDF names several directors; the extract picked someone other than the title's person
        logger.info("[llm] %s mgmt extract named %r, title names %r: using the title", ticker, info.get("person_name"), titled["person_name"])
        info = titled
    person_name = (info.get("person_name") or "Unknown").strip()
    role = (info.get("role") or "").strip()
    action = (info.get("action") or "Management Change").strip()

    web_results = _search_executive_web(person_name, company_name or ticker)

    bio_prompt = f"""You are preparing an executive briefing card for an institutional investor on a management change at {company_name} ({ticker}).

Extracted Filing Info:
{json.dumps(info, ensure_ascii=False)}

Web Search Snippets (DuckDuckGo):
{json.dumps(web_results, ensure_ascii=False)}

Write a plain factual record of the change, like a filing register entry.
Rules:
- State only facts given in the Extracted Filing Info: who, their role, what changed, the effective date, and the stated reason or tenure. Add prior roles, employers or education only if the filing info or a snippet clearly about this same person names them. Never guess.
- No praise, judgment or impact claims: do not rate their leadership or credit them with growth. Never use words such as exemplary, visionary, veteran, seasoned, experienced, extensive, distinguished, eminent, strong, significant, instrumental or "credited with".
Return a JSON object with:
- "person_name": "{person_name}"
- "role": "{role}"
- "action": "{action}"
- "effective_date": "{info.get('effective_date', '')}"
- "two_line_bio": At most 2 short sentences following the rules above.
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
    if not isinstance(enriched, dict):
        return {}
    if _PRAISE_WORDS.search(enriched.get("two_line_bio") or ""):
        # The model ignored the rules: keep only the extracted facts
        logger.info("[llm] management bio for %s had praise words, using the filing facts", ticker)
        enriched["two_line_bio"] = _factual_mgmt_bio({**info, **enriched})
    return {**enriched, "model": served_by}


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

