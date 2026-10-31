"""Offline checks: no network, no LLM calls, never writes finn.db. Run with: python backend/test_offline.py"""
import logging
import os
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from types import SimpleNamespace

from google.genai import errors as genai_errors

from backend import llm_router, main
from backend.db import DB_PATH, SEED_PORTFOLIO, init_db, normalize_ticker
from backend.scrapers.filings import enforce_filing_hygiene

TICKER_TABLES = ("portfolio", "eod_prices", "announcements", "pull_reports_cache")


def _copy_real_db(dst: Path) -> Path:
    """Snapshot finn.db (read-only, WAL included) into a temp file via the sqlite backup API."""
    src = sqlite3.connect(f"{DB_PATH.as_uri()}?mode=ro", uri=True)
    out = sqlite3.connect(dst)
    src.backup(out)
    src.close()
    out.close()
    return dst


def _snapshot(db: Path) -> dict[str, list[tuple]]:
    conn = sqlite3.connect(db)
    snap = {t: sorted(conn.execute(f"SELECT * FROM {t}").fetchall(), key=repr) for t in TICKER_TABLES}
    conn.close()
    return snap


def test_newspaper_publication_stays_routine() -> None:
    # Start from the worst case (LLM said "material results") to prove the guard demotes it.
    cat, mat = enforce_filing_hygiene("Copy of Newspaper Publication", "results", 1)
    assert mat == 0, f"expected materiality 0, got {mat} (category={cat})"


def test_normalize_ticker() -> None:
    assert normalize_ticker("zomato") == "ETERNAL"
    assert normalize_ticker(" TATAMOTORS.NS ") == "TMPV"
    assert normalize_ticker("Zomato.BO") == "ETERNAL"
    assert normalize_ticker("tcs.ns") == "TCS"
    assert normalize_ticker("M&M") == "M&M"
    assert normalize_ticker("BAJAJ-AUTO") == "BAJAJ-AUTO"
    for bad in ["TCS&from_date=x", "../x", "", "   ", "A" * 21, "TCS/../x", "TCS NS", "INFY;DROP"]:
        try:
            normalize_ticker(bad)
        except ValueError:
            continue
        raise AssertionError(f"normalize_ticker accepted {bad!r}")


def test_rename_migration_is_idempotent() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = _copy_real_db(Path(tmp) / "finn_copy.db")
        conn = sqlite3.connect(db)
        # Recreate the legacy state regardless of whether the real DB is already migrated.
        conn.execute("DELETE FROM portfolio WHERE ticker IN ('ETERNAL','TMPV')")
        conn.executemany(
            "INSERT OR REPLACE INTO portfolio (ticker, bse_code, company_name, sector, cap_tier) VALUES (?,?,?,?,?)",
            [("ZOMATO", "543320", "Zomato Ltd (Eternal)", "Consumer Tech", "Large Cap"),
             ("TATAMOTORS", "500570", "Tata Motors Ltd", "Automobiles", "Large Cap")],
        )
        eod = "INSERT OR REPLACE INTO eod_prices (ticker, trade_date, close_price, prev_close, pct_change, volume, avg_volume_20d, volume_ratio) VALUES (?,?,1,1,0,1,1,1)"
        conn.executemany(eod, [("ZOMATO", "2000-01-03"), ("ZOMATO", "2000-01-04"), ("ETERNAL", "2000-01-04")])  # 01-04 collides
        conn.execute(
            "INSERT OR REPLACE INTO announcements (filing_id, ticker, title, published_at) VALUES ('legacy-test-1','ZOMATO','t','2000-01-03')"
        )
        conn.execute("DELETE FROM pull_reports_cache WHERE ticker IN ('TMPV','TATAMOTORS')")
        conn.execute(
            "INSERT INTO pull_reports_cache (ticker, quarter_key, fundamentals_json, report_json) VALUES ('TATAMOTORS','Q','{}','{}')"
        )
        portfolio_size = conn.execute("SELECT COUNT(*) FROM portfolio").fetchone()[0]
        conn.commit()
        conn.close()

        init_db(db)
        first = _snapshot(db)
        init_db(db)
        assert _snapshot(db) == first, "second init_db() changed data: migration is not idempotent"

        conn = sqlite3.connect(db)
        for t in TICKER_TABLES:
            left = conn.execute(f"SELECT COUNT(*) FROM {t} WHERE ticker IN ('ZOMATO','TATAMOTORS')").fetchone()[0]
            assert left == 0, f"{left} legacy rows left in {t}"
        names = dict(conn.execute("SELECT ticker, company_name FROM portfolio WHERE ticker IN ('ETERNAL','TMPV')"))
        assert names == {"ETERNAL": "Eternal Ltd", "TMPV": "Tata Motors Passenger Vehicles Ltd"}, names
        assert conn.execute("SELECT COUNT(*) FROM portfolio").fetchone()[0] == portfolio_size
        dates = [d for (d,) in conn.execute("SELECT trade_date FROM eod_prices WHERE ticker='ETERNAL' AND trade_date LIKE '2000-%' ORDER BY 1")]
        assert dates == ["2000-01-03", "2000-01-04"], dates
        assert conn.execute("SELECT ticker FROM announcements WHERE filing_id='legacy-test-1'").fetchone() == ("ETERNAL",)
        assert conn.execute("SELECT COUNT(*) FROM pull_reports_cache WHERE ticker='TMPV'").fetchone()[0] == 1
        conn.close()


def test_deleted_stock_survives_init_db() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "fresh.db"
        init_db(db)  # empty DB -> seeded
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT COUNT(*) FROM portfolio").fetchone()[0] == len(SEED_PORTFOLIO)
        conn.execute("DELETE FROM portfolio WHERE ticker = 'ITC'")
        conn.commit()
        conn.close()

        init_db(db)  # simulates a backend restart
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT COUNT(*) FROM portfolio WHERE ticker = 'ITC'").fetchone()[0] == 0, "ITC was re-seeded"
        assert conn.execute("SELECT COUNT(*) FROM portfolio").fetchone()[0] == len(SEED_PORTFOLIO) - 1
        conn.close()


@contextmanager
def _env(**values: str | None):
    old = {k: os.environ.get(k) for k in values}
    try:
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _expect_http(status: int, fn, *args) -> None:
    try:
        fn(*args)
    except HTTPException as exc:
        assert exc.status_code == status, f"expected {status}, got {exc.status_code}: {exc.detail}"
        return
    raise AssertionError(f"expected HTTP {status}, nothing raised")


def test_daily_budget_cap_counts_only_today() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "ledger.db"
        init_db(db)
        conn = sqlite3.connect(db)
        ins = "INSERT INTO llm_cost_ledger (workflow, model, tokens_in, tokens_out, cost_usd, created_at) VALUES ('t','m',1,1,?,{})"
        conn.execute(ins.format("datetime('now')"), (0.06,))
        conn.execute(ins.format("datetime('now')"), (0.05,))
        conn.execute(ins.format("datetime('now','-1 day')"), (10.0,))  # yesterday: must not count
        conn.commit()
        conn.close()

        assert abs(main._spent_today_usd(db) - 0.11) < 1e-9, main._spent_today_usd(db)
        with _env(FINN_DAILY_BUDGET_USD="0.10"):
            _expect_http(429, main._enforce_daily_budget, db)
        with _env(FINN_DAILY_BUDGET_USD="0.50"):
            main._enforce_daily_budget(db)  # under budget: no exception


def test_rate_limit_sliding_window() -> None:
    main._rate_hits.clear()
    key = "pull:1.2.3.4"
    assert [main._rate_limit_retry_after(key, 3, now=t) for t in (0, 1, 2)] == [0, 0, 0]
    wait = main._rate_limit_retry_after(key, 3, now=3)
    assert 3590 < wait <= 3601, wait  # blocked until the t=0 hit ages out
    assert main._rate_limit_retry_after("pull:5.6.7.8", 3, now=3) == 0  # other IPs unaffected
    assert main._rate_limit_retry_after(key, 3, now=3600.5) == 0  # t=0 hit expired, slot free
    main._rate_hits.clear()


def test_client_ip_ignores_spoofed_forwarded_for() -> None:
    req = Request({"type": "http", "headers": [(b"x-forwarded-for", b"6.6.6.6, 1.2.3.4")], "client": ("10.0.0.1", 5000)})
    with _env(FINN_PROXY_HOPS="1"):
        assert main._client_ip(req) == "1.2.3.4"  # entry appended by the trusted proxy, not the spoofed one
    with _env(FINN_PROXY_HOPS=None):
        assert main._client_ip(req) == "10.0.0.1"  # no proxy configured: socket peer


def test_paid_endpoints_are_guarded() -> None:
    # Budget 0 means any paid path must 429 before reaching the LLM. No `with` block, so lifespan/init_db don't run.
    client = TestClient(main.app)
    main._rate_hits.clear()
    with _env(FINN_DAILY_BUDGET_USD="0", FINN_ADMIN_TOKEN="t0k"):
        admin = {"X-Admin-Token": "t0k"}
        assert client.post("/api/push/refresh", json={}).status_code == 401
        assert client.post("/api/push/refresh", json={}, headers={"X-Admin-Token": "nope"}).status_code == 401
        assert client.post("/api/push/refresh", json={}, headers=admin).status_code == 429
        assert client.post("/api/push/refresh", json={"max_enrich": 500}, headers=admin).status_code == 422
        assert client.post("/api/push/enrich/abc", headers=admin).status_code == 429
        assert client.get("/api/pull/TRENT?force=true").status_code == 401
        assert client.get("/api/pull/ZZTESTCO").status_code == 429  # uncached -> would cost money
        assert client.post("/api/pull/ZZTESTCO/ask", json={"question": "q"}).status_code == 429
        turn = {"question": "q", "answer": "a"}
        assert client.post("/api/pull/ZZTESTCO/ask", json={"question": "q", "history": [turn] * 3}).status_code == 422
        r = client.get("/api/pull/TRENT")  # cached report costs $0, so it is served even with no budget
        assert r.status_code == 200 and r.json()["cached"] is True, r.status_code
    main._rate_hits.clear()


