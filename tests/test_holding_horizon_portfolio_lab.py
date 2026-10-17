"""Execution timing and cash constraints for the research-only portfolio replay."""

from scripts.measure.holding_horizon_portfolio_lab import (
    first_buy_decisions, replay_portfolio,
)
from scripts.measure.price_challenger_lab import PricePanel


def _panel(prices, *, zero_open=None):
    zero_open = zero_open or set()
    return PricePanel({f"2024-01-{index + 1:02d}": [
        {"ISU_CD": ticker, "TDD_OPNPRC": 0 if (index, ticker) in zero_open else price,
         "TDD_CLSPRC": price}
        for ticker, price in day.items()]
        for index, day in enumerate(prices)})


def test_decision_close_only_buys_next_open_and_exits_at_future_open():
    panel = _panel([{"005930": price} for price in (100, 100, 110, 120, 130)])
    result = replay_portfolio(panel, {panel.sessions[0]: [{"ticker": "005930", "score": 1}]},
                              start=panel.sessions[0], end=panel.sessions[0], horizon=2)
    assert result["state"] == "complete"
    assert [(fill["side"], fill["day"], fill["price"]) for fill in result["fills"]] == [
        ("buy", panel.sessions[1], 100), ("sell", panel.sessions[3], 120)]
    assert result["net_return_pct"] > 0


def test_missing_entry_open_is_skipped_not_filled():
    panel = _panel([{"005930": 100}] * 5, zero_open={(1, "005930")})
    result = replay_portfolio(panel, {panel.sessions[0]: [{"ticker": "005930", "score": 1}]},
                              start=panel.sessions[0], end=panel.sessions[0], horizon=2)
    assert result["state"] == "complete"
    assert result["buy_fills"] == 0
    assert result["skips"] == {"missing_entry_open": 1}


def test_missing_exit_open_prevents_return_claim():
    panel = _panel([{"005930": 100}] * 5, zero_open={(3, "005930")})
    result = replay_portfolio(panel, {panel.sessions[0]: [{"ticker": "005930", "score": 1}]},
                              start=panel.sessions[0], end=panel.sessions[0], horizon=2)
    assert result["state"] == "incomplete_exit_open"
    assert "net_return_pct" not in result


def test_risk_observed_at_close_executes_next_open_not_stop_price():
    from signal_desk.signals.risk import RiskConfig
    panel = _panel([{"005930": p} for p in (100, 100, 90, 80, 100, 100)])
    candidates = {panel.sessions[0]: [{"ticker": "005930", "score": 1}]}
    base = replay_portfolio(panel, candidates, start=panel.sessions[0], end=panel.sessions[0], horizon=4)
    result = replay_portfolio(panel, candidates, start=panel.sessions[0], end=panel.sessions[0], horizon=4,
                              risk_config=RiskConfig())
    assert base["fills"][-1]["day"] == panel.sessions[5]
    assert result["fills"][-1]["day"] == panel.sessions[3]
    assert result["fills"][-1]["price"] == 80
    assert result["risk_exits"] == {"STOP_LOSS": 1}
    assert result["net_return_pct"] < base["net_return_pct"]  # rebound opportunity is not hidden


def test_risk_exit_does_not_hide_a_missing_next_open():
    from signal_desk.signals.risk import RiskConfig
    panel = _panel([{"005930": p} for p in (100, 100, 90, 80, 100, 100)], zero_open={(3, "005930")})
    result = replay_portfolio(panel, {panel.sessions[0]: [{"ticker": "005930", "score": 1}]},
                              start=panel.sessions[0], end=panel.sessions[0], horizon=4,
                              risk_config=RiskConfig())
    assert result["state"] == "incomplete_exit_open" and "net_return_pct" not in result


def test_same_day_close_does_not_determine_whether_open_order_fills():
    panel = _panel([{"005930": 100}] * 5)
    entry_day = panel.sessions[1]
    panel.bars[entry_day, "005930"] = (100, None)
    result = replay_portfolio(panel, {panel.sessions[0]: [{"ticker": "005930", "score": 1}]},
                              start=panel.sessions[0], end=panel.sessions[0], horizon=2)
    assert result["state"] == "incomplete_held_close"
    assert result["fills"] == 1
    assert "net_return_pct" not in result


