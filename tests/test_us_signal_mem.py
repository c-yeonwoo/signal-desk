"""US 시그널 메모리 최적화 — parquet 1회 캐시 · items 캐시 · bot↔api evaluate 공유."""

from types import SimpleNamespace

import pandas as pd

from signal_desk import api, bot, store


def _seed_us(tmp_path, tickers=("AAPL", "MSFT")):
    (tmp_path / "data/cache").mkdir(parents=True)
    rows = []
    for t in tickers:
        for i, d in enumerate(("2026-07-01", "2026-07-02", "2026-07-03")):
            rows.append({"date": d, "ticker": t, "open": 100 + i, "close": 100 + i,
                         "volume": 1000 + i * 10})
    pd.DataFrame(rows).to_parquet(store.US_PRICES_FILE, index=False)
    store.clear_us_price_cache()


def test_us_price_bundle_reads_parquet_once(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _seed_us(tmp_path)
    calls = {"n": 0}
    real = store._read_parquet

    def counting(path):
        calls["n"] += 1
        return real(path)

    monkeypatch.setattr(store, "_read_parquet", counting)
    s1, q1 = store.load_us_price_bundle()
    s2 = store.load_us_price_series()
    q2 = store.load_us_quotes()
    h = store.load_us_price_history("AAPL")
    assert calls["n"] == 1  # series+quotes+history 전부 1회 읽기
    assert set(s1) == {"AAPL", "MSFT"} and s1["AAPL"][-1] == 102.0
    assert q1["AAPL"]["vol"] == 1020.0 and q2 == q1
    assert s2["AAPL"] == s1["AAPL"]
    assert h[-1] == {"date": "2026-07-03", "close": 102.0}


def test_us_price_cache_invalidates_on_rewrite(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _seed_us(tmp_path, ("AAPL",))
    assert store.load_us_price_series()["AAPL"][-1] == 102.0
    pd.DataFrame([{"date": "2026-07-04", "ticker": "AAPL", "open": 110, "close": 110, "volume": 1}]
                 ).to_parquet(store.US_PRICES_FILE, index=False)
    assert store.load_us_price_series()["AAPL"] == [110.0]


def test_us_signal_items_cached(monkeypatch):
    monkeypatch.setattr(api, "_us_signals", lambda: {})
    api._us_signal_items.cache_clear()
    a = api._us_signal_items()
    b = api._us_signal_items()
    info = api._us_signal_items.cache_info()
    assert a == b == []
    assert info.hits >= 1 and info.misses == 1


def test_clear_us_signal_caches_clears_items(monkeypatch):
    monkeypatch.setattr(api, "_us_signals", lambda: {})
    # cache_clear만 있는 stub으로 교체(원본 lru 유지 대신 clear 호출 검증)
    cleared = {"us": 0}

    class _Stub:
        def cache_clear(self):
            cleared["us"] += 1

        def __call__(self):
            return {}

    monkeypatch.setattr(api, "_us_signals", _Stub())
    api._us_signal_items.cache_clear()
    api._us_signal_items()
    assert api._us_signal_items.cache_info().currsize == 1
    api._clear_us_signal_caches()
    assert cleared["us"] == 1
    assert api._us_signal_items.cache_info().currsize == 0


def test_bot_us_signals_reuses_api(monkeypatch):
    fake = {"AAA": SimpleNamespace(ticker="AAA", score=1.5),
            "BBB": SimpleNamespace(ticker="BBB", score=2.0)}
    monkeypatch.setattr(api, "_us_signals", lambda: fake)
    out = bot.us_signals()
    assert [s.ticker for s in out] == ["BBB", "AAA"]  # 점수 내림차순
