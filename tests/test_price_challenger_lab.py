"""Price challenger is a C-level experiment, never a live order or registered look."""

import datetime as dt

from scripts.measure import price_challenger_lab as lab


def _panel(*, missing_next_open=False):
    days = {}
    start = dt.date(2025, 1, 1)
    for i in range(100):
        date = (start + dt.timedelta(days=i)).isoformat()
        close = 100.0 if i < 75 else 94.0 if i < 85 else 80.0
        opened = 93.0 if i == 76 else close
        if missing_next_open and i == 76:
            opened = None
        days[date] = [{"ISU_CD": "005930", "TDD_OPNPRC": opened,
                       "TDD_CLSPRC": close}]
    return lab.PricePanel(days)


def _episode(panel):
    entry, end = panel.sessions[70], panel.sessions[90]
    return {"ticker": "005930", "first_buy_date": panel.sessions[69],
            "h20": {"state": "matured", "entry_date": entry,
                    "entry_open": 100.0, "exit_date": end, "exit_close": 80.0,
                    "net_pct": -20.25}}


def test_trend_uses_only_observed_closes_up_to_decision_day():
    panel = _panel()
    day = panel.sessions[70]
    before = panel.trend_features(day, "005930")
    for future in panel.sessions[71:]:
        panel.bars[future, "005930"] = (1.0, 1.0)
    panel._features.clear()
    assert panel.trend_features(day, "005930") == before
    assert panel.closes(panel.sessions[59], "005930", 61) is None


def test_risk_trigger_fills_next_open_not_signal_close():
    panel = _panel()
    result = lab._risk_exit(panel, _episode(panel))
    assert result["state"] == "triggered"
    assert result["trigger_date"] == panel.sessions[75]
    assert result["exit_date"] == panel.sessions[76]
    assert result["exit_open"] == 93.0
    assert result["net_pct"] == -7.25
    assert result["before_first_10pct_close"] is True


def test_missing_next_open_cannot_be_called_a_fill():
    panel = _panel(missing_next_open=True)
    assert lab._risk_exit(panel, _episode(panel)) == {
        "state": "trigger_without_next_open", "trigger_date": panel.sessions[75]}


def test_stale_close_on_zero_open_is_not_a_tradable_horizon():
    panel = _panel()
    day = panel.sessions[89]
    panel.bars[day, "005930"] = (0.0, 80.0)
    assert panel.closes(panel.sessions[90], "005930", 20) is None
    panel.bars[panel.sessions[90], "005930"] = (0.0, 80.0)
    assert lab._risk_exit(panel, _episode(panel))["state"] == "baseline_exit_untradeable"


def test_registered_window_is_rejected_before_scoring():
    pilot = {"signal_dates": ["2026-08-05"]}
    try:
        lab.compare_window(_panel(), pilot)
    except ValueError as exc:
        assert "registered period" in str(exc)
    else:
        raise AssertionError("protected window was scored")
