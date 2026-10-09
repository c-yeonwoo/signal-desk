"""Research and portfolio consumers must not reopen a verified KRX closure."""

import datetime as dt

import exchange_calendars as xcals
import pandas as pd

from signal_desk import market_clock
from signal_desk.signals import portfolio_audit, revision_price


def _calendar_with_false_october_session(monkeypatch):
    original = xcals.get_calendar("XKRX")
    fake_day = pd.Timestamp("2026-10-05")
    fake_row = pd.DataFrame({
        "open": [pd.Timestamp("2026-10-05T00:00:00Z")],
        "close": [pd.Timestamp("2026-10-05T06:30:00Z")],
    }, index=pd.DatetimeIndex([fake_day]))

    class WrongCalendar:
        schedule = pd.concat([original.schedule, fake_row]).sort_index()
        first_session = original.first_session
        last_session = original.last_session

        def is_session(self, day):
            return pd.Timestamp(day) == fake_day or original.is_session(day)

        def previous_session(self, day):
            if pd.Timestamp(day) == pd.Timestamp("2026-10-06"):
                return fake_day
            if pd.Timestamp(day) == fake_day:
                return pd.Timestamp("2026-10-02")
            return original.previous_session(day)

    wrong = WrongCalendar()
    monkeypatch.setattr(market_clock, "_calendar", lambda market: wrong)
    monkeypatch.setattr(portfolio_audit.xcals, "get_calendar", lambda name: wrong)
    assert wrong.is_session("2026-10-05")
    assert not market_clock.is_session("kr", "2026-10-05")


def test_portfolio_clock_skips_false_holiday_in_completed_and_future_sessions(monkeypatch):
    _calendar_with_false_october_session(monkeypatch)
    on_holiday = portfolio_audit.clock_context(
        "kr", dt.datetime(2026, 10, 5, 7, tzinfo=dt.timezone.utc))
    assert on_holiday["ready"]
    assert on_holiday["expected_price_session"] == "2026-10-02"
    assert on_holiday["evaluation_sessions"][0]["date"] == "2026-10-06"
    assert "2026-10-05" not in {item["date"] for item in on_holiday["evaluation_sessions"]}

    before_holiday = portfolio_audit.clock_context(
        "kr", dt.datetime(2026, 10, 2, 7, tzinfo=dt.timezone.utc))
    assert before_holiday["evaluation_sessions"][0]["date"] == "2026-10-06"


def test_revision_price_window_skips_false_holiday(monkeypatch):
    _calendar_with_false_october_session(monkeypatch)
    session = "2026-10-08"
    expected = ["2026-09-30", "2026-10-01", "2026-10-02",
                "2026-10-06", "2026-10-07", session]
    rows, dates_by, closes_by, sectors = [], {}, {}, {}
    for index in range(1, 13):
        ticker = f"{index:06d}"
        rows.extend([
            {"date": "2026-10-02", "ticker": ticker, "fwd1_year": "202712",
             "fwd1_eps": 100.0, "observed_at": "2026-10-02T08:00:00+00:00"},
            {"date": session, "ticker": ticker, "fwd1_year": "202712",
             "fwd1_eps": 100.0 + index, "observed_at": "2026-10-08T07:00:00+00:00"},
        ])
        dates_by[ticker] = expected
        closes_by[ticker] = [100.0] * 5 + [100.0 + index]
        sectors[ticker] = "same-sector"
    result = revision_price.build(
        observations=pd.DataFrame(rows),
        as_of=dt.datetime(2026, 10, 8, 8, tzinfo=dt.timezone.utc),
        dates_by=dates_by, closes_by=closes_by, sector_by=sectors)
    assert result["research_ready"]
    assert result["price_session"] == session
    assert result["eligible_count"] == 12
