"""
Warm finn.db for the demo with the current code: Push refresh, triage, card enrichment, then Pull reports.
Tickers come from the DB: the portfolio plus every ticker with a cached Pull report.

    python backend/warmup_demo.py --dry-run                       # $0: print the plan, call no LLM, write nothing
    python backend/warmup_demo.py --reenrich --force ETERNAL,HAL  # re-enrich every card, force those Pull reports

Pull reports not in --force are regenerated only when the cached one fails a check (see `pull_problems`).
Paid steps stop once the llm_cost_ledger total reaches --stop-at-usd.
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from backend.db import get_conn, get_portfolio, init_db, normalize_ticker
from backend.llm_router import _PRAISE_WORDS, get_cost_summary
from backend.pull_engine import (
    _SOURCE_AMOUNT_RE,
    _quarters_without_prior_call,
    _report_staleness,
    get_or_generate_pull_report,
)
from backend.push_engine import enrich_single_filing, refresh_portfolio_push_data, triage_untriaged_filings
from backend.scrapers.filings import enforce_filing_hygiene, is_bank_or_nbfc

# ponytail: fixed headroom from the ledger (a cold Pull has cost $0.06-$0.10); a pricier report could overshoot by the difference
PULL_HEADROOM_USD = 0.15
CARD_HEADROOM_USD = 0.02


def ledger_total() -> float:
    return float(get_cost_summary()["totals"].get("cost_usd") or 0.0)


def can_spend(stop_at: float, headroom: float, what: str) -> bool:
    total = ledger_total()
    if total + headroom > stop_at:
        print(f"  [budget] skip {what}: ledger ${total:.4f} + ${headroom:.2f} headroom > stop ${stop_at:.2f}")
        return False
    return True


def db_tickers() -> list[str]:
    with get_conn() as conn:
        cached = [r[0] for r in conn.execute("SELECT ticker FROM pull_reports_cache")]
    return sorted({p["ticker"] for p in get_portfolio()} | set(cached))


def apply_hygiene(dry_run: bool) -> None:
    """Re-run the current filing hygiene rules on every stored row; cards that become routine lose their enrichment."""
    with get_conn() as conn:
        rows = conn.execute("SELECT filing_id, title, company_name, category, materiality, enriched_json FROM announcements").fetchall()
    changes = []
    for r in rows:
        cat, mat = enforce_filing_hygiene(
            r["title"], r["category"], int(r["materiality"]), is_lender=is_bank_or_nbfc(r["company_name"] or "")
        )
        enr = "{}" if mat == 0 or cat not in ("results", "management_change") else (r["enriched_json"] or "{}")
        if (cat, mat, enr) != (r["category"], int(r["materiality"]), r["enriched_json"] or "{}"):
            changes.append((cat, mat, enr, r["filing_id"]))
    if changes and not dry_run:
        with get_conn() as conn:
            conn.executemany("UPDATE announcements SET category = ?, materiality = ?, enriched_json = ? WHERE filing_id = ?", changes)
    print(f"[push] hygiene: {len(changes)} row(s) {'would change' if dry_run else 'changed'}")


def triage_all(dry_run: bool, stop_at: float) -> None:
    """`triage_untriaged_filings` caps each call, so call it until nothing is left or a call makes no progress."""
    with get_conn() as conn:
        left = conn.execute("SELECT COUNT(*) FROM announcements WHERE summary IS NULL OR summary = ''").fetchone()[0]
    while left and not dry_run and can_spend(stop_at, CARD_HEADROOM_USD, "triage"):
        out = triage_untriaged_filings()
        print(f"[push] triaged {out['triaged']}, {out['left']} left")
        if not out["triaged"]:
            break
        left = out["left"]
    print(f"[push] untriaged rows: {left}")


def card_problem(row, reenrich: bool) -> str:
    """Why a material results / management_change card needs (re-)enrichment, or '' if it is current."""
    try:
        enr = json.loads(row["enriched_json"] or "{}")
    except ValueError:
        enr = {}
    if not enr:
        return "not enriched"
    if reenrich:
        return "--reenrich"
    if row["category"] == "results":
        # Current results cards record verification_status; ones with numbers also carry basis_note + computed metrics
        if "verification_status" not in enr or (enr.get("has_numbers") and not ("basis_note" in enr and "metrics" in enr)):
            return "old results format"
    elif not enr.get("two_line_bio") or _PRAISE_WORDS.search(enr["two_line_bio"]):
        return "missing or praise-worded bio"
    return ""


def enrich_cards(tickers: list[str], reenrich: bool, dry_run: bool, stop_at: float) -> None:
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT filing_id, ticker, category, title, enriched_json FROM announcements
            WHERE materiality = 1 AND category IN ('results', 'management_change')
              AND ticker IN ({','.join('?' for _ in tickers)})
            ORDER BY published_at DESC
            """,
            tickers,
        ).fetchall()
    todo = [(r, why) for r in rows if (why := card_problem(r, reenrich))]
    print(f"[push] {len(todo)} of {len(rows)} material cards need enrichment")
    done = errors = 0
    for r, why in todo:
        label = f"{r['ticker']} {r['category']} ({why}): {(r['title'] or '')[:60]}"
        if dry_run:
            print(f"  would enrich {label}")
            continue
        if not can_spend(stop_at, CARD_HEADROOM_USD, label):
            break
        try:
            enr = enrich_single_filing(r["filing_id"])
            done += bool(enr)
            print(f"  enriched {label} -> {enr.get('verification_status') or enr.get('person_name') or 'empty'}")
        except Exception as exc:  # same per-card isolation as refresh_portfolio_push_data
            errors += 1
            print(f"  FAILED {label}: {type(exc).__name__}: {str(exc)[:200]}")
    if not dry_run:
        print(f"[push] enriched {done} card(s), {errors} error(s)")


