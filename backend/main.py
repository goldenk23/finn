import hmac
import json
import logging
import os
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.llm_router import _sanitize_log_message  # shared with the LLM fallback warnings

logger = logging.getLogger("finn.api")


def _friendly_upstream_error(context: str, exc: Exception) -> HTTPException:
    """
    Logs technical details server-side (without secrets) and returns a short,
    helpful user-facing HTTPException for the mobile UI.
    """
    raw = _sanitize_log_message(f"{type(exc).__name__}: {exc}")
    logger.error("[%s] Upstream failure: %s", context, raw)

    low = raw.lower()
    if isinstance(exc, ValueError) and "could not find ticker" in low:
        return HTTPException(
            status_code=404,
            detail=str(exc),
        )
    if any(
        k in low
        for k in (
            "winerror 10013",
            "newconnectionerror",
            "failed to establish a new connection",
            "connecterror",
            "nameresolutionerror",
            "connection refused",
        )
    ):
        return HTTPException(
            status_code=503,
            detail="Cannot reach Google AI service (outbound HTTPS socket blocked). Ensure the FastAPI backend is running in a normal network-authorized terminal.",
        )
    if any(
        k in low
        for k in (
            "defaultcredentialserror",
            "refresherror",
            "unauthenticated",
            "permission_denied",
            "api_key_invalid",
            "neither gemini_api_key nor google_cloud_project",
        )
    ):
        return HTTPException(
            status_code=503,
            detail="AI authentication failed. Verify GEMINI_API_KEY or Vertex AI credentials (gcloud auth application-default login).",
        )
    if "429" in low or "resource_exhausted" in low or "quota" in low:
        return HTTPException(
            status_code=429,
            detail="AI rate limit reached. Please wait a moment and try again.",
        )
    return HTTPException(
        status_code=502,
        detail=f"{context} is temporarily unavailable. Check backend logs for details.",
    )

from backend.db import init_db, get_conn, get_portfolio, get_alert_rules, normalize_ticker, DB_PATH
from backend.scrapers.market_data import fetch_eod_metrics
from backend.scrapers.filings import fetch_company_filings
from backend.scrapers.screener import fetch_screener_fundamentals
from backend.push_engine import (
    refresh_portfolio_push_data,
    get_windowed_digest,
    get_triggered_alerts,
    enrich_single_filing,
)
from backend.pull_engine import get_or_generate_pull_report, ask_pull_copilot
from backend.llm_router import get_cost_summary


def _ticker_or_400(raw: str) -> str:
    try:
        return normalize_ticker(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# --- Paid-endpoint protection: daily budget cap, per-IP rate limit, optional admin token ---

# (max paid requests per client IP per rolling hour, label shown to the user)
RATE_LIMITS: dict[str, tuple[int, str]] = {
    "pull": (6, "new Pull reports"),
    "ask": (30, "Copilot questions"),
    "push": (10, "Push refresh/enrich requests"),
}
_RATE_WINDOW_S = 3600.0
# ponytail: in-memory and per-process. Counts reset on restart and aren't shared across uvicorn workers or
# container instances, so deploy a single instance (e.g. Cloud Run --max-instances=1). Upgrade path: Redis INCR+EXPIRE.
_rate_hits: dict[str, deque[float]] = defaultdict(deque)
_rate_lock = threading.Lock()


def _rate_limit_retry_after(key: str, limit: int, now: float | None = None) -> int:
    """Record a hit for `key`. Returns 0 if allowed, else seconds until the oldest hit leaves the window."""
    now = time.monotonic() if now is None else now
    with _rate_lock:
        hits = _rate_hits[key]
        while hits and now - hits[0] >= _RATE_WINDOW_S:
            hits.popleft()
        if len(hits) >= limit:
            return int(_RATE_WINDOW_S - (now - hits[0])) + 1
        hits.append(now)
        return 0


def _client_ip(request: Request) -> str:
    """Behind a platform load balancer the socket peer is the proxy. Each trusted proxy appends the address it saw
    to X-Forwarded-For, so with FINN_PROXY_HOPS=N the real client is the Nth entry from the right.
    Entries further left are client-supplied and spoofable, so they are never used."""
    hops = int(os.getenv("FINN_PROXY_HOPS", "0") or 0)
    if hops > 0:
        parts = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
        if len(parts) >= hops:
            return parts[-hops]
    return request.client.host if request.client else "unknown"


def _spent_today_usd(db_path: Path = DB_PATH) -> float:
    # Ledger timestamps are SQLite datetime('now') = UTC, so "today" resets at 00:00 UTC.
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_cost_ledger WHERE created_at >= date('now')"
        ).fetchone()
    return float(row[0])


