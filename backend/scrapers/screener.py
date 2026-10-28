from typing import Any
import httpx
from bs4 import BeautifulSoup, Tag

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def _parse_html_table(section: Tag | None) -> dict[str, Any]:
    if not section:
        return {"headers": [], "rows": []}
    table = section.find("table", class_="data-table") or section.find("table")
    if not table:
        return {"headers": [], "rows": []}

    headers: list[str] = []
    thead = table.find("thead")
    if thead:
        headers = [th.get_text(" ", strip=True) for th in thead.find_all("th")]

    rows: list[dict[str, str]] = []
    tbody = table.find("tbody")
    if tbody:
        for tr in tbody.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
            if not cells or all(c == "" for c in cells):
                continue
            metric_name = cells[0].replace("+", "").strip()
            if headers and len(cells) == len(headers):
                row_dict = {"metric": metric_name}
                for h, val in zip(headers[1:], cells[1:]):
                    if h:
                        row_dict[h] = val
                rows.append(row_dict)
            else:
                rows.append({"metric": metric_name, "values": cells[1:]})

    return {"headers": [h for h in headers if h], "rows": rows}


def fetch_screener_fundamentals(ticker: str) -> dict[str, Any]:
    """
    Scrape Screener.in for any NSE/BSE ticker:
    -Tries /company/{TICKER}/consolidated/ first; falls back to /company/{TICKER}/ if empty.
    -Extracts Top Ratios, Quarterly Results, Annual P&L, Balance Sheet, Cash Flow, Ratios,
     plus links to Annual Reports and Concall Transcripts / Notes.
    """
    clean_ticker = ticker.upper().replace(".NS", "").replace(".BO", "").strip()
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    urls = [
        f"https://www.screener.in/company/{clean_ticker}/consolidated/",
        f"https://www.screener.in/company/{clean_ticker}/",
    ]

    with httpx.Client(headers=headers, follow_redirects=True, timeout=20.0) as client:
        soup: BeautifulSoup | None = None
        used_url = urls[0]

        for candidate_url in urls:
            resp = client.get(candidate_url)
            if resp.status_code != 200:
                continue
            candidate_soup = BeautifulSoup(resp.text, "html.parser")
            q_table = _parse_html_table(candidate_soup.find("section", id="quarters"))
            # Check if consolidated actually has data rows
            if q_table["rows"] and len(q_table["headers"]) >= 2:
                soup = candidate_soup
                used_url = str(resp.url)
                break
            if soup is None:
                soup = candidate_soup
                used_url = str(resp.url)

        if soup is None:
            raise ValueError(f"Could not find ticker {clean_ticker} on Screener.in")

        # 1. Company Name & About
        h1 = soup.find("h1")
        company_name = h1.get_text(strip=True) if h1 else clean_ticker
        about_div = soup.find("div", class_="company-profile") or soup.find("div", class_="about")
        about_text = about_div.get_text(" ", strip=True) if about_div else ""

        # 2. Key Top Ratios (Market Cap, Current Price, Stock P/E, ROCE, ROE, etc.)
        top_ratios: dict[str, str] = {}
        ratios_ul = soup.find("ul", id="top-ratios")
        if ratios_ul:
            for li in ratios_ul.find_all("li"):
                name_el = li.find("span", class_="name")
                val_el = li.find("span", class_="value") or li.find("span", class_="number")
                if name_el and val_el:
                    top_ratios[name_el.get_text(" ", strip=True)] = val_el.get_text(" ", strip=True)

        # 3. Core Financial Tables
        quarters = _parse_html_table(soup.find("section", id="quarters"))
        profit_loss = _parse_html_table(soup.find("section", id="profit-loss"))
        balance_sheet = _parse_html_table(soup.find("section", id="balance-sheet"))
        cash_flow = _parse_html_table(soup.find("section", id="cash-flow"))
        ratios = _parse_html_table(soup.find("section", id="ratios"))

        # 4. Documents Section: Annual Reports & Concalls
        annual_reports: list[dict[str, str]] = []
        concalls: list[dict[str, str]] = []

        docs_section = soup.find("section", id="documents")
        if docs_section:
            # Annual reports
            ar_box = docs_section.find("div", class_="annual-reports")
            if ar_box:
                for a in ar_box.find_all("a", href=True):
                    title = " ".join(a.get_text(" ", strip=True).split())
                    href = a["href"]
                    if href.startswith("/"):
                        href = f"https://www.screener.in{href}"
                    annual_reports.append({"title": title, "url": href})

            # Concalls (Transcripts, Notes, PPTs)
            cc_box = docs_section.find("div", class_="concalls")
            if cc_box:
                for li in cc_box.find_all("li"):
                    date_text = ""
                    # Usually first div/span has the month/year e.g. "Oct 2025"
                    first_div = li.find("div")
                    if first_div:
                        date_text = first_div.get_text(" ", strip=True)
                    else:
                        raw_t = li.get_text(" ", strip=True)
                        date_text = raw_t[:12].strip()

                    links: dict[str, str] = {}
                    for a in li.find_all("a", href=True):
                        label = a.get_text(" ", strip=True).lower()
                        href = a["href"]
                        if href.startswith("/"):
                            href = f"https://www.screener.in{href}"
                        if "transcript" in label:
                            links["transcript_url"] = href
                        elif "ppt" in label or "presentation" in label:
                            links["ppt_url"] = href
                        elif "rec" in label or "audio" in label:
                            links["audio_url"] = href
                        else:
                            links[label or "link"] = href

                    if links:
                        concalls.append({"period": date_text, **links})

        return {
            "ticker": clean_ticker,
            "company_name": company_name,
            "screener_url": used_url,
            "about": about_text,
            "top_ratios": top_ratios,
            "quarters": quarters,
            "profit_loss": profit_loss,
            "balance_sheet": balance_sheet,
            "cash_flow": cash_flow,
            "ratios": ratios,
            "annual_reports": annual_reports[:5],
            "concalls": concalls[:8],
        }

