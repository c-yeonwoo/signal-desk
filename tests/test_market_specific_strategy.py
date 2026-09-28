"""KR/US policy context and forward-only market benchmark must remain isolated."""

import datetime as dt
from types import SimpleNamespace

import pandas as pd

from signal_desk import api, bot, db, store
from signal_desk.signals import market_regime_study, performance_evidence


def test_us_market_read_uses_only_aligned_us_breadth_and_fred(monkeypatch):
    tickers = [f"US{i:03d}" for i in range(100)]
    dates = pd.bdate_range(end="2026-09-23", periods=61).strftime("%Y-%m-%d").tolist()
    captured = []
    monkeypatch.setattr(bot.market_clock, "latest_completed_session", lambda market, now: "2026-09-23")
    monkeypatch.setattr(bot.store, "load_us_universe", lambda: [{"ticker": t} for t in tickers])
    monkeypatch.setattr(bot.store, "load_portfolio_close_bundle", lambda market: (
        {t: [100.0] * 60 + [110.0] for t in tickers},
        {t: dates for t in tickers}))
    monkeypatch.setattr(bot.store, "load_price_series", lambda: (_ for _ in ()).throw(AssertionError("KR prices")))
    monkeypatch.setattr(bot.store, "load_macro_kr", lambda: (_ for _ in ()).throw(AssertionError("KR macro")))
    monkeypatch.setattr(bot.store, "load_market_flow", lambda: (_ for _ in ()).throw(AssertionError("KR flow")))
    monkeypatch.setattr(bot.store, "load_macro", lambda: [])
    monkeypatch.setattr(bot.regime, "classify", lambda prices: captured.append(prices) or
                        {"ready": True, "regime": "강세"})

    out = bot._market_read_for("us")
    assert out["context"]["market"] == "us"
    assert out["context"]["regime"] == "강세"
    assert out["context"]["exposure"] == 1.0
    assert out["context"]["regime_coverage"] == 1.0
    assert out["context"]["regime_n"] == 100
    assert out["context"]["regime_universe_n"] == 100
    assert len(captured[0]) == 100


def test_bot_day_uses_exchange_local_date_across_kst_midnight(monkeypatch):
    class FrozenDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 9, 23, 15, 30, tzinfo=dt.timezone.utc).astimezone(tz)

    monkeypatch.setattr(bot.datetime, "datetime", FrozenDateTime)
    assert bot._today("us") == "2026-09-23"
    assert bot._today("kr") == "2026-09-24"


def test_us_missing_or_stale_breadth_does_not_use_kr_or_full_exposure(monkeypatch):
    monkeypatch.setattr(bot.market_clock, "latest_completed_session", lambda market, now: "2026-09-23")
    monkeypatch.setattr(bot.store, "load_us_universe", lambda: [{"ticker": "A"}])
    monkeypatch.setattr(bot.store, "load_portfolio_close_bundle", lambda market: (
        {"A": [100.0]}, {"A": ["2026-09-22"]}))
    monkeypatch.setattr(bot.store, "load_price_series", lambda: {"KR": [100.0] * 100})
    monkeypatch.setattr(bot.store, "load_macro", lambda: [])
    monkeypatch.setattr(bot.store, "load_macro_kr", lambda: (_ for _ in ()).throw(AssertionError("KR macro")))
    out = bot._market_read_for("us")
    assert out["context"]["regime_ready"] is False
    assert out["context"]["regime"] is None
    assert out["context"]["exposure"] == 0.7


