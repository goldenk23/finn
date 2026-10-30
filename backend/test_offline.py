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


if __name__ == "__main__":
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_") and callable(fn)]
    for fn in tests:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(tests)} offline test(s) passed")
