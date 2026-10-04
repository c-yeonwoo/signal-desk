"""Failed close-price refresh must produce one actionable operator Telegram alert."""

import datetime as dt
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from signal_desk import api, store


KST = ZoneInfo("Asia/Seoul")


def _capture(monkeypatch):
    queued = []
    drained = []
    monkeypatch.setattr(api.notify, "enqueue", lambda text, **kwargs: queued.append((text, kwargs)) or True)
    monkeypatch.setattr(api.notify, "drain", lambda **kwargs: drained.append(kwargs) or {"sent": 1})
    return queued, drained


def test_kr_alert_uses_actual_latest_bars_after_close(monkeypatch):
    queued, drained = _capture(monkeypatch)
    now = dt.datetime(2026, 10, 6, 15, 41, tzinfo=KST)
    monkeypatch.setattr(api.market_clock, "latest_completed_session", lambda market, at: "2026-10-06")
    monkeypatch.setattr(api.store, "load_universe", lambda: [{"ticker": "005930"}, {"ticker": "000660"}])
    monkeypatch.setattr(api.store, "kr_price_last_dates",
                        lambda: {"005930": "2026-10-06", "000660": "2026-10-02"})

    assert api._check_kr_price_refresh(now, reason="재시도 후 최신 종가 없음") is True
    message, opts = queued[0]
    assert "2026-10-06" in message and "000660(2026-10-02)" in message
    assert "005930(" not in message
    assert opts["priority"] == "critical"
    assert opts["dedupe_key"] == "price-stale:kr:2026-10-06"
    assert len(drained) == 1


def test_kr_alert_skips_holiday_and_recovered_bars(monkeypatch):
    queued, _ = _capture(monkeypatch)
    now = dt.datetime(2026, 10, 5, 16, tzinfo=KST)
    monkeypatch.setattr(api.market_clock, "latest_completed_session", lambda market, at: "2026-10-02")
    monkeypatch.setattr(api.store, "load_universe", lambda: [{"ticker": "005930"}])
    monkeypatch.setattr(api.store, "kr_price_last_dates", lambda: {"005930": "2026-10-02"})
    assert api._check_kr_price_refresh(now, reason="ProviderError") is False
    monkeypatch.setattr(api.market_clock, "latest_completed_session", lambda market, at: "2026-10-06")
    now = dt.datetime(2026, 10, 6, 16, tzinfo=KST)
    monkeypatch.setattr(api.store, "kr_price_last_dates", lambda: {"005930": "2026-10-06"})
    assert api._check_kr_price_refresh(now, reason="재시도 후 최신 종가 없음") is False
    assert queued == []


def test_us_alert_only_names_retried_and_still_stale(monkeypatch):
    queued, drained = _capture(monkeypatch)
    now = dt.datetime(2026, 10, 6, 16, tzinfo=KST)
    monkeypatch.setattr(api.store, "us_prices_stale_tickers",
                        lambda tickers, **kwargs: [ticker for ticker in tickers if ticker == "MSFT"])
    monkeypatch.setattr(api.store, "us_price_last_dates",
                        lambda: {"AAPL": "2026-10-05", "MSFT": "2026-10-02", "GOOG": "2026-10-02"})
    monkeypatch.setattr(api.market_clock, "latest_completed_session", lambda market, at: "2026-10-05")

    assert api._check_us_price_refresh(now, ["AAPL", "MSFT"], max_trading_days=0,
                                       reason="재시도 후 최신 종가 없음") is True
    assert "MSFT(2026-10-02)" in queued[0][0]
    assert "GOOG" not in queued[0][0]  # not part of this retry batch
    assert queued[0][1]["dedupe_key"] == "price-stale:us:2026-10-05"
    assert len(drained) == 1


def test_us_refresh_checks_failed_attempt_immediately(monkeypatch):
    checked = []
    monkeypatch.setattr(api, "_us_refresh_tickers", lambda: ["AAPL"])
    monkeypatch.setattr(api.store, "us_price_skips", lambda: {})
    monkeypatch.setattr(api.store, "us_price_deferred", lambda ticker, skips: False)
    monkeypatch.setattr(api.store, "us_prices_stale_tickers", lambda tickers, **kwargs: list(tickers))
    monkeypatch.setattr(api.store, "us_price_gap_depth", lambda tickers: 3)
    monkeypatch.setattr(api.store, "fetch_us_prices", lambda tickers, days: 0)
    monkeypatch.setattr(api, "_check_us_price_refresh",
                        lambda now, targets, **kwargs: checked.append((targets, kwargs)))

    assert api._refresh_us_prices_stale(batch=1, max_trading_days=0) == {"filled": 0, "stale": 0}
    assert checked == [(["AAPL"], {"max_trading_days": 0, "reason": "재시도 후 최신 종가 없음"})]


def test_kr_last_bar_read_ignores_parquet_mtime_and_live_quotes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store.PRICES_FILE.parent.mkdir(parents=True)
    pd.DataFrame([{"ticker": "005930", "date": "2026-10-06", "close": 0},
                  {"ticker": "005930", "date": "2026-10-02", "close": 100},
                  {"ticker": "005930", "date": "2026-09-30", "close": 99}]).to_parquet(store.PRICES_FILE)
    assert store.kr_price_last_dates() == {"005930": "2026-10-02"}


def test_us_refresh_alerts_even_when_provider_returns_old_bars(monkeypatch):
    """A provider can report one successful ticker while returning no newer bar."""
    checked = []
    monkeypatch.setattr(api, "_us_refresh_tickers", lambda: ["AAPL"])
    monkeypatch.setattr(api.store, "us_price_skips", lambda: {})
    monkeypatch.setattr(api.store, "us_price_deferred", lambda ticker, skips: False)
    monkeypatch.setattr(api.store, "us_prices_stale_tickers", lambda tickers, **kwargs: list(tickers))
    monkeypatch.setattr(api.store, "us_price_gap_depth", lambda tickers: 3)
    monkeypatch.setattr(api.store, "fetch_us_prices", lambda tickers, days: 1)
    monkeypatch.setattr(api, "_check_us_price_refresh",
                        lambda now, targets, **kwargs: checked.append(targets))
    assert api._refresh_us_prices_stale(batch=1, max_trading_days=0)["filled"] == 1
    assert checked == [["AAPL"]]


def test_us_refresh_exception_alerts_and_preserves_failure(monkeypatch):
    checked = []
    monkeypatch.setattr(api, "_us_refresh_tickers", lambda: ["AAPL"])
    monkeypatch.setattr(api.store, "us_price_skips", lambda: {})
    monkeypatch.setattr(api.store, "us_price_deferred", lambda ticker, skips: False)
    monkeypatch.setattr(api.store, "us_prices_stale_tickers", lambda tickers, **kwargs: list(tickers))
    monkeypatch.setattr(api.store, "us_price_gap_depth", lambda tickers: 3)

    def unavailable(tickers, days):
        raise OSError("provider unavailable")

    monkeypatch.setattr(api.store, "fetch_us_prices", unavailable)
    monkeypatch.setattr(api, "_check_us_price_refresh",
                        lambda now, targets, **kwargs: checked.append(kwargs["reason"]))
    with pytest.raises(OSError, match="provider unavailable"):
        api._refresh_us_prices_stale(batch=1, max_trading_days=0)
    assert checked == ["OSError"]
