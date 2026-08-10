"""배포 환경 US 시세 증분 백필 — us_prices.parquet은 gitignore라 배포 시 비어 있으므로
갱신/백그라운드 루프가 S&P500 시세를 점진 적재해 시그널 노출을 회복해야 한다.

누락 백필과 별개로, 이미 채워진 종목의 stale 일봉도 주기적으로 다시 당겨야 한다
(실측: 백필 no-op만 돌면 499종목이 7/2에 고정)."""

import datetime

import pandas as pd

from signal_desk import api, store


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
    # `shallow` = 봉이 있지만 252거래일(모멘텀 요건) 미만인 종목 수 — 뒤처짐과 다른 결함이다.
    assert out == {"filled": 0, "missing": 0, "deferred": 0, "shallow": 0}
    assert called["n"] == 0  # 채울 게 없으면 네트워크 호출 안 함


def test_backfill_empty_universe(monkeypatch):
    monkeypatch.setattr(api.store, "load_us_universe", lambda: [])
    monkeypatch.setattr(api.store, "load_us_price_series", lambda: {})
    out = api._backfill_us_prices_batch()
    assert out == {"filled": 0, "missing": 0, "deferred": 0}


def test_stale_refresh_skips_fresh_and_respects_batch(monkeypatch):
    """`us_prices_stale_tickers` 를 **실제 함수로** 돌린다 — 가짜로 대체하면 판정 규약이
    검사에서 빠지고, 실제로 그 틈으로 달력일 버그가 살아 있었다(2026-08-07 실측).

    기대 마지막 봉을 금요일(2026-08-07)로 고정하고 거래일 갭으로 판정한다.
    """
    universe = [{"ticker": t} for t in ("AAPL", "MSFT", "GOOG", "AMZN")]
    last = {"AAPL": "2026-08-07",   # 갭 0 — 신선
            "MSFT": "2026-07-24",   # 갭 10거래일 — stale
            "GOOG": "2026-08-04",   # 갭 3거래일(05·06·07) — stale
            "AMZN": "2026-08-06"}   # 갭 1거래일 — 공휴일 여유 안쪽이라 신선
    requested: list[list[str]] = []

    monkeypatch.setattr(api.store, "load_us_universe", lambda: universe)
    monkeypatch.setattr(api.store, "us_price_last_dates", lambda: last)
    monkeypatch.setattr(api.store, "us_expected_last_bar", lambda as_of=None: "2026-08-07")
    monkeypatch.setattr(api.store, "us_price_skips", lambda: {})
    monkeypatch.setattr(api.store, "us_price_deferred", lambda t, skip=None: False)
    monkeypatch.setattr(api.store, "fetch_us_prices",
                        lambda ts, days=60: (requested.append(list(ts)), len(ts))[1])

    out = api._refresh_us_prices_stale(batch=1)
    assert requested == [["MSFT"]]          # stale 중 앞에서 1개만
    assert out == {"filled": 1, "stale": 1}  # GOOG 남음


def test_stale_refresh_noop_when_fresh(monkeypatch):
    monkeypatch.setattr(api.store, "load_us_universe", lambda: [{"ticker": "AAPL"}])
    monkeypatch.setattr(api.store, "us_prices_stale_tickers", lambda *a, **k: [])
    monkeypatch.setattr(api.store, "us_price_skips", lambda: {})
    called = {"n": 0}
    monkeypatch.setattr(api.store, "fetch_us_prices",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or 0)
    assert api._refresh_us_prices_stale(batch=50) == {"filled": 0, "stale": 0}
    assert called["n"] == 0


def test_fetch_us_prices_short_window_keeps_old_history(tmp_path, monkeypatch):
    """짧은 days 갱신이 과거 일봉을 지우면 안 된다 — 예전엔 요청 종목 행을 통째로 교체했다."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data/cache").mkdir(parents=True)
    old = [{"date": "2025-01-02", "ticker": "AAPL", "open": 1.0, "close": 1.0, "volume": 1},
           {"date": "2025-06-01", "ticker": "AAPL", "open": 2.0, "close": 2.0, "volume": 1},
           {"date": "2025-06-01", "ticker": "MSFT", "open": 9.0, "close": 9.0, "volume": 1}]
    pd.DataFrame(old).to_parquet(store.US_PRICES_FILE, index=False)

    def fake_ohlcv(sym, count=200):
        if sym != "AAPL":
            return []
        return [{"date": "2026-07-31", "open": 3.0, "close": 3.0, "volume": 2}]

    monkeypatch.setattr(store, "_symbol_candidates", lambda t, r: [t])
    from signal_desk.ingest import toss
    monkeypatch.setattr(toss, "available", lambda: True)
    monkeypatch.setattr(toss, "daily_ohlcv", fake_ohlcv)

    assert store.fetch_us_prices(["AAPL"], days=5) == 1
    series = store.load_us_price_series()
    assert series["AAPL"] == [1.0, 2.0, 3.0]   # 과거 2봉 + 신규
    assert series["MSFT"] == [9.0]             # 다른 종목 보존
