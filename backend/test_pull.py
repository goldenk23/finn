import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.db import init_db
from backend.pull_engine import get_or_generate_pull_report, ask_pull_copilot, resolve_copilot_citations
from backend.llm_router import get_cost_summary
from backend.main import _sanitize_log_message

REQUIRED_MODULES = [
    "business_snapshot",
    "financial_snapshot",
    "trajectory",
    "balance_sheet",
    "cash_quality",
    "narrative_vs_numbers",
    "guidance",
    "bull_vs_bear",
]


def main() -> None:
    init_db()
    test_ticker = "TRENT"

    # Regenerating costs paid Gemini calls, so only do it when explicitly asked: python backend/test_pull.py --force
    force = "--force" in sys.argv
    print(f"=== 1. Generating Deep Pull Research Report for {test_ticker} (Primary Sources Only, force={force}) ===")
    t0 = time.perf_counter()
    res = get_or_generate_pull_report(test_ticker, force_refresh=force)
    elapsed_cold = time.perf_counter() - t0

    report = res["report"]
    print(f"Completed in {elapsed_cold:.2f}s | Cached: {res['cached']} | "
          f"Tokens: {res['tokens_in']} in / {res['tokens_out']} out | Cost: ${res['cost_usd']:.4f}")
    print("Documents Parsed:", json.dumps(report.get("sources_used", {}).get("documents_parsed", []), indent=2))

    for mod in REQUIRED_MODULES:
        assert mod in report, f"Missing module {mod} in Pull report"

    print("\n--- Module 1 & 2: Business & Financial Snapshot ---")
    print("Overview:", report["business_snapshot"]["overview"])
    print("KPIs:", json.dumps(report["financial_snapshot"]["kpis"], indent=2, ensure_ascii=False))

    print("\n--- Module 6 (Worked Example): Narrative vs. Numbers (Last 4 Quarters Audit) ---")
    nvn = report["narrative_vs_numbers"]
    print(f"Badge: {nvn.get('credibility_badge')} | Synthesis: {nvn.get('synthesis')}")
    for q in nvn.get("quarters_audit", []):
        print(f"  * [{q['quarter']}] Verdict: {q['verdict']}")
        print(f"    Promised/Narrative: {q['management_narrative']}")
        print(f"    Actual Delivered  : {q['actual_numbers']}")
        print(f"    Audit Note        : {q['audit_note']}")

    print("\n--- Module 8: Bull vs. Bear ---")
    print("Bull:", report["bull_vs_bear"]["bull_case"])
    print("Bear:", report["bull_vs_bear"]["bear_case"])

    print(f"\n=== 2. Testing SQLite Cache Hit on {test_ticker} ===")
    t1 = time.perf_counter()
    cached_res = get_or_generate_pull_report(test_ticker, force_refresh=False)
    elapsed_warm = (time.perf_counter() - t1) * 1000.0
    print(f"Cache hit in {elapsed_warm:.2f} ms | Cached={cached_res['cached']}")
    assert cached_res["cached"] is True

    print(f"\n=== 3. Testing Primary-Source Pull Copilot Q&A on {test_ticker} ===")
    qa = ask_pull_copilot(test_ticker, "How has Trent's operating margin (OPM) and cash conversion evolved over the last 3 years?")
    print("Answer:\n", qa["answer"])
    print("Citations:", qa["citations"])
    print("Citation Links:", json.dumps(qa.get("citation_links", []), indent=2))
    assert "citation_links" in qa and len(qa["citation_links"]) == len(qa["citations"]), "Missing citation_links"

    # Unit check for citation resolution & log redaction
    sample_links = resolve_copilot_citations(
        ["Screener Q4 FY26 Table", "Synthesized Report", "Nonexistent Document XYZ"],
        res["fundamentals"],
        report,
    )
    assert sample_links[0]["kind"] == "webpage" and sample_links[0]["available"] is True
    assert sample_links[1]["kind"] == "internal_report" and sample_links[1]["available"] is True
    assert sample_links[2]["kind"] == "unavailable" and sample_links[2]["available"] is False
    redacted = _sanitize_log_message("key=AQ.Ab8RN6Kxyz1234567890 token=ya29.a0AfH6SMxyz1234567890")
    assert "AQ.Ab8" not in redacted and "ya29." not in redacted, f"Redaction failed: {redacted}"

    print("\n=== 4. Updated LLM Cost Ledger ===")
    print(json.dumps(get_cost_summary(), indent=2))


if __name__ == "__main__":
    main()

