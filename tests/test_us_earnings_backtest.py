"""US EPS reconstruction invariants, not assertions of investment profitability."""

import pytest

from scripts.measure.us_earnings_backtest import adoption, eps_events, execution_day, simulate


def _body(actual="1.1", expected="1", surprise="10"):
    return {"symbol": "AAPL", "quarterlyEarnings": [{"reportedDate": "2025-01-30", "fiscalDateEnding": "2024-12-31",
             "reportedEPS": actual, "estimatedEPS": expected, "surprisePercentage": surprise, "reportTime": "post-market"}]}


def test_eps_joins_same_quarter_and_does_not_read_future_protected_features():
    body = _body()
    body["quarterlyEarnings"].append({"reportedDate": "2026-10-30", "reportedEPS": object()})
    events, ledger = eps_events(body, "AAPL")
    assert len(events) == len(ledger) == 1
    assert events[0]["eligible"] and events[0]["surprise_pct"] == pytest.approx(10)
    assert not eps_events(_body(surprise="50"), "AAPL")[0]
    negative, audit = eps_events(_body(actual="-1", surprise="-200"), "AAPL")
    assert not negative[0]["eligible"] and audit[0]["status"] == "valid_nonbuy_report"
    with pytest.raises(ValueError, match="identity/history"):
        eps_events(body, "MSFT")


def test_date_only_release_waits_for_two_sessions_and_another_for_stress():
    assert execution_day("2025-01-30") == "2025-02-03"
    assert execution_day("2025-01-30", 1) == "2025-02-04"


def test_quantity_uses_known_prior_close_not_the_future_open_and_retains_costs():
    events, _ = eps_events(_body(), "AAPL")
    prices = {"AAPL": {"2025-01-31": {"clos": 100, "tvol": 1_000_000},
                       "2025-02-03": {"open": 100, "clos": 103, "tvol": 1_000_000}}}
    result = simulate(events, prices, "2025", "eps", 75, 1)
    trade = result["trades"][0]
    assert trade["shares"] == 12
    assert trade["net_pnl"] == pytest.approx(12*(103*.99625-100*1.00375))
    prices["AAPL"]["2025-02-03"]["open"] = 90
    assert simulate(events, prices, "2025", "eps", 75, 1)["trades"][0]["shares"] == 12
    del prices["AAPL"]["2025-02-03"]
    assert simulate(events, prices, "2025", "eps", 75, 1)["net_return_pct"] is None
    assert simulate(events, prices, "2025", "eps", 75, 0)["trade_count"] == 0


def test_source_year_does_not_leak_development_events_into_next_execution_year():
    events, _ = eps_events(_body(), "AAPL")
    events[0]["reported_date"] = "2024-12-31"
    result = simulate(events, {}, "2025", "eps", 75, 1)
    assert result["events"] == result["trade_count"] == 0


def test_positive_control_can_pass_but_cannot_authorize_live_orders():
    trades = [{"symbol": f"S{i % 8}", "net_pnl": 10} for i in range(16)]
    positive = {"state": "complete", "trade_count": 16, "trades": trades,
                "net_return_pct": 4, "quarter_pnl": {str(q): 40 for q in range(1, 5)},
                "quarter_trades": {str(q): 4 for q in range(1, 5)}}
    scenarios = {key: {"eps": positive, "market": {**positive, "net_return_pct": 1},
                       "all_reports": {**positive, "net_return_pct": 2}}
                 for key in ("75bp_full_delay0", "120bp_full_delay0", "75bp_full_delay1")}
    verdict = adoption(scenarios)
    assert all(verdict["checks"].values())
    assert verdict["adoption"] == "shadow_candidate" and verdict["live_order_eligible"] is False
    scenarios["120bp_full_delay0"]["eps"] = {**positive, "net_return_pct": -1}
    assert adoption(scenarios)["adoption"] == "rejected"
