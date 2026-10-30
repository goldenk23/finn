import json
from datetime import datetime, timedelta
from pathlib import Path
import re
from typing import Any

from backend.db import get_conn, get_portfolio, get_alert_rules, DB_PATH
from backend.scrapers.market_data import fetch_eod_metrics
from backend.scrapers.filings import (
    PERSON_HONORIFIC,
    enforce_filing_hygiene,
    fetch_company_filings,
    is_bank_or_nbfc,
    is_vague_title,
)
from backend.llm_router import (
    latest_model_for,
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

# ponytail: hard cap of 20 x 15 = 300 filings per refresh; the rest wait for the next refresh
TRIAGE_BATCH_SIZE = 15
MAX_TRIAGE_BATCHES = 20

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
                "UPDATE announcements SET enriched_json = ?, summary = COALESCE(?, summary) WHERE filing_id = ?",
                # results cards carry a numbers-based summary that replaces the generic triage line
                (json.dumps(enriched, ensure_ascii=False), enriched.get("summary") if cat == "results" else None, filing_id),
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

    # Triage every filing with a blank summary, including rows Pull saved for stocks outside the portfolio.
    # A refresh scoped to `tickers` triages only those stocks.
    with get_conn(db_path) as conn:
        where = "(summary IS NULL OR summary = '')"
        params: list[Any] = []
        if tickers:
            where += f" AND ticker IN ({','.join('?' for _ in ticker_list)})"
            params = ticker_list
        untriaged = [
            dict(r)
            for r in conn.execute(f"SELECT * FROM announcements WHERE {where} ORDER BY published_at DESC", params)
        ]

    # Each filing is tried at most once per refresh; failures stay blank for the next refresh, and a batch with
    # no progress (LLM down, budget hit) stops the loop so a failing filing can't keep spending.
    triaged_count = 0
    max_rows = MAX_TRIAGE_BATCHES * TRIAGE_BATCH_SIZE
    for i in range(0, min(len(untriaged), max_rows), TRIAGE_BATCH_SIZE):
        triaged_map = classify_and_summarize_batch(
            untriaged[i : i + TRIAGE_BATCH_SIZE], db_path=db_path, batch_size=TRIAGE_BATCH_SIZE
        )
        if not triaged_map:
            break
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
        triaged_count += len(triaged_map)

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
        "filings_triaged_by_llm": triaged_count,
        "filings_left_untriaged": len(untriaged) - triaged_count,
        "filings_deep_enriched": enriched_count,
    }


def _format_announcement_row(row: Any) -> dict[str, Any]:
    d = dict(row)
    cat, mat = enforce_filing_hygiene(
        d.get("title", ""), d.get("category", "unclassified"), int(d.get("materiality", 0)),
        is_lender=is_bank_or_nbfc(d.get("company_name") or ""),
    )
    d["category"] = cat
    d["materiality"] = mat
    try:
        if mat == 1 and cat in ("results", "management_change"):
            d["enriched"] = json.loads(d.get("enriched_json") or "{}")
        else:
            d["enriched"] = {}
    except Exception:
        d["enriched"] = {}
    d.pop("enriched_json", None)
    d["priority_rank"] = CATEGORY_PRIORITY.get(d.get("category", "unclassified"), 17)
    return d


_PERSON_ACTION = re.compile(r"\b(?:retire\w*|appoint\w*|resign\w*|cessation)\b")


def _enrichment_richness(item: dict[str, Any]) -> tuple[int, int]:
    """(has payload + has metrics/source link, number of filled fields)."""
    enr = item.get("enriched") or {}
    has_source_or_metrics = 1 if (enr.get("metrics") or enr.get("source_url")) else 0
    filled = sum(1 for v in enr.values() if v not in (None, "", [], {}, "Unknown"))
    return (int(bool(enr)) + has_source_or_metrics, filled)


def _primary_score(item: dict[str, Any]) -> tuple[Any, ...]:
    """
    Ranks duplicate filings for the same corporate event so the most informative title becomes the card:
    2 = names a person and what happened to them, 1 = a specific subject, 0 = a generic filing type
    ("Change in Director(s)"). Ties go to the richer enrichment, a board outcome, the longer title, then the newest.
    """
    title = (item.get("title") or "").lower()
    if PERSON_HONORIFIC.search(title) and _PERSON_ACTION.search(title):
        specificity = 2
    else:
        specificity = 0 if is_vague_title(title) else 1
    return (
        specificity,
        _enrichment_richness(item),
        1 if "outcome of board meeting" in title else 0,
        len(title),
        _ts_epoch(item.get("published_at", "")),
    )


