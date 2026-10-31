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


def _wb(pattern: str) -> re.Pattern[str]:
    """Compile `pattern` so every alternative matches whole words only: "fine" no longer hits "refinery".
    Same as wrapping in \\b...\\b, but also works when a phrase ends in punctuation, e.g. "regulation 74(5)"."""
    return re.compile(rf"(?<!\w)(?:{pattern})(?!\w)")


def _wb_any(phrases: list[str]) -> re.Pattern[str]:
    return _wb("|".join(re.escape(p) for p in phrases))


# ponytail: fast regex-based pre-classifier before LLM triage so basic rule categorization works with zero LLM cost.
# First match wins. Stems end in \w* because matching is whole-word.
CATEGORY_RULES: list[tuple[re.Pattern[str], str, int]] = [
    (_wb(r"financial results?|un-?audited|audited|outcome of board meeting.*results?|investor presentation.*q[1-4]|earnings?(?! call| conference)"), "results", 1),
    (_wb(r"acqui\w*|mergers?|demergers?|amalgamation|joint ventures?|stakes?|subsidiar(?:y|ies)|incorporation|slump sale"), "M&A", 1),
    (_wb(r"qip|qualified institution\w*|preferential|rights issue|buy-?back|allotment of .*debentures?|non[- ]convertible|ncds?|commercial paper|fund rais\w*"), "fund_raise", 1),
    # No bare "board of directors": board meeting intimations name the board without any change in it
    (_wb(r"resign\w*|appoint\w*|cessation|retire\w*|managing director|ceo|cfo|whole-?time director|key managerial|change in (?:the )?(?:directors?|management|kmp)"), "management_change", 1),
    # Phrases only. Bare "sebi"/"gst" hit every Reg 30 / GST registration filing, bare "order" hits order wins, and bare
    # "notice" hits AGM and board meeting notices.
    (_wb(r"litigation|lawsuit|penalt(?:y|ies)|levied a fine|imposed a fine|fine of|show[- ]cause notice|demand notice|tax demand|demand order|assessment order|adjudication order|court order|tribunal order|order passed|writ petition|warning letter|arbitration|nclt|insolvency|disputes?"), "litigation", 1),
    (_wb(r"sast|insider trading|continual disclosure|regulation 7\(2\)|regulation 29"), "insider_trading", 1),
    (_wb(r"earnings call|concall|conference call|audio recording|transcript"), "earnings_call", 1),
    (_wb(r"analysts?|institutional investors?|investor meet|schedule of .*meet\w*"), "investor_meetings", 0),
    (_wb(r"credit rating|crisil|icra|care ratings|india ratings|brickwork"), "credit_rating", 1),
    (_wb(r"dividends?"), "dividend", 1),
    (_wb(r"bonus|sub-division|stock split"), "bonus_split", 1),
    (_wb(r"capacity expansion|capex|new plant|commission(?:s|ed|ing)|commercial production"), "capex", 1),
    # Order wins are press_release per the ENAM reference ("General news — order wins, new products, partnerships")
    (_wb(r"press release|media release|orders? received|receipt of (?:an? )?orders?|bagged.*orders?|order wins?|launch\w*|partnerships?"), "press_release", 1),
    (_wb(r"postal ballot"), "postal_ballot", 0),
    (_wb(r"annual general meeting|extraordinary general meeting|agm|egm"), "agm_egm", 0),
    (_wb(r"pledg\w*|encumbrances?|release of shares"), "pledging", 1),
]

_ROUTINE_NOISE = _wb_any([
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
    "esops",
    "employee stock option",
    "closure of trading window",
    "trading window",
    "compliance certificate",
    "redemption of commercial paper",
])
_INVESTOR_MEET = _wb_any([
    "analysts/institutional investor meet",
    "analyst/institutional investor meet",
    "schedule of analyst",
    "investor meet",
])
_RECORDING = _wb_any(["transcript", "audio recording", "link of recording", "video recording"])
# Generic date phrases: only a prior-notice signal on results rows (an earnings call "is scheduled on" stays a call)
_RESULTS_DATE_NOTICE = _wb_any(["is scheduled on", "will be held on"])
_BOARD_MEETING_NOTICE = _wb(
    r"meeting of the board of directors of the company is scheduled|intimation of (?:the )?board meeting"
    r"|notice of (?:the )?board meeting|board of directors (?:to|will|shall) meet|board meeting (?:is )?(?:scheduled|to be held)"
)
_OUTCOME = _wb_any(["outcome of board meeting"])
_IPO_SUBSIDIARY = _wb(r"jio platforms|initial public offer\w*")
# ENAM puts auditor appointments in `unclassified` (routine paperwork); scrutinizers are AGM/postal ballot admin.
# Matches only the auditor clause itself: the gap can't cross a comma, semicolon or another "appoint", so in
# "appointment of Mr. A as CFO and re-appointment of Cost Auditors" only the second clause matches.
_AUDITOR_APPOINTMENT = _wb(
    r"(?:re-?)?appoint\w*(?:(?!appoint)[^,;])*?(?:(?:statutory|secretarial|cost|internal)\s+auditors?|scrutini[sz]ers?)"
    r"(?:\s+of\s+(?:the\s+)?(?:company|(?:wholly[- ]owned\s+)?subsidiar(?:y|ies)))?"
    r"|(?:statutory|secretarial|cost|internal)\s+auditors?[\s:–-]*(?:re-?)?appoint\w*"
)
_DEBT_PAPER = _wb(r"non[- ]convertible|ncds?|debentures?|commercial papers?")
_SUBSIDIARY_SUPPORT = _wb(r"letters? of comfort|comfort letters?|corporate guarantees?|guarantees?|surety")
_SUBSIDIARY = _wb(r"subsidiar(?:y|ies)")
# ponytail: name-based lender check. No offline bank/NBFC helper exists (results enrichment reads Screener's
# "Financing Profit" row over the network), so this misses lenders without these words in their name (e.g. REC Ltd).
# Upgrade path: store an is_financial flag on the portfolio row from the Screener scrape.
_LENDER_NAME = _wb(r"banks?|nbfc|finance|financial|fincorp|finserv")