def test_llm_cost_includes_thinking_tokens() -> None:
    # gemini-3.1-pro-preview: $2.00 in / $12.00 out per 1M; thinking tokens are billed at the output rate.
    cost = llm_router.llm_cost_usd("gemini-3.1-pro-preview", tokens_in=10_000, tokens_out=2_000, tokens_thinking=500)
    assert abs(cost - (10_000 * 2.00 + 2_500 * 12.00) / 1e6) < 1e-9, cost  # $0.05
    assert llm_router.llm_cost_usd("gemini-3.1-flash-lite", 1_000_000, 1_000_000) == 1.75
    # Unknown model -> priced at the most expensive known rates, never the cheap default
    top_in = max(p[0] for p in llm_router.MODEL_PRICING_PER_1M.values())
    top_out = max(p[1] for p in llm_router.MODEL_PRICING_PER_1M.values())
    assert llm_router.llm_cost_usd("gemini-unknown", 1_000_000, 1_000_000) == top_in + top_out
    for model, (pin, pout) in llm_router.MODEL_PRICING_PER_1M.items():
        assert 0 < pin < pout, f"suspicious pricing for {model}"


def test_fallback_order() -> None:
    fb = llm_router.MODEL_FALLBACKS
    pro_chain = fb[llm_router.MODEL_PRO]
    assert pro_chain[0] == llm_router.MODEL_PRO == "gemini-3.1-pro-preview"
    assert pro_chain[-1] == llm_router.MODEL_FLASH, "Flash must be the last resort for Pull synthesis"
    assert not any("flash" in m for m in pro_chain[:-1]), f"a Flash model is ahead of a Pro model: {pro_chain}"
    for primary, chain in fb.items():
        assert chain[0] == primary, f"{primary} chain doesn't start with itself"
        assert len(set(chain)) == len(chain), f"duplicate in {chain}"
        assert all(m in llm_router.MODEL_PRICING_PER_1M for m in chain), f"unpriced model in {chain}"
    # Thinking config per family, as verified by the probe
    assert llm_router._thinking_config("gemini-2.5-flash").thinking_budget == 0
    assert llm_router._thinking_config("gemini-2.5-pro").thinking_budget == 512
    assert llm_router._thinking_config("gemini-4-argon").thinking_budget is None  # budget -> 400 on argon
    assert llm_router._thinking_config("gemini-3.1-pro-preview").thinking_level is not None


class _FakeModels:
    """generate_content stand-in: raises the scripted error for a model, otherwise returns a JSON response."""

    def __init__(self, failures: dict[str, Exception]):
        self.failures, self.calls = failures, []

    def generate_content(self, model, contents, config):
        self.calls.append(model)
        if model in self.failures:
            raise self.failures[model]
        usage = SimpleNamespace(prompt_token_count=100, candidates_token_count=20, thoughts_token_count=30)
        return SimpleNamespace(text='{"ok": true}', usage_metadata=usage)


def _api_error(code: int) -> genai_errors.APIError:
    return genai_errors.ClientError(code, {"error": {"code": code, "message": f"fake {code}", "status": "X"}})


def test_fallback_walks_pro_chain_and_logs() -> None:
    orig_client = llm_router._get_client
    llm_router._UNAVAILABLE_MODELS.clear()
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = Path(tmp) / "ledger.db"
            init_db(db)
            fake = _FakeModels({"gemini-3.1-pro-preview": _api_error(404), "gemini-4-argon": _api_error(429)})
            llm_router._get_client = lambda: SimpleNamespace(models=fake)
            warnings: list[str] = []
            handler = logging.Handler()
            handler.emit = lambda record: warnings.append(record.getMessage())
            llm_router.logger.addHandler(handler)
            try:
                data, served = llm_router.call_gemini_pro_json("p", ticker="TEST", db_path=db)
            finally:
                llm_router.logger.removeHandler(handler)
            assert data == {"ok": True} and served == "gemini-2.5-pro", served
            assert fake.calls == ["gemini-3.1-pro-preview", "gemini-4-argon", "gemini-2.5-pro"], fake.calls
            assert len(warnings) == 2 and "falling back to gemini-4-argon" in warnings[0], warnings
            assert "gemini-3.1-pro-preview" in llm_router._UNAVAILABLE_MODELS  # 404 is remembered, 429 is not
            assert "gemini-4-argon" not in llm_router._UNAVAILABLE_MODELS
            conn = sqlite3.connect(db)
            row = conn.execute("SELECT model, tokens_in, tokens_out, cost_usd FROM llm_cost_ledger").fetchone()
            conn.close()
            assert row == ("gemini-2.5-pro", 100, 50, round((100 * 1.25 + 50 * 10.0) / 1e6, 6)), row

            # Auth errors hit every model alike: no fallback, error propagates
            fake2 = _FakeModels({llm_router.MODEL_FLASH: _api_error(401)})
            llm_router._get_client = lambda: SimpleNamespace(models=fake2)
            try:
                llm_router._call_gemini_json("p", model=llm_router.MODEL_FLASH, workflow="t", db_path=db)
                raise AssertionError("401 should not fall back")
            except genai_errors.APIError as exc:
                assert exc.code == 401 and fake2.calls == [llm_router.MODEL_FLASH], fake2.calls
    finally:
        llm_router._get_client = orig_client
        llm_router._UNAVAILABLE_MODELS.clear()


def test_annual_report_read_alongside_concalls() -> None:
    import re
    import fitz
    from backend import pull_engine

    # 60-page fake AR: cover pages 0-2, the 5 brief sections scattered, everything else filler.
    sections = {
        8: "Management Discussion and Analysis: industry structure, opportunities and threats.",
        15: "Segment information: segment revenue and segment results by operating segment.",
        22: "Contingent liabilities: claims against the company not acknowledged as\ndebt Rs 120 Cr.",
        28: "Related party transactions with key managerial personnel.",
        41: "Independent Auditor's Report. Basis for Opinion. Key Audit Matters.",
    }
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        ar_pdf = Path(tmp) / "ar.pdf"
        doc = fitz.open()
        for i in range(60):
            page = doc.new_page()
            body = f"PAGE{i} {sections.get(i, 'Sustainability narrative and CSR spend.')} " + "filler " * 600
            assert page.insert_textbox(page.rect + (36, 36, -36, -36), body, fontsize=5) >= 0, "text overflowed"
        doc.save(ar_pdf)
        doc.close()

        real, fetched = pull_engine.extract_pdf_text, []

        def fake_extract(url, **kw):
            fetched.append(url)
            return real(ar_pdf, **kw) if url == "https://x/ar25.pdf" else "management guided growth " * 50

        pull_engine.extract_pdf_text = fake_extract
        try:
            docs = pull_engine._extract_primary_documents({
                "concalls": [{"period": f"Q{i}", "transcript_url": f"https://x/cc{i}.pdf"} for i in range(4)],
                "annual_reports": [{"title": "Financial Year 2025", "url": "https://x/ar25.pdf"},
                                   {"title": "Financial Year 2024", "url": "https://x/ar24.pdf"}],
            })
        finally:
            pull_engine.extract_pdf_text = real

    assert [d["doc_type"] for d in docs] == ["Concall Transcript"] * 4 + ["Annual Report"], docs
    assert "https://x/ar24.pdf" not in fetched, "only the latest AR should be downloaded"
    ar = docs[-1]
    assert ar["period"] == "Financial Year 2025"
    pages = {int(n) for n in re.findall(r"PAGE(\d+) ", ar["text"])}
    assert pages == {0, 1, 2, *sections}, pages  # cover + every section page, no filler
    assert len(ar["text"]) <= pull_engine.AR_MAX_CHARS
    for chunk in ar["text"].split("\n--- [Page")[1:]:
        assert len(chunk) <= pull_engine.AR_MAX_CHARS_PER_PAGE + 40, len(chunk)


def test_distilled_notes_persisted_in_cached_report() -> None:
    from backend import pull_engine

    docs = [
        {"period": "Feb 2026", "doc_type": "Concall Transcript", "url": "https://x/cc.pdf", "text": "t"},
        {"period": "Financial Year 2025", "doc_type": "Annual Report", "url": "https://x/ar.pdf", "text": "t"},
    ]
    mapped = [  # model echoed D2's id, dropped D1's id (matched by period + doc_type), and invented a third doc
        {"doc_id": "D2", "period": "FY25", "doc_type": "Annual Report", "contingent_liabilities": ["Rs 120 Cr"]},
        {"period": "Feb 2026", "doc_type": "Concall Transcript", "stated_guidance_and_promises": ["15% growth"]},
        {"doc_id": "D9", "period": "Nov 2025", "doc_type": "Concall Transcript"},
    ]
    fundamentals = {k: {} for k in ("top_ratios", "quarters", "profit_loss", "balance_sheet", "cash_flow", "ratios")}
    fundamentals.update(company_name="Test Co", about="", screener_url="https://x", concalls=[], annual_reports=[])
    patches = {
        "fetch_screener_fundamentals": lambda t: fundamentals,
        "_get_material_filings": lambda t, db: [],
        "_extract_primary_documents": lambda f, n: docs,
        "_call_gemini_json_with_model": lambda **kw: (mapped, "flash"),
        "call_gemini_pro_json": lambda **kw: ({}, "pro"),
    }
    orig = {k: getattr(pull_engine, k) for k in patches}
    try:
        for k, v in patches.items():
            setattr(pull_engine, k, v)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = Path(tmp) / "pull.db"
            init_db(db)
            pull_engine.get_or_generate_pull_report("ZZTESTCO", force_refresh=True, db_path=db)
            cached = pull_engine.get_or_generate_pull_report("ZZTESTCO", db_path=db)
            assert cached["cached"] is True
            notes = cached["report"]["document_notes"]
            assert [(n["doc_id"], n["period"], n["doc_type"], n["url"]) for n in notes] == [
                ("D2", "Financial Year 2025", "Annual Report", "https://x/ar.pdf"),
                ("D1", "Feb 2026", "Concall Transcript", "https://x/cc.pdf"),
            ], notes
            assert notes[0]["contingent_liabilities"] == ["Rs 120 Cr"]

            # A report cached before this field existed still loads, with an empty notes list
            conn = sqlite3.connect(db)
            conn.execute("UPDATE pull_reports_cache SET report_json = '{\"sources_used\": {}}' WHERE ticker = 'ZZTESTCO'")
            conn.commit()
            conn.close()
            assert pull_engine.get_or_generate_pull_report("ZZTESTCO", db_path=db)["report"]["document_notes"] == []
    finally:
        for k, v in orig.items():
            setattr(pull_engine, k, v)