def test_us_universe_history_is_first_observed_and_never_backfilled(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    original = [{"ticker": f"US{i:03d}"} for i in range(500)]
    changed = [{"ticker": f"US{i:03d}"} for i in range(499)] + [{"ticker": "NEW"}]
    first = dt.datetime(2026, 9, 22, 22, tzinfo=dt.timezone.utc)
    later = dt.datetime(2026, 9, 23, 22, tzinfo=dt.timezone.utc)
    assert store._snapshot_us_universe(original, first)
    assert not store._snapshot_us_universe(changed, first)
    assert store._snapshot_us_universe(changed, later)
    history = store.load_us_universe_history()
    assert sorted(history) == ["2026-09-23"]  # 같은 날 구성 변경은 기준선에서 격리
    assert history["2026-09-23"][-1]["ticker"] == "NEW"
    assert '"revision_detected_at"' in store.US_UNIVERSE_HISTORY_FILE.read_text()


def test_incomplete_us_universe_response_keeps_previous_cache(tmp_path, monkeypatch):
    from signal_desk.ingest import us

    monkeypatch.chdir(tmp_path)
    previous = [{"ticker": f"US{i:03d}"} for i in range(500)]
    store._write_json(store.US_UNIVERSE_FILE, previous)
    monkeypatch.setattr(us, "sp500_constituents", lambda: [{"ticker": "AAPL"}])
    assert store.fetch_us_universe() == []
    assert store.load_us_universe() == previous
    assert store.load_us_universe_history() == {}


def test_us_universe_is_refreshed_once_after_its_own_close(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    now = dt.datetime(2026, 9, 23, 22, tzinfo=dt.timezone.utc)
    calls = []
    calendar = SimpleNamespace(schedule=pd.DataFrame(
        {"close": [pd.Timestamp("2026-09-23T20:00:00Z")]}, index=["2026-09-23"]))
    monkeypatch.setattr(api.market_clock, "latest_completed_session", lambda market, at: "2026-09-23")
    monkeypatch.setattr(api.market_clock, "is_open", lambda market, at: False)
    monkeypatch.setattr(api.market_clock, "_calendar", lambda market: calendar)
    monkeypatch.setattr(api.store, "fetch_us_universe", lambda: calls.append(True) or
                        [{"ticker": f"US{i:03d}"} for i in range(500)])
    assert api._maybe_refresh_us_universe(now)
    assert not api._maybe_refresh_us_universe(now)
    assert len(calls) == 1


def test_us_benchmark_starts_only_after_observed_membership(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    days = ["2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25"]
    curve = [100.0, 90.0, 99.0, 108.9]
    for day, value in zip(days, curve):
        db.bot_equity_record(911, "us", day, value, value, 0)
    monkeypatch.setattr(bot, "_cfg", lambda uid: {"seed_cash_us": 100, "seed_cash": 100})
    monkeypatch.setattr(bot.paper, "balance", lambda uid, market: {"total_eval": 108.9})
    history = {day: [{"ticker": "A"}, {"ticker": "B"}] for day in days[:3]}
    closes = {"A": (days, [100.0, 110.0, 121.0, 133.1]),
              "B": (days, [100.0, 90.0, 99.0, 108.9])}
    full = db.bot_equity_curve(911, "us")
    assert performance_evidence.pit_equal_weight_curve(
        full, "us", dated_closes=closes, universe_history=history) is None
    out = bot.performance(911, "us", dated_closes=closes, universe_history=history)
    assert out["comparison_window"] == ["2026-09-23", "2026-09-25"]
    assert out["benchmark_return_pct"] == 21.0
    assert out["comparison_return_pct"] == 21.0
    assert out["excess_return_pct"] == 0.0
    assert len(out["benchmark_curve"]) == len(out["comparison_curve"]) == 3


def test_us_benchmark_refuses_missing_member_price(monkeypatch):
    days = ["2026-09-23", "2026-09-24"]
    out = performance_evidence.pit_equal_weight_curve(
        [{"date": d} for d in days], "us",
        dated_closes={"A": (days, [100.0, 110.0])},
        universe_history={"2026-09-22": [{"ticker": "A"}, {"ticker": "B"}]})
    assert out is None


def test_us_benchmark_does_not_extend_stale_membership():
    days = ["2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25"]
    prices = {"A": (days, [100.0, 110.0, 121.0, 133.1])}
    result = performance_evidence.pit_equal_weight_curve(
        [{"date": d} for d in days[1:]], "us", dated_closes=prices,
        universe_history={days[0]: [{"ticker": "A"}]})
    assert result is None  # 23일 관측 누락: 24일 구간에 22일 구성종목을 재사용 금지


def test_regime_research_pairs_only_same_market_next_session(monkeypatch):
    monkeypatch.setattr(market_regime_study.store, "regime_history", lambda market: [
        {"date": "2026-09-22", "regime": "약세", "exposure": 0.4, "ready": True},
        {"date": "2026-09-23", "regime": None, "exposure": 0.7, "ready": False}]
        if market == "us" else [])
    monkeypatch.setattr(market_regime_study.store, "market_return_by_date", lambda market: {
        "2026-09-23": 0.02, "2026-09-24": -0.01} if market == "us" else {})
    monkeypatch.setattr(market_regime_study.market_clock, "next_sessions",
                        lambda market, day, n: [{"2026-09-22": "2026-09-23",
                                                "2026-09-23": "2026-09-24"}[day]])
    out = market_regime_study.report("us")
    assert out["market"] == "us" and out["paired_sessions"] == 1
    assert out["excluded"]["unready"] == 1
    assert out["binding"] is False and out["live_eligible"] is False
    assert market_regime_study.report("kr")["paired_sessions"] == 0


def test_us_regime_snapshot_uses_us_session_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    now = dt.datetime(2026, 9, 23, 22, tzinfo=dt.timezone.utc)
    db.kv_set("us_signal_snapshot_session", "2026-09-23")
    monkeypatch.setattr(api.market_clock, "latest_completed_session", lambda market, at: "2026-09-23")
    monkeypatch.setattr(api.bot, "_market_read_for", lambda market: {"context": {
        "regime": "강세", "exposure": 1.0, "regime_ready": True,
        "price_session": "2026-09-23"}})
    assert api._maybe_snapshot_us_regime(now)
    assert not api._maybe_snapshot_us_regime(now)
    assert store.regime_history("kr") == []
    assert store.regime_history("us")[0]["date"] == "2026-09-23"


def test_kr_regime_snapshot_records_bot_exposure_and_readiness(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(api.bot, "_market_read_for", lambda market: {"context": {
        "regime": "약세", "exposure": 0.32, "regime_ready": True}})
    assert api._snapshot_kr_regime("2026-09-28")
    assert store.regime_history("kr") == [{"date": "2026-09-28", "regime": "약세",
                                          "ready": True, "exposure": 0.32}]
    assert store.regime_history("us") == []

    monkeypatch.setattr(api.bot, "_market_read_for", lambda market: {"context": {
        "regime": None, "exposure": 0.7, "regime_ready": False}})
    assert api._snapshot_kr_regime("2026-09-29")
    assert store.regime_history("kr")[-1]["ready"] is False


def test_market_regime_research_api_is_admin_only(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_EMAILS", "market-admin@example.com")
    monkeypatch.setattr(api.market_regime_study, "report", lambda market: {
        "market": market, "binding": False, "live_eligible": False})
    monkeypatch.setattr(api.bot, "_market_read_for", lambda market: {"context": {
        "regime_ready": False, "regime_n": 470, "regime_universe_n": 500,
        "regime_coverage": 0.94, "price_session": "2026-09-28"}})
    guest = TestClient(api.app)
    assert guest.get("/api/admin/research/market-regime?market=us").status_code == 401
    guest.post("/api/auth/signup", json={"email": "reader@example.com", "pw": "abcdef12"})
    assert guest.get("/api/admin/research/market-regime?market=us").status_code == 403
    admin = TestClient(api.app)
    admin.post("/api/auth/signup", json={"email": "market-admin@example.com", "pw": "abcdef12"})
    response = admin.get("/api/admin/research/market-regime?market=us")
    assert response.status_code == 200 and response.json()["market"] == "us"
    assert response.json()["current_input"]["regime_coverage"] == 0.94
    assert admin.get("/api/admin/research/market-regime?market=eu").status_code == 422


def test_meta_entry_shadow_loads_closes_only_for_its_market(monkeypatch):
    history = pd.DataFrame([{"date": "2026-09-28", "ticker": "AAPL", "kind": "BUY"}])
    seen = []
    monkeypatch.setattr(api.store, "load_signal_history", lambda market: history)
    monkeypatch.setattr(api.store, "load_market_dated_closes", lambda market: seen.append(market) or {
        "AAPL": (["2026-09-28"], [100.0])})
    monkeypatch.setattr(api.store, "load_all_dated_closes", lambda: (_ for _ in ()).throw(
        AssertionError("시장 혼합 가격열 사용 금지")))
    monkeypatch.setattr(api.meta_entry, "build_labeled_rows", lambda rows, series, cfg, **kwargs: [])
    result = api._meta_entry_shadow("us")
    assert result["market"] == "us" and result["live_eligible"] is False
    assert result["promotion"]["eligible"] is False
    assert seen == ["us"]
