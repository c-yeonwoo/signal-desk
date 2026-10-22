"""As-of downside, economically distinct actions and replay comparability."""
from copy import deepcopy
from itertools import product
import json
import math
from pathlib import Path
import random

import pytest

from scripts.measure import kr_stock_downside_action as lab
from scripts.measure import kr_frozen_forecast_policy as frozen
from scripts.measure import kr_frozen_bias_calibration as calibration
from scripts.measure import kr_joint_weakness_regime as parent


@pytest.fixture
def spec():
    value = json.loads((Path(__file__).resolve().parents[1]/frozen.PLAN_PATH).read_text())
    value["portfolio"]["min_forecast_names"] = 2
    value["periods"][0]["arms"] = ["candidate", "action_only", *parent.PARENT_ARMS]
    return value


def coordinate(old, mu, downside, upper=1/6):
    return lab.coordinate_choice(old, mu, downside, 1/6, upper, .005, .006, 1e-12)


def test_entry_and_holding_have_distinct_economic_hurdles():
    assert coordinate(0, .015, 0)["weight"] == 0
    assert coordinate(.1, .015, 0)["weight"] == .1
    assert coordinate(.1, .004, 0)["weight"] == 0
    assert coordinate(0, .017, 0)["weight"] == 0  # tie keeps cash
    assert coordinate(.1, .005, 0)["weight"] == .1  # tie saves the sell
    assert coordinate(0, .03, .04)["weight"] == pytest.approx((.03-.017)/.04/6)
    assert coordinate(.1, .03, .04)["weight"] == .1  # no-trade interval
    assert coordinate(.1, .03, .08)["weight"] == pytest.approx((.03-.005)/.08/6)


def test_risk_increase_reduces_size_and_recovery_does_not_need_waiting():
    sizes = [coordinate(.1, .02, d)["weight"] for d in (0., .02, .04, .08)]
    assert sizes == sorted(sizes, reverse=True)
    assert sizes[-1] > 0
    assert coordinate(sizes[-1], .02, 0)["weight"] == 1/6


def test_coordinate_solution_dominates_independent_dense_grid():
    rng = random.Random(20261023)
    for _ in range(40):
        old, mu, d, upper = rng.uniform(0, .25), rng.uniform(-.03, .08), rng.uniform(0, .2), rng.uniform(0, 1/6)
        solved = coordinate(old, mu, d, upper)
        grid = [upper*i/500 for i in range(501)]
        objective = lambda w: (mu-.005)*w-.006*(abs(w-old)+w)-.5*d*w*w/(1/6)
        assert 0 <= solved["weight"] <= upper
        assert solved["utility"] == pytest.approx(objective(solved["weight"]))
        assert solved["utility"] >= max(map(objective, grid))-1e-12


@pytest.mark.parametrize("old,mu,risk,upper", [(0, float("nan"), 0, .1),
    (0, .03, -.01, .1), (-.1, .03, 0, .1), (.1, .03, 0, .2)])
def test_invalid_coordinate_cannot_be_safe_or_zero(old, mu, risk, upper):
    with pytest.raises(ValueError, match="invalid_coordinate"):
        coordinate(old, mu, risk, upper)


def test_pool_restrictions_position_count_and_portfolio_objective(spec):
    spec["portfolio"].update(max_positions=2, slot_weight=.5)
    spec["candidate"]["new_candidate_count"] = 2
    forecasts, previous = {"A": .05, "B": .04, "C": .035, "D": .03}, {"C": .2, "D": .2}
    risks = {t: .02 for t in forecasts}
    result = lab.choose(forecasts, previous, risks, spec)
    assert len(result["weights"]) <= 2 and sum(result["weights"].values()) <= 1
    assert result["coordinates"]["C"]["upper"] == .2  # held outside top cannot add
    objective = lambda w: sum((forecasts[t]-.005)*w.get(t, 0)-.006*(abs(w.get(t, 0)-previous.get(t, 0))+w.get(t, 0))
        -.5*risks[t]*w.get(t, 0)**2/.5 for t in forecasts)
    assert result["utility"] == pytest.approx(objective(result["weights"]))
    # Independent coarse portfolio oracle includes the cardinality restriction.
    for a, b, c, d in product((0, .25, .5), (0, .25, .5), (0, .1, .2), (0, .1, .2)):
        if sum(v > 0 for v in (a, b, c, d)) <= 2:
            assert result["utility"] >= objective(dict(zip("ABCD", (a, b, c, d))))-1e-12
    assert previous == {"C": .2, "D": .2} and forecasts["A"] == .05