def test_quarters_without_prior_call_say_no_guidance() -> None:
    from backend import pull_engine

    # HAL-style: one concall a year, in May. Only the Jun quarter has a call in its 3 months.
    headers = ["Sep 2025", "Dec 2025", "Mar 2026", "Jun 2026"]
    concalls = [{"period": "May 2026"}, {"period": "May 2025"}, {"period": "Recent Quarter"}]
    assert pull_engine._quarters_without_prior_call(headers, concalls) == ["Sep 2025", "Dec 2025", "Mar 2026"]

    fundamentals = {k: {} for k in ("top_ratios", "profit_loss", "balance_sheet", "cash_flow", "ratios")}
    fundamentals.update(company_name="Test Co", about="", screener_url="https://x", annual_reports=[],
                        quarters={"headers": headers}, concalls=concalls)
    rows = [{"quarter": q, "management_narrative": "Guided 10% growth", "verdict": "Beat"} for q in headers]
    patches = {
        "fetch_screener_fundamentals": lambda t: fundamentals,
        "_get_material_filings": lambda t, db: [],
        "_extract_primary_documents": lambda f, n: [],
        "call_gemini_pro_json": lambda **kw: ({"narrative_vs_numbers": {"quarters_audit": rows}}, "pro"),
    }
    orig = {k: getattr(pull_engine, k) for k in patches}
    try:
        for k, v in patches.items():
            setattr(pull_engine, k, v)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = Path(tmp) / "pull.db"
            init_db(db)
            out = pull_engine.get_or_generate_pull_report("ZZTESTCO", force_refresh=True, db_path=db)
        got = [(r["quarter"], r["management_narrative"], r["verdict"]) for r in out["report"]["narrative_vs_numbers"]["quarters_audit"]]
        assert got == [(q, "No guidance", "No guidance") for q in headers[:3]] + [("Jun 2026", "Guided 10% growth", "Beat")], got
    finally:
        for k, v in orig.items():
            setattr(pull_engine, k, v)


def test_pull_amounts_converted_to_crore() -> None:
    from backend import pull_engine

    # Unit rules: lakhs / 100, million / 10, thousand / 10,000; crore and "lakh crore" are left alone
    notes = [{"doc_type": "Concall Transcript", "notable_quotes_or_facts": ["Pipeline of Rs. 1 lakh crores", "Capex ₹45 crore"]},
             {"doc_type": "Annual Report", "related_party_transactions": ["Rs 512 million to JV", "INR 25,000 thousands"]}]
    assert pull_engine._annotate_crore(notes) == {512.0: 51.2, 25000.0: 2.5}
    assert notes[0]["notable_quotes_or_facts"] == ["Pipeline of Rs. 1 lakh crores", "Capex ₹45 crore"]
    assert notes[1]["related_party_transactions"] == ["Rs 512 million (₹51.2 Cr) to JV", "INR 25,000 thousands (₹2.5 Cr)"]

    # End to end, HAL-style: the AR note is in lakhs, the reduce model relabels the lakh total as Cr
    hal_note = {"doc_id": "D1", "period": "Financial Year 2026", "doc_type": "Annual Report",
                "contingent_liabilities": ["Claims not acknowledged as debts: Total ₹198456 lakhs (Page 234)"]}
    fundamentals = {k: {} for k in ("top_ratios", "profit_loss", "cash_flow", "ratios")}
    fundamentals.update(company_name="Test Co", about="", screener_url="https://x", annual_reports=[], concalls=[],
                        quarters={"headers": ["Mar 2026"]},
                        balance_sheet={"headers": ["Mar 2025", "Mar 2026"],
                                       "rows": [{"metric": "Borrowings", "Mar 2025": "51", "Mar 2026": "66"},
                                                {"metric": "Total Assets", "Mar 2025": "1,11,997", "Mar 2026": "1,34,395"}]})
    report = {"balance_sheet": {"summary": "Debt of ₹66 Cr. Contingent liabilities of ₹1,98,456 Cr, an unmodified opinion."},
              "bull_vs_bear": {"bear_case": ["Tax claims of ₹70,00,000 Cr"]}}
    prompts = []
    patches = {
        "fetch_screener_fundamentals": lambda t: fundamentals,
        "_get_material_filings": lambda t, db: [],
        "_extract_primary_documents": lambda f, n: [{"period": "Financial Year 2026", "doc_type": "Annual Report", "url": "https://x/ar.pdf", "text": "ar"}],
        "_call_gemini_json_with_model": lambda **kw: ([{**hal_note}], "flash"),
        "call_gemini_pro_json": lambda **kw: (prompts.append(kw["prompt"]) or report, "pro"),
    }
    orig = {k: getattr(pull_engine, k) for k in patches}
    try:
        for k, v in patches.items():
            setattr(pull_engine, k, v)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = Path(tmp) / "pull.db"
            init_db(db)
            out = pull_engine.get_or_generate_pull_report("ZZTESTCO", force_refresh=True, db_path=db)["report"]
        assert "₹198456 lakhs (₹1,984.56 Cr)" in prompts[0] and "thousand ÷ 10,000" in prompts[0]
        assert out["document_notes"][0]["contingent_liabilities"] == ["Claims not acknowledged as debts: Total ₹198456 lakhs (₹1,984.56 Cr) (Page 234)"]
        # Unconverted lakh total fixed; Screener's ₹66 Cr kept; an amount >= 50x total assets (₹1,34,395 Cr) flagged
        assert out["balance_sheet"]["summary"] == "Debt of ₹66 Cr. Contingent liabilities of ₹1,984.56 Cr, an unmodified opinion.", out
        assert out["bull_vs_bear"]["bear_case"] == ["Tax claims of ₹70,00,000 Cr (unit unverified)"], out
    finally:
        for k, v in orig.items():
            setattr(pull_engine, k, v)


def test_pull_report_staleness() -> None:
    from datetime import datetime
    from backend import pull_engine

    # Indian fiscal quarters: Q1=Jun, Q2=Sep, Q3=Dec of the prior calendar year, Q4=Mar of the FY year
    fq = pull_engine._filing_quarter
    assert [fq(f"Q{q} FY27") for q in (1, 2, 3, 4)] == [2026 * 12 + 6, 2026 * 12 + 9, 2026 * 12 + 12, 2027 * 12 + 3]
    assert fq('titled "Q2FY27 Financial Results"') == fq("RESULTS FOR Q2 FY2027") == 2026 * 12 + 9
    for text in ("the quarter ended September 30, 2026", "half-year ended 30th September 2026", "ended 30 Sep 2026"):
        assert fq(text) == 2026 * 12 + 9, text
    assert fq("Q2 2026 results") is None and fq("press release dated December 1, 2026") is None
    assert [pull_engine._fiscal_label(fq(f"Q{q} FY27")) for q in (1, 2, 3, 4)] == ["Q1 FY27", "Q2 FY27", "Q3 FY27", "Q4 FY27"]

    now = datetime(2026, 11, 1, 12, 0, 0)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "stale.db"
        init_db(db)
        conn = sqlite3.connect(db)
        ins = "INSERT INTO announcements (filing_id, ticker, title, published_at, category) VALUES (?,?,?,?,?)"
        conn.executemany(ins, [
            # filed 29 Oct 16:21 IST = 10:51 UTC, after a report generated 28 Oct 23:16 UTC
            ("f1", "ZZNEW", "Results for the second quarter ended 30th September 2026", "2026-10-29 16:21:37", "results"),
            # names Q2 FY27 but was filed before the report: Screener hadn't caught up when it was generated
            ("f2", "ZZLAG", 'Press release titled "Q2FY27 Financial Results"', "2026-10-27 13:33:54", "results"),
            ("f3", "ZZOK", "Results for the quarter ended June 30, 2026", "2026-08-10 15:00:00", "results"),
            ("f4", "ZZOK", "Q2 FY27 board meeting intimation", "2026-10-30 10:00:00", "board_meeting"),  # not a results row
        ])
        conn.commit()
        conn.close()

        def check(ticker, quarter_key, generated_at):
            return pull_engine._report_staleness(ticker, quarter_key, generated_at, db, now=now)

        new = check("ZZNEW", "Jun 2026", "2026-10-28 23:16:23")
        assert new == {"stale": True, "stale_reason": "New Q2 FY27 results filed since this report was generated", "screener_lag": False}, new
        # 16:21 IST is 10:51 UTC, so a report generated at 11:00 UTC already saw it (and Screener had Sep 2026)
        assert check("ZZNEW", "Sep 2026", "2026-10-29 11:00:00")["stale"] is False
        lag = check("ZZLAG", "Jun 2026", "2026-10-30 09:00:00")
        assert lag["stale"] and lag["screener_lag"] and "Q2 FY27" in lag["stale_reason"] and "Jun 2026" in lag["stale_reason"], lag
        old = check("ZZOK", "Jun 2026", "2026-10-20 09:00:00")
        assert old["stale"] and not old["screener_lag"] and "12 days old" in old["stale_reason"], old
        assert check("ZZOK", "Jun 2026", "2026-10-30 09:00:00") == {"stale": False, "stale_reason": "", "screener_lag": False}
        assert check("ZZNONE", "Latest", "2026-10-31 09:00:00")["stale"] is False

        # Cached read and re-run both carry the fields; a re-run while Screener still lags stays flagged with that reason
        fundamentals = {k: {} for k in ("top_ratios", "profit_loss", "balance_sheet", "cash_flow", "ratios")}
        fundamentals.update(company_name="Lag Co", about="", screener_url="https://x", annual_reports=[], concalls=[],
                            quarters={"headers": ["Mar 2026", "Jun 2026"]})
        patches = {
            "fetch_screener_fundamentals": lambda t: fundamentals,
            "_get_material_filings": lambda t, d: [],
            "_extract_primary_documents": lambda f, n: [],
            "call_gemini_pro_json": lambda **kw: ({}, "pro"),
        }
        orig = {k: getattr(pull_engine, k) for k in patches}
        try:
            for k, v in patches.items():
                setattr(pull_engine, k, v)
            rerun = pull_engine.get_or_generate_pull_report("ZZLAG", force_refresh=True, db_path=db)
            assert rerun["cached"] is False and rerun["stale"] and rerun["screener_lag"], rerun
            cached = pull_engine.get_or_generate_pull_report("ZZLAG", db_path=db)
            assert cached["cached"] is True and cached["screener_lag"] and cached["stale_reason"] == rerun["stale_reason"]
        finally:
            for k, v in orig.items():
                setattr(pull_engine, k, v)


