"""거래소 일정이 페이퍼 주문 루프의 단일 시간 게이트인지 검증."""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from signal_desk import api, bot, db, market_clock

KST = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")


def test_kr_holiday_is_closed_even_on_weekday():
    # 2026 추석 연휴 중 평일. 기존 weekday-only 판정은 이때 가상 주문을 허용했다.
    holiday = dt.datetime(2026, 9, 24, 10, 0, tzinfo=KST)
    assert not market_clock.is_session("kr", holiday.date())
    assert not bot.is_market_hours(holiday)
    assert bot.is_market_hours(dt.datetime(2026, 9, 23, 10, 0, tzinfo=KST))


def test_october_2026_substitute_holiday_does_not_advance_pit_sessions():
    # 개천절 대체휴일 10/05, 한글날 10/09. 10/02 이후 여섯 번째 국내
    # 세션은 10/13이 아니라 10/14다. 휴장일을 PIT 날짜로 세면 interim을 엿본다.
    dates = [dt.date(2026, 10, day) for day in range(3, 15)]
    sessions = [date.isoformat() for date in dates if market_clock.is_session("kr", date)]
    assert sessions == ["2026-10-06", "2026-10-07", "2026-10-08",
                        "2026-10-12", "2026-10-13", "2026-10-14"]


def test_kr_auction_buffer_and_naive_time_rejected():
    assert bot.is_market_hours(dt.datetime(2026, 9, 23, 15, 19, tzinfo=KST))
    assert not bot.is_market_hours(dt.datetime(2026, 9, 23, 15, 20, tzinfo=KST))
    with pytest.raises(ValueError, match="timezone-aware"):
        market_clock.is_open("kr", dt.datetime(2026, 9, 23, 10))


def test_us_daylight_saving_and_standard_open_differ_in_kst():
    assert bot.is_us_market_hours(dt.datetime(2026, 7, 6, 22, 30, tzinfo=KST))
    assert not bot.is_us_market_hours(dt.datetime(2026, 1, 5, 22, 30, tzinfo=KST))
    assert bot.is_us_market_hours(dt.datetime(2026, 1, 5, 23, 30, tzinfo=KST))


def test_us_early_close_and_holiday():
    # 추수감사절 다음 날 NYSE 13:00 조기마감.
    assert bot.is_us_market_hours(dt.datetime(2026, 11, 27, 12, 59, tzinfo=NY))
    assert not bot.is_us_market_hours(dt.datetime(2026, 11, 27, 13, 0, tzinfo=NY))
    assert not bot.is_us_market_hours(dt.datetime(2026, 11, 26, 10, 0, tzinfo=NY))


def test_completed_us_session_uses_new_york_date_not_kst_date():
    now = dt.datetime(2026, 9, 28, 15, 40, tzinfo=KST)
    assert market_clock.latest_completed_session("us", now) == "2026-09-25"


def test_us_snapshot_only_fresh_completed_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    now = dt.datetime(2026, 9, 28, 15, 40, tzinfo=KST)
    calls = []
    monkeypatch.setattr(api.store, "us_price_last_dates", lambda: {"A": "2026-09-25", "B": "2026-09-24"})
    monkeypatch.setattr(api.store, "clear_live_quotes", lambda: calls.append("clear"))
    monkeypatch.setattr(api, "_clear_us_signal_caches", lambda: calls.append("cache"))
    monkeypatch.setattr(api, "_us_signals", lambda: {"A": _Signal("A"), "B": _Signal("B")})
    monkeypatch.setattr(api.store, "snapshot_signals", lambda sigs, date, market: (
        calls.append((date, market, [s.ticker for s in sigs])), len(sigs))[1])
    assert api._maybe_snapshot_us_signals(now) == 1
    assert calls == ["clear", "cache", ("2026-09-25", "us", ["A"])]
    assert api._maybe_snapshot_us_signals(now) == 0  # 동일 세션 중복 기록 금지


def test_us_snapshot_names_a_session_with_no_fresh_close(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    now = dt.datetime(2026, 9, 28, 15, 40, tzinfo=KST)
    monkeypatch.setattr(api.store, "us_price_last_dates", lambda: {"A": "2026-09-24"})
    assert api._maybe_snapshot_us_signals(now) == 0
    last = db.kv_get("us_signal_snapshot_last")
    assert last["saved"] == 0 and last["session"] == "2026-09-25"
    assert "2026-09-25" in last["reason"] and "종가" in last["reason"]


class _Signal:
    def __init__(self, ticker):
        self.ticker = ticker
