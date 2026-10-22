"""Synthetic temporal isolation, single-bias selection and common replay gates."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from scripts.measure import kr_frozen_bias_calibration as lab
from scripts.measure import kr_frozen_forecast_policy as frozen


def training_market():
    dates = frozen.calendar("2024-10-02", "2024-10-31")
    train = dates[:2]
    maturity = frozen.market_clock.next_sessions("kr", train[-1], 6)[-1]
    start = frozen.market_clock.next_sessions("kr", maturity, 1)[0]
    names = [str(i) for i in range(6)]
    rows = {d: {t: {"TDD_OPNPRC": 100., "LIST_SHRS": 1000.} for t in names} for d in dates}
    data = frozen.Inputs({d: {t: .03 for t in names} for d in train+[start]}, {}, rows,
        {d: dates[i-1] for i, d in enumerate(dates) if i}, {}, {})
    plan = {"calibration": {"dates": 2, "start": train[0], "end": train[-1],
        "stock_date_labels": 12, "latest_target_maturity": maturity}, "evaluation": {"start": start}}
    return data, plan


def test_fit_uses_only_mature_training_targets_and_freezes_one_bias():
    data, plan = training_market()
    fitted = lab.fit_calibration(data, plan)
    assert fitted["bias"] == pytest.approx(.03) and fitted["fit_calls"] == 1
    assert fitted["evaluation_labels_used"] == 0
    # Corrupt all later inputs; neither future forecasts nor realized prices may affect fit.
    start = plan["evaluation"]["start"]
    for d in data.rows:
        if d >= start:
            data.rows[d] = {}
    data.forecasts[start] = {"future": 999.}
    assert lab.fit_calibration(data, plan) == fitted


def test_fit_does_not_hide_unknown_targets_or_overlap_evaluation():
    data, plan = training_market()
    overlap = deepcopy(plan)
    overlap["evaluation"]["start"] = overlap["calibration"]["latest_target_maturity"]
    with pytest.raises(ValueError, match="not_mature"):
        lab.fit_calibration(data, overlap)
    entry = frozen.market_clock.next_sessions("kr", plan["calibration"]["start"], 1)[0]
    data.rows[entry]["0"]["TDD_OPNPRC"] = 0
    with pytest.raises(ValueError, match="all_top6_labels"):
        lab.fit_calibration(data, plan)


def test_negative_training_error_gives_zero_not_forecast_increase():
    data, plan = training_market()
    for p in data.forecasts.values():
        for t in p:
            p[t] = -.02
    out = lab.fit_calibration(data, plan)
    assert out["unconstrained_mean_error"] == pytest.approx(-.02)
    assert out["bias"] == 0


@pytest.fixture
def spec():
    value = json.loads((Path(__file__).resolve().parents[1]/frozen.PLAN_PATH).read_text())
    value["portfolio"]["min_forecast_names"] = 2
    return value


@pytest.mark.parametrize("bad", [-.1, float("nan"), float("inf"), True])
def test_bad_bias_is_rejected(bad):
    with pytest.raises(ValueError, match="nonnegative_finite"):
        lab.selector(bad)


def test_only_candidate_changes_and_original_forecasts_are_not_mutated(spec):
    forecasts = {"A": .018, "B": .016}
    args = (forecasts, {"A": 2, "B": 1}, ["A"], {}, {"A": 100, "B": 100}, 100000, spec)
    old = frozen.targets("candidate", *args)
    out = lab.selector(.006)("candidate", *args)
    assert old[0] and out[0] == {}
    assert out[2]["calibration_bias"] == .006 and forecasts == {"A": .018, "B": .016}
    for arm in frozen.CONTROLS:
        assert lab.selector(.006)(arm, *args) == frozen.targets(arm, *args)
    assert lab.selector(.006)("prior_cost_aware", *args) == old


def test_all_forecasts_including_held_coordinates_are_corrected_without_rank_change(spec, monkeypatch):
    seen = []
    def capture(arm, forecasts, *args):
        seen.append(forecasts)
        return {}, {}, {}
    monkeypatch.setattr(frozen, "targets", capture)
    original = {"A": .03, "B": .03, "held": .007}
    lab.selector(.01)("candidate", original, {}, [], {"held": 1}, {}, 1000, spec)
    assert seen[0] == pytest.approx({"A": .02, "B": .02, "held": -.003})
    assert sorted(original, key=lambda t: (-original[t], t)) == sorted(seen[0], key=lambda t: (-seen[0][t], t))


def test_missing_evaluation_target_cannot_be_averaged_away():
    out = lab.prediction_diagnostic([{"error": .02}, {"error": None}], .01)
    assert out["raw"]["mean_error"] is None and out["calibrated"]["rmse"] is None
    out = lab.prediction_diagnostic([{"error": .03}, {"error": -.01}], .01)
    assert out["raw"]["mean_error"] == pytest.approx(.01)
    assert out["calibrated"]["mean_error"] == pytest.approx(0.)
    assert out["calibrated"]["mae"] == pytest.approx(.02)


def complete_groups(spec):
    spec = deepcopy(spec)
    spec["periods"][0]["arms"] = ["candidate", "prior_cost_aware", *frozen.CONTROLS]
    groups = [{"scenario": s, "phase": p, "metrics": {a: {"complete": True,
        "net_return_pct": 3. if a == "candidate" else 2. if a == "prior_cost_aware" else 1.,
        "price_mdd_pct": -3. if a == "candidate" else -4.}
        for a in spec["periods"][0]["arms"]}} for s in spec["execution"]["scenarios"]
        for p in spec["schedule"]["phases"]]
    return spec, groups


def test_incremental_improvement_risk_and_unknown_paths_are_not_conflated(spec):
    spec, groups = complete_groups(spec)
    assert lab.verdict(groups, spec)["incremental_assessment"]["decision"] == "incremental_support"
    groups[0]["metrics"]["candidate"]["price_mdd_pct"] = -4.1
    assert lab.verdict(groups, spec)["incremental_assessment"]["decision"] == "incremental_rejection"
    spec, groups = complete_groups(spec)
    groups[0]["metrics"]["prior_cost_aware"].update(complete=False, net_return_pct=None, price_mdd_pct=None)
    out = lab.verdict(groups, spec)
    assert out["scenario_mean_price_return_pct"]["75bp_full"]["prior_cost_aware"] is None
    assert out["incremental_assessment"]["decision"] == "not_evaluable_source_or_tail"


def test_primary_equal_mean_and_stress_regression_reject(spec):
    spec, groups = complete_groups(spec)
    for g in groups:
        if g["scenario"] == "75bp_full":
            g["metrics"]["candidate"]["net_return_pct"] = 2.
    assert "75bp_full:mean_not_improved" in lab.verdict(groups, spec)["incremental_assessment"]["violations"]
    spec, groups = complete_groups(spec)
    for g in groups:
        if g["scenario"] == "75bp_delay1":
            g["metrics"]["candidate"]["net_return_pct"] = 1.9
    assert "75bp_delay1:mean_regression" in lab.verdict(groups, spec)["incremental_assessment"]["violations"]


def test_zero_bias_reproduces_original_economic_replay(spec):
    dates = frozen.calendar("2024-10-02", "2024-11-01")
    spec = deepcopy(spec)
    spec["portfolio"]["initial_cash_krw"] = 100000
    spec["periods"][0].update(decision_start=dates[0], decision_end=dates[5], tail_through=dates[-1],
        arms=["candidate", "prior_cost_aware", *frozen.CONTROLS])
    rows = {d: {t: {"TDD_OPNPRC": 100., "TDD_CLSPRC": 100., "LIST_SHRS": 1000000.,
        "ACC_TRDVOL": 1000000., "MKTCAP": 2000-i} for i, t in enumerate(("A", "B"))} for d in dates}
    data = frozen.Inputs({d: {"A": .03, "B": .02} for d in dates[:6]}, {d: ["A"] for d in dates[:6]},
        rows, {d: dates[i-1] for i, d in enumerate(dates) if i}, {}, {})
    group = frozen.replay_group(data, spec, "75bp_full", 0, frozen.Budget(),
        target_selector=lab.selector(0.), forecast_required_arms=("candidate", "prior_cost_aware"))
    assert group["metrics"]["candidate"] == group["metrics"]["prior_cost_aware"]
    assert all(m["complete"] for m in group["metrics"].values())


def test_evaluation_window_is_derived_from_maturity_not_label_performance():
    data, plan = training_market()
    start = plan["evaluation"]["start"]
    plan["evaluation"].update(end=start, dates=1, forecast_rows=6, tail_through="2024-10-31")
    plan["comparison"] = {"arms": ["candidate"]}
    plan["candidate_id"] = "synthetic"
    data.legacy[start] = []
    out, _ = lab.evaluation_inputs(data, {"periods": [{}]}, plan)
    assert sorted(out.forecasts) == [start]
    assert len(data.forecasts) == 3  # original calibration inputs unchanged
