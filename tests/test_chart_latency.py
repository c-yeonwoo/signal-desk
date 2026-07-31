"""차트 클릭 경로 지연 — cold evaluate·parquet 재파싱이 클릭마다 붙으면 안 된다."""

import pandas as pd

from signal_desk import api, store


def test_anchor_skips_cold_signal_evaluate(monkeypatch):
    """시그널 캐시가 비어 있으면 차트 앵커가 전 종목 evaluate를 돌리지 않는다."""
    api._signals.cache_clear()
    api._us_signals.cache_clear()
    called = {"n": 0}

    def boom():
        called["n"] += 1
        raise AssertionError("cold evaluate must not run on chart path")

    boom.cache_info = lambda: type("I", (), {"currsize": 0})()
    monkeypatch.setattr(api, "_signals", boom)
    out = api._anchor_today_score([0.1, 0.2], "005930", "kospi")
    assert out == [0.1, 0.2]
    assert called["n"] == 0


def test_anchor_uses_warm_cache(monkeypatch):
    api._signals.cache_clear()
    sig = type("S", (), {"ticker": "005930", "score": 1.2345})()

    def warm():
        return [sig]

    warm.cache_info = lambda: type("I", (), {"currsize": 1})()
    monkeypatch.setattr(api, "_signals", warm)
    out = api._anchor_today_score([0.1, 0.2], "005930", "kospi")
    assert out[-1] == 1.2345


def test_kr_price_history_uses_mtime_cache(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data/cache").mkdir(parents=True)
    store.clear_kr_price_cache()
    rows = [{"date": f"2026-01-{i:02d}", "ticker": "005930",
             "open": 100.0, "close": float(100 + i), "volume": 1}
            for i in range(1, 11)]
    rows += [{"date": "2026-01-01", "ticker": "000660",
              "open": 1.0, "close": 1.0, "volume": 1}]
    pd.DataFrame(rows).to_parquet(store.PRICES_FILE, index=False)

    h1 = store.load_price_history("005930")
    assert len(h1) == 10 and h1[-1]["close"] == 110.0
    reads = {"n": 0}
    real = store._read_parquet

    def counted(path):
        reads["n"] += 1
        return real(path)

    monkeypatch.setattr(store, "_read_parquet", counted)
    h2 = store.load_price_history("005930")
    assert h2 == h1 and reads["n"] == 0

    rows.append({"date": "2026-01-11", "ticker": "005930",
                 "open": 111.0, "close": 111.0, "volume": 1})
    pd.DataFrame(rows).to_parquet(store.PRICES_FILE, index=False)
    store.clear_kr_price_cache()
    h3 = store.load_price_history("005930")
    assert h3[-1]["close"] == 111.0 and reads["n"] == 1


def test_chart_response_omits_quote_lookup(monkeypatch):
    """차트 클릭이 _quotes() cold 파싱을 기다리지 않는다."""
    history = [{"date": f"2026-01-{i:02d}", "close": 100.0 + i} for i in range(1, 26)]
    monkeypatch.setattr(api.store, "load_price_history", lambda ticker: history)
    monkeypatch.setattr(api.store, "signal_history_for", lambda ticker: {})
    called = {"n": 0}

    def boom():
        called["n"] += 1
        return {}

    monkeypatch.setattr(api, "_quotes", boom)
    monkeypatch.setattr(api, "_anchor_today_score", lambda scores, t, m: scores)
    d = api.signal_chart_get("005930", market="kospi", flow=False)
    assert d["ready"] is True
    assert "quote" not in d
    assert called["n"] == 0
