import hashlib
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote
import httpx
from backend.db import get_conn, normalize_ticker, DB_PATH

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# ponytail: fast regex-based pre-classifier before LLM triage so basic rule categorization works with zero LLM cost
CATEGORY_RULES: list[tuple[str, str, int]] = [
    (r"financial result|un-audited|audited|outcome of board meeting.*result|investor presentation.*q[1-4]|earning", "results", 1),
    (r"acqui|merger|demerger|amalgamation|joint venture|stake|subsidiary|incorporation|slump sale", "M&A", 1),
    (r"qip|qualified institutional|preferential|rights issue|buyback|allotment of .*debenture|commercial paper|fund rais", "fund_raise", 1),
    (r"resign|appoint|cessation|managing director|ceo|cfo|whole-time director|key managerial|board of directors", "management_change", 1),
    (r"litigation|penalty|fine|order passed|show cause|arbitration|nclt|insolvency|sebi|tax demand|gst", "litigation", 1),
    (r"sast|insider trading|continual disclosure|regulation 7\(2\)|regulation 29", "insider_trading", 1),
    (r"earnings call|concall|conference call|audio recording|transcript", "earnings_call", 1),
    (r"analyst|institutional investor|investor meet|schedule of .*meet", "investor_meetings", 0),
    (r"credit rating|crisil|icra|care ratings|india ratings|brickwork", "credit_rating", 1),
    (r"dividend|record date.*dividend", "dividend", 1),
    (r"bonus|sub-division|stock split", "bonus_split", 1),
    (r"capacity expansion|capex|new plant|commissioning|commercial production", "capex", 1),
    (r"press release|media release|bagged.*order|order win|launch|partnership", "press_release", 1),
    (r"postal ballot", "postal_ballot", 0),
    (r"annual general meeting|extraordinary general meeting|\bagm\b|\begm\b", "agm_egm", 0),
    (r"pledge|encumbrance|release of shares", "pledging", 1),
]


def enforce_filing_hygiene(title: str, category: str, materiality: int) -> tuple[str, int]:
    """
    Root-cause guard ensuring routine exchange noise (newspaper clippings, Reg 74(5) depository
    certificates, trading window closures, analyst meet schedules, prior board meeting notices)
    is never promoted to materiality=1 by either regex heuristics or LLM triage.
    """
    t = (title or "").lower()

    # 1. Hard routine noise -> always unclassified, materiality=0
    if any(
        noise in t
        for noise in [
            "copy of newspaper publication",
            "newspaper publication",
            "newspaper advertisement",
            "newspaper clipping",
            "newspaper intimation",
            "certificate under sebi (depositories and participants)",
            "regulation 74(5)",
            "loss of share certificate",
            "duplicate share",
            "esop",
            "employee stock option",
            "closure of trading window",
            "trading window",
            "compliance certificate",
            "redemption of commercial paper",
        ]
    ):
        return "unclassified", 0

    # 2. Analyst / Investor Meet updates: only material if it is an actual Transcript or Recording
    if any(
        k in t
        for k in [
            "analysts/institutional investor meet",
            "analyst/institutional investor meet",
            "schedule of analyst",
            "investor meet",
        ]
    ):
        if any(rec in t for rec in ["transcript", "audio recording", "link of recording", "video recording"]):
            return "earnings_call", 1
        return "investor_meetings", 0

    # 3. Prior board meeting date intimations vs actual quarterly financial results
    if category == "results":
        if ("jio platforms" in t or "initial public offer" in t) and "outcome of board meeting" not in t:
            return "fund_raise", 1
        if any(
            prior in t
            for prior in [
                "is scheduled on",
                "will be held on",
                "meeting of the board of directors of the company is scheduled",
                "intimation of board meeting",
            ]
        ) and "outcome of board meeting" not in t:
            return "unclassified", 0

    if category in ("unclassified", "investor_meetings", "postal_ballot", "agm_egm"):
        return category, 0

    return category, materiality


def classify_heuristic(title: str, desc: str = "") -> tuple[str, int]:
    full_title = f"{title}: {desc}" if desc and desc != title else title
    text = full_title.lower()
    cat, mat = "unclassified", 0
    for pattern, c, m in CATEGORY_RULES:
        if re.search(pattern, text):
            cat, mat = c, m
            break
    return enforce_filing_hygiene(full_title, cat, mat)


