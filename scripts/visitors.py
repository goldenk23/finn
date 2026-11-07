"""Who visited FINN: summarise Caddy's JSON access log by visitor IP.

Run on the EC2 host:  sudo python3 scripts/visitors.py [--since 2026-11-08] [--me 1.2.3.4] [--all]
Standard library only. Bots, scanners and command-line clients are hidden unless --all is given.
"""
import argparse
import glob
import gzip
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone

LOG_GLOB = "/var/lib/docker/volumes/*caddy_data/_data/access*.log*"
IST = timezone(timedelta(hours=5, minutes=30))
BOT_UA = ("bot", "crawl", "spider", "curl", "wget", "python", "go-http", "zgrab", "masscan", "censys", "httpx",
          "scan", "headless", "okhttp", "java/", "libwww", "facebookexternalhit", "preview")
# Pages a person using the app actually requests; scanners probe paths like /.env or /wp-login.php instead.
APP_PATHS = ("/", "/_next/", "/api/", "/favicon")


def read_lines():
    for path in sorted(glob.glob(LOG_GLOB)):
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="only requests on/after this IST date, e.g. 2026-11-08")
    ap.add_argument("--me", action="append", default=[], help="your own IP(s), labelled 'you'")
    ap.add_argument("--all", action="store_true", help="include bots, scanners and command-line clients")
    args = ap.parse_args()
    since = datetime.fromisoformat(args.since).replace(tzinfo=IST) if args.since else None

    v = defaultdict(lambda: {"n": 0, "first": None, "last": None, "ua": set(), "paths": defaultdict(int), "asks": 0,
                             "reruns": 0, "tickers": set(), "probe": 0})
    for e in read_lines():
        req = e.get("request") or {}
        ts = datetime.fromtimestamp(e.get("ts", 0), IST)
        if since and ts < since:
            continue
        ip = req.get("client_ip") or req.get("remote_ip") or "?"
        uri = req.get("uri") or ""
        path = uri.split("?")[0]
        ua = ((req.get("headers") or {}).get("User-Agent") or [""])[0]
        x = v[ip]
        x["n"] += 1
        x["first"] = min(x["first"] or ts, ts)
        x["last"] = max(x["last"] or ts, ts)
        x["ua"].add(ua[:90])
        x["probe"] += not path.startswith(APP_PATHS)
        if path.startswith("/api/"):
            x["paths"][path if not path.startswith("/api/pull/") else "/api/pull/<ticker>"] += 1
        if path.startswith("/api/pull/") and path.count("/") == 3:
            x["tickers"].add(path.rsplit("/", 1)[1])
        x["asks"] += path.endswith("/ask")
        x["reruns"] += "force=true" in uri

    rows = []
    for ip, x in v.items():
        ua_text = " ".join(x["ua"]).lower()
        is_bot = any(b in ua_text for b in BOT_UA) or not ua_text.strip() or x["probe"] > x["n"] / 2
        if is_bot and not args.all:
            continue
        rows.append((x["first"], ip, x, is_bot))

    print(f"{len(rows)} visitor IP(s){' (bots hidden; --all shows them)' if not args.all else ''}\n")
    for first, ip, x, is_bot in sorted(rows):
        who = "you" if ip in args.me else ("bot/scanner" if is_bot else "visitor")
        mins = (x["last"] - first).total_seconds() / 60
        print(f"{ip:<16} [{who}]  {first:%a %d %b %H:%M} -> {x['last']:%H:%M} IST ({mins:.0f} min), {x['n']} requests")
        print(f"    browser: {' | '.join(sorted(x['ua']))[:160]}")
        api = ", ".join(f"{p} x{n}" for p, n in sorted(x["paths"].items(), key=lambda kv: -kv[1]))
        if api:
            print(f"    used: {api}")
        if x["tickers"]:
            print(f"    Pull reports opened: {', '.join(sorted(x['tickers']))}")
        if x["asks"] or x["reruns"]:
            print(f"    copilot questions: {x['asks']}, report re-runs: {x['reruns']}")
        print()


if __name__ == "__main__":
    main()
