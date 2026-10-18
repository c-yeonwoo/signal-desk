"""Deterministic mechanics, not historical strategy profitability evidence."""

import copy
import json
from pathlib import Path

import pytest

from signal_desk import market_clock
from signal_desk.signals.research_net_alpha_execution import ExecutionBook

DAY = "2022-07-01"


def book(scenario="75bp_full"):
    spec = json.loads(Path("docs/kr-net-alpha-plan-2026-10-17.json").read_text())
    spec["initial_cash_krw"] = 100000
    return ExecutionBook(spec, scenario)


def observed(price=100):
    return {"price": price, "open_tradable": True, "source_id": "synthetic-not-source-certification"}


def submit(b, target, *, day=DAY, force=False):
    names = set(target) | set(b.holdings)
    return b.submit(day, target, {t: 100 for t in names}, {t: 100000 for t in names},
        b.price_nav({t: 100 for t in names}), priority={t: 1 for t in target},
        input_id="synthetic/"+day, budget=1, force_roundtrip=force)


def test_same_holding_netting_has_zero_cost_and_roundtrip_pays_both_sides():
    net, forced = book(), book()
    for b in (net, forced):
        b.holdings = {"A": 100}; b.settled_cash = 90000
    assert submit(net, {"A": .1}) == []
    submit(forced, {"A": .1}, force=True)
    next_day = market_clock.next_sessions("kr", DAY, 1)[0]
    net.open_session(next_day, {"A": observed()})
    fills = forced.open_session(next_day, {"A": observed()})
    assert [f["side"] for f in fills] == ["sell", "buy"]
    assert net.fees == 0 and forced.fees > 0
    assert net.price_nav({"A": 100})-forced.price_nav({"A": 100}) == pytest.approx(forced.fees)
    assert forced.state()["live_order_eligible"] is False


def test_cheap_open_cannot_increase_precommitted_order_or_use_future_volume():
    b = book(); requests = submit(b, {"A": .1})
    quantity = requests[0]["shares"]
    next_day = market_clock.next_sessions("kr", DAY, 1)[0]
    fills = b.open_session(next_day, {"A": {**observed(50), "full_session_volume": 999999999}})
    assert fills[0]["shares"] == quantity
    assert b.price_nav({"A": 50}) == pytest.approx(100000-b.fees)
    assert b.settled_cash == 100000 and b.buying_power < b.settled_cash


@pytest.mark.parametrize("scenario", ["75bp_buy_half", "75bp_none"])
def test_one_share_half_and_no_fill_do_not_invent_one_filled_share(scenario):
    b = book(scenario); b.settled_cash = 102
    requests = submit(b, {"A": 1})
    assert requests[0]["shares"] == 1
    day = market_clock.next_sessions("kr", DAY, 1)[0]
    assert b.open_session(day, {"A": observed()}) == []
    assert b.holdings == {} and b.pending == [] and b.buying_power == 102


def test_gap_limit_and_missing_source_cancel_buys_without_late_reissue():
    for observation in (observed(102), {"price": 100, "open_tradable": True}):
        b = book(); submit(b, {"A": .1})
        day = market_clock.next_sessions("kr", DAY, 1)[0]
        assert b.open_session(day, {"A": observation}) == []
        assert b.pending == []
        day = market_clock.next_sessions("kr", day, 1)[0]
        assert b.open_session(day, {"A": observed()}) == []


def test_partial_sell_retries_are_bounded_and_halts_consume_regular_sessions():
    b = book("75bp_sell_half"); b.holdings = {"A": 3}
    submit(b, {})
    days = market_clock.next_sessions("kr", DAY, 6)
    for i, day in enumerate(days):
        b.open_session(day, {"A": observed()} if i == 0 else {})
    assert b.holdings == {"A": 2} and b.pending == []
    assert len(b.fills) == 1 and b.fills[0]["shares"] == 1
    assert b.events[-1]["attempts"] == 6
    assert b.price_nav({}) is None  # never mark an unpriced holding as zero


def test_delay_preserves_original_quantity_limit_and_source_decision():
    b = book("75bp_delay1"); requests = submit(b, {"A": .1})
    first, second = market_clock.next_sessions("kr", DAY, 2)
    assert b.open_session(first, {"A": observed()}) == []
    fills = b.open_session(second, {"A": observed(99)})
    assert fills[0]["shares"] == requests[0]["shares"]
    assert fills[0]["decision_on"] == DAY and fills[0]["input_id"] == "synthetic/"+DAY
    with pytest.raises(ValueError, match="missing or repeated"):
        b.open_session(second, {"A": observed()})


