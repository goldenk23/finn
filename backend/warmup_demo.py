import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.db import init_db, get_portfolio, get_conn
from backend.push_engine import refresh_portfolio_push_data, get_windowed_digest
from backend.pull_engine import get_or_generate_pull_report
from backend.llm_router import get_cost_summary

PULL_WARMUP_TICKERS = ["TRENT", "KPITTECH", "DIXON", "POLYCAB"]


def main() -> None:
    t0 = time.perf_counter()
    init_db()
    portfolio = get_portfolio()
    tickers = [p["ticker"] for p in portfolio]
    print(f"[warmup] 1/3 Refreshing EOD metrics & Push filings for all {len(tickers)} portfolio stocks: {tickers}")

    push_stats = refresh_portfolio_push_data(
        tickers=tickers,
        days=14,
        max_enrich=8,
    )
    print(f"[warmup] Push refresh stats: {push_stats}")

    digest_24h = get_windowed_digest("24h")
    digest_7d = get_windowed_digest("7d")
    digest_14d = get_windowed_digest("14d")
    print(
        f"[warmup] Digest counts -> 24h: {digest_24h['stats']}, "
        f"7d: {digest_7d['stats']}, 14d: {digest_14d['stats']}"
    )

    print(f"\n[warmup] 2/3 Pre-warming Pull Research reports for {PULL_WARMUP_TICKERS} "
          f"(leaving INDHOTEL & PERSISTENT uncached for live cold test)...")
    for ticker in PULL_WARMUP_TICKERS:
        t_start = time.perf_counter()
        try:
            res = get_or_generate_pull_report(ticker, force_refresh=False)
            dt = time.perf_counter() - t_start
            print(
                f"  -> {ticker}: cached={res['cached']} | quarter={res['quarter_key']} | "
                f"time={dt:.2f}s | cost=${res['cost_usd']:.4f}"
            )
        except Exception as exc:
            print(f"  -> [WARN] {ticker} pull warmup error: {exc}")

    with get_conn() as conn:
        eod_cnt = conn.execute("SELECT COUNT(DISTINCT ticker) FROM eod_prices").fetchone()[0]
        ann_cnt = conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0]
        enr_cnt = conn.execute(
            "SELECT COUNT(*) FROM announcements WHERE enriched_json IS NOT NULL AND enriched_json != '{}' AND enriched_json != ''"
        ).fetchone()[0]
        pull_rows = conn.execute("SELECT ticker, quarter_key, cost_usd FROM pull_reports_cache").fetchall()

    print(f"\n[warmup] 3/3 Database Summary ({time.perf_counter() - t0:.1f}s total):")
    print(f"  - EOD Stocks Priced : {eod_cnt}/{len(tickers)}")
    print(f"  - Total Filings     : {ann_cnt} ({enr_cnt} deep-enriched)")
    print(f"  - Cached Pull Stocks: {[dict(r) for r in pull_rows]}")
    print(f"  - Cost Ledger Totals: {get_cost_summary()['totals']}")


if __name__ == "__main__":
    main()