def test_copilot_sees_notes_and_history_and_cites_only_loaded_docs() -> None:
    from backend import pull_engine

    fundamentals = {
        "company_name": "Test Co", "ticker": "ZZTESTCO", "screener_url": "https://x",
        "quarters": {"headers": ["Dec 2025"]},
        "concalls": [{"period": "Feb 2026", "transcript_url": "https://x/cc-feb26.pdf"},
                     {"period": "Nov 2024", "transcript_url": "https://x/cc-nov24.pdf"}],  # Nov 2024 listed, never read
        "annual_reports": [],
    }
    report = {
        "sources_used": {"documents_parsed": [{"period": "Feb 2026", "doc_type": "Concall Transcript", "url": "https://x/cc-feb26.pdf"}]},
        "document_notes": [{"doc_id": "D1", "period": "Feb 2026", "doc_type": "Concall Transcript",
                            "url": "https://x/cc-feb26.pdf", "stated_guidance_and_promises": ["GUIDE-18PCT-FY27"]}],
    }
    seen: dict[str, str] = {}

    def fake_llm(**kw):
        seen["prompt"] = kw["prompt"]
        return {"answer": "Guided 18%.", "citations": ["Feb 2026 Concall Transcript", "Nov 2024 Concall Transcript"]}, "flash"

    patches = {
        "get_or_generate_pull_report": lambda t, force_refresh=False, db_path=None: {"fundamentals": fundamentals, "report": report},
        "_call_gemini_json_with_model": fake_llm,
    }
    orig = {k: getattr(pull_engine, k) for k in patches}
    orig_ask, orig_guard = main.ask_pull_copilot, main._guard_paid_call
    try:
        for k, v in patches.items():
            setattr(pull_engine, k, v)
        history = [{"question": f"OLDQ{i}", "answer": f"OLDA{i}"} for i in range(3)]
        out = pull_engine.ask_pull_copilot("ZZTESTCO", "What is FY27 guidance?", history=history)
        p = seen["prompt"]
        assert "GUIDE-18PCT-FY27" in p and '"source_tag": "Feb 2026 Concall Transcript"' in p
        assert "OLDQ1" in p and "OLDA2" in p and "OLDQ0" not in p, "only the last 2 turns go in"
        assert "not in the loaded filings/transcripts" in p and "only cite documents present" in p
        assert "Nov 2024" not in p, "a transcript listed on Screener but never read is offered as a source"
        assert [(c["label"], c["url"]) for c in out["citation_links"]] == [("Feb 2026 Concall Transcript", "https://x/cc-feb26.pdf")]

        # The API route forwards the chat history to the copilot
        got: dict = {}
        main._guard_paid_call = lambda request, bucket: None
        main.ask_pull_copilot = lambda **kw: got.update(kw) or {"ticker": kw["ticker"]}
        r = TestClient(main.app).post("/api/pull/ZZTESTCO/ask", json={"question": "q", "history": history[1:]})
        assert r.status_code == 200 and got["history"] == history[1:], (r.status_code, got)
    finally:
        for k, v in orig.items():
            setattr(pull_engine, k, v)
        main.ask_pull_copilot, main._guard_paid_call = orig_ask, orig_guard


# SUNPHARMA Q2 FY27 consolidated P&L as printed (₹ million). Current and previous columns are the real filing;
# exceptional items are printed positive and subtracted ("PBT (V-VI)"). Year-ago column is built to match Screener.
SUN_RAW = {
    "has_numbers": True,
    "basis": "Consolidated",
    "is_bank_or_nbfc": False,
    "unit_as_printed": "₹ in Million",
    "quarter_end": "Sep 2026",
    "columns": {
        "current": {"revenue_from_operations": 161987.1, "other_income": 5173.2, "total_expenses": 122878.3,
                    "finance_costs": 746.7, "depreciation_amortisation": 7583.4, "exceptional_items": 12688.3,
                    "pbt": 31593.7, "pat": 19950.7},
        "previous": {"revenue_from_operations": 152998.8, "other_income": 7236.2, "total_expenses": 117206.0,
                     "finance_costs": 996.7, "depreciation_amortisation": 7387.2, "exceptional_items": 2040.9,
                     "pbt": 40988.1, "pat": 29012.3},
        "year_ago": {"revenue_from_operations": 144780.0, "other_income": 4700.0, "total_expenses": 107810.0,
                     "finance_costs": 1000.0, "depreciation_amortisation": 7300.0, "exceptional_items": 0,
                     "pbt": 41670.0, "pat": 31250.0},
    },
}
# Screener's SUNPHARMA consolidated quarterly table (₹ Cr), as scraped on 2026-11-01
SUN_SCREENER_COLS = ["Jun 2025", "Sep 2025", "Dec 2025", "Mar 2026", "Jun 2026", "Sep 2026"]
SUN_SCREENER_ROWS = {
    "Sales": ["13,851", "14,478", "15,521", "14,612", "15,300", "16,199"],
    "Operating Profit": ["4,302", "4,527", "4,948", "3,954", "4,418", "4,744"],
    "Profit before tax": ["3,173", "4,168", "4,227", "3,551", "4,099", "3,159"],
    "Net Profit": ["2,293", "3,125", "3,381", "2,710", "2,901", "1,995"],
}


def _sun_screener(latest: str) -> dict:
    """Fake fetch_screener_fundamentals output whose latest column is `latest`."""
    cols = SUN_SCREENER_COLS[: SUN_SCREENER_COLS.index(latest) + 1]
    rows = [{"metric": name, **dict(zip(SUN_SCREENER_COLS, vals))} for name, vals in SUN_SCREENER_ROWS.items()]
    return {"screener_url": "https://www.screener.in/company/SUNPHARMA/consolidated/",
            "quarters": {"headers": cols, "rows": rows}}


def test_results_operating_profit_formulas() -> None:
    op = llm_router._operating_profit
    # CIPLA Q2 FY27 consolidated (₹ Cr): revenue - total expenses + finance costs + D&A = Screener Operating Profit 1,388
    cipla = {"revenue_from_operations": 7403.93, "other_income": 151.16, "total_expenses": 6377.75,
             "finance_costs": 16.74, "depreciation_amortisation": 344.86}
    assert abs(op(cipla, bank=False, financial=False) - 1387.78) < 0.01, op(cipla, False, False)
    assert op({**cipla, "other_income": 9999.0}, False, False) == op(cipla, False, False), "other income leaked in"
    assert op({k: v for k, v in cipla.items() if k != "total_expenses"}, False, False) is None
    # BAJFINANCE consolidated (Ind AS NBFC): finance costs are its interest cost, so only D&A is added back.
    # = Screener Financing Profit 8,825 (the old PPOP card showed 10,699, +21%)
    bfl = {"revenue_from_operations": 24546.37, "other_income": 29.45, "total_expenses": 16042.35,
           "finance_costs": 8601.73, "depreciation_amortisation": 321.07}
    assert abs(op(bfl, False, True) - 8825.09) < 0.01, op(bfl, False, True)
    # Bank (RBI format): interest earned - interest expended - operating expenses - provisions, other income excluded
    bank = {"interest_earned": 1000.0, "other_income": 300.0, "interest_expended": 550.0,
            "operating_expenses": 250.0, "provisions": 80.0}
    assert op(bank, True, True) == 120.0
    assert op({**bank, "provisions": None}, True, True) is None

    # Bank filing end to end: interest earned is revenue, label is Financing Profit
    bank_raw = {"basis": "Standalone", "unit_as_printed": "crore", "quarter_end": "Sep 2026",
                "columns": {"current": {**bank, "pbt": 420.0, "pat": 315.0}, "previous": {}, "year_ago": {}}}
    res, _ = llm_router._results_from_pdf_items(bank_raw, "results")
    assert res["metrics"]["revenue"]["value"] == "1,000" and res["metrics"]["ebitda"]["value"] == "120", res["metrics"]
    assert res["operating_label"] == "Financing Profit" and res["is_financial"] is True
    assert res["basis_note"] == "Standalone • Financing Profit ex-other income, after provisions"

    # SUNPHARMA in ₹ million: converted to ₹ Cr, exceptional sign reconciled to PBT, EBITDA = Screener's 4,744
    res, series = llm_router._results_from_pdf_items(SUN_RAW, "Outcome of Board Meeting")
    m = res["metrics"]
    assert [m[k]["value"] for k in ("revenue", "ebitda", "pbt", "pat")] == ["16,199", "4,744", "3,159", "1,995"], m
    assert abs(res["exceptional_items_cr"] + 1268.83) < 0.01, res["exceptional_items_cr"]
    assert res["period"] == "Q2 FY27 (Sep 2026)" and res["basis_note"] == "Consolidated • EBITDA ex-other income"
    assert (m["pat"]["qoq_pct"], m["pat"]["yoy_pct"]) == ("-31.2%", "-36.2%"), m["pat"]
    # No quarter_end from the LLM -> quarter read from the filing title
    no_q = {**SUN_RAW, "quarter_end": ""}
    assert llm_router._results_from_pdf_items(no_q, "Results for the quarter ended 30 June 2026")[0]["quarter_end"] == "Jun 2026"
    assert llm_router._results_from_pdf_items({**SUN_RAW, "unit_as_printed": "units"}, "t") is None


