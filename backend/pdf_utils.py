import hashlib
import io
import zipfile
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import fitz  # PyMuPDF
import httpx

CACHE_DIR = Path(__file__).parent / "cache_pdfs"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

RESULTS_KEYWORDS = [
    "revenue from operations",
    "total income",
    "profit before tax",
    "profit after tax",
    "net profit",
    "ebitda",
    "quarter ended",
    "statement of consolidated",
    "statement of standalone",
    "unaudited financial results",
    "audited financial results",
    "particulars",
]


def get_cached_pdf_path(url: str) -> Path:
    url_hash = hashlib.sha256(url.strip().encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{url_hash}.pdf"


def _candidate_pdf_urls(url: str) -> list[str]:
    """
    Expand BSE AnnPdfOpen.aspx wrapper URLs into direct AttachLive / AttachHis PDF URLs.
    """
    candidates = [url]
    if "AnnPdfOpen.aspx" in url:
        parsed = urlparse(url)
        pname = parse_qs(parsed.query).get("Pname", [""])[0]
        if pname:
            candidates = [
                f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{pname}",
                f"https://www.bseindia.com/xml-data/corpfiling/AttachHis/{pname}",
                url,
            ]
    return candidates


def download_pdf(url: str, timeout: float = 25.0) -> Path | None:
    """
    Download a PDF from NSE/BSE/Screener URL into backend/cache_pdfs/<sha256>.pdf.
    Returns the local Path if valid PDF, or None if download fails.
    """
    if not url or not url.startswith("http"):
        return None

    target_path = get_cached_pdf_path(url)
    if target_path.exists() and target_path.stat().st_size > 500:
        return target_path

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/pdf,application/octet-stream,*/*;q=0.8",
        "Referer": "https://www.nseindia.com/" if "nseindia" in url else "https://www.bseindia.com/",
    }

    try:
        with httpx.Client(headers=headers, follow_redirects=True, timeout=timeout) as client:
            for candidate in _candidate_pdf_urls(url):
                resp = client.get(candidate)
                if resp.status_code != 200:
                    continue
                if resp.content[:4] == b"%PDF":
                    target_path.write_bytes(resp.content)
                    return target_path
                # NSE often wraps filing PDFs inside a .zip archive
                if resp.content[:2] == b"PK":
                    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                        for name in zf.namelist():
                            if name.lower().endswith(".pdf"):
                                pdf_bytes = zf.read(name)
                                if pdf_bytes[:4] == b"%PDF":
                                    target_path.write_bytes(pdf_bytes)
                                    return target_path
    except Exception as exc:
        print(f"[pdf_utils] Failed to download {url}: {exc}")

    return None


def extract_pdf_text(
    url_or_path: str | Path,
    max_pages: int = 15,
    keywords: list[str] | None = None,
    max_chars: int = 45000,
    max_chars_per_page: int | None = None,
) -> str:
    """
    Extract text from a PDF (local path or URL) using PyMuPDF.
    If keywords are passed (or mode='results'), prioritizes the cover pages + pages with highest keyword hits
    so large 100-page filings don't waste LLM tokens.
    `max_chars_per_page` trims each selected page so late pages aren't crowded out by the `max_chars` cap.
    """
    pdf_path: Path | None
    if isinstance(url_or_path, Path):
        pdf_path = url_or_path if url_or_path.exists() else None
    elif str(url_or_path).startswith("http"):
        pdf_path = download_pdf(str(url_or_path))
    else:
        p = Path(str(url_or_path))
        pdf_path = p if p.exists() else None

    if not pdf_path:
        return ""

    try:
        doc = fitz.open(pdf_path)
        total_pages = len(doc)
        if total_pages == 0:
            doc.close()
            return ""

        if total_pages <= max_pages or not keywords:
            selected_indices = list(range(min(total_pages, max_pages)))
        else:
            # Always keep first 3 pages (cover letter / outcome summary)
            must_keep = set(range(min(3, total_pages)))
            scored: list[tuple[int, int]] = []
            kw_lower = [k.lower() for k in keywords]
            for idx in range(total_pages):
                if idx in must_keep:
                    continue
                # Collapse whitespace so multi-word keywords still match when wrapped across lines
                page_txt = " ".join(doc[idx].get_text("text").lower().split())
                score = sum(1 for k in kw_lower if k in page_txt)
                if score > 0:
                    scored.append((score, idx))

            scored.sort(key=lambda x: (-x[0], x[1]))
            top_kw_pages = [idx for _, idx in scored[: max_pages - len(must_keep)]]
            selected_indices = sorted(must_keep.union(top_kw_pages))
            # Fill up to max_pages if very few keyword pages matched
            if len(selected_indices) < min(6, total_pages):
                for idx in range(min(max_pages, total_pages)):
                    if idx not in selected_indices:
                        selected_indices.append(idx)
                selected_indices.sort()

        chunks: list[str] = []
        total_len = 0
        for idx in selected_indices:
            txt = doc[idx].get_text("text").strip()[:max_chars_per_page]
            if not txt:
                continue
            header = f"\n--- [Page {idx + 1} of {total_pages}] ---\n"
            piece = header + txt
            chunks.append(piece)
            total_len += len(piece)
            if total_len >= max_chars:
                break

        doc.close()
        return "\n".join(chunks)[:max_chars]
    except Exception as exc:
        print(f"[pdf_utils] Failed to parse PDF {pdf_path}: {exc}")
        return ""


def extract_results_pdf_text(url_or_path: str | Path, max_pages: int = 12) -> str:
    """Helper tailored for quarterly/annual financial results PDFs."""
    return extract_pdf_text(url_or_path, max_pages=max_pages, keywords=RESULTS_KEYWORDS, max_chars=35000)

