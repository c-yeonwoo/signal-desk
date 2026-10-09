"""C-level replay must not leak later prices into a past cross-sectional decision."""

import exchange_calendars as xcals
import pytest

from signal_desk.signals.historical_replay import replay_price_only_asof


def _panel():
    calendar = xcals.get_calendar("XKRX")
    dates = [day.date().isoformat() for day in
             calendar.sessions_in_range("2025-01-02", "2026-10-08")][:266]
    assert len(dates) == 266
    as_of = dates[259]
    universe = [{"ticker": ticker, "name": ticker} for ticker in ("AAA", "BBB", "CCC")]
    closes = {ticker: [100.0 + (i * scale) for i in range(len(dates))]
              for ticker, scale in (("AAA", 0.12), ("BBB", -0.08), ("CCC", 0.04))}
    dates_by = {ticker: list(dates) for ticker in closes}
    return as_of, universe, closes, dates_by


def test_future_price_mutation_cannot_change_past_engine_output():
    as_of, universe, closes, dates = _panel()
    first = replay_price_only_asof(market="kr", as_of=as_of, universe=universe,
                                   closes_by=closes, dates_by=dates)
    changed = {ticker: list(series) for ticker, series in closes.items()}
    for ticker in changed:
        changed[ticker][260:] = [value * 10 for value in changed[ticker][260:]]
    second = replay_price_only_asof(market="kr", as_of=as_of, universe=universe,
                                    closes_by=changed, dates_by=dates)
    assert first["rows"] == second["rows"]
    assert first["frozen_universe_size"] == first["priced_tickers"] == 3
    assert first["future_bars_removed"] == 3 * 6
    assert first["strict_pit_eligible"] is False
    assert first["live_eligible"] is False
    assert len(first["rows"]) == 3


def test_stale_and_short_history_remain_explicitly_excluded():
    as_of, universe, closes, dates = _panel()
    dates["BBB"].pop(259)
    closes["BBB"].pop(259)
    dates["CCC"] = dates["CCC"][100:]
    closes["CCC"] = closes["CCC"][100:]
    result = replay_price_only_asof(market="kr", as_of=as_of, universe=universe,
                                    closes_by=closes, dates_by=dates)
    assert result["frozen_universe_size"] == 3
    assert result["priced_tickers"] == 1
    assert result["excluded_tickers"] == {
        "BBB": "missing_decision_close", "CCC": "insufficient_history"}
    assert [row["ticker"] for row in result["rows"]] == ["AAA"]


def test_undated_duplicate_or_non_session_price_is_rejected():
    as_of, universe, closes, dates = _panel()
    dates["AAA"].append("2026-10-09")  # holiday and unmatched tail
    with pytest.raises(ValueError, match="undated or mismatched"):
        replay_price_only_asof(market="kr", as_of=as_of, universe=universe,
                               closes_by=closes, dates_by=dates)
    closes["AAA"].append(999.0)
    with pytest.raises(ValueError, match="invalid price sessions"):
        replay_price_only_asof(market="kr", as_of=as_of, universe=universe,
                               closes_by=closes, dates_by=dates)
    dates["AAA"][-1] = dates["AAA"][-2]
    with pytest.raises(ValueError, match="invalid price sessions"):
        replay_price_only_asof(market="kr", as_of=as_of, universe=universe,
                               closes_by=closes, dates_by=dates)