def test_results_growth_math() -> None:
    pc = llm_router._pct_change
    assert pc(110, 100) == "+10.0%" and pc(90, 100) == "-10.0%"
    assert pc(-50, 100) == "-150.0%"  # profit to loss off a positive base is still a real change
    assert pc(50, -100) == "n/a" and pc(-200, -100) == "n/a" and pc(50, 0) == "n/a"  # zero/negative base
    assert pc(None, 100) == "—" and pc(100, None) == "—"
    assert llm_router._metric((852.0, 786.0, 1353.0)) == {"value": "852", "qoq_pct": "+8.4%", "yoy_pct": "-37.0%"}
    assert llm_router._format_pct("n/a") == "n/a" and llm_router._format_pct(None) == "—"  # normalizing keeps n/a


def test_results_takeaway_rules() -> None:
    res, series = llm_router._results_from_pdf_items(SUN_RAW, "t")
    ok = llm_router._takeaway_ok
    good = "Revenue rose 11.9% YoY but PAT fell 36.2% YoY, dragged by an exceptional charge of ₹1,269 Cr."
    assert ok(good, res, series)
    assert not ok("SUNPHARMA delivered a strong Q2 FY27; exceptional charge of ₹1,269 Cr hit PAT.", res, series)
    assert not ok("Revenue rose 11.9% YoY while PAT fell 36.2% YoY.", res, series), "exceptional items not mentioned"
    assert not ok("Exceptional charge of ₹1,269 Cr; PAT fell 40% YoY.", res, series), "a % that wasn't computed"
    # A quarter where PAT and EBITDA grew may be called strong
    up = {"metrics": {"revenue": llm_router._metric((110, 100, 100)), "ebitda": llm_router._metric((22, 20, 20)),
                      "pbt": llm_router._metric((15, 14, 14)), "pat": llm_router._metric((11, 10, 10))}}
    up_series = {"ebitda": (22, 20, 20), "pat": (11, 10, 10)}
    assert ok("A strong quarter: PAT up 10.0% QoQ.", up, up_series)

    det = llm_router._deterministic_takeaway("SUNPHARMA", res)
    assert "PAT ₹1,995 Cr (-31.2% QoQ, -36.2% YoY)" in det and "exceptional charge of ₹1,269 Cr" in det, det
    assert ok(det, res, series)

    orig = llm_router._call_gemini_json_with_model
    try:
        llm_router._call_gemini_json_with_model = lambda **kw: ({"takeaway": "A strong, robust quarter."}, "flash")
        assert llm_router._results_takeaway("SUNPHARMA", res, series) == (det, None)
        llm_router._call_gemini_json_with_model = lambda **kw: ({"takeaway": good}, "flash")
        assert llm_router._results_takeaway("SUNPHARMA", res, series) == (good, "flash")
    finally:
        llm_router._call_gemini_json_with_model = orig


def test_results_screener_quarter_gate() -> None:
    orig_fetch = llm_router.fetch_screener_fundamentals
    try:
        sc = {}
        for q in ("Jun 2026", "Sep 2026"):
            llm_router.fetch_screener_fundamentals = lambda t, q=q: _sun_screener(q)
            sc[q] = llm_router._compute_screener_quarterly_metrics("SUNPHARMA")
    finally:
        llm_router.fetch_screener_fundamentals = orig_fetch
    assert sc["Sep 2026"]["basis"] == "Consolidated" and sc["Sep 2026"]["period"] == "Q2 FY27 (Sep 2026)"

    def check(sc_ref, **overrides):
        res = {**llm_router._results_from_pdf_items(SUN_RAW, "t")[0], **overrides}
        return llm_router._is_pdf_extraction_sane(res, sc_ref), res["verification_status"]

    # Right after the filing Screener still shows Jun 2026: PDF kept, no cross-check against the wrong quarter
    assert check(sc["Jun 2026"]) == (True, "pdf_only_screener_behind")
    assert check(sc["Sep 2026"]) == (True, "verified_screener")
    assert check(None) == (True, "pdf_only_no_screener")
    assert check({**sc["Sep 2026"], "basis": "Standalone"}) == (True, "pdf_only_basis_mismatch")
    assert check(sc["Sep 2026"], basis="") == (True, "pdf_only_basis_mismatch")  # card cached before basis existed
    assert check(sc["Sep 2026"], quarter_end="Jun 2026", period="") == (True, "pdf_only_screener_ahead")
    # Same quarter: EBITDA incl. other income (old CIPLA card: 1,539 vs Screener 1,388, +11%) is rejected
    wrong = {"ebitda": {"value": "5,261", "qoq_pct": "", "yoy_pct": ""}}
    assert check(sc["Sep 2026"], metrics={**sc["Sep 2026"]["metrics"], **wrong}) == (False, "screener_mismatch")
    within = {"pat": {"value": "2,010", "qoq_pct": "", "yoy_pct": ""}}  # +0.75% vs 1,995: inside the 1% tolerance
    assert check(sc["Sep 2026"], metrics={**sc["Sep 2026"]["metrics"], **within}) == (True, "verified_screener")
    implausible = {"pat": {"value": "15,000", "qoq_pct": "", "yoy_pct": ""}}
    assert check(sc["Sep 2026"], metrics={**sc["Sep 2026"]["metrics"], **implausible}) == (False, "pdf_implausible")


def test_results_enrichment_end_to_end() -> None:
    from backend import push_engine

    pdf_reply: dict = {}

    def fake_llm(**kw):
        if kw["workflow"] == "push_enrich_results_pdf":
            return pdf_reply, "gemini-2.5-flash"
        return {"takeaway": "Sun Pharma delivered a strong Q2 FY27."}, "gemini-2.5-flash"

    names = ("fetch_screener_fundamentals", "extract_results_pdf_text", "_call_gemini_json_with_model")
    orig = {k: getattr(llm_router, k) for k in names}
    title = "Outcome of Board Meeting: Unaudited Financial Results for the quarter and half year ended 30 September 2026"
    try:
        llm_router.extract_results_pdf_text = lambda url, max_pages=10: "statement of profit and loss " * 40
        llm_router._call_gemini_json_with_model = fake_llm
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = Path(tmp) / "push.db"
            init_db(db)
            conn = sqlite3.connect(db)
            conn.execute(
                "INSERT INTO announcements (filing_id, ticker, title, published_at, category, materiality, summary, pdf_url)"
                " VALUES ('r1','SUNPHARMA',?,'2026-10-30 14:15:10','results',1,"
                "'Unaudited financial results provide key insights into company performance.','https://x/r.pdf')",
                (title,),
            )
            conn.commit()
            conn.close()

            # Screener still on Jun 2026: PDF numbers kept, hype takeaway replaced, summary rewritten in the DB
            llm_router.fetch_screener_fundamentals = lambda t: _sun_screener("Jun 2026")
            pdf_reply.clear()
            pdf_reply.update(SUN_RAW)
            out = push_engine.enrich_single_filing("r1", db_path=db)
            assert out["verification_status"] == "pdf_only_screener_behind" and out["source"] == "Exchange Filing PDF", out
            assert out["metrics"]["pat"]["value"] == "1,995" and "exceptional charge of ₹1,269 Cr" in out["takeaway"], out
            assert out["takeaway_model"] is None, "hype takeaway should have been rejected"
            conn = sqlite3.connect(db)
            summary = conn.execute("SELECT summary FROM announcements WHERE filing_id='r1'").fetchone()[0]
            conn.close()
            assert summary.startswith("Q2 FY27 (Sep 2026) consolidated results: revenue ₹16,199 Cr"), summary
            assert "PAT ₹1,995 Cr (-31.2% QoQ, -36.2% YoY)" in summary, summary

            # PDF unusable and Screener behind: no fallback to last quarter's numbers
            pdf_reply.clear()
            pdf_reply.update(has_numbers=False)
            out = llm_router.enrich_quarterly_results("SUNPHARMA", title, "https://x/r.pdf", db_path=db)
            assert out["has_numbers"] is False and out["verification_status"] == "pdf_only_screener_behind", out

            # PDF unusable, Screener on the filing's quarter: Screener numbers with the same definitions
            llm_router.fetch_screener_fundamentals = lambda t: _sun_screener("Sep 2026")
            out = llm_router.enrich_quarterly_results("SUNPHARMA", title, "https://x/r.pdf", db_path=db)
            assert out["verification_status"] == "screener_fallback" and out["metrics"]["ebitda"]["value"] == "4,744", out
            assert out["basis_note"] == "Consolidated • EBITDA ex-other income" and out["model"] is None

            # Live SUNPHARMA miss: PDF revenue read off a sub-line fails the cross-check, Screener numbers are used,
            # and the filing's exceptional charge still reaches the takeaway (the hype takeaway is rejected)
            cur = {**SUN_RAW["columns"]["current"], "revenue_from_operations": 161000.1}
            pdf_reply.clear()
            pdf_reply.update(SUN_RAW, columns={**SUN_RAW["columns"], "current": cur})
            out = llm_router.enrich_quarterly_results("SUNPHARMA", title, "https://x/r.pdf", db_path=db)
            assert out["verification_status"] == "screener_fallback" and out["metrics"]["revenue"]["value"] == "16,199", out
            assert "exceptional charge of ₹1,269 Cr" in out["takeaway"] and out["model"] is None, out
    finally:
        for k, v in orig.items():
            setattr(llm_router, k, v)


KPIT_COMFORT = (
    "Giving guarantees/indemnity/ becoming a surety for third party: KPIT Technologies Limited has informed the Exchange "
    "about Issue of Letter of Comfort in favor of KPIT Technologies (UK) Limited, a wholly-owned subsidiary of the Company."
)
BFL_NCD = (
    "Allotment of Securities: Bajaj Finance Limited has informed the Exchange regarding allotment of 2,12,000 securities "
    "pursuant to Non Convertible Securities at its meeting held on October 21, 2026"
)