def _enforce_daily_budget(db_path: Path = DB_PATH) -> None:
    # ponytail: check-then-spend, so concurrent in-flight requests can overshoot by roughly one cold Pull (~$0.07) each.
    budget = float(os.getenv("FINN_DAILY_BUDGET_USD") or "5.00")
    spent = _spent_today_usd(db_path)
    if spent >= budget:
        raise HTTPException(
            status_code=429,
            detail=(
                f"FINN's daily AI budget (${budget:.2f}) is used up. It resets at 00:00 UTC (05:30 IST). "
                "Cached Pull reports and filings still work."
            ),
        )


def _guard_paid_call(request: Request, bucket: str) -> None:
    _enforce_daily_budget()
    limit, label = RATE_LIMITS[bucket]
    wait = _rate_limit_retry_after(f"{bucket}:{_client_ip(request)}", limit)
    if wait:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit reached: up to {limit} {label} per hour per visitor. Try again in {wait // 60 + 1} min.",
            headers={"Retry-After": str(wait)},
        )


def _require_admin(request: Request) -> None:
    """If FINN_ADMIN_TOKEN is set, require it as the X-Admin-Token header (or ?admin_token=, which ends up in access logs)."""
    expected = os.getenv("FINN_ADMIN_TOKEN", "").strip()
    if not expected:
        return  # local dev: no token configured
    given = request.headers.get("x-admin-token") or request.query_params.get("admin_token") or ""
    if not hmac.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="This action is restricted to the FINN admin (X-Admin-Token required).")


def _is_pull_cached(ticker: str) -> bool:
    with get_conn() as conn:
        return conn.execute("SELECT 1 FROM pull_reports_cache WHERE ticker = ?", (ticker,)).fetchone() is not None


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="FINN — Financial Investment Agent API",
    description="Push (proactive digest & configurable threshold alerts) + Pull (primary-source deep equity research)",
    version="1.0.0",
    lifespan=lifespan,
)

# The single-origin deploy needs no CORS. FINN_CORS_ORIGINS (comma-separated) is only for a separately hosted frontend.
# No cookies or auth headers are sent cross-origin, so credentials stay off (also required with "*").
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.getenv("FINN_CORS_ORIGINS", "*").split(",") if o.strip()],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "X-Admin-Token"],
)


class AlertRulesUpdate(BaseModel):
    price_move_pct: float
    volume_multiplier: float
    watched_categories: list[str]


class RefreshRequest(BaseModel):
    # Bounded so a single request can't fan out into a large paid batch.
    tickers: list[str] | None = Field(None, max_length=30)
    days: int = Field(14, ge=1, le=30)
    max_enrich: int = Field(5, ge=0, le=6)


class AddPortfolioTickerRequest(BaseModel):
    ticker: str
    bse_code: str = ""
    company_name: str = ""
    sector: str = "Equities"
    cap_tier: str = "Listed"


class AskCopilotRequest(BaseModel):
    question: str = Field(..., max_length=1000)


@app.get("/api/health")
def health_check() -> dict[str, Any]:
    return {"status": "ok", "service": "FINN Backend"}


