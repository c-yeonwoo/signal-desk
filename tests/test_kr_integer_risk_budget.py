"""Executable integer sizing and as-of risk matching, without historical returns."""
from copy import deepcopy
from itertools import product
import json
from pathlib import Path
import random

import pytest

from scripts.measure import kr_integer_risk_budget as lab
from scripts.measure import kr_frozen_forecast_policy as frozen
from scripts.measure import kr_stock_downside_action as parent


@pytest.fixture
def spec():
    root = Path(__file__).resolve().parents[1]
    value = json.loads((root/frozen.PLAN_PATH).read_text())
    value["portfolio"]["min_forecast_names"] = 2
    value["periods"][0]["arms"] = json.loads((root/lab.PLAN_PATH).read_text())["comparison"]["arms"]
    return value


def context(prices=None, downside=.04, volume=100000):
    return {"session": "2024-10-02", "stocks": {t: {"close": p, "volume": volume,
        "downside": downside} for t, p in (prices or {"A": 100., "B": 150.}).items()}}


def test_integer_coordinate_matches_exhaustive_quantities(spec):
    rng = random.Random(20261023)
    for _ in range(100):
        h, lo, hi = rng.randrange(12), rng.randrange(4), rng.randrange(4, 16)
        row = dict(held=h, price=100., downside=rng.uniform(0, .2), minimum=lo, maximum=hi)
        mu = rng.uniform(-.03, .08)
        solved = lab.quantity_choice(row, mu, 10000, spec)
        values = {q: parent.coordinate_utility(q*.01, h*.01, mu, row["downside"], 1/6, .005, .006)
                  for q in range(lo, hi+1)}
        best = max(values.values())
        expected = min((q for q, v in values.items() if best-v <= 1e-12), key=lambda q: (abs(q-h), q))
        assert solved["quantity"] == expected
        assert solved["utility"] == pytest.approx(best, abs=1e-12)


def test_one_share_is_optional_not_forced_roundup(spec):
    row = dict(held=0, price=1000., downside=.04, minimum=0, maximum=1)
    # Continuous optimum is 0.75 shares: choose one only if its objective beats cash.
    assert lab.quantity_choice(row, .035, 10000, spec)["quantity"] == 1
    assert lab.quantity_choice(row, .025, 10000, spec)["quantity"] == 0
    assert lab.quantity_choice({**row, "maximum": 0}, .2, 10000, spec)["quantity"] == 0


def test_order_encoding_matches_every_reachable_integer(spec):
    ctx, nav = context({"A": 123.45}), 10000.
    for h in (0, 1, 5, 13, 20):
        row = lab.coordinates({"A"}, {"A"}, {"A": h}, nav, ctx, spec)["A"]
        for q in range(row["minimum"], row["maximum"]+1):
            weights, orders = lab.encode_quantities({"A": q}, {"A": h}, nav, ctx, spec)
            assert h+sum(o["shares"]*(1 if o["side"] == "buy" else -1) for o in orders) == q
            assert all(0 < w <= 1/6+1e-12 for w in weights.values())
    weights, _ = lab.encode_quantities({"A": 1}, {}, nav, ctx, spec)
    assert weights["A"] > ctx["stocks"]["A"]["close"]/nav  # reserve, not economic weight


def test_volume_residuals_and_cap_infeasibility_are_not_zero_risk(spec):
    ctx = context({"A": 100.}, volume=100.)
    row = lab.coordinates({"A"}, set(), {"A": 5}, 10000., ctx, spec)["A"]
    assert (row["minimum"], row["maximum"]) == (4, 5)
    with pytest.raises(frozen.DataGap) as caught:
        lab.coordinates({"A"}, {"A"}, {"A": 20}, 10000., ctx, spec)
    assert caught.value.detail["reason"] == "execution_constraints_infeasible"
    ctx["stocks"]["A"]["downside"] = None
    with pytest.raises(frozen.DataGap):
        lab.coordinates({"A"}, {"A"}, {}, 10000., ctx, spec)


def test_joint_integer_optimum_with_position_limit_and_mandatory_holding(spec):
    spec["portfolio"].update(max_positions=2, slot_weight=.5)
    spec["candidate"]["new_candidate_count"] = 2
    ctx, holdings = context({"A": 1000., "B": 1000., "C": 1000.}, volume=100.), {"C": 2}
    forecasts = {"A": .05, "B": .04, "C": .001}
    q, choice = lab.choose_quantities(forecasts, holdings, 10000., ctx, spec)
    assert q["C"] == 1 and sum(n > 0 for n in q.values()) <= 2
    rows = choice["coordinates"]
    objective = lambda target: sum(parent.coordinate_utility(target[t]*.1, holdings.get(t, 0)*.1,
        forecasts[t], .04, .5, .005, .006) for t in rows)
    for a, b, c in product(range(2), range(2), range(1, 3)):
        if sum(n > 0 for n in (a, b, c)) <= 2:
            assert objective(q) >= objective(dict(zip("ABC", (a, b, c))))-1e-12


def match_context(ctx, charge):
    ctx["candidate_risk_budget"] = {"session": ctx["session"], "charge": charge, "plan_sha256": "fixed"}
    return ctx


