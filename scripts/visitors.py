"""Who used FINN: real browser sessions and link previews from Caddy's JSON access log.

Run on the EC2 host:  sudo python3 scripts/visitors.py [--since 2026-11-08] [--me 1.2.3.4] [--no-lookup]
Standard library only.

A "real visitor" is an IP whose browser ran the app: it called the app's startup APIs (/api/portfolio, /api/push/...).
Scanners request the page or probe paths like /.env but never call these, so they are counted, not listed.
A "link preview" is a chat or social app fetching the page after someone pasted the link (WhatsApp, LinkedIn, Slack...).
"""
import argparse
import glob
import gzip
import json
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

LOG_GLOB = "/var/lib/docker/volumes/*caddy_data/_data/access*.log*"
IST = timezone(timedelta(hours=5, minutes=30))
STARTUP = {"/api/portfolio", "/api/push/digest", "/api/push/alerts", "/api/pull-cached", "/api/cost"}
PREVIEW_UA = {"facebookexternalhit": "Meta (WhatsApp/Instagram/Messenger)", "whatsapp": "WhatsApp",
              "linkedinbot": "LinkedIn", "slackbot": "Slack", "slack-imgproxy": "Slack", "telegrambot": "Telegram",
              "discordbot": "Discord", "twitterbot": "X/Twitter", "skypeuripreview": "Teams/Skype",
              "microsoftpreview": "Teams/Outlook"}
# Search crawlers (often faked by scanners) are counted separately: they say nothing about a person sharing the link.
CRAWLER_UA = ("googlebot", "bingbot", "yandex", "baiduspider", "duckduckbot", "applebot", "ahrefs", "semrush")
PREVIEW_IP = {"2a03:2880:": "Meta (WhatsApp/Instagram/Messenger)"}  # Meta's crawler sometimes sends a plain Chrome UA


def read_events(since):
    for path in sorted(glob.glob(LOG_GLOB)):
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                ts = datetime.fromtimestamp(e.get("ts", 0), IST)
                if since and ts < since:
                    continue
                r = e.get("request") or {}
                h = r.get("headers") or {}
                yield (ts, r.get("client_ip") or r.get("remote_ip") or "?", r.get("uri") or "", e.get("status"),
                       ((h.get("User-Agent") or [""])[0]), ((h.get("Referer") or [""])[0]))


def lookup(ip):
    try:
        with urllib.request.urlopen(f"https://ipinfo.io/{ip}/json", timeout=5) as r:
            j = json.load(r)
        return f"{j.get('org', '?')} | {j.get('city', '?')}, {j.get('country', '?')}"
    except Exception:
        return "network lookup failed"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="only requests on/after this IST date, e.g. 2026-11-08")
    ap.add_argument("--me", action="append", default=[], help="your own IP(s), labelled YOU")
    ap.add_argument("--no-lookup", action="store_true", help="skip the ipinfo.io network/city lookup")
    args = ap.parse_args()
    since = datetime.fromisoformat(args.since).replace(tzinfo=IST) if args.since else None

    v = defaultdict(lambda: {"hits": [], "ua": Counter(), "ref": set()})
    previews, crawler_hits, total, first, last = [], 0, 0, None, None
    for ts, ip, uri, status, ua, ref in read_events(since):
        total += 1
        first, last = min(first or ts, ts), max(last or ts, ts)
        if any(c in ua.lower() for c in CRAWLER_UA):
            crawler_hits += 1
            continue
        src = next((name for k, name in PREVIEW_UA.items() if k in ua.lower()), None) or \
            next((name for k, name in PREVIEW_IP.items() if ip.startswith(k)), None)
        if src:
            previews.append((ts, src, ip))
            continue
        x = v[ip]
        x["hits"].append((ts, uri, status))
        x["ua"][ua[:120]] += 1
        if ref and "13.233.140.42" not in ref:
            x["ref"].add(ref[:80])

    def is_real(x):
        # The page's startup fires these together; a scanner may probe one, but not get 200s from 3 and few 404s.
        ok_startup = {u.split("?")[0] for _, u, s in x["hits"] if s == 200 and u.split("?")[0] in STARTUP}
        misses = sum(1 for _, u, s in x["hits"] if s == 404 and not u.startswith("/favicon"))
        return len(ok_startup) >= 3 and misses <= len(x["hits"]) * 0.2

    real = {ip: x for ip, x in v.items() if is_real(x)}
    if not total:
        print("No requests in the log for this period.")
        return
    print(f"Log {first:%a %d %b %H:%M} -> {last:%a %d %b %H:%M} IST: {total} requests from {len(v)} IPs.")
    print(f"Real visitors (browser ran the app): {len(real)}. Scanners/bots ignored: {len(v) - len(real)}.\n")

    for ip, x in sorted(real.items(), key=lambda kv: kv[1]["hits"][0][0]):
        hits = x["hits"]
        paths = [u for _, u, _ in hits]
        reports = Counter(u.split("?")[0].rsplit("/", 1)[1] for u in paths
                          if u.startswith("/api/pull/") and u.split("?")[0].count("/") == 3)
        tickers = sorted({u.split("ticker=")[1].split("&")[0] for u in paths if "ticker=" in u})
        errors = [(f"{t:%H:%M}", u[:50], s) for t, u, s in hits if s and s >= 400 and not u.startswith("/favicon")]
        mins = (hits[-1][0] - hits[0][0]).total_seconds() / 60
        who = "YOU" if ip in args.me else "VISITOR"
        print(f"[{who}] {ip}  {hits[0][0]:%a %d %b %H:%M} -> {hits[-1][0]:%H:%M} IST ({mins:.0f} min, {len(hits)} requests)")
        if not args.no_lookup:
            print(f"   network: {lookup(ip)}")
        print(f"   device:  {x['ua'].most_common(1)[0][0]}")
        if x["ref"]:
            print(f"   came from: {', '.join(sorted(x['ref']))}")
        if reports:
            print(f"   Pull reports opened: {', '.join(t + (f' x{n}' if n > 1 else '') for t, n in sorted(reports.items()))}")
        if tickers:
            print(f"   Push filtered by: {', '.join(tickers)}")
        print(f"   copilot questions: {sum(u.split('?')[0].endswith('/ask') for u in paths)}, "
              f"re-runs: {sum('force=true' in u for u in paths)}, "
              f"Refresh Live: {sum(u.split('?')[0] == '/api/push/refresh' for u in paths)}, "
              f"errors: {errors or 'none'}")
        print()

    if previews:
        print("Link previews (someone pasted the link into a chat/social app), grouped by 10 minutes:")
        groups = Counter((ts.replace(minute=ts.minute // 10 * 10, second=0, microsecond=0), src) for ts, src, _ in previews)
        for (ts, src), n in sorted(groups.items()):
            print(f"   {ts:%a %d %b %H:%M} IST  {src}  ({n} request{'s' if n > 1 else ''})")
    else:
        print("Link previews: none.")
    print(f"Search-engine crawler requests ignored: {crawler_hits}")


if __name__ == "__main__":
    main()
