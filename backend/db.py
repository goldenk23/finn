import sqlite3
import json
import re
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).parent / "finn.db"

# Exchange symbol renames (old -> current). NSE, BSE, Screener and yfinance only answer to the new symbols.
TICKER_RENAMES = {"ZOMATO": "ETERNAL", "TATAMOTORS": "TMPV"}
_TICKER_RE = re.compile(r"[A-Z0-9&-]{1,20}")


def normalize_ticker(raw: str) -> str:
    """Uppercase, drop a .NS/.BO suffix, apply renames, and reject anything that isn't a plain NSE symbol.

    The result is interpolated into NSE/Screener URLs, so the strict whitelist is the trust boundary.
    """
    t = (raw or "").strip().upper()
    if t.endswith((".NS", ".BO")):
        t = t[:-3]
    t = TICKER_RENAMES.get(t, t)
    if not _TICKER_RE.fullmatch(t):
        raise ValueError(
            f"Invalid ticker {raw!r}: use an NSE symbol of 1-20 letters, digits, '&' or '-' (e.g. TCS, M&M, BAJAJ-AUTO)"
        )
    return t


SEED_PORTFOLIO = [
    ("RELIANCE", "500325", "Reliance Industries Ltd", "Energy & Retail", "Large Cap"),
    ("TCS", "532540", "Tata Consultancy Services Ltd", "IT Services", "Large Cap"),
    ("HDFCBANK", "500180", "HDFC Bank Ltd", "Banking", "Large Cap"),
    ("INFY", "500209", "Infosys Ltd", "IT Services", "Large Cap"),
    ("TMPV", "500570", "Tata Motors Passenger Vehicles Ltd", "Automobiles", "Large Cap"),
    ("SUNPHARMA", "524715", "Sun Pharmaceutical Industries Ltd", "Pharma", "Large Cap"),
    ("TRENT", "500251", "Trent Ltd", "Retail", "Large Cap"),
    ("LT", "500510", "Larsen & Toubro Ltd", "Infrastructure", "Large Cap"),
    ("BAJFINANCE", "500034", "Bajaj Finance Ltd", "NBFC", "Large Cap"),
    ("MARUTI", "532500", "Maruti Suzuki India Ltd", "Automobiles", "Large Cap"),
    ("ETERNAL", "543320", "Eternal Ltd", "Consumer Tech", "Large Cap"),
    ("HAL", "541154", "Hindustan Aeronautics Ltd", "Defence", "Large Cap"),
    ("ITC", "500875", "ITC Ltd", "FMCG", "Large Cap"),
    ("TITAN", "500114", "Titan Company Ltd", "Consumer Discretionary", "Large Cap"),
    ("KPITTECH", "542651", "KPIT Technologies Ltd", "Auto Tech / ER&D", "Mid Cap"),
    ("CIPLA", "500087", "Cipla Ltd", "Pharma", "Large Cap"),
]


# Key/value app state, e.g. Push refresh bookkeeping (last_refreshed_at, last_refresh_error)
_APP_META_DDL = (
    "CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT DEFAULT (datetime('now')))"
)


