import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from backend.db import get_conn, get_portfolio, get_alert_rules, DB_PATH
from backend.scrapers.market_data import fetch_eod_metrics
from backend.scrapers.filings import fetch_company_filings
from backend.llm_router import (
    classify_and_summarize_batch,
    enrich_quarterly_results,
    enrich_management_change,
)

WINDOW_HOURS = {
    "24h": 24,
    "48h": 48,
    "7d": 24 * 7,
    "14d": 24 * 14,
    "30d": 24 * 30,
}

CATEGORY_PRIORITY = {
    "results": 1,
    "M&A": 2,
    "management_change": 3,
    "fund_raise": 4,
    "litigation": 5,
    "capex": 6,
    "credit_rating": 7,
    "dividend": 8,
    "bonus_split": 9,
    "insider_trading": 10,
    "pledging": 11,
    "press_release": 12,
    "earnings_call": 13,
    "investor_meetings": 14,
    "agm_egm": 15,
    "postal_ballot": 16,
    "unclassified": 17,
}


def enrich_single_filing(filing_id: str, db_path: Path = DB_PATH) -> dict[str, Any]:
    """
    Run deep Tier-2 enrichment (gemini-2.5-flash) on a specific announcement
    if it is 'results' or 'management_change', and persist to SQLite.
    """
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM announcements WHERE filing_id = ?", (filing_id,)).fetchone()
        if not row:
            return {}
        filing = dict(row)

    cat = filing["category"]
    enriched: dict[str, Any] = {}

    if cat == "results":
        enriched = enrich_quarterly_results(
            ticker=filing["ticker"],
            filing_title=filing["title"],
            pdf_url=filing.get("pdf_url") or "",
            db_path=db_path,
        )
    elif cat == "management_change":
        enriched = enrich_management_change(
            ticker=filing["ticker"],
            company_name=filing.get("company_name") or filing["ticker"],
            filing_title=filing["title"],
            pdf_url=filing.get("pdf_url") or "",
            db_path=db_path,
        )

    if enriched:
        with get_conn(db_path) as conn:
            conn.execute(
                "UPDATE announcements SET enriched_json = ? WHERE filing_id = ?",
                (json.dumps(enriched, ensure_ascii=False), filing_id),
            )
    return enriched


def refresh_portfolio_push_data(
    tickers: list[str] | None = None,
    days: int = 10,
    max_enrich: int = 6,
    db_path: Path = DB_PATH,
) -> dict[str, Any]:
    """
    End-to-end Push pipeline:
    1. Refresh EOD price & volume metrics for portfolio stocks.
    2. Scrape new NSE/BSE filings for portfolio stocks.
    3. Batch-classify & 1-line summarize untriaged filings via gemini-2.5-flash-lite.
    4. Enrich material 'results' (QoQ/YoY headline table) & 'management_change' (2-line exec bio) via gemini-2.5-flash.
    """
    portfolio = get_portfolio(db_path)
    if tickers:
        wanted = {t.upper().strip() for t in tickers}
        portfolio = [p for p in portfolio if p["ticker"] in wanted]

    ticker_list = [p["ticker"] for p in portfolio]
    eod_metrics = fetch_eod_metrics(ticker_list, db_path=db_path, save_to_db=True)

    scraped_count = 0
    for p in portfolio:
        items = fetch_company_filings(p["ticker"], bse_code=p["bse_code"], days=days, db_path=db_path, save_to_db=True)
        scraped_count += len(items)

    # Find filings that need LLM triage (summary is NULL or empty)
    with get_conn(db_path) as conn:
        if ticker_list:
            placeholders = ",".join("?" for _ in ticker_list)
            untriaged_rows = conn.execute(
                f"""
                SELECT * FROM announcements
                WHERE (summary IS NULL OR summary = '')
                  AND ticker IN ({placeholders})
                ORDER BY published_at DESC
                LIMIT 60
                """,
                ticker_list,
            ).fetchall()
        else:
            untriaged_rows = []
        untriaged = [dict(r) for r in untriaged_rows]

    triaged_map = classify_and_summarize_batch(untriaged, db_path=db_path) if untriaged else {}

    if triaged_map:
        with get_conn(db_path) as conn:
            for fid, tdata in triaged_map.items():
                conn.execute(
                    """
                    UPDATE announcements
                    SET category = ?, materiality = ?, summary = ?
                    WHERE filing_id = ?
                    """,
                    (tdata["category"], tdata["materiality"], tdata["summary"], fid),
                )

    # Enrich material 'results' and 'management_change' filings that haven't been enriched yet
    with get_conn(db_path) as conn:
        if ticker_list:
            placeholders = ",".join("?" for _ in ticker_list)
            to_enrich_rows = conn.execute(
                f"""
                SELECT * FROM announcements
                WHERE materiality = 1
                  AND category IN ('results', 'management_change')
                  AND (enriched_json IS NULL OR enriched_json = '{{}}' OR enriched_json = '')
                  AND ticker IN ({placeholders})
                ORDER BY published_at DESC
                LIMIT ?
                """,
                (*ticker_list, max_enrich),
            ).fetchall()
        else:
            to_enrich_rows = []

    enriched_count = 0
    for row in to_enrich_rows:
        enriched_data = enrich_single_filing(row["filing_id"], db_path=db_path)
        if enriched_data:
            enriched_count += 1

    return {
        "stocks_refreshed": len(ticker_list),
        "eod_updated": len(eod_metrics),
        "filings_checked": scraped_count,
        "filings_triaged_by_llm": len(triaged_map),
        "filings_deep_enriched": enriched_count,
    }


