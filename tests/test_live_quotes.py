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


def test_overlay_ignores_bad_values(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_prices(tmp_path)
    store.set_live_quotes({"AAA": 0, "BBB": None, "CCC": "x"})  # 양수만 반영
    try:
        s = store.load_price_series()
        assert s["AAA"] == [100.0, 110.0] and s["BBB"] == [50.0, 55.0]  # 무효값 → 오버레이 없음
    finally:
        store.clear_live_quotes()