def test_matching_is_asof_largest_feasible_scale_not_realized_risk(spec):
    ctx = match_context(context(), .001)
    q, choice = lab.matched_quantities({"A": 1/6, "B": 1/6}, {}, 10000., ctx, spec)
    assert lab.risk_charge(q, 10000., ctx, spec) <= .001+1e-12
    assert 0 < choice["scale"] < 1 and choice["next_scale_feasible"] is False
    ctx["candidate_risk_budget"]["charge"] = 0
    q, _ = lab.matched_quantities({"A": 1/6, "B": 1/6}, {}, 10000., ctx, spec)
    assert q == {"A": 0, "B": 0}
    # Observed no downside is a zero proxy, NOT proof of no actual risk.
    for row in ctx["stocks"].values():
        row["downside"] = 0
    q, choice = lab.matched_quantities({"A": 1/6, "B": 1/6}, {}, 10000., ctx, spec)
    assert sum(q.values()) > 0 and choice["scale"] == 1


def test_matching_cannot_silently_discard_unsellable_holdings(spec):
    ctx = match_context(context(volume=0), 0.)
    with pytest.raises(frozen.DataGap) as caught:
        lab.matched_quantities({"B": 1/6}, {"A": 1}, 10000., ctx, spec)
    assert caught.value.detail["field"] == "matched_unavoidable_risk_above_ceiling"
    ctx["candidate_risk_budget"]["session"] = "2024-10-01"
    with pytest.raises(ValueError, match="must_precede"):
        lab.matched_quantities({}, {}, 10000., ctx, spec)


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


def replay(spec, inputs, factory=None):
    return frozen.replay_group(inputs, spec, "75bp_full", 0, frozen.Budget(),
        target_selector=lab.selector(.004), forecast_required_arms=("candidate", *lab.PARENT_ARMS),
        decision_context_factory=factory or (lambda d, f: lab.decision_context(inputs, d, f)))


def test_ten_arm_replay_keeps_parent_economics_and_context_binding(market):
    spec, inputs = market
    oldspec = deepcopy(spec)
    oldspec["periods"][0]["arms"] = list(lab.PARENT_ARMS.values())
    old = frozen.replay_group(inputs, oldspec, "75bp_full", 0, frozen.Budget(),
        target_selector=parent.selector(.004),
        decision_context_factory=lambda d, f: parent.decision_context(inputs, d, f))
    result = replay(spec, inputs)
    assert len(result["metrics"]) == 10 and all(m["complete"] for m in result["metrics"].values())
    assert all(r["complete_match"] for r in lab.parent_crosscheck(result, old))
    decisions = [r["decision"] for r in result["trace"] if r["decision"] and not r["decision"]["terminal"]]
    assert all(d["context"]["candidate_risk_budget"]["session"] == d["context"]["session"] for d in decisions)
    for d in decisions:
        plan = d["plans"]["candidate"]
        assert [{k: o[k] for k in expected} for o, expected in zip(plan["orders"], plan["choice"]["roundtrip_orders"])] == plan["choice"]["roundtrip_orders"]
        assert len(plan["orders"]) == len(plan["choice"]["roundtrip_orders"])
    assert replay(spec, inputs) == result  # fresh contexts, not stale budgets
    day = sorted(inputs.forecasts)[0]
    ctx = lab.decision_context(inputs, day, inputs.forecasts[day])
    for d in inputs.rows:
        if d > day:
            inputs.rows[d] = {}
    assert lab.decision_context(inputs, day, inputs.forecasts[day]) == ctx


def test_unknown_risk_common_stops_and_reused_context_fails(market):
    spec, inputs = market
    def bad(d, f):
        ctx = lab.decision_context(inputs, d, f)
        ctx["stocks"]["A"]["downside"] = None
        return ctx
    result = replay(spec, inputs, bad)
    assert all(not m["complete"] and m["net_return_pct"] is None for m in result["metrics"].values())
    def reused(d, f):
        return match_context(lab.decision_context(inputs, d, f), 0.)
    with pytest.raises(ValueError, match="reused_candidate"):
        replay(spec, inputs, reused)


def test_verdict_does_not_confuse_risk_match_with_full_budget_gate(spec):
    def groups(usage):
        return [{"scenario": s, "phase": p, "metrics": {a: {"complete": True,
            "net_return_pct": 3. if a == "candidate" else 4. if a == "cap6" else 2., "price_mdd_pct": -3.}
            for a in spec["periods"][0]["arms"]}, "trace": [{"decision": {"terminal": False,
                "context": {"candidate_risk_budget": {"charge": .001}},
                "plans": {a: {"choice": {"planned_charge": .001*usage}} for a in lab.MATCHED}}}]}
            for s in spec["execution"]["scenarios"] for p in range(5)]
    result = lab.verdict(groups(.8), spec)
    assert result["decision"] == "conditional_risk_budget_support"
    assert result["legacy_full_budget"]["decision"] == "observed_rejection"
    assert lab.verdict(groups(.79), spec)["decision"] == "risk_match_insufficient"
    bad = groups(.8)
    bad[0]["metrics"]["candidate"]["net_return_pct"] = 1.
    assert lab.verdict(bad, spec)["decision"] == "rejected"
