import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.db import init_db, get_conn
from backend.push_engine import (
    refresh_portfolio_push_data,
    get_windowed_digest,
    get_triggered_alerts,
    enrich_single_filing,
)
from backend.llm_router import get_cost_summary


def main() -> None:
    init_db()
    test_tickers = ["RELIANCE", "TRENT", "TCS", "INFY"]

    print(f"=== 1. Running Push Pipeline Refresh on {test_tickers} ===")
    stats = refresh_portfolio_push_data(tickers=test_tickers, days=20, max_enrich=4)
    print("Refresh summary:", json.dumps(stats, indent=2))

    # Ensure we test at least one 'results' and one 'management_change' enrichment explicitly
    with get_conn() as conn:
        res_row = conn.execute(
            """
            SELECT * FROM announcements
            WHERE category = 'results'
            ORDER BY CASE WHEN LOWER(title) LIKE '%financial result%' OR LOWER(title) LIKE '%outcome%' THEN 0 ELSE 1 END,
                     published_at DESC
            LIMIT 1
            """
        ).fetchone()
        mgmt_row = conn.execute(
            "SELECT * FROM announcements WHERE category = 'management_change' ORDER BY published_at DESC LIMIT 1"
        ).fetchone()

    print("\n=== 2. Verifying Quarterly Results Enrichment (Revenue, EBITDA, PBT, PAT + QoQ/YoY) ===")
    if res_row:
        res_enriched = json.loads(res_row["enriched_json"] or "{}")
        if not res_enriched.get("metrics"):
            res_enriched = enrich_single_filing(res_row["filing_id"])
        print(f"Filing: [{res_row['ticker']}] {res_row['title'][:100]}")
        print(json.dumps(res_enriched, indent=2, ensure_ascii=False))
        assert "metrics" in res_enriched, "Expected 'metrics' in results enrichment"
    else:
        print("No 'results' filing found in window.")

    print("\n=== 3. Verifying Management Change Bio Enrichment (PDF + DuckDuckGo Search) ===")
    if mgmt_row:
        mgmt_enriched = json.loads(mgmt_row["enriched_json"] or "{}")
        if not mgmt_enriched.get("two_line_bio") or mgmt_enriched.get("person_name") == "Unknown":
            mgmt_enriched = enrich_single_filing(mgmt_row["filing_id"])
        print(f"Filing: [{mgmt_row['ticker']}] {mgmt_row['title'][:100]}")
        print(json.dumps(mgmt_enriched, indent=2, ensure_ascii=False))
        assert "two_line_bio" in mgmt_enriched and mgmt_enriched.get("person_name") != "Unknown", (
            "Expected valid person_name and two_line_bio in management_change enrichment"
        )
    else:
        print("No 'management_change' filing found in window.")

    print("\n=== 4. Testing Windowed Digest (14d window) ===")
    digest = get_windowed_digest(window="14d", tickers=test_tickers)
    print("Digest Stats:", json.dumps(digest["stats"], indent=2))
    print(f"Top Material Filings ({len(digest['material'])}):")
    for m in digest["material"][:5]:
        print(f"  - [{m['ticker']}] ({m['category']}) {m['summary']}")
    print(f"Collapsed Routine Filings ({len(digest['routine'])}):")
    for r in digest["routine"][:3]:
        print(f"  - [{r['ticker']}] ({r['category']}) {r['title'][:80]}...")

    print("\n=== 5. Testing Triggered Alerts ===")
    alerts = get_triggered_alerts(window="14d", tickers=test_tickers)
    print(f"Total Alerts: {alerts['total_alerts']} "
          f"(Market: {len(alerts['market_alerts'])}, Filings: {len(alerts['filing_alerts'])})")
    for ma in alerts["market_alerts"]:
        print(f"  [MARKET ALERT] {ma['ticker']}: ₹{ma['close_price']} -> {ma['trigger_reasons']}")
    for fa in alerts["filing_alerts"][:4]:
        print(f"  [FILING ALERT] {fa['ticker']} ({fa['category']}): {fa['summary']}")

    print("\n=== 6. Checking LLM Cost & Token Usage Ledger ===")
    cost_summary = get_cost_summary()
    print(json.dumps(cost_summary, indent=2))
    assert cost_summary["totals"]["total_calls"] > 0, "Expected LLM calls logged in llm_cost_ledger"


if __name__ == "__main__":
    main()