def pull_problems(ticker: str) -> list[str]:
    """Reasons a cached Pull report must be regenerated; [] if it passes every check. $0: SQLite only."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM pull_reports_cache WHERE ticker = ?", (ticker,)).fetchone()
    if not row:
        return ["no cached report"]
    report, fund = json.loads(row["report_json"]), json.loads(row["fundamentals_json"])
    reasons = []
    if not report.get("document_notes"):
        reasons.append("missing document_notes")
    src = report.get("sources_used") or {}
    if not any(d.get("doc_type") == "Annual Report" for d in src.get("documents_parsed") or []):
        if src.get("annual_reports_available") or fund.get("annual_reports"):
            reasons.append("no Annual Report in documents_parsed")
        else:
            print(f"  note {ticker}: Screener lists no Annual Report")
    st = _report_staleness(ticker, row["quarter_key"], row["created_at"])
    if st["stale"] and st["screener_lag"]:
        print(f"  note {ticker}: screener_lag ({st['stale_reason']}); a re-run would not fix it")
    elif st["stale"]:
        reasons.append(f"stale: {st['stale_reason']}")
    no_call = _quarters_without_prior_call(((fund.get("quarters") or {}).get("headers") or [])[-4:], fund.get("concalls") or [])
    for q in (report.get("narrative_vs_numbers") or {}).get("quarters_audit") or []:
        if isinstance(q, dict) and any(n in str(q.get("quarter", "")) for n in no_call) and (q.get("management_narrative"), q.get("verdict")) != ("No guidance",) * 2:
            reasons.append(f"{q.get('quarter')} had no prior call but does not say 'No guidance'")
    prose = json.dumps({k: v for k, v in report.items() if k not in ("document_notes", "sources_used")}, ensure_ascii=False)
    raw = [m.group(1) for m in _SOURCE_AMOUNT_RE.finditer(prose) if not prose[m.end():].lstrip().startswith("(₹")]
    if raw or "(unit unverified)" in prose:
        reasons.append(f"amounts not in ₹ Cr: {raw[:3] or ['(unit unverified)']}")
    return reasons


def warm_pull(tickers: list[str], force: list[str], dry_run: bool, stop_at: float) -> None:
    # Forced tickers first, so a tight budget is spent on them before the conditional ones
    for t in force + [t for t in tickers if t not in force]:
        reasons = ["forced"] if t in force else pull_problems(t)
        if not reasons:
            print(f"  {t}: cached report OK")
            continue
        if dry_run:
            print(f"  {t}: would regenerate ({'; '.join(reasons)})")
            continue
        if not can_spend(stop_at, PULL_HEADROOM_USD, f"Pull {t}"):
            continue
        try:
            res = get_or_generate_pull_report(t, force_refresh=True)
            print(f"  {t}: regenerated ({'; '.join(reasons)}) quarter={res['quarter_key']} cost=${res['cost_usd']:.4f} "
                  f"stale={res['stale']} {res['stale_reason']}")
        except Exception as exc:
            print(f"  {t}: FAILED ({'; '.join(reasons)}): {type(exc).__name__}: {str(exc)[:200]}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print the plan only: no scraping, no LLM call, no DB write")
    ap.add_argument("--force", default="", help="comma-separated tickers whose Pull report is always regenerated")
    ap.add_argument("--reenrich", action="store_true", help="re-enrich every material results / management_change card")
    ap.add_argument("--stop-at-usd", type=float, default=4.50, help="skip paid steps once the ledger total would pass this")
    ap.add_argument("--days", type=int, default=14, help="filing look-back window for the Push refresh")
    args = ap.parse_args()
    force = [normalize_ticker(t) for t in args.force.split(",") if t.strip()]

    t0 = time.perf_counter()
    if not args.dry_run:
        init_db()
    spent0 = ledger_total()
    tickers = db_tickers()
    print(f"[warmup] ledger ${spent0:.4f}, stop at ${args.stop_at_usd:.2f}; tickers: {', '.join(tickers)}")

    if args.dry_run:
        print("[push] dry run: skipping the NSE/BSE refresh")
    elif can_spend(args.stop_at_usd, CARD_HEADROOM_USD, "Push refresh"):
        # max_enrich=0: cards are enriched below, after every row is triaged and hygiene has run
        out = refresh_portfolio_push_data(days=args.days, max_enrich=0)
        print(f"[push] refresh: {json.dumps({k: v for k, v in out.items() if k != 'source_status'})} "
              f"sources={json.dumps(out['source_status'])}")

    def push_cards(reenrich: bool) -> None:
        triage_all(args.dry_run, args.stop_at_usd)
        apply_hygiene(args.dry_run)
        enrich_cards(tickers, reenrich, args.dry_run, args.stop_at_usd)

    push_cards(args.reenrich)
    print(f"[pull] force: {force or 'none'}")
    warm_pull(tickers, force, args.dry_run, args.stop_at_usd)
    if not args.dry_run:
        push_cards(False)  # Pull may have saved new filings for tickers that had none stored
        with get_conn() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    spent1 = ledger_total()
    print(f"\n[warmup] done in {time.perf_counter() - t0:.1f}s; ledger ${spent0:.4f} -> ${spent1:.4f} "
          f"(this run ${spent1 - spent0:.4f})")


if __name__ == "__main__":
    main()
