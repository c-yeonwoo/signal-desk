"""Synthetic as-of histories, bounded market veto and shared-replay isolation."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from scripts.measure import kr_joint_weakness_regime as lab
from scripts.measure import kr_frozen_forecast_policy as frozen
from scripts.measure import kr_frozen_bias_calibration as calibration


def history_market():
    day = "2024-10-10"
    dates = frozen.calendar("2024-09-20", "2024-10-31")
    rows = {d: {t: {"TDD_CLSPRC": 100., "LIST_SHRS": 1000.} for t in "ABCDE"} for d in dates}
    for t, close in zip("ABCDE", [90., 80., 95., 110., 120.]):
        rows[day][t]["TDD_CLSPRC"] = close
    data = frozen.Inputs({}, {}, rows, {d: dates[i-1] for i, d in enumerate(dates) if i}, {}, {})
    return data, day


def test_context_reads_only_six_asof_closes_and_preserves_unknown_universe():
    data, day = history_market()
    forecasts = {t: .03 for t in "ABCDE"}
    context = lab.decision_context(data, day, forecasts)
    assert context["market_state"] == "weak" and context["universe"] == 5
    assert context["stocks"]["A"]["return5"] == pytest.approx(-.1)
    assert len(context["history_sessions"]) == 6 and context["history_sessions"][-1] == day
    for d in data.rows:
        if d > day:
            data.rows[d] = {}
    assert lab.decision_context(data, day, forecasts) == context
    # Unknown history stays in the five-name universe and expands the bounds.
    data.rows[day]["E"]["LIST_SHRS"] = 2000
    changed = lab.decision_context(data, day, forecasts)
    assert changed["universe"] == 5 and changed["unknown"] == 1
    assert changed["stocks"]["E"]["return5"] is None
    assert changed["market_return5_median_bounds"] == pytest.approx([-.1, -.05])


def test_history_checks_internal_units_but_not_change_before_first_close():
    data, day = history_market()
    dates = lab.history_sessions(day)
    for d in dates:
        data.rows[d]["A"]["LIST_SHRS"] = 2000
    assert lab.historical_return(data, dates, "A")["return5"] == pytest.approx(-.1)
    for d in dates[2:]:
        data.rows[d]["A"]["LIST_SHRS"] = 3000
    assert lab.historical_return(data, dates, "A")["return5"] is None
    data.reviewed[dates[2], "A"] = {"holder_quantity_effect": "unchanged", "previous_session": dates[1],
        "listed_shares_before": 2000., "listed_shares_after": 3000.}
    assert lab.historical_return(data, dates, "A")["return5"] == pytest.approx(-.1)
    data.rows[dates[1]]["A"]["TDD_CLSPRC"] = 0
    assert lab.historical_return(data, dates, "A")["return5"] is None


@pytest.mark.parametrize("bounds,state", [([-.1, -.01], "weak"), ([0., .1], "not_weak"),
    ([-.1, .1], "ambiguous"), ([-.1, 0.], "ambiguous")])
def test_market_state_requires_definitely_negative_upper_bound(bounds, state):
    assert lab.market_state(bounds) == state


def test_unavailable_market_bounds_raise_common_source_error():
    with pytest.raises(ValueError, match="unavailable_market"):
        lab.market_state([None, None])
    data, day = history_market()
    for t in "ABCDE":
        data.rows[day][t]["TDD_CLSPRC"] = 0
    with pytest.raises(frozen.DataGap, match="unavailable_market_history"):
        lab.decision_context(data, day, {t: .03 for t in "ABCDE"})


def context(day="2024-10-02", state="weak"):
    return {"session": day, "market_state": state,
        "stocks": {"A": {"return5": -.1}, "B": {"return5": .1}, "C": {"return5": -.01}}}


def test_veto_exits_weak_holding_blocks_addition_and_does_not_resize_winners():
    weights = {"A": 1/6, "B": .1, "C": 1/6}
    original = deepcopy(weights)
    result, why = lab.veto(weights, {"A": 10}, {"A": 100}, 100000, context())
    assert result == {"B": .1} and weights == original
    assert why["blocked_new_entry_intent"] == ["C"]
    assert why["blocked_held_exit_intent"] == why["blocked_held_addition_notional_intent"] == ["A"]
    assert why["intents_are_not_executed_orders"]
    for state in ("not_weak", "ambiguous"):
        assert lab.veto(weights, {}, {}, 100000, context(state=state))[0] == original


def test_unknown_required_stock_is_not_neutral_or_automatic_sell():
    c = context()
    c["stocks"]["A"]["return5"] = None
    with pytest.raises(frozen.DataGap, match="required_stock_regime_unknown"):
        lab.veto({"A": .1}, {}, {}, 100000, c)
    # No new input is needed when the original selector already wants zero.
    assert lab.veto({}, {"A": 1}, {}, 100000, c)[0] == {}
    assert lab.veto({"A": .1}, {}, {}, 100000, context(state="not_weak"))[0] == {"A": .1}


@pytest.fixture
def market():
    spec = json.loads((Path(__file__).resolve().parents[1]/frozen.PLAN_PATH).read_text())
    dates = frozen.calendar("2024-09-23", "2024-11-01")
    first = dates.index("2024-10-02")
    decisions = dates[first:first+6]
    spec["periods"][0].update(decision_start=decisions[0], decision_end=decisions[-1],
        tail_through=dates[-1], arms=["candidate", *lab.PARENT_ARMS])
    spec["portfolio"].update(initial_cash_krw=100000, min_forecast_names=2)
    rows = {d: {t: {"TDD_OPNPRC": 100., "TDD_CLSPRC": 100., "LIST_SHRS": 1000000.,
        "ACC_TRDVOL": 1000000., "MKTCAP": 2000-i} for i, t in enumerate("AB")} for d in dates}
    data = frozen.Inputs({d: {"A": .04, "B": .03} for d in decisions}, {d: ["A"] for d in decisions},
        rows, {d: dates[i-1] for i, d in enumerate(dates) if i}, {}, {})
    return spec, data


def replay(spec, data, *, state="weak"):
    return frozen.replay_group(data, spec, "75bp_full", 0, frozen.Budget(),
        target_selector=lab.selector(.004), forecast_required_arms=("candidate", *list(lab.PARENT_ARMS)[:2]),
        decision_context_factory=lambda d, p: context(d, state))


def test_context_is_opt_in_hash_bound_and_terminal_does_not_request_history(market):
    spec, data = market
    weak = replay(spec, data)
    safe = replay(spec, data, state="not_weak")
    assert all(m["complete"] for m in weak["metrics"].values())
    assert safe["metrics"]["candidate"] == safe["metrics"]["prior_calibrated"]
    first_weak = next(r["decision"] for r in weak["trace"] if r["decision"])
    first_safe = next(r["decision"] for r in safe["trace"] if r["decision"])
    assert first_weak["input_id"] != first_safe["input_id"]
    assert "A" not in first_weak["plans"]["candidate"]["weights"]
    assert "A" in first_weak["plans"]["prior_calibrated"]["weights"]
    assert all(r["decision"]["context"] is None for r in weak["trace"] if r["decision"] and r["decision"]["terminal"])


def test_default_replay_ids_and_parent_economic_paths_are_preserved(market):
    spec, data = market
    old_spec = deepcopy(spec)
    old_spec["periods"][0]["arms"] = ["candidate", "prior_cost_aware", *frozen.CONTROLS]
    parent = frozen.replay_group(data, old_spec, "75bp_full", 0, frozen.Budget(),
        target_selector=calibration.selector(.004), forecast_required_arms=("candidate", "prior_cost_aware"))
    first = next(r for r in parent["trace"] if r["decision"])
    day = first["session"]
    expected = frozen.encoded_sha({"date": day, "forecasts": data.forecasts[day], "legacy": data.legacy[day],
        "closes": {t: 100. for t in "AB"}, "volumes": {t: 1000000. for t in "AB"},
        "caps": {"A": 2000., "B": 1999.}, "plan_sha256": frozen.PLAN_SHA, "terminal": False})
    assert first["decision"]["input_id"] == expected and "context" not in first["decision"]
    changed = replay(spec, data)
    assert all(r["complete_match"] for r in lab.parent_crosscheck(changed, parent))
    changed["metrics"]["prior_calibrated"]["net_pnl_krw"] += 1
    with pytest.raises(ValueError, match="parent_complete_metric_mismatch"):
        lab.parent_crosscheck(changed, parent)


def test_context_source_failure_stops_all_six_arms(market):
    spec, data = market
    def missing(day, forecasts):
        raise frozen.DataGap(day, "*", "regime_history_missing")
    group = frozen.replay_group(data, spec, "75bp_full", 0, frozen.Budget(),
        target_selector=lab.selector(.004), decision_context_factory=missing)
    assert group["source_stop"]["field"] == "regime_history_missing"
    assert all(not m["complete"] and m["net_return_pct"] is None for m in group["metrics"].values())


def complete_groups(spec):
    return [{"scenario": s, "phase": p, "metrics": {a: {"complete": True,
        "net_return_pct": 3. if a == "candidate" else 2.,
        "price_mdd_pct": -3. if a == "candidate" else -4.}
        for a in spec["periods"][0]["arms"]}} for s in spec["execution"]["scenarios"]
        for p in spec["schedule"]["phases"]]


def test_direct_incremental_baseline_risk_and_incomplete_mean_gates(market):
    spec, _ = market
    groups = complete_groups(spec)
    out = lab.verdict(groups, spec)
    assert out["incremental_assessment"]["baseline"] == "prior_calibrated"
    assert out["incremental_assessment"]["decision"] == "incremental_support"
    groups[0]["metrics"]["candidate"]["price_mdd_pct"] = -4.1
    assert lab.verdict(groups, spec)["incremental_assessment"]["decision"] == "incremental_rejection"
    groups = complete_groups(spec)
    groups[0]["metrics"]["prior_calibrated"].update(complete=False, net_return_pct=None, price_mdd_pct=None)
    out = lab.verdict(groups, spec)
    assert out["scenario_mean_price_return_pct"]["75bp_full"]["prior_calibrated"] is None
    assert out["incremental_assessment"]["decision"] == "not_evaluable_source_or_tail"
