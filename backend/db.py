import sqlite3
import json
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).parent / "finn.db"

SEED_PORTFOLIO = [
    ("RELIANCE", "500325", "Reliance Industries Ltd", "Energy & Retail", "Large Cap"),
    ("TCS", "532540", "Tata Consultancy Services Ltd", "IT Services", "Large Cap"),
    ("HDFCBANK", "500180", "HDFC Bank Ltd", "Banking", "Large Cap"),
    ("INFY", "500209", "Infosys Ltd", "IT Services", "Large Cap"),
    ("TATAMOTORS", "500570", "Tata Motors Ltd", "Automobiles", "Large Cap"),
    ("SUNPHARMA", "524715", "Sun Pharmaceutical Industries Ltd", "Pharma", "Large Cap"),
    ("TRENT", "500251", "Trent Ltd", "Retail", "Large Cap"),
    ("LT", "500510", "Larsen & Toubro Ltd", "Infrastructure", "Large Cap"),
    ("BAJFINANCE", "500034", "Bajaj Finance Ltd", "NBFC", "Large Cap"),
    ("MARUTI", "532500", "Maruti Suzuki India Ltd", "Automobiles", "Large Cap"),
    ("ZOMATO", "543320", "Zomato Ltd (Eternal)", "Consumer Tech", "Large Cap"),
    ("HAL", "541154", "Hindustan Aeronautics Ltd", "Defence", "Large Cap"),
    ("ITC", "500875", "ITC Ltd", "FMCG", "Large Cap"),
    ("TITAN", "500114", "Titan Company Ltd", "Consumer Discretionary", "Large Cap"),
    ("KPITTECH", "542651", "KPIT Technologies Ltd", "Auto Tech / ER&D", "Mid Cap"),
    ("CIPLA", "500087", "Cipla Ltd", "Pharma", "Large Cap"),
]


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
        conn.executemany(
            """
            INSERT OR IGNORE INTO portfolio (ticker, bse_code, company_name, sector, cap_tier)
            VALUES (?, ?, ?, ?, ?)
            """,
            SEED_PORTFOLIO,
        )
        conn.execute("INSERT OR IGNORE INTO alert_rules (id) VALUES (1);")


def get_portfolio(db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    with get_conn(db_path) as conn:
        rows = conn.execute("SELECT * FROM portfolio ORDER BY ticker").fetchall()
        return [dict(r) for r in rows]


def get_alert_rules(db_path: Path = DB_PATH) -> dict[str, Any]:
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM alert_rules WHERE id = 1").fetchone()
        data = dict(row)
        data["watched_categories"] = json.loads(data["watched_categories"])
        return data