def is_bank_or_nbfc(company_name: str) -> bool:
    return bool(_LENDER_NAME.search((company_name or "").lower()))


# Titles that name the filing type but not the event ("Change in Director(s)", "Disclosure under Reg 30")
_GENERIC_TITLE = _wb(
    r"regulation 30|reg\.? 30|outcome of (?:the )?board meeting|change in (?:the )?(?:director\(s\)|directors?|management|kmp)"
    r"|resignation of director/kmp/smp|general updates?|issue of securities|intimation under sebi|disclosure under"
)
PERSON_HONORIFIC = re.compile(r"\b(?:mr|mrs|ms|dr|shri|smt)\b\.?\s+[a-z]")
_RESULTS_WORD = _wb(r"results?")


def is_vague_title(title: str) -> bool:
    """A generic filing type with no person named and no results mentioned: the event is only in the PDF."""
    t = (title or "").lower()
    return bool(_GENERIC_TITLE.search(t)) and not PERSON_HONORIFIC.search(t) and not _RESULTS_WORD.search(t)


def enforce_filing_hygiene(title: str, category: str, materiality: int, is_lender: bool = False) -> tuple[str, int]:
    """
    Root-cause guard ensuring routine exchange noise (newspaper clippings, Reg 74(5) depository
    certificates, trading window closures, analyst meet schedules, prior board meeting notices,
    auditor appointments, lenders' routine debt allotments) is never promoted to materiality=1
    by either regex heuristics or LLM triage. `is_lender`: the filer is a bank/NBFC (see is_bank_or_nbfc).
    """
    t = (title or "").lower()

    # 1. Hard routine noise -> always unclassified, materiality=0
    if _ROUTINE_NOISE.search(t):
        return "unclassified", 0
    # Demote an auditor appointment only when it is the whole event. A board outcome that also declares a dividend
    # or appoints a CFO is classified on what is left after the auditor clause is cut out.
    if category != "results" and _AUDITOR_APPOINTMENT.search(t):
        rest = _AUDITOR_APPOINTMENT.sub(" ", t)
        category, materiality = next(((c, m) for p, c, m in CATEGORY_RULES if p.search(rest)), ("unclassified", 0))
        if not materiality:
            return "unclassified", 0

    # A letter of comfort / guarantee for a subsidiary raises no money from outside investors (ENAM's fund_raise) and
    # moves no money into the subsidiary (ENAM's M&A "investing in a subsidiary"). It is a contingent liability that
    # Pull's balance-sheet module picks up from the annual report, so in the feed it is routine paperwork.
    if _SUBSIDIARY_SUPPORT.search(t) and _SUBSIDIARY.search(t):
        return "unclassified", 0

    # Banks and NBFCs issue NCDs / commercial paper every week as their lending raw material: routine for them.
    # A non-financial company's NCD issue stays a material fund_raise.
    if is_lender and category == "fund_raise" and _DEBT_PAPER.search(t):
        return "fund_raise", 0

    # 2. Analyst / Investor Meet updates: only material if it is an actual Transcript or Recording
    if _INVESTOR_MEET.search(t):
        if _RECORDING.search(t):
            return "earnings_call", 1
        return "investor_meetings", 0

    # 3. Prior board meeting date intimations (for results, fund raising or anything else) are routine
    if not _OUTCOME.search(t):
        if category == "results" and _IPO_SUBSIDIARY.search(t):
            return "fund_raise", 1
        if _BOARD_MEETING_NOTICE.search(t) or (category == "results" and _RESULTS_DATE_NOTICE.search(t)):
            return "unclassified", 0

    if category in ("unclassified", "investor_meetings", "postal_ballot", "agm_egm"):
        return category, 0

    return category, materiality


def classify_heuristic(title: str, desc: str = "", is_lender: bool = False) -> tuple[str, int]:
    full_title = f"{title}: {desc}" if desc and desc != title else title
    text = full_title.lower()
    cat, mat = "unclassified", 0
    for pattern, c, m in CATEGORY_RULES:
        if pattern.search(text):
            cat, mat = c, m
            break
    return enforce_filing_hygiene(full_title, cat, mat, is_lender=is_lender)


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

                cat, materiality = classify_heuristic(subject, att_text, is_lender=is_bank_or_nbfc(company_name))
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

                cat, materiality = classify_heuristic(headline, more, is_lender=is_bank_or_nbfc(company_name))
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