def test_classifier_rules() -> None:
    from backend.scrapers.filings import classify_heuristic, is_bank_or_nbfc

    cases = {
        "Reliance commissions new refinery unit": ("capex", 1),  # "fine" inside "refinery" is not litigation
        "Disclosure under Regulation 30 of SEBI (LODR) Regulations, 2015": ("unclassified", 0),
        "GST registration certificate update": ("unclassified", 0),
        "Intimation of Board Meeting to consider raising funds; Board of Directors to meet": ("unclassified", 0),
        "Appointment of Statutory Auditors": ("unclassified", 0),
        "General Updates: Appointment of Statutory Auditors Pursuant to Mandatory Auditor Rotation": ("unclassified", 0),
        "Appointment of Secretarial Auditor for FY27": ("unclassified", 0),
        "Appointment of Scrutinizer for Postal Ballot": ("unclassified", 0),
        "Order received from NHAI for ₹2,000 Cr": ("press_release", 1),
        "Notice of AGM": ("agm_egm", 0),
        "Notice of Board Meeting": ("unclassified", 0),
        KPIT_COMFORT: ("unclassified", 0),
        # ...but a results, dividend or acquisition filed with a subsidiary guarantee stays material
        "Outcome of Board Meeting: approved financial results for Q2 FY27 and corporate guarantee for wholly owned subsidiary": ("results", 1),
        "Board approved interim dividend of Rs 5 and issue of corporate guarantee for subsidiary": ("dividend", 1),
        "Acquisition of 100% stake in ABC; corporate guarantee to subsidiary": ("M&A", 1),
        # Investing in a subsidiary is M&A per ENAM, even with a guarantee for it in the same filing
        "Investment in wholly owned subsidiary and corporate guarantee": ("M&A", 1),
        "Outcome of Board Meeting: approval for investment in and corporate guarantee to wholly owned subsidiary": ("M&A", 1),
        "Capital infusion into subsidiary and corporate guarantee": ("M&A", 1),
        "Post Earnings Conference Call of the Company is scheduled on October 28, 2026": ("earnings_call", 1),
        "Show cause notice received from the GST department": ("litigation", 1),
        "SEBI levied a fine of ₹5 lakh on the Company": ("litigation", 1),
        "Receipt of tax demand order of ₹120 Cr": ("litigation", 1),
        "Retirement of Mr. Noel N Tata as the Chairman": ("management_change", 1),
        "Change in Director(s)": ("management_change", 1),
    }
    for title, want in cases.items():
        assert classify_heuristic(title) == want, (title, classify_heuristic(title), want)

    # Lenders' routine NCD allotments are not material; a non-financial company's NCD raise is
    assert is_bank_or_nbfc("Bajaj Finance Limited") and is_bank_or_nbfc("HDFC Bank Limited")
    assert not is_bank_or_nbfc("Sun Pharmaceutical Industries Limited") and not is_bank_or_nbfc("KPIT Technologies Ltd")
    assert classify_heuristic(BFL_NCD, is_lender=True) == ("fund_raise", 0)
    assert classify_heuristic(BFL_NCD.replace("Bajaj Finance", "Sun Pharmaceutical Industries")) == ("fund_raise", 1)
    assert classify_heuristic("Allotment of Commercial Paper", is_lender=True) == ("fund_raise", 0)
    assert classify_heuristic("Allotment of equity shares under QIP", is_lender=True) == ("fund_raise", 1)

    # The same guards hold when the LLM returns the wrong material category
    for title, llm_cat in [
        ("Appointment of Statutory Auditors", "management_change"),
        ("Intimation of Board Meeting to consider raising funds; Board of Directors to meet", "management_change"),
        (KPIT_COMFORT, "fund_raise"),
    ]:
        assert enforce_filing_hygiene(title, llm_cat, 1) == ("unclassified", 0), title
    assert enforce_filing_hygiene(BFL_NCD, "fund_raise", 1, is_lender=True) == ("fund_raise", 0)
    # An auditor appointment inside a results outcome keeps the results material
    both = "Outcome of Board Meeting: financial results for Q2 and appointment of secretarial auditors"
    assert enforce_filing_hygiene(both, "results", 1) == ("results", 1)
    # ...and a dividend or CFO appointment filed together with an auditor appointment stays material
    bundled = [
        ("Board approved interim dividend of Rs 10 per share and appointment of Secretarial Auditors", ("dividend", 1)),
        ("Appointment of Mr. Rajesh Kumar as Chief Financial Officer and re-appointment of Cost Auditors",
         ("management_change", 1)),
    ]
    for desc, want in bundled:
        got = classify_heuristic("Outcome of Board Meeting", desc)
        assert got == want, (desc, got, want)
    cfo = "Outcome of Board Meeting: appointment of Mr. A as CFO and appointment of internal auditors"
    assert enforce_filing_hygiene(cfo, "management_change", 1) == ("management_change", 1)
    assert enforce_filing_hygiene("Investment in wholly owned subsidiary and corporate guarantee", "M&A", 1) == ("M&A", 1)
    # Subsidiary IPO filings misread as results are fund raises; matched as whole words ("offering" via offer\w*)
    assert enforce_filing_hygiene("Results of Jio Platforms initial public offering", "results", 1) == ("fund_raise", 1)


def test_batch_triage_reads_pdf_for_vague_titles() -> None:
    sent: dict = {}
    calls: list = []

    def fake_extract(url, **kw):
        calls.append((url, kw))
        return "\n--- [Page 1 of 2] ---\nRetirement of Mr. Noel N Tata as Chairman w.e.f. 11 Nov 2026. " + "x" * 5000 if url == "https://x/a.pdf" else ""

    def fake_llm(prompt, **kw):
        sent["prompt"] = prompt
        return [
            {"filing_id": "a", "category": "management_change", "materiality": 1, "summary": "Noel Tata retires as Chairman."},
            {"filing_id": "c", "category": "press_release", "materiality": 1, "summary": "Wins ₹2,000 Cr NHAI order."},
            {"filing_id": "d", "category": "litigation", "materiality": 1, "summary": ""},  # no summary -> not triaged
            {"filing_id": "zz", "category": "litigation", "materiality": 1, "summary": "invented"},
        ]

    filings = [
        {"filing_id": "a", "ticker": "TRENT", "company_name": "Trent Limited", "category": "management_change", "materiality": "1",
         "title": "Change in Director(s): Trent Limited has informed the Exchange regarding Change in Director(s) of the company.",
         "pdf_url": "https://x/a.pdf"},
        {"filing_id": "b", "ticker": "HDFCBANK", "company_name": "HDFC Bank Limited", "category": "unclassified", "materiality": 0,
         "title": "Disclosure under Regulation 30 of SEBI (LODR) Regulations, 2015", "pdf_url": "https://x/b.pdf"},
        {"filing_id": "c", "ticker": "LT", "category": "press_release", "materiality": 1,
         "title": "Order received from NHAI for ₹2,000 Cr", "pdf_url": "https://x/c.pdf"},
        {"filing_id": "d", "ticker": "LT", "category": "unclassified", "materiality": 0, "title": "Update", "pdf_url": ""},
    ]
    orig = llm_router.extract_pdf_text, llm_router._call_gemini_json
    try:
        llm_router.extract_pdf_text, llm_router._call_gemini_json = fake_extract, fake_llm
        out = llm_router.classify_and_summarize_batch(filings)
    finally:
        llm_router.extract_pdf_text, llm_router._call_gemini_json = orig

    p = sent["prompt"]
    assert "Retirement of Mr. Noel N Tata as Chairman" in p and "x" * 1500 not in p, "excerpt missing or not capped"
    assert "[Page 1" not in p and llm_router.NO_DETAIL_SUMMARY in p
    assert '"b"' not in p, "a vague filing with no PDF text was sent to the model"
    assert [u for u, _ in calls] == ["https://x/a.pdf", "https://x/b.pdf"], calls  # only vague titles open the PDF
    assert all(kw.get("max_pages") == 1 for _, kw in calls)
    assert out["b"] == {"category": "unclassified", "materiality": 0, "summary": "Filing does not specify details; see attached PDF"}
    assert out["a"]["summary"] == "Noel Tata retires as Chairman." and out["c"]["category"] == "press_release"
    assert "d" not in out and "zz" not in out, out


def test_triage_loop_covers_all_untriaged_and_stops() -> None:
    from backend import push_engine

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "triage.db"
        init_db(db)
        conn = sqlite3.connect(db)
        rows = [(f"f{i:02d}", "DIXON" if i % 2 else "TRENT", f"Filing {i}", f"2026-10-{1 + i % 28:02d} 10:00:00") for i in range(40)]
        conn.executemany("INSERT INTO announcements (filing_id, ticker, title, published_at) VALUES (?,?,?,?)", rows)
        conn.commit()
        conn.close()

        batches: list[int] = []

        def fake_triage(batch, db_path=None, batch_size=15):
            batches.append(len(batch))
            return {f["filing_id"]: {"category": "unclassified", "materiality": 0, "summary": "s"}
                    for f in batch if f["filing_id"] != "f07"}  # f07 keeps failing

        patches = {"fetch_eod_metrics": lambda *a, **k: [], "fetch_company_filings": lambda *a, **k: [],
                   "classify_and_summarize_batch": fake_triage}
        orig = {k: getattr(push_engine, k) for k in patches}
        orig_max = push_engine.MAX_TRIAGE_BATCHES
        try:
            for k, v in patches.items():
                setattr(push_engine, k, v)
            out = push_engine.refresh_portfolio_push_data(db_path=db)
            assert batches == [15, 15, 10], batches  # DIXON (not in portfolio, saved by Pull) is included
            assert out["filings_triaged_by_llm"] == 39 and out["filings_left_untriaged"] == 1, out
            conn = sqlite3.connect(db)
            blank = [r[0] for r in conn.execute("SELECT filing_id FROM announcements WHERE summary IS NULL OR summary = ''")]
            conn.close()
            assert blank == ["f07"], blank  # the failed row is not marked as triaged

            # No progress stops the loop after one batch
            batches.clear()
            push_engine.classify_and_summarize_batch = lambda batch, **k: batches.append(len(batch)) or {}
            push_engine.refresh_portfolio_push_data(db_path=db)
            assert batches == [1], batches

            # Max batch count caps spend
            conn = sqlite3.connect(db)
            conn.execute("UPDATE announcements SET summary = NULL")
            conn.commit()
            conn.close()
            batches.clear()
            push_engine.classify_and_summarize_batch = fake_triage
            push_engine.MAX_TRIAGE_BATCHES = 2
            out = push_engine.triage_untriaged_filings(db_path=db)  # the entry point warmup_demo.py also uses
            assert batches == [15, 15] and out == {"triaged": 29, "left": 11}, (batches, out)
        finally:
            for k, v in orig.items():
                setattr(push_engine, k, v)
            push_engine.MAX_TRIAGE_BATCHES = orig_max