def test_listed_share_change_invalidates_historical_return():
    panel = _panel([{"005930": 100}] * 5)
    shares = {(day, "005930"): 1000 for day in panel.sessions}
    shares[panel.sessions[2], "005930"] = 2000
    result = replay_portfolio(panel, {panel.sessions[0]: [{"ticker": "005930", "score": 1}]},
                              start=panel.sessions[0], end=panel.sessions[0], horizon=2,
                              share_counts=shares)
    assert result["state"] == "incomplete_share_history"
    assert result["day"] == panel.sessions[2]
    assert "net_return_pct" not in result


def test_cash_and_slots_are_not_exceeded():
    tickers = [f"{i:06d}" for i in range(1, 11)]
    panel = _panel([{ticker: 100 for ticker in tickers}] * 4)
    candidates = [{"ticker": ticker, "score": 10 - index} for index, ticker in enumerate(tickers)]
    result = replay_portfolio(panel, {panel.sessions[0]: candidates},
                              start=panel.sessions[0], end=panel.sessions[0], horizon=1)
    assert result["state"] == "complete"
    assert result["buy_fills"] == 6
    assert result["skips"]["full_slots"] == 4
    assert result["cash_end"] < 10_000_000


def test_reviewed_cancellation_changes_issuer_count_not_holder_quantity():
    import datetime as dt
    from dataclasses import replace
    from signal_desk.signals.research_action_coverage import ReviewedShareCountChange
    panel = _panel([{"005930": 100}] * 5)
    days = panel.sessions
    shares = {(day, "005930"): 1000 if day < days[2] else 900 for day in days}
    fact = ReviewedShareCountChange(
        ticker="005930", security_id="KR7005930003", share_class="ordinary_common",
        kind="treasury_cancellation", effective_on=days[1], listing_session=days[2],
        previous_session=days[1], listed_shares_before=1000, listed_shares_after=900,
        source_url="https://kind.krx.co.kr/external/synthetic-test", source_sha256="a" * 64,
        source_filing_date=days[1], source_observed_at=dt.datetime(2024, 2, 1, tzinfo=dt.timezone.utc),
        source_span="synthetic cancellation")
    kwargs = dict(start=days[0], end=days[0], horizon=2, share_counts=shares)
    candidates = {days[0]: [{"ticker": "005930", "score": 1}]}
    result = replay_portfolio(panel, candidates, **kwargs, reviewed_share_changes={(days[2], "005930"): fact})
    assert result["state"] == "complete"
    assert result["fills"][0]["shares"] == result["fills"][-1]["shares"]
    assert result["action_coverage"] == "partial" and result["verified_total_return"] is False
    assert len(result["applied_reviewed_share_facts"]) == 1
    wrong = replace(fact, listed_shares_after=899)
    blocked = replay_portfolio(panel, candidates, **kwargs, reviewed_share_changes={(days[2], "005930"): wrong})
    assert blocked["state"] == "incomplete_share_history" and "net_return_pct" not in blocked


def test_first_buy_requires_contiguous_prior_nonbuy_and_protected_dates_rejected():
    panel = _panel([{"005930": 100}] * 4)
    dates = panel.sessions
    pilot = {"source_level": "C_offline_price_only_current_engine", "registered_verdict": False,
             "signal_dates": dates, "rows": [
                 {"date": dates[0], "ticker": "005930", "kind": "BUY", "score": 2},
                 {"date": dates[1], "ticker": "005930", "kind": "HOLD", "score": 1},
                 {"date": dates[2], "ticker": "005930", "kind": "STRONG_BUY", "score": 3},
                 {"date": dates[3], "ticker": "005930", "kind": "BUY", "score": 2},
             ]}
    assert first_buy_decisions(pilot, panel) == {dates[2]: [{"ticker": "005930", "score": 3}]}
    pilot["signal_dates"] = ["2026-08-05"]
    try:
        first_buy_decisions(pilot, panel)
    except ValueError as exc:
        assert "registered period" in str(exc)
    else:
        raise AssertionError("protected date scored")


def test_buy_label_outside_top_six_is_not_a_portfolio_candidate():
    panel = _panel([{f"{index:06d}": 100 for index in range(1, 8)}] * 2)
    first, second = panel.sessions
    pilot = {"source_level": "C_offline_price_only_current_engine", "registered_verdict": False,
             "signal_dates": panel.sessions, "rows": [
                 {"date": day, "ticker": f"{index:06d}",
                  "kind": "HOLD" if day == first else "BUY", "score": 10 - index}
                 for day in panel.sessions for index in range(1, 8)]}
    selected = first_buy_decisions(pilot, panel)
    assert [row["ticker"] for row in selected[second]] == [f"{index:06d}" for index in range(1, 7)]
