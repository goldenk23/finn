"""Offline checks: no network, no LLM calls, no writes. Run with: python backend/test_offline.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.scrapers.filings import enforce_filing_hygiene


def test_newspaper_publication_stays_routine() -> None:
    # Start from the worst case (LLM said "material results") to prove the guard demotes it.
    cat, mat = enforce_filing_hygiene("Copy of Newspaper Publication", "results", 1)
    assert mat == 0, f"expected materiality 0, got {mat} (category={cat})"


if __name__ == "__main__":
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_") and callable(fn)]
    for fn in tests:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(tests)} offline test(s) passed")