def test_dedup_picks_informative_primary_card() -> None:
    import json
    from backend import push_engine

    pub = "2026-10-29 16:32:13"
    bio = {"role": "Chairman and Director", "action": "Retirement", "effective_date": "11th November 2026",
           "two_line_bio": "b", "source_url": "https://x/web"}
    rows = [  # the real TRENT pair: same timestamp, enrichment names the same person, vague one comes first
        ("vague", "management_change", "Change in Director(s): Trent Limited has informed the Exchange regarding Change in Director(s) of the company.",
         "https://x/cid.pdf", {**bio, "person_name": "Mr. Noel N. Tata"}),
        ("named", "management_change", "Retirement: Trent Limited has informed the Exchange about Retirement of Mr. Noel N Tata as the Chairman",
         "https://x/ret.pdf", {**bio, "person_name": "Noel N. Tata"}),
        # results: the vague board outcome carries the metrics, the press release names the results
        ("outcome", "results", "Outcome of Board Meeting: Trent Limited has submitted the Outcome of Board Meeting held on October 29, 2026",
         "https://x/out.pdf", {"metrics": {"pat": {"value": "1"}}, "summary": "PAT up"}),
        ("press", "results", "Press Release: Trent Limited has informed the Exchange regarding Q2FY27 financial results",
         "https://x/pr.pdf", {}),
    ]
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "dedup.db"
        init_db(db)
        conn = sqlite3.connect(db)
        conn.executemany(
            "INSERT INTO announcements (filing_id, ticker, company_name, title, pdf_url, published_at, category, materiality, summary, enriched_json)"
            " VALUES (?, 'TRENT', 'Trent Limited', ?, ?, ?, ?, 1, 's', ?)",
            [(fid, title, url, pub, cat, json.dumps(enr)) for fid, cat, title, url, enr in rows],
        )
        conn.commit()
        conn.close()
        digest = push_engine.get_windowed_digest("30d", tickers=["TRENT"], db_path=db)

    by_id = {x["filing_id"]: x for x in digest["material"]}
    assert sorted(by_id) == ["named", "press"], sorted(by_id)
    assert digest["routine"] == [], "duplicates must not land in the routine drawer"
    assert digest["stats"]["duplicates_merged"] == 2
    named = by_id["named"]
    assert [(d["filing_id"], d["pdf_url"]) for d in named["also_filed_as"]] == [("vague", "https://x/cid.pdf")]
    assert named["enriched"]["person_name"] == "Noel N. Tata", "equal payloads: primary keeps its own"
    press = by_id["press"]
    assert press["enriched"]["metrics"] == {"pat": {"value": "1"}}, "primary must take the richest enrichment"
    assert press["also_filed_as"][0]["filing_id"] == "outcome"


def test_window_anchor_uses_last_refresh() -> None:
    import inspect
    from backend import push_engine
    from backend.db import set_meta

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "anchor.db"
        init_db(db)
        conn = sqlite3.connect(db)
        ins = "INSERT INTO announcements (filing_id, ticker, title, published_at, category, materiality, summary) VALUES (?, 'TRENT', ?, ?, ?, 1, 's')"
        conn.executemany(ins, [("new", "Financial results Q4", "2024-05-10 10:00:00", "results"),
                               ("old", "Acquisition of a stake", "2024-05-01 10:00:00", "M&A")])
        conn.commit()
        conn.close()

        # Never refreshed: the window falls back to the latest filing
        d = push_engine.get_windowed_digest("24h", db_path=db)
        assert d["last_refreshed_at"] is None and d["window_anchor_source"] == "latest_filing", d
        assert d["window_start"] == "2024-05-09T10:00:00+05:30", d["window_start"]
        assert [x["filing_id"] for x in d["material"]] == ["new"]

        # After a refresh the window is measured from last_refreshed_at, not MAX(published_at)
        set_meta({"last_refreshed_at": "2024-05-20T09:00:00+05:30"}, db)
        d = push_engine.get_windowed_digest("24h", db_path=db)
        assert d["window_anchor_source"] == "last_refresh" and d["window_start"] == "2024-05-19T09:00:00+05:30", d
        assert d["material"] == [] and d["cutoff_timestamp"] == "2024-05-19 09:00:00"
        assert len(push_engine.get_windowed_digest("14d", db_path=db)["material"]) == 1  # 05-06 onward: "new" only
        alerts = push_engine.get_triggered_alerts("3d", db_path=db)
        assert alerts["window_start"] == "2024-05-17T09:00:00+05:30" and alerts["last_refreshed_at"] == "2024-05-20T09:00:00+05:30"
        assert len(push_engine.get_triggered_alerts("30d", db_path=db)["filing_alerts"]) == 2

    # The API default matches the UI's 24h
    assert inspect.signature(main.api_get_digest).parameters["window"].default.default == "24h"


def test_eod_alerts_use_completed_session_and_stable_ids() -> None:
    from datetime import date, datetime, timezone
    import pandas as pd
    from backend import push_engine
    from backend.scrapers import market_data

    done = market_data.is_completed_session
    utc = lambda *a: datetime(*a, tzinfo=timezone.utc)
    fri, sat = date(2026, 10, 30), date(2026, 10, 31)
    assert not done(fri, utc(2026, 10, 30, 8, 30))  # Fri 14:00 IST: today's bar still trading
    assert done(date(2026, 10, 29), utc(2026, 10, 30, 8, 30))  # yesterday's bar is final
    assert not done(fri, utc(2026, 10, 30, 10, 0))  # exactly 15:30 IST: not after the close yet
    assert done(fri, utc(2026, 10, 30, 10, 1))  # 15:31 IST
    assert done(fri, utc(2026, 10, 31, 5, 30))  # Saturday: Friday's bar is the latest and final
    # 19:00 UTC Fri = 00:30 IST Sat: IST is already on the 31st, so Friday is final and a "Sat" bar is not
    assert done(fri, utc(2026, 10, 30, 19, 0)) and not done(sat, utc(2026, 10, 30, 19, 0))

    with _env(FINN_REFRESH_MINUTES=None):
        assert main._refresh_minutes() == 0
    for raw, want in (("abc", 0), ("-5", 0), ("0", 0), ("1.5", 0), (" 15 ", 15)):
        with _env(FINN_REFRESH_MINUTES=raw):
            assert main._refresh_minutes() == want, raw

    # 21 closed sessions at Rs 100 / 1000 shares, then Fri +5%, then Mon's unfinished bar (-14%, 5x volume so far)
    days = list(pd.bdate_range("2026-10-01", "2026-10-30")) + [pd.Timestamp("2026-11-02")]
    closes = [100.0] * (len(days) - 2) + [105.0, 90.0]
    hist = pd.DataFrame({"Close": closes, "Volume": [1000] * (len(days) - 1) + [5000]}, index=pd.DatetimeIndex(days))
    orig_yf = market_data.yf
    market_data.yf = SimpleNamespace(Ticker=lambda sym: SimpleNamespace(history=lambda **kw: hist.copy()))
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = Path(tmp) / "eod.db"
            init_db(db)  # default rules: 3.0% / 2.0x
            ins = "INSERT INTO announcements (filing_id, ticker, title, published_at, category, materiality, summary) VALUES (?, 'TRENT', ?, ?, 'results', 1, 's')"
            conn = sqlite3.connect(db)
            conn.execute(ins, ("f-77", "Financial results Q2", "2026-10-30 18:00:00"))
            conn.commit()
            conn.close()

            # Mon 11:00 IST: the partial bar is dropped, Friday's close is evaluated
            [m] = market_data.fetch_eod_metrics(["TRENT"], db_path=db, now=utc(2026, 11, 2, 5, 30))
            assert (m["trade_date"], m["pct_change"], m["volume_ratio"]) == ("2026-10-30", 5.0, 1.0), m
            alerts = push_engine.get_triggered_alerts("30d", db_path=db)
            [a] = alerts["market_alerts"]
            assert a["trade_date"] == "2026-10-30" and a["alert_ids"] == ["market:TRENT:2026-10-30:price"], a
            assert [f["alert_ids"] for f in alerts["filing_alerts"]] == [["filing:f-77"]]
            assert push_engine.get_triggered_alerts("30d", db_path=db)["market_alerts"][0]["alert_ids"] == a["alert_ids"]

            # Mon 16:00 IST: the session closed, so Monday's bar now counts and trips both rules
            [m] = market_data.fetch_eod_metrics(["TRENT"], db_path=db, now=utc(2026, 11, 2, 10, 30))
            assert m["trade_date"] == "2026-11-02" and m["volume_alert"] and m["price_alert"], m
            [a] = push_engine.get_triggered_alerts("30d", db_path=db)["market_alerts"]
            assert a["alert_ids"] == ["market:TRENT:2026-11-02:price", "market:TRENT:2026-11-02:volume"], a
    finally:
        market_data.yf = orig_yf