def test_settlement_credit_is_not_withdrawable_cash_and_balances_reconcile():
    b = book(); b.holdings = {"A": 100}; b.settled_cash = 0
    submit(b, {"B": 1})
    days = market_clock.next_sessions("kr", DAY, 3)
    b.open_session(days[0], {"A": observed(), "B": observed()})
    assert [f["side"] for f in b.fills] == ["sell", "buy"]
    assert b.settled_cash == 0 and b.buying_power > 0
    assert b.price_nav({"B": 100}) == pytest.approx(10000-b.fees)
    b.open_session(days[1], {}); b.open_session(days[2], {})
    assert b.settlements == [] and b.settled_cash == pytest.approx(b.buying_power)


def test_invalid_decision_cannot_erase_existing_residuals():
    b = book(); b.holdings = {"A": 100}; submit(b, {})
    before = copy.deepcopy(b.state())
    with pytest.raises(ValueError, match="incomplete decision"):
        b.submit("2022-07-04", {"B": .1}, {"A": 100, "B": 100}, {"A": 100, "B": 100},
            110000, priority={"C": 1}, input_id="bad", budget=1)
    assert b.state() == before


def test_partial_sells_cannot_finance_extra_slots_or_break_exposure_budget():
    b = book("75bp_sell_half"); b.holdings = {t: 10 for t in "ABCDEF"}
    submit(b, {"Z": .1})
    day = market_clock.next_sessions("kr", DAY, 1)[0]
    b.open_session(day, {t: observed() for t in "ABCDEFZ"})
    assert "Z" not in b.holdings and len(b.holdings) == 6
    b = book("75bp_sell_half"); b.holdings = {"A": 100}; b.settled_cash = 90000
    b.submit(DAY, {"B": .1}, {"A": 100, "B": 100}, {"A": 100000, "B": 100000},
        100000, priority={"B": 1}, input_id="budget", budget=.1)
    b.open_session(day, {"A": observed(200), "B": observed()})
    invested = b.holdings.get("A", 0)*200+b.holdings.get("B", 0)*100
    assert invested <= .1*b.price_nav({"A": 200, "B": 100})+1e-6


def test_first_open_cannot_skip_missing_sessions_to_get_a_later_buy_price():
    b = book(); submit(b, {"A": .1})
    late = market_clock.next_sessions("kr", DAY, 2)[-1]
    with pytest.raises(ValueError, match="missing first execution"):
        b.open_session(late, {"A": observed()})
    assert b.fills == []
    b = book()
    with pytest.raises(ValueError, match="no new buys"):
        submit(b, {"A": .1}, day="2024-01-02")


def test_frozen_grid_and_six_arm_targets_ignore_outcomes_and_do_not_retrain():
    import numpy as np
    from signal_desk.signals import research_net_alpha_execution as execution
    from signal_desk.signals import research_net_alpha as core
    b = book(); spec = {**b.spec, "min_feature_eligible": 2}
    assert len(execution.path_grid(spec)) == 210
    days = np.arange(253)
    prices = np.exp(np.column_stack((.001*days+.01*np.sin(days), .0005*days+.005*np.cos(days))))*100
    f = core.features(prices, np.ones((21, 2))*1000, [1e9, 2e9])
    snapshot = {"date": DAY, "state": "ready", "tickers": ["A", "B"], "features": f, "labels": "not consumed"}
    model = {"candidate_id": spec["candidate_id"], "selection_scale": [1]*11,
        "selection_coefficients": [.01]*11, "market_means": [0]*4, "market_scales": [1]*4,
        "market_coefficients": [.1, 0, 0, 0, 0]}
    saved = copy.deepcopy(model)
    plans = {a: execution.portfolio_plan(snapshot, model, 0, {}, {"A": 1e9, "B": 2e9}, spec, arm=a)
        for a in spec["arms"]}
    assert model == saved and plans["cash"]["weights"] == {}
    assert plans["forced_roundtrip"]["weights"] == plans["candidate"]["weights"]
    assert plans["forced_roundtrip"]["force_roundtrip"]
    assert plans["fixed_budget"]["budget"] == 1
    assert sorted(plans["shuffled_alpha"]["priority"].values()) == sorted(plans["candidate"]["priority"].values())
    for a in spec["arms"]:
        changed = {**snapshot, "labels": [9999, -9999]}
        assert execution.portfolio_plan(changed, model, 0, {}, {"A": 1e9, "B": 2e9}, spec, arm=a) == plans[a]
    with pytest.raises(ValueError, match="held risk/unit history missing"):
        execution.portfolio_plan(snapshot, model, 0, {"Z": .1}, {"A": 1e9, "B": 2e9}, spec, arm="candidate")
