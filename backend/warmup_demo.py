import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from backend.db import init_db, get_portfolio, get_conn
from backend.scrapers.filings import enforce_filing_hygiene, is_bank_or_nbfc
from backend.push_engine import refresh_portfolio_push_data, get_windowed_digest, enrich_single_filing
from backend.pull_engine import get_or_generate_pull_report
from backend.llm_router import (
    get_cost_summary,
    classify_and_summarize_batch,
    _normalize_results_enrichment,
    _is_pdf_extraction_sane,
    _compute_screener_quarterly_metrics,
)

PULL_WARMUP_TICKERS = ["TRENT", "KPITTECH", "DIXON", "POLYCAB"]


def sanitize_and_repair_announcements() -> None:
    """
    1. Enforces filing hygiene across all existing rows in `announcements` (pushing newspaper
       publications, Reg 74(5) depository certificates, and meeting notices to routine materiality=0).
    2. Re-validates all enriched `results` cards against Screener's normalized ₹ Cr table (fixing
       unit shifts like MARUTI and normalizing all QoQ/YoY % strings to +/-X.X%).
    3. Enriches any un-enriched primary `Outcome of Board Meeting` results filings.
    """
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM announcements").fetchall()

    demoted = 0
    for r in rows:
        fid = r["filing_id"]
        old_cat, old_mat = r["category"], int(r["materiality"])
        new_cat, new_mat = enforce_filing_hygiene(
            r["title"], old_cat, old_mat, is_lender=is_bank_or_nbfc(r["company_name"] or "")
        )
        should_clear_enr = new_mat == 0 or new_cat not in ("results", "management_change")
        new_enr_json = "{}" if should_clear_enr else (r["enriched_json"] or "{}")
        if (new_cat, new_mat, new_enr_json) != (old_cat, old_mat, r["enriched_json"] or "{}"):
            with get_conn() as conn:
                conn.execute(
                    "UPDATE announcements SET category = ?, materiality = ?, enriched_json = ? WHERE filing_id = ?",
                    (new_cat, new_mat, new_enr_json, fid),
                )
            if old_mat == 1 and new_mat == 0:
                demoted += 1

    print(f"[hygiene] Demoted {demoted} routine noise rows (newspaper ads / Reg 74(5) / meet notices) to materiality=0.")

    # Triage any remaining material filings that lack a 1-line summary
    with get_conn() as conn:
        untriaged = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM announcements WHERE materiality = 1 AND (summary IS NULL OR summary = '')"
            ).fetchall()
        ]
    if untriaged:
        print(f"[hygiene] Generating 1-line summaries for {len(untriaged)} untriaged filings...")
        t_map = classify_and_summarize_batch(untriaged)
        with get_conn() as conn:
            for fid, tdata in t_map.items():
                conn.execute(
                    "UPDATE announcements SET category = ?, materiality = ?, summary = ? WHERE filing_id = ?",
                    (tdata["category"], tdata["materiality"], tdata["summary"], fid),
                )

    # Verify and normalize all material 'results' cards
    with get_conn() as conn:
        res_rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM announcements WHERE materiality = 1 AND category = 'results' ORDER BY published_at DESC"
            ).fetchall()
        ]

    seen_tickers: set[str] = set()
    for r in res_rows:
        ticker = r["ticker"]
        fid = r["filing_id"]
        # Enrich the primary results filing per ticker
        if ticker in seen_tickers and "outcome of board meeting" not in (r["title"] or "").lower():
            continue
        seen_tickers.add(ticker)

        enr = {}
        try:
            enr = json.loads(r.get("enriched_json") or "{}")
        except Exception:
            enr = {}

        sc_ref = None
        try:
            sc_ref = _compute_screener_quarterly_metrics(ticker)
        except Exception:
            pass

        if (
            not enr
            or not enr.get("has_numbers")
            or not _is_pdf_extraction_sane(enr, sc_ref)
            or (ticker == "TRENT" and enr.get("period") == "Jun 2026")
        ):
            print(f"[hygiene] Re-enriching {ticker} results card ({fid}) with Screener ₹ Cr cross-check...")
            new_enr = enrich_single_filing(fid)
            print(f"  -> {ticker} updated: {new_enr.get('metrics')}")
        else:
            norm_enr = _normalize_results_enrichment(enr)
            with get_conn() as conn:
                conn.execute(
                    "UPDATE announcements SET enriched_json = ? WHERE filing_id = ?",
                    (json.dumps(norm_enr, ensure_ascii=False), fid),
                )
            print(f"[hygiene] Normalized % formatting for {ticker}: {norm_enr.get('metrics')}")

    # Enrich any un-enriched portfolio 'management_change' cards
    with get_conn() as conn:
        mgmt_rows = [
            dict(r)
            for r in conn.execute(
                """
                SELECT * FROM announcements
                WHERE materiality = 1
                  AND category = 'management_change'
                  AND ticker IN (SELECT ticker FROM portfolio)
                  AND (enriched_json IS NULL OR enriched_json = '{}' OR enriched_json = '')
                ORDER BY published_at DESC
                """
            ).fetchall()
        ]
    for r in mgmt_rows:
        fid = r["filing_id"]
        ticker = r["ticker"]
        print(f"[hygiene] Enriching management_change card [{ticker}] ({fid}): {r['title'][:70]}...")
        new_enr = enrich_single_filing(fid)
        print(f"  -> {ticker} mgmt bio: {new_enr.get('person_name')} ({new_enr.get('action')})")


def main() -> None:
    t0 = time.perf_counter()
    init_db()
    with get_conn() as conn:
        ann_count = conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0]
    if ann_count < 50:
        portfolio = get_portfolio()
        tickers = [p["ticker"] for p in portfolio]
        print(f"[warmup] 1/4 Scraping EOD metrics & Push filings for {len(tickers)} portfolio stocks...")
        refresh_portfolio_push_data(tickers=tickers, days=14, max_enrich=6)
    else:
        print(f"[warmup] 1/4 Using {ann_count} existing filings in finn.db...")

    print("\n[warmup] 2/4 Running filing hygiene, deduplication & results table verification...")
    sanitize_and_repair_announcements()

    digest_24h = get_windowed_digest("24h")
    digest_7d = get_windowed_digest("7d")
    digest_14d = get_windowed_digest("14d")
    print(
        f"[warmup] Deduped Digest counts -> 24h: {digest_24h['stats']}, "
        f"7d: {digest_7d['stats']}, 14d: {digest_14d['stats']}"
    )

    print(f"\n[warmup] 3/4 Verifying pre-warmed Pull Research reports for {PULL_WARMUP_TICKERS}...")
    for ticker in PULL_WARMUP_TICKERS:
        res = get_or_generate_pull_report(ticker, force_refresh=False)
        print(f"  -> {ticker}: cached={res['cached']} | quarter={res['quarter_key']} | cost=${res['cost_usd']:.4f}")

    # Checkpoint SQLite WAL so finn.db is self-contained for Git
    with get_conn() as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")

    print(f"\n[warmup] 4/4 Completed in {time.perf_counter() - t0:.1f}s. Cost summary: {get_cost_summary()['totals']}")


if __name__ == "__main__":
    main()