def test_background_refresh_skips_overlap_survives_errors_and_cancels() -> None:
    import asyncio
    import time

    calls: list[int] = []

    def fake_refresh():
        calls.append(len(calls))
        if len(calls) == 2:
            raise RuntimeError("boom")
        time.sleep(0.6)
        return {"ok": True}

    logs: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: logs.append(record.getMessage())
    orig_refresh, orig_init = main.refresh_portfolio_push_data, main.init_db
    main.refresh_portfolio_push_data = fake_refresh
    main._bg_log.addHandler(handler)
    main._bg_log.setLevel(logging.INFO)  # uvicorn normally sets INFO on its parent logger
    try:
        async def run() -> None:
            # 0.4 s ticks, 0.6 s runs: run 1 at .4 (to 1.0), skip .8, run 2 at 1.2 fails, run 3 at 1.6, skip 2.0, cancel 2.1
            task = asyncio.create_task(main._background_refresh_loop(0.4 / 60))
            await asyncio.sleep(2.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await asyncio.sleep(0.05)
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
            assert pending == [], pending  # the in-flight run 3 was cancelled with the loop

        asyncio.run(run())
        assert len(calls) == 3, calls
        assert sum("skipping" in m for m in logs) == 2, logs
        assert any("cycle failed: RuntimeError: boom" in m for m in logs), logs

        # Lifespan: no loop without the env var; with it, shutdown cancels the sleeping loop promptly
        main.init_db = lambda *a, **k: None
        for minutes, enabled in ((None, False), ("1", True)):
            logs.clear()
            with _env(FINN_REFRESH_MINUTES=minutes):
                started = time.monotonic()
                with TestClient(main.app):
                    pass
            assert time.monotonic() - started < 5
            assert any("enabled: every 1 min" in m for m in logs) == enabled, logs
    finally:
        main.refresh_portfolio_push_data, main.init_db = orig_refresh, orig_init
        main._bg_log.removeHandler(handler)
        main._bg_log.setLevel(logging.NOTSET)


def test_failed_refresh_keeps_last_refreshed_at() -> None:
    from backend import push_engine
    from backend.db import get_meta, set_meta

    ok = {"status": "ok", "http_code": 200, "message": "3 filings"}
    blocked = {"status": "blocked", "http_code": 403, "message": "Access denied"}
    err = {"status": "error", "http_code": None, "message": "ConnectError: boom"}
    empty = {"status": "empty", "http_code": 200, "message": "0 filings"}
    scenario: dict = {}

    def fake_fetch(ticker, **kw):
        kw["source_status"].update(scenario.get(ticker, scenario["*"]))
        return []

    patches = {"fetch_eod_metrics": lambda *a, **k: [], "fetch_company_filings": fake_fetch}
    orig = {k: getattr(push_engine, k) for k in patches}
    try:
        for k, v in patches.items():
            setattr(push_engine, k, v)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = Path(tmp) / "refresh.db"
            init_db(db)
            set_meta({"last_refreshed_at": "2024-05-20T09:00:00+05:30"}, db)

            # Every source failed: last_refreshed_at stays put, the failed attempt is recorded
            scenario.update({"*": {"NSE": blocked, "BSE": blocked}, "TRENT": {"NSE": err, "BSE": blocked}})
            out = push_engine.refresh_portfolio_push_data(db_path=db)
            meta = get_meta(db)
            assert out["refresh_ok"] is False and meta["last_refreshed_at"] == "2024-05-20T09:00:00+05:30", (out, meta)
            assert meta["last_refresh_attempt_at"] > "2024-05-20" and "NSE blocked (HTTP 403)" in meta["last_refresh_error"], meta
            nse = out["source_status"]["NSE"]
            assert nse["status"] == "blocked" and nse["counts"] == {"blocked": len(SEED_PORTFOLIO) - 1, "error": 1}, nse
            status = push_engine.get_push_data_status(24, db_path=db)
            assert status["last_refresh_error"] and status["source_status"]["BSE"]["status"] == "blocked", status
            assert status["window_start"] == "2024-05-19T09:00:00+05:30"

            # One stock's exchange answered (even with no rows): the refresh counts, the error clears
            scenario.clear()
            scenario.update({"*": {"NSE": err, "BSE": blocked}, "TRENT": {"NSE": err, "BSE": empty}})
            out = push_engine.refresh_portfolio_push_data(db_path=db)
            meta = get_meta(db)
            assert out["refresh_ok"] is True and meta["last_refreshed_at"] == meta["last_refresh_attempt_at"] > "2024-05-21", meta
            assert out["source_status"]["BSE"]["status"] == "empty" and "stocks failed" in out["source_status"]["BSE"]["message"]
            assert push_engine.get_push_data_status(24, db_path=db)["last_refresh_error"] is None

            scenario.clear()
            scenario["*"] = {"NSE": ok}
            out = push_engine.refresh_portfolio_push_data(db_path=db)
            assert out["source_status"] == {"NSE": {**ok, "message": f"{len(SEED_PORTFOLIO)} stocks checked", "counts": {"ok": len(SEED_PORTFOLIO)}}}, out
    finally:
        for k, v in orig.items():
            setattr(push_engine, k, v)


def test_one_failed_enrichment_does_not_fail_refresh() -> None:
    import json
    from backend import push_engine

    def fake_eod(tickers, db_path=None, save_to_db=True):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO eod_prices (ticker, trade_date, close_price, prev_close, pct_change, volume, avg_volume_20d, "
                     "volume_ratio) VALUES ('TRENT', '2026-10-30', 4321.5, 4300, 0.5, 1, 1, 1)")
        conn.commit()
        conn.close()
        return [{"ticker": "TRENT"}]

    def fake_filings(ticker, db_path=None, source_status=None, **kw):
        source_status.update({"NSE": {"status": "ok", "http_code": 200, "message": "3 filings"}})
        rows = [(f"r{i}", ticker, f"Results Q{i}", f"2026-10-2{i} 10:00:00") for i in (1, 2, 3)]
        conn = sqlite3.connect(db_path)
        conn.executemany("INSERT INTO announcements (filing_id, ticker, title, published_at, category, materiality, summary) "
                         "VALUES (?,?,?,?,'results',1,'s')", rows)
        conn.commit()
        conn.close()
        return rows

    def fake_results(ticker, filing_title, **kw):
        if filing_title == "Results Q2":
            raise RuntimeError("Gemini 500")
        return {"summary": f"ok {filing_title}", "metrics": {}}

    patches = {"fetch_eod_metrics": fake_eod, "fetch_company_filings": fake_filings, "enrich_quarterly_results": fake_results}
    orig = {k: getattr(push_engine, k) for k in patches}
    try:
        for k, v in patches.items():
            setattr(push_engine, k, v)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = Path(tmp) / "enrich.db"
            init_db(db)
            out = push_engine.refresh_portfolio_push_data(tickers=["TRENT"], db_path=db)
            assert out["enrichment_errors"] == 1 and out["filings_deep_enriched"] == 2 and out["refresh_ok"], out
            conn = sqlite3.connect(db)
            enriched = dict(conn.execute("SELECT filing_id, enriched_json FROM announcements WHERE ticker = 'TRENT'"))
            price = conn.execute("SELECT close_price FROM eod_prices WHERE ticker = 'TRENT'").fetchone()
            conn.close()
            assert json.loads(enriched["r1"])["summary"] == "ok Results Q1" and json.loads(enriched["r3"])["summary"] == "ok Results Q3"
            assert enriched["r2"] in (None, "", "{}"), enriched  # failed card stays blank for the next refresh
            assert price == (4321.5,), price
    finally:
        for k, v in orig.items():
            setattr(push_engine, k, v)


def test_filing_source_status_and_bse_fallback() -> None:
    import httpx
    from backend.scrapers import filings

    nse_rows = [{"desc": f"Disclosure {i}", "attchmntFile": f"https://x/{i}.pdf", "an_dt": "10-May-2024 10:00:00",
                 "sm_name": "Trent Limited"} for i in range(30)]
    bse_rows = {"Table": [{"NEWSSUB": "Outcome of Board Meeting", "ATTACHMENTNAME": "a.pdf", "NEWS_DT": "2024-05-10T10:00:00",
                           "SLONGNAME": "Trent Ltd"}]}
    script: dict = {}
    calls: list[str] = []

    class FakeClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, **kw):
            calls.append(url)
            host = "bse_api" if "api.bseindia.com" in url else "nse_api" if "/api/" in url else "warmup"
            reply = script.get(host, httpx.Response(200, text="<html></html>"))
            if isinstance(reply, Exception):
                raise reply
            return reply

    def run(nse, bse=None, **kw):
        calls.clear()
        script.clear()
        script.update(nse_api=nse, **({"bse_api": bse} if bse is not None else {}))
        status: dict = {}
        items = filings.fetch_company_filings("TRENT", bse_code="500251", source_status=status, **{"save_to_db": False, **kw})
        return items, {k: v["status"] for k, v in status.items()}

    orig = filings.httpx.Client
    filings.httpx.Client = FakeClient
    try:
        # NSE exception -> error, BSE tried as the fallback after a cookie warm-up on www.bseindia.com
        items, st = run(httpx.ConnectError("boom"), httpx.Response(403, text="Access Denied"))
        assert items == [] and st == {"NSE": "error", "BSE": "blocked"}, st
        assert calls.index("https://www.bseindia.com/") < next(i for i, u in enumerate(calls) if "api.bseindia.com" in u), calls
        items, st = run(httpx.Response(403, text="Access Denied"), httpx.Response(200, json=bse_rows))
        assert st == {"NSE": "blocked", "BSE": "ok"} and [f["exchange"] for f in items] == ["BSE"], (st, items)
        items, st = run(httpx.Response(200, json=[]), httpx.Response(200, json={"Table": []}))
        assert items == [] and st == {"NSE": "empty", "BSE": "empty"}, st
        _, st = run(httpx.Response(200, text="<html>Access Denied</html>"), httpx.Response(500, text="x"))
        assert st == {"NSE": "blocked", "BSE": "error"}, st

        # 30 NSE filings in the window: all kept (no 25-row cutoff), BSE not called, Pull-style save still works
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = Path(tmp) / "filings.db"
            init_db(db)
            items, st = run(httpx.Response(200, json=nse_rows), save_to_db=True, db_path=db)
            conn = sqlite3.connect(db)
            saved = conn.execute("SELECT COUNT(*) FROM announcements WHERE ticker = 'TRENT'").fetchone()[0]
            conn.close()
        assert st == {"NSE": "ok"} and len(items) == 30 == saved, (st, len(items), saved)
        assert not any("bseindia" in u for u in calls), calls
    finally:
        filings.httpx.Client = orig


if __name__ == "__main__":
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_") and callable(fn)]
    for fn in tests:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(tests)} offline test(s) passed")
