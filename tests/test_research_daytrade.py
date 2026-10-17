"""Financial accounting and future-input isolation for the bounded research batch."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from scripts.measure import research_daytrade_batch as batch
from signal_desk import market_clock
from signal_desk.signals.research_daytrade import (
    Features, execute_day, grade_validation, plan_orders, select,
)


def _feature(ticker="005930", **kwargs):
    return replace(Features(ticker, 100.0, 100_000.0, 1e12, 95.0, -0.02,
                            0.1, 0.2, 2.5, 0.9, 0.02, 1.0, True), **kwargs)


def test_preopen_quantity_and_exact_net_cash_are_independent_of_future_price():
    selected = (_feature(),)
    orders = plan_orders(selected, 10_000.0, 0.00375)
    assert orders[0].shares == 12
    bars = {"005930": {"open": 120.0, "close": 110.0, "high": 125.0,
                       "low": 105.0, "volume": 500_000}}
    result = execute_day(orders, bars, cash=10_000.0, side_cost=0.00375)
    assert result["cash_end"] == pytest.approx(10_000 + 12 * (110 * 0.99625 - 120 * 1.00375))
    assert result["fills"][0]["shares"] == 12
    assert result["fills"][0]["reference_close"] == 100.0


def test_no_open_keeps_cash_but_missing_exit_never_discards_filled_loss():
    orders = plan_orders((_feature(),), 10_000, 0.00375)
    result = execute_day(orders, {}, cash=10_000, side_cost=0.00375)
    assert result["cash_end"] == 10_000 and not result["fills"]
    missing_exit = {"005930": {"open": 100, "close": None, "high": 110,
                               "low": 90, "volume": 500_000}}
    result = execute_day(orders, missing_exit, cash=10_000, side_cost=0.00375)
    assert result["state"] == "incomplete_exit" and result["cash_end"] is None


def test_fixed_budget_and_fill_stress_do_not_borrow_or_resize_after_open():
    orders = plan_orders((_feature(),), 10_000, 0.00375)
    bars = {"005930": {"open": 200, "close": 250, "high": 260, "low": 190, "volume": 10_000}}
    result = execute_day(orders, bars, cash=10_000, side_cost=0.00375)
    assert result["cash_end"] == 10_000
    assert result["skips"] == {"precommitted_budget_exceeded": 1}
    bars["005930"].update(open=100, close=105, high=110, low=95)
    half = execute_day(orders, bars, cash=10_000, side_cost=0.00375, fill_fraction=0.5)
    assert half["fills"][0]["shares"] == 6
    none = execute_day(orders, bars, cash=10_000, side_cost=0.00375, fill_fraction=0)
    assert none["cash_end"] == 10_000 and not none["fills"]


def test_legacy_rejected_slot_is_not_backfilled_and_thin_day_abstains():
    features = tuple(_feature(str(index), legacy_score=200-index,
                              legacy_buy=index != 0) for index in range(160))
    assert [row.ticker for row in select(features, "legacy_price")] == ["1", "2", "3", "4", "5"]
    assert select(features[:159], "trend_leader") == ()


def test_fixed_verdict_rejects_bad_block_and_preserves_missing_data():
    def result(net):
        return {"state": "complete", "net_return_pct": net,
                "trade_count": 20, "active_sessions": 10}
    windows = {str(i): {"75bp_full": {"trend_leader": result(2),
                                      "legacy_price": result(1), "cap_control": result(0)},
                        "120bp_full": {"trend_leader": result(1)}} for i in range(4)}
    assert grade_validation(windows, "trend_leader")["decision"] == "shadow_candidate"
    windows["0"]["75bp_full"]["trend_leader"] = result(-6)
    verdict = grade_validation(windows, "trend_leader")
    assert verdict["decision"] == "rejected"
    assert verdict["checks"]["worst_block_at_least_minus5"] is False
    windows["0"]["75bp_full"]["trend_leader"]["state"] = "incomplete_exit"
    assert grade_validation(windows, "trend_leader")["decision"] == "inconclusive"


def test_next_session_mutation_does_not_change_features_or_selection(monkeypatch):
    sessions = ["2023-01-02"]
    while len(sessions) < 255:
        sessions.extend(market_clock.next_sessions("kr", sessions[-1], min(60, 255-len(sessions))))
    decision, execution = sessions[-2:]
    monkeypatch.setattr(batch, "WINDOWS", (("tiny", decision, decision),))
    monkeypatch.setattr(batch.engine, "evaluate", lambda universe, prices, **kwargs: [
        SimpleNamespace(ticker=item["ticker"], score=prices[item["ticker"]][-1], kind="BUY")
        for item in universe if item["ticker"] in prices])
    days = {}
    for offset, session in enumerate(sessions):
        close = 100 + offset * 0.1
        days[session] = [{"ISU_CD": f"{100000 + i * 10:06d}", "ISU_NM": str(i),
                          "MKTCAP": 1e12 - i * 1e8, "TDD_OPNPRC": close - 1,
                          "TDD_CLSPRC": close, "TDD_HGPRC": close + 2,
                          "TDD_LWPRC": close - 2, "ACC_TRDVOL": 100_000}
                         for i in range(200)]
    before, before_census = batch.build_inputs(days)
    for row in days[execution]:
        row.update(TDD_CLSPRC=1_000_000, ACC_TRDVOL=1)
    after, after_census = batch.build_inputs(days)
    assert before["tiny"][0]["selected"] == after["tiny"][0]["selected"]
    assert before_census["selection_sha256"] == after_census["selection_sha256"]
    assert before["tiny"][0]["next_bars"] != after["tiny"][0]["next_bars"]