def get_conn(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db(db_path: Path = DB_PATH) -> None:
    with get_conn(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS portfolio (
                ticker TEXT PRIMARY KEY,
                bse_code TEXT NOT NULL,
                company_name TEXT NOT NULL,
                sector TEXT NOT NULL,
                cap_tier TEXT NOT NULL,
                added_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS eod_prices (
                ticker TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                close_price REAL NOT NULL,
                prev_close REAL NOT NULL,
                pct_change REAL NOT NULL,
                volume INTEGER NOT NULL,
                avg_volume_20d REAL NOT NULL,
                volume_ratio REAL NOT NULL,
                updated_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (ticker, trade_date)
            );

            CREATE TABLE IF NOT EXISTS announcements (
                filing_id TEXT PRIMARY KEY,
                ticker TEXT NOT NULL,
                company_name TEXT,
                title TEXT NOT NULL,
                pdf_url TEXT,
                published_at TEXT NOT NULL,
                exchange TEXT DEFAULT 'NSE',
                category TEXT DEFAULT 'unclassified',
                materiality INTEGER DEFAULT 0,
                summary TEXT,
                enriched_json TEXT DEFAULT '{}',
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_ann_ticker_pub ON announcements(ticker, published_at DESC);
            CREATE INDEX IF NOT EXISTS idx_ann_cat ON announcements(category);

            CREATE TABLE IF NOT EXISTS alert_rules (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                price_move_pct REAL DEFAULT 3.0,
                volume_multiplier REAL DEFAULT 2.0,
                watched_categories TEXT DEFAULT '["results","M&A","management_change","fund_raise","litigation","capex","credit_rating"]',
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS pull_reports_cache (
                ticker TEXT PRIMARY KEY,
                quarter_key TEXT NOT NULL,
                fundamentals_json TEXT NOT NULL,
                report_json TEXT NOT NULL,
                tokens_in INTEGER DEFAULT 0,
                tokens_out INTEGER DEFAULT 0,
                cost_usd REAL DEFAULT 0.0,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS llm_cost_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow TEXT NOT NULL,
                ticker TEXT,
                model TEXT NOT NULL,
                tokens_in INTEGER NOT NULL,
                tokens_out INTEGER NOT NULL,
                cost_usd REAL NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );
            """
        )
        conn.execute(_APP_META_DDL)
        _migrate_renamed_tickers(conn)
        # Seed only a brand-new DB, so stocks the user deleted stay deleted across restarts.
        if conn.execute("SELECT COUNT(*) FROM portfolio").fetchone()[0] == 0:
            conn.executemany(
                """
                INSERT INTO portfolio (ticker, bse_code, company_name, sector, cap_tier)
                VALUES (?, ?, ?, ?, ?)
                """,
                SEED_PORTFOLIO,
            )
        conn.execute("INSERT OR IGNORE INTO alert_rules (id) VALUES (1);")


def _migrate_renamed_tickers(conn: sqlite3.Connection) -> None:
    """Move rows from old symbols to new ones. Idempotent: a no-op once no old-symbol rows remain.

    UPDATE OR IGNORE skips rows whose new key already exists (e.g. an ETERNAL price for the same day);
    those old-symbol duplicates are stale, so they are deleted.
    """
    seed_names = {row[0]: row[2] for row in SEED_PORTFOLIO}
    for old, new in TICKER_RENAMES.items():
        conn.execute(
            "UPDATE OR IGNORE portfolio SET ticker = ?, company_name = ? WHERE ticker = ?",
            (new, seed_names.get(new, new), old),
        )
        for table in ("eod_prices", "announcements", "pull_reports_cache"):
            conn.execute(f"UPDATE OR IGNORE {table} SET ticker = ? WHERE ticker = ?", (new, old))
        for table in ("portfolio", "eod_prices", "announcements", "pull_reports_cache"):
            conn.execute(f"DELETE FROM {table} WHERE ticker = ?", (old,))


def get_portfolio(db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    with get_conn(db_path) as conn:
        rows = conn.execute("SELECT * FROM portfolio ORDER BY ticker").fetchall()
        return [dict(r) for r in rows]


def get_meta(db_path: Path = DB_PATH) -> dict[str, str]:
    with get_conn(db_path) as conn:
        conn.execute(_APP_META_DDL)  # also on read: a DB copied from before app_meta existed must not crash the digest
        return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM app_meta")}


def set_meta(values: dict[str, str], db_path: Path = DB_PATH) -> None:
    with get_conn(db_path) as conn:
        conn.execute(_APP_META_DDL)
        conn.executemany(
            "INSERT INTO app_meta (key, value, updated_at) VALUES (?, ?, datetime('now'))"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            list(values.items()),
        )


def get_alert_rules(db_path: Path = DB_PATH) -> dict[str, Any]:
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM alert_rules WHERE id = 1").fetchone()
        data = dict(row)
        data["watched_categories"] = json.loads(data["watched_categories"])
        return data