def _make_filing_id(ticker: str, title: str, pub_dt: str, pdf_url: str) -> str:
    raw = f"{ticker}|{title.strip()}|{pub_dt[:16]}|{pdf_url}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def fetch_nse_announcements(ticker: str, days: int = 10) -> list[dict[str, Any]]:
    """
    Fetch corporate announcements for an NSE ticker using NSE's official JSON API
    with session cookie warm-up.
    """
    clean_ticker = normalize_ticker(ticker)
    symbol_q = quote(clean_ticker, safe="")  # "M&M" -> "M%26M" so '&' can't split the query string
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"https://www.nseindia.com/get-quotes/equity?symbol={symbol_q}",
    }
    to_date = datetime.now().strftime("%d-%m-%Y")
    from_date = (datetime.now() - timedelta(days=days)).strftime("%d-%m-%Y")
    url = (
        f"https://www.nseindia.com/api/corporate-announcements"
        f"?index=equities&symbol={symbol_q}&from_date={from_date}&to_date={to_date}"
    )

    items: list[dict[str, Any]] = []
    try:
        with httpx.Client(headers=headers, follow_redirects=True, timeout=15.0) as client:
            # Warm up NSE cookies
            client.get("https://www.nseindia.com")
            resp = client.get(url)
            if resp.status_code != 200:
                return []
            data = resp.json()
            if not isinstance(data, list):
                return []

            for row in data[:25]:
                subject = (row.get("desc") or "").strip()
                att_text = (row.get("attchmntText") or "").strip()
                title = f"{subject}: {att_text}" if att_text and att_text != subject else subject
                pdf_url = (row.get("attchmntFile") or "").strip()
                pub_raw = (row.get("an_dt") or row.get("sort_date") or "").strip()
                company_name = (row.get("sm_name") or clean_ticker).strip()

                # Normalize date to ISO 8601 if possible
                pub_iso = pub_raw
                for fmt in ("%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%b-%Y %H:%M"):
                    try:
                        pub_iso = datetime.strptime(pub_raw, fmt).strftime("%Y-%m-%d %H:%M:%S")
                        break
                    except ValueError:
                        continue

                cat, materiality = classify_heuristic(subject, att_text)
                filing_id = _make_filing_id(clean_ticker, title, pub_iso, pdf_url)
                items.append(
                    {
                        "filing_id": filing_id,
                        "ticker": clean_ticker,
                        "company_name": company_name,
                        "title": title[:500],
                        "pdf_url": pdf_url,
                        "published_at": pub_iso,
                        "exchange": "NSE",
                        "category": cat,
                        "materiality": materiality,
                    }
                )
    except Exception as exc:
        print(f"[filings:NSE] {clean_ticker} failed: {exc}")
    return items


def fetch_bse_announcements(ticker: str, bse_code: str, days: int = 10) -> list[dict[str, Any]]:
    """
    Fallback/supplemental fetcher using BSE India's public corporate announcements API.
    """
    if not bse_code:
        return []
    clean_ticker = normalize_ticker(ticker)
    to_dt = datetime.now().strftime("%Y%m%d")
    from_dt = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    url = (
        f"https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
        f"?pageno=1&strCat=-1&strPrevDate={from_dt}&strScrip={bse_code}"
        f"&strSearch=P&strToDate={to_dt}&strType=C&subcategory=-1"
    )
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.bseindia.com/",
        "Origin": "https://www.bseindia.com",
    }
    items: list[dict[str, Any]] = []
    try:
        with httpx.Client(headers=headers, follow_redirects=True, timeout=15.0) as client:
            resp = client.get(url)
            if resp.status_code != 200:
                return []
            payload = resp.json()
            rows = payload.get("Table") or []
            for row in rows[:25]:
                headline = (row.get("NEWSSUB") or row.get("HEADLINE") or "").strip()
                more = (row.get("MORE") or "").strip()
                title = f"{headline} — {more}" if more and more not in headline else headline
                att = (row.get("ATTACHMENTNAME") or "").strip()
                pdf_url = f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{att}" if att else ""
                pub_raw = (row.get("NEWS_DT") or "").replace("T", " ")[:19]
                company_name = (row.get("SLONGNAME") or clean_ticker).strip()

                cat, materiality = classify_heuristic(headline, more)
                filing_id = _make_filing_id(clean_ticker, title, pub_raw, pdf_url)
                items.append(
                    {
                        "filing_id": filing_id,
                        "ticker": clean_ticker,
                        "company_name": company_name,
                        "title": title[:500],
                        "pdf_url": pdf_url,
                        "published_at": pub_raw,
                        "exchange": "BSE",
                        "category": cat,
                        "materiality": materiality,
                    }
                )
    except Exception as exc:
        print(f"[filings:BSE] {clean_ticker} ({bse_code}) failed: {exc}")
    return items


def fetch_company_filings(
    ticker: str, bse_code: str = "", days: int = 10, db_path: Path = DB_PATH, save_to_db: bool = True
) -> list[dict[str, Any]]:
    """
    Fetch recent filings for a stock (NSE primary, BSE fallback), classify them,
    and persist to SQLite.
    """
    filings = fetch_nse_announcements(ticker, days=days)
    if not filings and bse_code:
        filings = fetch_bse_announcements(ticker, bse_code=bse_code, days=days)

    if save_to_db and filings:
        with get_conn(db_path) as conn:
            for f in filings:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO announcements
                    (filing_id, ticker, company_name, title, pdf_url, published_at, exchange, category, materiality)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f["filing_id"],
                        f["ticker"],
                        f["company_name"],
                        f["title"],
                        f["pdf_url"],
                        f["published_at"],
                        f["exchange"],
                        f["category"],
                        f["materiality"],
                    ),
                )
    return filings