@pytest.fixture
def market(spec):
    dates = frozen.calendar("2024-09-20", "2024-11-01")
    first = dates.index("2024-10-02")
    days = dates[first:first+6]
    spec["periods"][0].update(decision_start=days[0], decision_end=days[-1], tail_through=dates[-1])
    spec["portfolio"]["initial_cash_krw"] = 100000
    rows = {d: {t: {"TDD_CLSPRC": 100., "TDD_OPNPRC": 100., "LIST_SHRS": 1000000.,
        "ACC_TRDVOL": 1000000., "MKTCAP": 2000-i} for i, t in enumerate("AB")} for d in dates}
    inputs = frozen.Inputs({d: {"A": .04, "B": .03} for d in days}, {d: ["A"] for d in days}, rows,
        {d: dates[i-1] for i, d in enumerate(dates) if i}, {}, {})
    return spec, inputs


def test_downside_uses_only_past_adverse_moves_and_unit_consistency(market):
    _, inputs = market
    day = sorted(inputs.forecasts)[0]
    dates = parent.history_sessions(day)
    for d, close in zip(dates, (100., 110., 99., 99., 99., 99.)):
        inputs.rows[d]["A"]["TDD_CLSPRC"] = close
    ctx = lab.decision_context(inputs, day, inputs.forecasts[day])
    assert ctx["stocks"]["A"]["downside"] == pytest.approx(.1)
    assert ctx["stocks"]["A"]["return5"] == pytest.approx(-.01)
    for d in list(inputs.rows):
        if d > day:
            inputs.rows[d] = {}
    assert lab.decision_context(inputs, day, inputs.forecasts[day]) == ctx
    inputs.rows[dates[3]]["A"]["LIST_SHRS"] = 2000000
    missing = lab.decision_context(inputs, day, inputs.forecasts[day])
    assert missing["stocks"]["A"]["downside"] is None and missing["unknown"] == 1


def replay(spec, inputs, factory=None):
    return frozen.replay_group(inputs, spec, "75bp_full", 0, frozen.Budget(),
        target_selector=lab.selector(.004),
        forecast_required_arms=("candidate", "action_only", "prior_calibrated", "prior_cost_aware"),
        decision_context_factory=factory or (lambda d, f: lab.decision_context(inputs, d, f)))


def test_zero_downside_equals_structural_control_and_old_100_path_contract(market):
    spec, inputs = market
    oldspec = deepcopy(spec)
    oldspec["periods"][0]["arms"] = ["candidate", "prior_cost_aware", *frozen.CONTROLS]
    original = frozen.replay_group(inputs, oldspec, "75bp_full", 0, frozen.Budget(),
        target_selector=calibration.selector(.004), forecast_required_arms=("candidate", "prior_cost_aware"))
    result = replay(spec, inputs)
    assert all(m["complete"] for m in result["metrics"].values())
    assert result["metrics"]["candidate"] == result["metrics"]["action_only"]
    assert all(r["complete_match"] for r in parent.parent_crosscheck(result, original))
    assert all(r["decision"]["context"] is None for r in result["trace"] if r["decision"] and r["decision"]["terminal"])


def test_missing_required_downside_stops_all_arms_and_changes_are_hash_bound(market):
    spec, inputs = market
    def bad(d, f):
        ctx = lab.decision_context(inputs, d, f)
        ctx["stocks"]["A"]["downside"] = None
        return ctx
    result = replay(spec, inputs, bad)
    assert result["source_stop"]["field"] == "required_stock_downside_unknown"
    assert all(not m["complete"] and m["net_return_pct"] is None for m in result["metrics"].values())
    def risk(d, f):
        ctx = lab.decision_context(inputs, d, f)
        ctx["stocks"]["A"]["downside"] = .1
        return ctx
    ordinary, changed = replay(spec, inputs), replay(spec, inputs, risk)
    first = lambda g: next(r["decision"] for r in g["trace"] if r["decision"])
    assert first(ordinary)["input_id"] != first(changed)["input_id"]
    assert first(changed)["plans"]["candidate"]["weights"]["A"] < first(changed)["plans"]["action_only"]["weights"]["A"]


def test_adoption_requires_risk_effect_beyond_structural_control(spec):
    groups = [{"scenario": s, "phase": p, "metrics": {a: {"complete": True,
        "net_return_pct": 3. if a == "candidate" else 2., "price_mdd_pct": -3.}
        for a in spec["periods"][0]["arms"]}} for s in spec["execution"]["scenarios"] for p in range(5)]
    assert lab.verdict(groups, spec)["adoption"] == "conditional_development_support"
    for g in groups:
        g["metrics"]["action_only"]["net_return_pct"] = 4.
    assert lab.verdict(groups, spec)["adoption"] == "rejected"
    groups[0]["metrics"]["action_only"].update(complete=False, net_return_pct=None, price_mdd_pct=None)
    assert lab.verdict(groups, spec)["scenario_mean_price_return_pct"]["75bp_full"]["action_only"] is None
