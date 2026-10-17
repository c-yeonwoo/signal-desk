"""저장된 종가 차트에 현재 장중 판정을 과거 날짜로 덮어쓰지 않는다."""

from types import SimpleNamespace

from signal_desk import api


def _history():
    return [{"date": f"2026-01-{i:02d}", "close": 100.0 + i} for i in range(1, 26)]


def test_chart_keeps_recorded_score_without_reading_current_signal(monkeypatch):
    history = _history()
    monkeypatch.setattr(api.store, "load_price_history", lambda ticker: history)
    monkeypatch.setattr(api.store, "signal_history_for", lambda ticker: {
        history[-1]["date"]: {"kind": "BUY", "score": 1.87}})
    monkeypatch.setattr(api, "_signals", lambda: (_ for _ in ()).throw(
        AssertionError("현재 장중 판정을 종가 차트에 얹으면 안 된다")))

    result = api.signal_chart_get("005930", market="kospi")

    assert result["scores"][-1] == 1.87
    assert result["actual_dates"] == [history[-1]["date"]]
    assert result["close"][-1] == history[-1]["close"]


def test_us_chart_does_not_read_current_live_signal(monkeypatch):
    history = _history()
    monkeypatch.setattr(api.store, "load_us_price_history", lambda ticker: history)
    monkeypatch.setattr(api, "_us_signals", lambda: (_ for _ in ()).throw(
        AssertionError("현재 장중 판정을 종가 차트에 얹으면 안 된다")))

    result = api.signal_chart_get("AAPL", market="us")

    assert result["ready"] is True
    assert result["actual_dates"] == []
    assert len(result["scores"]) == len(history)


def test_signal_price_context_uses_captured_bundle_not_newer_quote():
    intraday = SimpleNamespace(ticker="AAA")
    close_only = SimpleNamespace(ticker="BBB")
    api._stamp_signal_price_context(
        [intraday, close_only],
        {"AAA": [100.0, 110.0, 121.0], "BBB": [50.0, 55.0]},
        {"AAA": ["2026-10-05", "2026-10-06"], "BBB": ["2026-10-05", "2026-10-06"]},
        {"quote_updated": {"AAA": 1791259200.0, "BBB": 1791259260.0}},
    )

    assert intraday.signal_price_basis == "intraday_quote"
    assert intraday.signal_quote_received_at == 1791259200.0
    assert intraday.signal_close_date == "2026-10-06"
    assert api._signal_display_price(intraday, 130.0, 18.0) == (121.0, 10.0)
    assert close_only.signal_price_basis == "stored_close"
    assert close_only.signal_quote_received_at is None
    assert api._signal_display_price(close_only, 60.0, 9.0) == (55.0, 10.0)
    assert intraday.computed_at == close_only.computed_at