def _deduplicate_material_filings(
    material: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Deduplicates multiple NSE/BSE exchange filings for the same stock and event
    (e.g. TRENT Harish Bhat resignation filed twice, or Outcome of Board Meeting + Auditor Report for same results).
    The primary card keeps the richest enrichment in its group and lists the others in `also_filed_as`.
    Returns (deduped_material, folded_duplicates).
    """
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}

    for item in material:
        ticker = item.get("ticker", "")
        cat = item.get("category", "")
        title_low = (item.get("title") or "").lower()
        date_day = (item.get("published_at") or "")[:10]
        enr = item.get("enriched") or {}

        if cat == "results":
            key: tuple[Any, ...] = (ticker, "results")
        elif cat == "management_change":
            person = (enr.get("person_name") or "").strip().lower()
            if person and person != "unknown":
                norm_person = re.sub(r"\b(mr|mrs|ms|shri|smt|dr)\b\.?", "", person).strip()
                norm_person = re.sub(r"[^a-z0-9]", "", norm_person)
                key = (ticker, "management_change", norm_person or person)
            else:
                norm_prefix = re.sub(r"[^a-z0-9]", "", title_low)[:48]
                key = (ticker, "management_change", date_day, norm_prefix)
        else:
            norm_prefix = re.sub(r"[^a-z0-9]", "", title_low)[:36]
            key = (ticker, cat, date_day, norm_prefix)

        groups.setdefault(key, []).append(item)

    deduped: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    for group in groups.values():
        primary = max(group, key=_primary_score)
        richest = max(group, key=_enrichment_richness)
        if _enrichment_richness(richest) > _enrichment_richness(primary):
            primary["enriched"] = richest["enriched"]
        others = [x for x in group if x is not primary]
        if others:
            primary["also_filed_as"] = [
                {k: x.get(k) for k in ("filing_id", "title", "pdf_url", "published_at")} for x in others
            ]
        deduped.append(primary)
        duplicates.extend(others)

    deduped.sort(key=lambda x: (x["priority_rank"], -_ts_epoch(x["published_at"])))
    return deduped, duplicates


def _get_reference_now(conn: Any) -> datetime:
    """
    Anchors digest/alert windows to the latest portfolio filing timestamp if the DB
    has not been refreshed in the current session, ensuring offline demo evaluation
    never renders an empty 24h window.
    """
    row = conn.execute(
        """
        SELECT MAX(published_at) as max_pub
        FROM announcements
        WHERE ticker IN (SELECT ticker FROM portfolio)
        """
    ).fetchone()
    now = datetime.now()
    if row and row["max_pub"]:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                max_dt = datetime.strptime(str(row["max_pub"])[:19], fmt)
                return min(now, max_dt)
            except ValueError:
                continue
    return now


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

    with get_conn(db_path) as conn:
        ref_now = _get_reference_now(conn)
        cutoff_str = (ref_now - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
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
                  AND ticker IN (SELECT ticker FROM portfolio)
                ORDER BY published_at DESC
                """,
                (cutoff_str,),
            ).fetchall()

    all_items = [_format_announcement_row(r) for r in rows]
    raw_material = [x for x in all_items if x["materiality"] == 1]
    routine = [x for x in all_items if x["materiality"] == 0]

    # Duplicates are folded into their primary card's "Also filed as" note, not shown in the routine drawer
    material, folded_dups = _deduplicate_material_filings(raw_material)

    total = len(all_items)
    noise_pct = round(((len(routine) + len(folded_dups)) / total) * 100, 1) if total > 0 else 0.0

    return {
        "window": window,
        "cutoff_timestamp": cutoff_str,
        "stats": {
            "total_filings": total,
            "material_count": len(material),
            "routine_collapsed_count": len(routine),
            "duplicates_merged": len(folded_dups),
            "noise_filtered_pct": noise_pct,
            # Model that served the latest LLM triage batch (None = only the regex classifier has run)
            "triage_model": latest_model_for("push_batch_triage", db_path=db_path),
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

    with get_conn(db_path) as conn:
        ref_now = _get_reference_now(conn)
        cutoff_str = (ref_now - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")

        # Latest EOD price row per portfolio ticker
        eod_rows = conn.execute(
            """
            SELECT e.*, p.company_name, p.sector
            FROM eod_prices e
            INNER JOIN portfolio p ON e.ticker = p.ticker
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
              AND ticker IN (SELECT ticker FROM portfolio)
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

    raw_filing_alerts: list[dict[str, Any]] = []
    for r in ann_rows:
        item = _format_announcement_row(r)
        if item["materiality"] != 1:
            continue
        if wanted and item["ticker"] not in wanted:
            continue
        if item["category"] in watched_cats:
            item["trigger_reason"] = f"Watched material category: {item['category']}"
            raw_filing_alerts.append(item)

    filing_alerts, _ = _deduplicate_material_filings(raw_filing_alerts)

    return {
        "rules": rules,
        "window": window,
        "total_alerts": len(market_alerts) + len(filing_alerts),
        "market_alerts": market_alerts,
        "filing_alerts": filing_alerts,
    }

