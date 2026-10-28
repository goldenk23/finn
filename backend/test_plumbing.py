import sys
from pathlib import Path

# Ensure repo root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.db import init_db, get_portfolio, get_alert_rules
from backend.scrapers.market_data import fetch_eod_metrics
from backend.scrapers.filings import fetch_company_filings
from backend.scrapers.screener import fetch_screener_fundamentals


def main() -> None:
    print("=== 1. Initializing SQLite DB (WAL Mode) & Seeding 16 Portfolio Stocks ===")
    init_db()
    portfolio = get_portfolio()
    rules = get_alert_rules()
    print(f"Seeded {len(portfolio)} stocks in portfolio: {[p['ticker'] for p in portfolio]}")
    print(f"Active Alert Rules: {rules}\n")

    test_stocks = [p for p in portfolio if p["ticker"] in ("TRENT", "RELIANCE")]

    print("=== 2. Testing EOD Price & Volume Anomaly Scraper (yfinance) ===")
    eod_data = fetch_eod_metrics([s["ticker"] for s in test_stocks])
    for row in eod_data:
        print(
            f"  [{row['ticker']}] Date: {row['trade_date']} | Close: ₹{row['close_price']} "
            f"({row['pct_change']:+.2f}%) | Vol: {row['volume']:,} "
            f"({row['volume_ratio']}x 20d avg) | Unusual: {row['is_unusual']}"
        )
    print()

    print("=== 3. Testing Corporate Announcements / Filings Scraper (NSE/BSE) ===")
    for s in test_stocks:
        filings = fetch_company_filings(s["ticker"], bse_code=s["bse_code"], days=15)
        print(f"  [{s['ticker']}] Fetched {len(filings)} recent announcements:")
        for f in filings[:4]:
            print(
                f"    - [{f['published_at']}] ({f['exchange']}) [{f['category']} | mat={f['materiality']}] "
                f"{f['title'][:90]}... | PDF: {bool(f['pdf_url'])}"
            )
    print()

    print("=== 4. Testing Screener.in Fundamentals, Concalls & Annual Reports Scraper ===")
    for s in test_stocks:
        data = fetch_screener_fundamentals(s["ticker"])
        q_headers = data["quarters"]["headers"][-4:] if data["quarters"]["headers"] else []
        sales_row = next((r for r in data["quarters"]["rows"] if "Sales" in r["metric"] or "Revenue" in r["metric"]), {})
        pat_row = next((r for r in data["quarters"]["rows"] if "Net Profit" in r["metric"]), {})

        print(f"  [{data['ticker']}] {data['company_name']} ({data['screener_url']})")
        print(f"    Top Ratios: Market Cap={data['top_ratios'].get('Market Cap')}, "
              f"P/E={data['top_ratios'].get('Stock P/E')}, ROCE={data['top_ratios'].get('ROCE')}, ROE={data['top_ratios'].get('ROE')}")
        print(f"    Recent 4 Quarters ({q_headers}):")
        if q_headers and sales_row:
            print(f"      Sales/Rev : {[sales_row.get(h) for h in q_headers]}")
        if q_headers and pat_row:
            print(f"      Net Profit: {[pat_row.get(h) for h in q_headers]}")
        print(f"    Tables scraped: P&L ({len(data['profit_loss']['rows'])} rows), "
              f"Balance Sheet ({len(data['balance_sheet']['rows'])} rows), "
              f"Cash Flow ({len(data['cash_flow']['rows'])} rows), "
              f"Ratios ({len(data['ratios']['rows'])} rows)")
        print(f"    Annual Reports found: {len(data['annual_reports'])} -> {data['annual_reports'][:2]}")
        print(f"    Concalls found      : {len(data['concalls'])} -> {data['concalls'][:2]}\n")


if __name__ == "__main__":
    main()

