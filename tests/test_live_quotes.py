"""장중 실시간가 오버레이 — 종가열 끝에 잠정봉 1개 append, 종가·날짜 정합, 장외 폴백."""

import pandas as pd

from signal_desk import store


def _write_prices(tmp_path):
    cache = tmp_path / "data" / "cache"
    cache.mkdir(parents=True)
    df = pd.DataFrame([
        {"ticker": "AAA", "date": "2026-07-01", "close": 100.0, "volume": 10},
        {"ticker": "AAA", "date": "2026-07-02", "close": 110.0, "volume": 12},
        {"ticker": "BBB", "date": "2026-07-01", "close": 50.0, "volume": 5},
        {"ticker": "BBB", "date": "2026-07-02", "close": 55.0, "volume": 6},
    ])
    df.to_parquet(cache / "prices.parquet")


def test_overlay_appends_provisional_bar(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_prices(tmp_path)
    base = store.load_price_series()
    assert base["AAA"] == [100.0, 110.0]

    store.set_live_quotes({"AAA": 121.0})  # 장중 현재가
    try:
        s = store.load_price_series()
        assert s["AAA"] == [100.0, 110.0, 121.0]      # 잠정봉 append
        assert s["BBB"] == [50.0, 55.0]               # 라이브 없는 종목은 그대로
        # 날짜열도 +1로 정합(백테스트 date-close 짝 유지)
        d = store.load_dates_by_ticker()
        assert len(d["AAA"]) == len(s["AAA"]) and len(d["BBB"]) == len(s["BBB"])
        # 현재가 표시: price=live, 전일=마지막 종가
        q = store.load_quotes()
        assert q["AAA"]["price"] == 121.0 and q["AAA"]["prev_close"] == 110.0
        assert round(q["AAA"]["change_pct"], 2) == 10.0
    finally:
        store.clear_live_quotes()

    assert store.load_price_series()["AAA"] == [100.0, 110.0]  # 해제 시 종가 복귀


def test_quote_failure_falls_back_to_close(tmp_path, monkeypatch):
    """시세 조회가 실패했는데 오버레이를 남기면 낡은 장중가가 계속 시그널·체결가로 쓰인다.
    '오래된 종가'는 정직한 상태지만 '고정된 장중가'는 조용한 거짓말이다."""
    monkeypatch.chdir(tmp_path)
    _write_prices(tmp_path)
    from signal_desk import api
    from signal_desk.ingest import toss

    monkeypatch.setattr(toss, "available", lambda: True)
    monkeypatch.setattr(api.store, "load_universe", lambda: [{"ticker": "AAA"}])
    store.set_live_quotes({"AAA": 121.0})
    try:
        monkeypatch.setattr(toss, "prices", lambda syms: (_ for _ in ()).throw(RuntimeError("429")))
        api._refresh_live_quotes(["kr"])
        assert store.load_price_series()["AAA"] == [100.0, 110.0]   # 종가로 복귀

        store.set_live_quotes({"AAA": 121.0})
        monkeypatch.setattr(toss, "prices", lambda syms: {})        # 빈 응답도 같다
        api._refresh_live_quotes(["kr"])
        assert store.load_price_series()["AAA"] == [100.0, 110.0]
    finally:
        store.clear_live_quotes()


def test_pit_snapshot_is_taken_on_closes(tmp_path, monkeypatch):
    """PIT 점수는 종가 기준이어야 한다 — 채점(accuracy)이 종가로 하는데 스냅샷이 장중가면
    같은 날짜에 두 기준이 섞여 실측이 오염된다."""
    monkeypatch.chdir(tmp_path)
    _write_prices(tmp_path)
    from signal_desk import api

    seen: list[list[float]] = []
    dates: list[str] = []
    monkeypatch.setattr(api, "kb", type("_", (), {"refresh": staticmethod(lambda t: None)}))
    monkeypatch.setattr(api, "_kb_targets", lambda: [])
    monkeypatch.setattr(api, "_regime", type("_", (), {"cache_clear": staticmethod(lambda: None)}))
    monkeypatch.setattr(api, "_signals", type("_", (), {"cache_clear": staticmethod(lambda: None),
                                                       "__call__": staticmethod(lambda: [])})())
    monkeypatch.setattr(api.store, "prices_need_deep_backfill", lambda: False)
    monkeypatch.setattr(api, "_refresh_us_prices_stale", lambda batch=0: {"filled": 0, "stale": 0})
    monkeypatch.setattr(api, "_clear_us_signal_caches", lambda: None)
    for name in ("fetch_prices", "fetch_flows", "fetch_market_flow", "fetch_short",
                 "fetch_consensus", "fetch_warnings", "load_universe", "us_price_deferred_tickers"):
        monkeypatch.setattr(api.store, name, lambda *a, **k: [])
    monkeypatch.setattr(api.climate, "snapshot_shadow", lambda s: None)
    monkeypatch.setattr(api.db, "kv_set", lambda k, v: None)
    # 스냅샷이 불리는 순간의 가격열 — 잠정봉이 남아 있으면 장중가 점수가 저장된다는 뜻
    monkeypatch.setattr(api.store, "snapshot_signals",
                        lambda s, date=None: (seen.append(store.load_price_series()["AAA"]),
                                              dates.append(date), 0)[-1])

    store.set_live_quotes({"AAA": 121.0})
    try:
        api._daily_maintenance([])
    finally:
        store.clear_live_quotes()
    assert seen == [[100.0, 110.0]]                 # 스냅샷 직전 오버레이가 걷혔다
    assert dates == [api._kst_today()]              # 거래일은 KST 기준


def test_overlay_ignores_bad_values(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_prices(tmp_path)
    store.set_live_quotes({"AAA": 0, "BBB": None, "CCC": "x"})  # 양수만 반영
    try:
        s = store.load_price_series()
        assert s["AAA"] == [100.0, 110.0] and s["BBB"] == [50.0, 55.0]  # 무효값 → 오버레이 없음
    finally:
        store.clear_live_quotes()