@app.get("/api/portfolio")
def api_get_portfolio() -> dict[str, Any]:
    rules = get_alert_rules()
    price_thresh = float(rules.get("price_move_pct", 3.0))
    vol_thresh = float(rules.get("volume_multiplier", 2.0))

    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT p.*,
                   e.trade_date,
                   e.close_price,
                   e.prev_close,
                   e.pct_change,
                   e.volume,
                   e.avg_volume_20d,
                   e.volume_ratio,
                   (SELECT COUNT(*) FROM announcements a
                    WHERE a.ticker = p.ticker AND a.materiality = 1) as material_filings_count,
                   (SELECT COUNT(*) FROM pull_reports_cache c
                    WHERE c.ticker = p.ticker) as has_cached_pull
            FROM portfolio p
            LEFT JOIN eod_prices e
              ON p.ticker = e.ticker
             AND e.trade_date = (SELECT MAX(trade_date) FROM eod_prices WHERE ticker = p.ticker)
            ORDER BY p.ticker ASC
            """
        ).fetchall()

    items: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        pct = float(d["pct_change"]) if d["pct_change"] is not None else 0.0
        vratio = float(d["volume_ratio"]) if d["volume_ratio"] is not None else 1.0
        d["price_alert"] = abs(pct) >= price_thresh if d["close_price"] is not None else False
        d["volume_alert"] = vratio >= vol_thresh if d["close_price"] is not None else False
        d["has_cached_pull"] = bool(d.get("has_cached_pull", 0))
        items.append(d)

    return {"portfolio": items, "count": len(items), "alert_rules": rules}


@app.post("/api/portfolio")
def api_add_portfolio_stock(req: AddPortfolioTickerRequest) -> dict[str, Any]:
    clean = _ticker_or_400(req.ticker)

    # Look the symbol up on Screener so we store the real company name and BSE code, not the raw ticker.
    company_name, bse_code = req.company_name.strip(), req.bse_code.strip()
    try:
        info = fetch_screener_fundamentals(clean)
        company_name = info.get("company_name") or company_name
        bse_code = bse_code or info.get("bse_code", "")
    except ValueError:
        raise HTTPException(status_code=404, detail=f"{clean} was not found on Screener.in. Check the NSE symbol.")
    except Exception as exc:
        # Screener unreachable: still add the stock rather than block the user on a transient outage.
        logger.warning("Screener lookup failed for %s: %s", clean, _sanitize_log_message(str(exc)))

    with get_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO portfolio (ticker, bse_code, company_name, sector, cap_tier)
            VALUES (?, ?, ?, ?, ?)
            """,
            (clean, bse_code, company_name or clean, req.sector.strip(), req.cap_tier.strip()),
        )

    eod = fetch_eod_metrics([clean], save_to_db=True)
    filings = fetch_company_filings(clean, bse_code=bse_code, days=14, save_to_db=True)
    return {
        "status": "added",
        "ticker": clean,
        "company_name": company_name or clean,
        "bse_code": bse_code,
        "eod": eod,
        "filings_fetched": len(filings),
    }


@app.delete("/api/portfolio/{ticker}")
def api_remove_portfolio_stock(ticker: str) -> dict[str, Any]:
    clean = _ticker_or_400(ticker)
    with get_conn() as conn:
        conn.execute("DELETE FROM portfolio WHERE ticker = ?", (clean,))
    return {"status": "removed", "ticker": clean}


@app.get("/api/push/digest")
def api_get_digest(
    window: str = Query("7d", description="Time window: 24h, 48h, 7d, 14d, 30d"),
    ticker: str | None = Query(None, description="Optional single ticker filter"),
) -> dict[str, Any]:
    tickers = [_ticker_or_400(ticker)] if ticker else None
    return get_windowed_digest(window=window, tickers=tickers)


@app.post("/api/push/refresh")
def api_refresh_push(req: RefreshRequest, request: Request) -> dict[str, Any]:
    _require_admin(request)
    _guard_paid_call(request, "push")
    stats = refresh_portfolio_push_data(
        tickers=[_ticker_or_400(t) for t in req.tickers] if req.tickers else None,
        days=req.days,
        max_enrich=req.max_enrich,
    )
    return stats


