"""Offline checks: no network, no LLM calls, never writes finn.db. Run with: python backend/test_offline.py"""
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.db import DB_PATH, SEED_PORTFOLIO, init_db, normalize_ticker
from backend.scrapers.filings import enforce_filing_hygiene

TICKER_TABLES = ("portfolio", "eod_prices", "announcements", "pull_reports_cache")


def _copy_real_db(dst: Path) -> Path:
    """Snapshot finn.db (read-only, WAL included) into a temp file via the sqlite backup API."""
    src = sqlite3.connect(f"{DB_PATH.as_uri()}?mode=ro", uri=True)
    out = sqlite3.connect(dst)
    src.backup(out)
    src.close()
    out.close()
    return dst


def _snapshot(db: Path) -> dict[str, list[tuple]]:
    conn = sqlite3.connect(db)
    snap = {t: sorted(conn.execute(f"SELECT * FROM {t}").fetchall(), key=repr) for t in TICKER_TABLES}
    conn.close()
    return snap


def test_newspaper_publication_stays_routine() -> None:
    # Start from the worst case (LLM said "material results") to prove the guard demotes it.
    cat, mat = enforce_filing_hygiene("Copy of Newspaper Publication", "results", 1)
    assert mat == 0, f"expected materiality 0, got {mat} (category={cat})"


def test_normalize_ticker() -> None:
    assert normalize_ticker("zomato") == "ETERNAL"
    assert normalize_ticker(" TATAMOTORS.NS ") == "TMPV"
    assert normalize_ticker("Zomato.BO") == "ETERNAL"
    assert normalize_ticker("tcs.ns") == "TCS"
    assert normalize_ticker("M&M") == "M&M"
    assert normalize_ticker("BAJAJ-AUTO") == "BAJAJ-AUTO"
    for bad in ["TCS&from_date=x", "../x", "", "   ", "A" * 21, "TCS/../x", "TCS NS", "INFY;DROP"]:
        try:
            normalize_ticker(bad)
        except ValueError:
            continue
        raise AssertionError(f"normalize_ticker accepted {bad!r}")


def test_rename_migration_is_idempotent() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = _copy_real_db(Path(tmp) / "finn_copy.db")
        conn = sqlite3.connect(db)
        # Recreate the legacy state regardless of whether the real DB is already migrated.
        conn.execute("DELETE FROM portfolio WHERE ticker IN ('ETERNAL','TMPV')")
        conn.executemany(
            "INSERT OR REPLACE INTO portfolio (ticker, bse_code, company_name, sector, cap_tier) VALUES (?,?,?,?,?)",
            [("ZOMATO", "543320", "Zomato Ltd (Eternal)", "Consumer Tech", "Large Cap"),
             ("TATAMOTORS", "500570", "Tata Motors Ltd", "Automobiles", "Large Cap")],
        )
        eod = "INSERT OR REPLACE INTO eod_prices (ticker, trade_date, close_price, prev_close, pct_change, volume, avg_volume_20d, volume_ratio) VALUES (?,?,1,1,0,1,1,1)"
        conn.executemany(eod, [("ZOMATO", "2000-01-03"), ("ZOMATO", "2000-01-04"), ("ETERNAL", "2000-01-04")])  # 01-04 collides
        conn.execute(
            "INSERT OR REPLACE INTO announcements (filing_id, ticker, title, published_at) VALUES ('legacy-test-1','ZOMATO','t','2000-01-03')"
        )
        conn.execute("DELETE FROM pull_reports_cache WHERE ticker IN ('TMPV','TATAMOTORS')")
        conn.execute(
            "INSERT INTO pull_reports_cache (ticker, quarter_key, fundamentals_json, report_json) VALUES ('TATAMOTORS','Q','{}','{}')"
        )
        portfolio_size = conn.execute("SELECT COUNT(*) FROM portfolio").fetchone()[0]
        conn.commit()
        conn.close()

        init_db(db)
        first = _snapshot(db)
        init_db(db)
        assert _snapshot(db) == first, "second init_db() changed data: migration is not idempotent"

        conn = sqlite3.connect(db)
        for t in TICKER_TABLES:
            left = conn.execute(f"SELECT COUNT(*) FROM {t} WHERE ticker IN ('ZOMATO','TATAMOTORS')").fetchone()[0]
            assert left == 0, f"{left} legacy rows left in {t}"
        names = dict(conn.execute("SELECT ticker, company_name FROM portfolio WHERE ticker IN ('ETERNAL','TMPV')"))
        assert names == {"ETERNAL": "Eternal Ltd", "TMPV": "Tata Motors Passenger Vehicles Ltd"}, names
        assert conn.execute("SELECT COUNT(*) FROM portfolio").fetchone()[0] == portfolio_size
        dates = [d for (d,) in conn.execute("SELECT trade_date FROM eod_prices WHERE ticker='ETERNAL' AND trade_date LIKE '2000-%' ORDER BY 1")]
        assert dates == ["2000-01-03", "2000-01-04"], dates
        assert conn.execute("SELECT ticker FROM announcements WHERE filing_id='legacy-test-1'").fetchone() == ("ETERNAL",)
        assert conn.execute("SELECT COUNT(*) FROM pull_reports_cache WHERE ticker='TMPV'").fetchone()[0] == 1
        conn.close()


def test_deleted_stock_survives_init_db() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "fresh.db"
        init_db(db)  # empty DB -> seeded
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT COUNT(*) FROM portfolio").fetchone()[0] == len(SEED_PORTFOLIO)
        conn.execute("DELETE FROM portfolio WHERE ticker = 'ITC'")
        conn.commit()
        conn.close()

        init_db(db)  # simulates a backend restart
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT COUNT(*) FROM portfolio WHERE ticker = 'ITC'").fetchone()[0] == 0, "ITC was re-seeded"
        assert conn.execute("SELECT COUNT(*) FROM portfolio").fetchone()[0] == len(SEED_PORTFOLIO) - 1
        conn.close()


if __name__ == "__main__":
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_") and callable(fn)]
    for fn in tests:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(tests)} offline test(s) passed")