def _format_announcement_row(row: Any) -> dict[str, Any]:
    d = dict(row)
    try:
        d["enriched"] = json.loads(d.get("enriched_json") or "{}")
    except Exception:
        d["enriched"] = {}
    d.pop("enriched_json", None)
    d["priority_rank"] = CATEGORY_PRIORITY.get(d.get("category", "unclassified"), 17)
    return d


def get_windowed_digest(
    window: str = "24h",
    tickers: list[str] | None = None,
    db_path: Path = DB_PATH,
) -> dict[str, Any]:
    """
    Returns corporate announcements within the chosen time window (24h, 7d, 30d),
    split into prioritized Material items and collapsed Routine items.
    """
    hours = WINDOW_HOURS.get(window, 24)
    cutoff_str = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")

    with get_conn(db_path) as conn:
        if tickers:
            clean = [t.upper().strip() for t in tickers]
            placeholders = ",".join("?" for _ in clean)
            rows = conn.execute(
                f"""
                SELECT * FROM announcements
                WHERE published_at >= ?
                  AND ticker IN ({placeholders})
                ORDER BY published_at DESC
                """,
                (cutoff_str, *clean),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM announcements
                WHERE published_at >= ?
                ORDER BY published_at DESC
                """,
                (cutoff_str,),
            ).fetchall()

    all_items = [_format_announcement_row(r) for r in rows]
    material = [x for x in all_items if x["materiality"] == 1]
    routine = [x for x in all_items if x["materiality"] == 0]

    # Sort material filings by priority category first, then newest timestamp
    material.sort(key=lambda x: (x["priority_rank"], -_ts_epoch(x["published_at"])))

    total = len(all_items)
    noise_pct = round((len(routine) / total) * 100, 1) if total > 0 else 0.0

    return {
        "window": window,
        "cutoff_timestamp": cutoff_str,
        "stats": {
            "total_filings": total,
            "material_count": len(material),
            "routine_collapsed_count": len(routine),
            "noise_filtered_pct": noise_pct,
        },
        "material": material,
        "routine": routine,
    }


def _ts_epoch(ts: str) -> float:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(ts[:19], fmt).timestamp()
        except ValueError:
            continue
    return 0.0


def get_triggered_alerts(
    window: str = "7d",
    tickers: list[str] | None = None,
    db_path: Path = DB_PATH,
) -> dict[str, Any]:
    """
    Evaluates both Market EOD anomalies (price move >= ±X% or volume >= Yx 20d avg)
    and Material Filing alerts matching the user's watched_categories.
    """
    rules = get_alert_rules(db_path)
    price_thresh = float(rules.get("price_move_pct", 3.0))
    vol_thresh = float(rules.get("volume_multiplier", 2.0))
    watched_cats = set(rules.get("watched_categories", []))

    hours = WINDOW_HOURS.get(window, 24 * 7)
    cutoff_str = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")

    with get_conn(db_path) as conn:
        # Latest EOD price row per ticker
        eod_rows = conn.execute(
            """
            SELECT e.*, p.company_name, p.sector
            FROM eod_prices e
            LEFT JOIN portfolio p ON e.ticker = p.ticker
            WHERE (e.ticker, e.trade_date) IN (
                SELECT ticker, MAX(trade_date) FROM eod_prices GROUP BY ticker
            )
            ORDER BY ABS(e.pct_change) DESC
            """
        ).fetchall()

        ann_rows = conn.execute(
            """
            SELECT * FROM announcements
            WHERE published_at >= ?
              AND materiality = 1
            ORDER BY published_at DESC
            """,
            (cutoff_str,),
        ).fetchall()

    wanted = {t.upper().strip() for t in tickers} if tickers else None

    market_alerts: list[dict[str, Any]] = []
    for r in eod_rows:
        d = dict(r)
        if wanted and d["ticker"] not in wanted:
            continue
        p_trig = abs(float(d["pct_change"])) >= price_thresh
        v_trig = float(d["volume_ratio"]) >= vol_thresh
        if p_trig or v_trig:
            reasons: list[str] = []
            if p_trig:
                reasons.append(f"Price moved {d['pct_change']:+.2f}% (threshold ±{price_thresh}%)")
            if v_trig:
                reasons.append(f"Volume {d['volume_ratio']}x 20d avg (threshold {vol_thresh}x)")
            d["trigger_reasons"] = reasons
            market_alerts.append(d)

    filing_alerts: list[dict[str, Any]] = []
    for r in ann_rows:
        item = _format_announcement_row(r)
        if wanted and item["ticker"] not in wanted:
            continue
        if item["category"] in watched_cats:
            item["trigger_reason"] = f"Watched material category: {item['category']}"
            filing_alerts.append(item)

    return {
        "rules": rules,
        "window": window,
        "total_alerts": len(market_alerts) + len(filing_alerts),
        "market_alerts": market_alerts,
        "filing_alerts": filing_alerts,
    }