@app.post("/api/push/enrich/{filing_id}")
def api_enrich_filing(filing_id: str, request: Request) -> dict[str, Any]:
    _require_admin(request)
    _guard_paid_call(request, "push")
    enriched = enrich_single_filing(filing_id)
    if not enriched:
        raise HTTPException(status_code=404, detail="Filing not found or not applicable for deep enrichment")
    return {"filing_id": filing_id, "enriched": enriched}


@app.get("/api/push/alerts")
def api_get_alerts(
    window: str = Query("14d", description="Time window for material filing alerts"),
    ticker: str | None = Query(None),
) -> dict[str, Any]:
    tickers = [_ticker_or_400(ticker)] if ticker else None
    return get_triggered_alerts(window=window, tickers=tickers)


@app.put("/api/push/alerts/rules")
def api_update_alert_rules(req: AlertRulesUpdate) -> dict[str, Any]:
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE alert_rules
            SET price_move_pct = ?,
                volume_multiplier = ?,
                watched_categories = ?,
                updated_at = datetime('now')
            WHERE id = 1
            """,
            (
                float(req.price_move_pct),
                float(req.volume_multiplier),
                json.dumps(req.watched_categories),
            ),
        )
    return get_alert_rules()


@app.get("/api/pull-cached")
def api_list_cached_pull_reports() -> dict[str, Any]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT c.ticker, c.quarter_key, c.tokens_in, c.tokens_out, c.cost_usd, c.created_at,
                   p.company_name, p.sector
            FROM pull_reports_cache c
            LEFT JOIN portfolio p ON c.ticker = p.ticker
            ORDER BY c.created_at DESC
            """
        ).fetchall()
    return {"cached_reports": [dict(r) for r in rows]}


@app.get("/api/pull/{ticker}")
def api_get_pull_report(
    request: Request,
    ticker: str,
    force: bool = Query(False, description="Force fresh scrape and Gemini synthesis"),
) -> dict[str, Any]:
    ticker = _ticker_or_400(ticker)
    if force:
        _require_admin(request)  # visitors may generate cold reports, but not wipe cached ones
    if force or not _is_pull_cached(ticker):
        _guard_paid_call(request, "pull")  # cached reads cost $0 and skip the guard
    try:
        data = get_or_generate_pull_report(ticker=ticker, force_refresh=force)
        # Attach latest EOD price row if available
        clean = data["ticker"]
        with get_conn() as conn:
            eod_row = conn.execute(
                "SELECT * FROM eod_prices WHERE ticker = ? ORDER BY trade_date DESC LIMIT 1",
                (clean,),
            ).fetchone()
        if not eod_row:
            eod_list = fetch_eod_metrics([clean], save_to_db=True)
            data["eod"] = eod_list[0] if eod_list else None
        else:
            data["eod"] = dict(eod_row)
        return data
    except HTTPException:
        raise
    except Exception as exc:
        raise _friendly_upstream_error(f"Pull research ({ticker})", exc)


@app.post("/api/pull/{ticker}/ask")
def api_ask_pull_copilot(ticker: str, req: AskCopilotRequest, request: Request) -> dict[str, Any]:
    ticker = _ticker_or_400(ticker)
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty")
    if not _is_pull_cached(ticker):
        _guard_paid_call(request, "pull")  # asking about an uncached ticker builds a full report first
    _guard_paid_call(request, "ask")
    try:
        return ask_pull_copilot(ticker=ticker, question=req.question.strip())
    except HTTPException:
        raise
    except Exception as exc:
        raise _friendly_upstream_error(f"Copilot Q&A ({ticker})", exc)


@app.get("/api/cost")
def api_get_cost_ledger() -> dict[str, Any]:
    return get_cost_summary()



# Single-origin deploy: serve the static Next.js export (FINN_STATIC_EXPORT=1 next build -> frontend/out).
# Mounted last so every /api route above takes precedence.
_STATIC_DIR = Path(__file__).resolve().parent.parent / "frontend" / "out"
if _STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="frontend")
