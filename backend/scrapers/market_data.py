from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from pathlib import Path
import yfinance as yf
from backend.db import get_conn, get_alert_rules, normalize_ticker, DB_PATH

# Fixed offset, not zoneinfo: India has no DST, and zoneinfo on Windows needs the tzdata package.
IST = timezone(timedelta(hours=5, minutes=30))
NSE_CLOSE_IST = time(15, 30)


def is_completed_session(bar_date: date, now: datetime | None = None) -> bool:
    """A daily bar is final once its IST date is past, or it is today and the 15:30 IST close has passed."""
    now_ist = (now or datetime.now(timezone.utc)).astimezone(IST)
    return bar_date < now_ist.date() or (bar_date == now_ist.date() and now_ist.time() > NSE_CLOSE_IST)


def fetch_eod_metrics(
    tickers: list[str], db_path: Path = DB_PATH, save_to_db: bool = True, now: datetime | None = None
) -> list[dict[str, Any]]:
    """
    Fetch end-of-day price, 1-day % change, volume, and 20-day average volume
    for a list of NSE tickers (automatically appends .NS if missing).
    Also flags unusual price/volume alerts based on alert_rules in SQLite.
    Only completed sessions count: during market hours yfinance includes today's unfinished bar
    (partial volume), which is dropped so alerts and stored rows use the last closed session.
    """
    rules = get_alert_rules(db_path)
    price_thresh = float(rules.get("price_move_pct", 3.0))
    vol_thresh = float(rules.get("volume_multiplier", 2.0))

    results: list[dict[str, Any]] = []
    for raw_ticker in tickers:
        clean_ticker = normalize_ticker(raw_ticker)
        candidate_symbols = [f"{clean_ticker}.NS", f"{clean_ticker}.BO"]
        try:
            hist = None
            for sym in candidate_symbols:
                h = yf.Ticker(sym).history(period="1mo", interval="1d", auto_adjust=True)
                if not h.empty:
                    h = h.dropna(subset=["Close"])
                if not h.empty and not is_completed_session(h.index[-1].date(), now):
                    h = h.iloc[:-1]
                if not h.empty and len(h) >= 2:
                    hist = h
                    break
            if hist is None or hist.empty or len(hist) < 2:
                continue

            latest = hist.iloc[-1]
            prev = hist.iloc[-2]
            trade_date = hist.index[-1].strftime("%Y-%m-%d")

            close_price = round(float(latest["Close"]), 2)
            prev_close = round(float(prev["Close"]), 2)
            pct_change = round(((close_price - prev_close) / prev_close) * 100.0, 2)
            volume = int(latest["Volume"])

            # 20-day trailing average volume (excluding today if >2 days available)
            trailing_vols = hist["Volume"].iloc[:-1].tail(20)
            avg_vol_20d = float(trailing_vols.mean()) if len(trailing_vols) > 0 else float(volume)
            vol_ratio = round(volume / avg_vol_20d, 2) if avg_vol_20d > 0 else 1.0

            price_alert = abs(pct_change) >= price_thresh
            volume_alert = vol_ratio >= vol_thresh

            item = {
                "ticker": clean_ticker,
                "trade_date": trade_date,
                "close_price": close_price,
                "prev_close": prev_close,
                "pct_change": pct_change,
                "volume": volume,
                "avg_volume_20d": round(avg_vol_20d, 0),
                "volume_ratio": vol_ratio,
                "price_alert": price_alert,
                "volume_alert": volume_alert,
                "is_unusual": price_alert or volume_alert,
            }
            results.append(item)

            if save_to_db:
                with get_conn(db_path) as conn:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO eod_prices
                        (ticker, trade_date, close_price, prev_close, pct_change, volume, avg_volume_20d, volume_ratio, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                        """,
                        (
                            clean_ticker,
                            trade_date,
                            close_price,
                            prev_close,
                            pct_change,
                            volume,
                            round(avg_vol_20d, 0),
                            vol_ratio,
                        ),
                    )
        except Exception as exc:
            print(f"[market_data] Error fetching {clean_ticker}: {exc}")

    return results

