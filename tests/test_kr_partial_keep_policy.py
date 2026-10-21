"""Synthetic menu, common-stop, parent identity and frozen verdict boundaries."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from scripts.measure import kr_frozen_forecast_policy as frozen
from scripts.measure import kr_partial_keep_policy as lab


@pytest.fixture
def spec():
    value = json.loads((Path(__file__).resolve().parents[1]/frozen.PLAN_PATH).read_text())
    value["portfolio"]["min_forecast_names"] = 2
    return value


def test_partial_keep_removes_weak_name_without_upsizing_healthy_name(spec):
    forecasts = {"A": .004, "B": .007}
    args = (forecasts, {"A": 2, "B": 1}, [], {"A": 100, "B": 120}, {"A": 100, "B": 100}, 100000, spec)
    old = frozen.targets("candidate", *args)
    assert old[0] == {"A": .1, "B": .12} and old[2]["choice"] == "keep"
    changed = lab.targets("candidate", *args)
    assert changed[0] == {"B": .12}
    assert changed[2]["choice"] == "partial_keep"
    assert changed[2]["utility_gain_over_baseline_same_state"] == pytest.approx(.0001)
    assert lab.targets("prior_cost_aware", *args) == old
    assert old[0] == {"A": .1, "B": .12}  # no mutation


def test_equal_utility_keeps_old_holding_and_new_entries_still_use_original_choice(spec):
    args = ({"A": .005, "B": .004}, {"A": 2, "B": 1}, [], {"A": 100}, {"A": 100, "B": 100}, 100000, spec)
    old = frozen.targets("candidate", *args)
    assert lab.targets("candidate", *args)[0] == old[0] == {"A": .1}
    args = ({"A": .03, "B": .025}, {"A": 2, "B": 1}, [], {}, {"A": 100, "B": 100}, 100000, spec)
    assert lab.targets("candidate", *args)[0] == frozen.targets("candidate", *args)[0]


def test_partial_keep_budget_cap_and_invalid_base_are_checked(spec):
    spec["portfolio"]["budget"] = .2
    args = ({"A": .004, "B": .007}, {"A": 2, "B": 1}, [], {"A": 100, "B": 120}, {"A": 100, "B": 100}, 100000, spec)
    out = lab.targets("candidate", *args)
    assert sum(out[0].values()) <= .2
    assert all(w <= {"A": .1, "B": .12}[t] for t, w in out[0].items())
    base = frozen.targets("candidate", *args)[2]
    base["utility"] += .1
    with pytest.raises(ValueError, match="base_utility_differs"):
        lab.partial_choice(base, args[0], {"A": .1, "B": .12}, spec)
    spec["candidate"]["risk_aversion"] = 1.
    with pytest.raises(ValueError, match="zero_risk"):
        lab.partial_choice(base, args[0], {"A": .1, "B": .12}, spec)


def market(spec):
    days = frozen.calendar("2024-10-02", "2024-11-01")
    spec = deepcopy(spec)
    spec["portfolio"]["initial_cash_krw"] = 100000
    spec["periods"][0].update(decision_start=days[0], decision_end=days[5], tail_through=days[-1])
    rows = {d: {t: {"TDD_OPNPRC": 100., "TDD_CLSPRC": 100., "LIST_SHRS": 1000000.,
                   "ACC_TRDVOL": 1000000., "MKTCAP": cap}
                for t, cap in (("A", 2000), ("B", 1000), ("C", 500))} for d in days}
    data = frozen.Inputs({d: {"A": .02, "B": .03} for d in days[:6]},
        {d: ["A"] for d in days[:6]}, rows, {d: days[i-1] for i, d in enumerate(days) if i}, {}, {})
    return spec, data, days


def five_arms(spec):
    out = deepcopy(spec)
    out["periods"][0]["arms"] = ["candidate", "prior_cost_aware", *frozen.CONTROLS]
    return out


def test_callback_default_parent_economics_and_contract_ids(spec):
    spec, data, _ = market(spec)
    parent = frozen.replay_group(data, spec, "75bp_full", 0, frozen.Budget())
    extended = frozen.replay_group(data, five_arms(spec), "75bp_full", 0, frozen.Budget(),
        target_selector=lab.targets, decision_contract_sha=lab.PLAN_SHA,
        forecast_required_arms=("candidate", "prior_cost_aware"))
    assert all(r["complete_match"] for r in lab.parent_crosscheck(extended, parent))
    assert parent["trace"][0]["decision"]["input_id"] != extended["trace"][0]["decision"]["input_id"]
    assert all(m["complete"] for m in extended["metrics"].values())
    extended["metrics"]["prior_cost_aware"]["fills"][0]["price"] += 1
    with pytest.raises(ValueError, match="parent_fill_prefix_mismatch"):
        lab.parent_crosscheck(extended, parent)


def test_missing_prior_held_forecast_stops_all_five_arms(spec):
    spec, data, days = market(spec)
    data.forecasts[days[5]] = {"B": .03, "C": .02}
    def selector(arm, forecasts, caps, legacy, holdings, closes, nav, spec):
        if arm in ("candidate", "prior_cost_aware"):
            ticker = "A" if arm == "prior_cost_aware" else "B"
            return {ticker: 1/6}, {ticker: 1.}, None
        return frozen.targets(arm, forecasts, caps, legacy, holdings, closes, nav, spec)
    out = frozen.replay_group(data, five_arms(spec), "75bp_full", 0, frozen.Budget(),
        target_selector=selector, forecast_required_arms=("candidate", "prior_cost_aware"))
    assert out["source_stop"]["field"] == "held_forecast_missing"
    assert out["source_stop"]["ticker"] == "A"
    assert all(not m["complete"] and m["net_return_pct"] is None for m in out["metrics"].values())


def complete_groups(spec, *, candidate=3., prior=2., control=1.):
    spec = five_arms(spec)
    groups = [{"scenario": s, "phase": p, "metrics": {a: {"complete": True,
        "net_return_pct": candidate if a == "candidate" else prior if a == "prior_cost_aware" else control,
        "price_mdd_pct": -3. if a == "candidate" else -4.}
        for a in spec["periods"][0]["arms"]}}
        for s in spec["execution"]["scenarios"] for p in spec["schedule"]["phases"]]
    return spec, groups


def test_incremental_and_overall_gates_are_separate(spec):
    spec, groups = complete_groups(spec)
    out = lab.verdict(groups, spec)
    assert out["decision"] == "conditional_development_support"
    assert out["incremental_assessment"]["decision"] == "incremental_support"
    for g in groups:
        g["metrics"]["ridge_net"]["net_return_pct"] = 4.
    out = lab.verdict(groups, spec)
    assert out["decision"] == "observed_rejection"
    assert out["incremental_assessment"]["decision"] == "incremental_support"
    assert not out["production_change_authorized"]


def test_not_improving_prior_and_incomplete_means_cannot_pass(spec):
    spec, groups = complete_groups(spec, candidate=2., prior=2.)
    assert lab.verdict(groups, spec)["incremental_assessment"]["decision"] == "incremental_rejection"
    spec, groups = complete_groups(spec, candidate=3., prior=2.)
    groups[0]["metrics"]["prior_cost_aware"].update(complete=False, net_return_pct=None, price_mdd_pct=None)
    out = lab.verdict(groups, spec)
    assert out["decision"] == "not_evaluable_source_or_tail"
    assert out["incremental_assessment"]["decision"] == "not_evaluable_source_or_tail"


def test_parent_risk_gate_and_incremental_mdd_non_regression_remain(spec):
    spec, groups = complete_groups(spec)
    groups[0]["metrics"]["candidate"]["price_mdd_pct"] = -11.
    out = lab.verdict(groups, spec)
    assert out["decision"] == "observed_rejection"
    assert out["incremental_assessment"]["decision"] == "incremental_rejection"
