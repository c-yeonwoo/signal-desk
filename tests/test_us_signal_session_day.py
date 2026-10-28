"""미국 시그널 표시는 한국 자정이 아닌 거래소 세션을 기준으로 한다."""

import datetime as dt
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd

from signal_desk import api, db, store


def _episode_price_fields(monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    monkeypatch.setattr(api.market_clock, "regular_window", lambda market, at:
                        (at - dt.timedelta(hours=1), at + dt.timedelta(hours=1)))
    return {"signal_close_date": api.market_clock.latest_completed_session("us", now),
            "signal_price_evidence": {"price_basis": "intraday_provisional",
                "price_session": now.astimezone(ZoneInfo("America/New_York")).date().isoformat(),
                "price_observation_id": "test", "price_received_at": now.timestamp(),
                "price_source_timestamp": now.isoformat()}}


def test_us_session_day_stays_friday_after_kst_midnight():
    during_friday = dt.datetime(2026, 10, 16, 15, 30, tzinfo=dt.timezone.utc)
    after_friday = dt.datetime(2026, 10, 17, 9, 0, tzinfo=dt.timezone.utc)
    assert api._signal_session_day("us", during_friday) == "2026-10-16"
    assert api._signal_session_day("us", after_friday) == "2026-10-16"
    assert api._signal_session_day("kospi", after_friday) == "2026-10-16"


def test_us_entry_uses_us_pit_and_session_day(monkeypatch):
    seen = []

    def history(market):
        seen.append(market)
        return pd.DataFrame([
            {"ticker": "AAPL", "date": "2026-10-15", "kind": "BUY"},
        ]) if market == "us" else pd.DataFrame()

    monkeypatch.setattr(store, "load_signal_history", history)
    monkeypatch.setattr(store, "load_us_price_series", lambda: {"AAPL": [100.0, 105.0]})
    monkeypatch.setattr(store, "load_us_dates_by_ticker",
                        lambda: {"AAPL": ["2026-10-15", "2026-10-16"]})
    monkeypatch.setattr(api, "_signal_session_day", lambda market: "2026-10-16")
    rows = [{"ticker": "AAPL", "kind": "BUY", "price": 105.0}]
    api._annotate_entry(rows, market="us")
    assert seen == ["us"]
    assert rows[0]["entry"]["fire_date"] == "2026-10-15"
    assert rows[0]["entry"]["age_days"] == 1


def test_us_entry_does_not_invent_first_fire_without_pit(monkeypatch):
    monkeypatch.setattr(store, "load_signal_history", lambda market: pd.DataFrame())
    monkeypatch.setattr(store, "load_us_price_series", lambda: {"AAPL": [105.0]})
    monkeypatch.setattr(store, "load_us_dates_by_ticker", lambda: {"AAPL": ["2026-10-16"]})
    monkeypatch.setattr(api, "_signal_session_day", lambda market: "2026-10-16")
    rows = [{"ticker": "AAPL", "kind": "BUY", "price": 105.0}]
    api._annotate_entry(rows, market="us")
    assert rows[0]["entry"] is None


def test_episode_only_records_open_market_in_exchange_date(monkeypatch):
    seen = []
    monkeypatch.setattr(api.market_clock, "is_open", lambda market, at=None: market == "us")
    monkeypatch.setattr(api, "_signal_session_day", lambda market: "2026-10-16")
    monkeypatch.setattr(store, "load_us_quotes", lambda: (_ for _ in ()).throw(
        AssertionError("발동가는 판정과 다른 시세를 재조회하면 안 됩니다")))
    monkeypatch.setattr(api.episode_state, "observe_rows",
                        lambda rows, **kwargs: seen.append((rows, kwargs)))
    monkeypatch.setattr(api, "_decision_payload", lambda result: {"buy_blocked": False})
    monkeypatch.setattr(api, "_hold_tag", lambda result, **kwargs: None)
    result = SimpleNamespace(ticker="AAPL", kind="BUY", reasons=[],
                             signal_price_basis="intraday_quote", signal_input_price=105.0,
                             **_episode_price_fields(monkeypatch))
    api._sync_episode_state([result], market="us")
    assert seen[0][1] == {"market": "us", "today": "2026-10-16"}
    assert seen[0][0][0]["price"] == 105.0

    seen.clear()
    monkeypatch.setattr(api.market_clock, "is_open", lambda market: False)
    api._sync_episode_state([result], market="us")
    assert seen == []


def test_episode_does_not_invent_intraday_fire_from_previous_close(monkeypatch):
    seen = []
    monkeypatch.setattr(api.market_clock, "is_open", lambda market, at=None: True)
    monkeypatch.setattr(api.episode_state, "observe_rows",
                        lambda rows, **kwargs: seen.append(rows))
    monkeypatch.setattr(api, "_decision_payload", lambda result: {"buy_blocked": False})
    monkeypatch.setattr(api, "_hold_tag", lambda result, **kwargs: None)
    rows = [SimpleNamespace(ticker="AAPL", kind="BUY", reasons=[],
                            signal_price_basis="stored_close", signal_input_price=100.0),
            SimpleNamespace(ticker="MSFT", kind="BUY", reasons=[],
                            signal_price_basis="intraday_quote", signal_input_price=200.0,
                            **_episode_price_fields(monkeypatch)),
            SimpleNamespace(ticker="NVDA", kind="BUY", reasons=[],
                            signal_price_basis="intraday_quote", signal_input_price=200.0)]
    monkeypatch.setattr(api, "_signal_session_day", lambda market: "2026-10-16")

    api._sync_episode_state(rows, market="us")

    assert [[row["ticker"] for row in batch] for batch in seen] == [["MSFT"]]
    assert seen[0][0]["price"] == 200.0


def test_us_priced_in_ages_event_by_us_session_not_kst(monkeypatch):
    dates = ["2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06",
             "2026-10-07", "2026-10-08", "2026-10-16"]
    closes = [100.0, 102.0, 104.0, 106.0, 108.0, 112.0, 115.0]
    monkeypatch.setattr(api, "_signal_session_day", lambda market: "2026-10-16")
    monkeypatch.setattr(api, "_kst_today", lambda: "2026-10-17")
    monkeypatch.setattr(store, "load_us_price_series", lambda: {"AAPL": closes})
    monkeypatch.setattr(store, "load_us_dates_by_ticker", lambda: {"AAPL": dates})
    monkeypatch.setattr(db, "kb_events_active", lambda: [{
        "ticker": "AAPL", "direction": "positive", "effective_at": "2026-10-09",
        "summary": "공식 발표",
    }])
    rows = [{"ticker": "AAPL"}]
    api._annotate_priced_in(rows, market="us")
    assert rows[0]["priced_in"]["event_date"] == "2026-10-09"
