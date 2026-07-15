"""배포 환경 US 시세 증분 백필 — us_prices.parquet은 gitignore라 배포 시 비어 있으므로
갱신/백그라운드 루프가 S&P500 시세를 점진 적재해 시그널 노출을 회복해야 한다."""

from signal_desk import api


def test_backfill_picks_only_missing_and_respects_batch(monkeypatch):
    universe = [{"ticker": f"T{i}", "name": f"n{i}"} for i in range(10)]
    have = {"T0": [1.0], "T1": [1.0]}  # 이미 시세 있는 2종목은 제외돼야
    requested = {}

    def fake_fetch(tickers, days=400):
        requested["tickers"] = list(tickers)
        return len(tickers)

    monkeypatch.setattr(api.store, "load_us_universe", lambda: universe)
    monkeypatch.setattr(api.store, "load_us_price_series", lambda: have)
    monkeypatch.setattr(api.store, "fetch_us_prices", fake_fetch)

    out = api._backfill_us_prices_batch(batch=3)
    assert out["filled"] == 3                         # 배치 상한만큼만
    assert requested["tickers"] == ["T2", "T3", "T4"]  # 누락분만, 앞에서부터
    assert out["missing"] == 5                          # 10 - 2(보유) - 3(이번) = 5 남음


def test_backfill_noop_when_complete(monkeypatch):
    universe = [{"ticker": "A", "name": "a"}]
    called = {"n": 0}

    def fake_fetch(tickers, days=400):
        called["n"] += 1
        return len(tickers)

    monkeypatch.setattr(api.store, "load_us_universe", lambda: universe)
    monkeypatch.setattr(api.store, "load_us_price_series", lambda: {"A": [1.0]})
    monkeypatch.setattr(api.store, "fetch_us_prices", fake_fetch)

    out = api._backfill_us_prices_batch(batch=50)
    assert out == {"filled": 0, "missing": 0}
    assert called["n"] == 0  # 채울 게 없으면 네트워크 호출 안 함


def test_backfill_empty_universe(monkeypatch):
    monkeypatch.setattr(api.store, "load_us_universe", lambda: [])
    monkeypatch.setattr(api.store, "load_us_price_series", lambda: {})
    out = api._backfill_us_prices_batch()
    assert out == {"filled": 0, "missing": 0}
