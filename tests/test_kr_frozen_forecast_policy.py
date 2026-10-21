"""Synthetic execution and verdict boundaries; never run preserved market data."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from scripts.measure import kr_frozen_forecast_policy as lab


@pytest.fixture
def spec():
    return json.loads((Path(__file__).resolve().parents[1] / lab.PLAN_PATH).read_text())


def fixture_market(spec):
    spec = deepcopy(spec)
    days = lab.calendar("2024-09-23", "2024-11-30")
    start = days.index("2024-10-02")
    sessions = days[start:start+15]
    spec["periods"][0].update(decision_start=sessions[0], decision_end=sessions[5],
                              tail_through=sessions[-1])
    spec["portfolio"].update(initial_cash_krw=100000, max_positions=1,
                             min_forecast_names=2, slot_weight=1., keep_name_weight_cap=1.)
    spec["candidate"]["new_candidate_count"] = 1
    rows = {d: {t: {"TDD_OPNPRC": 100., "TDD_CLSPRC": 100., "LIST_SHRS": 1000000,
                      "ACC_TRDVOL": 1000000, "MKTCAP": cap}
                 for t, cap in (("A", 20000), ("B", 10000))} for d in days}
    forecasts = {d: {"A": .018, "B": .020} for d in sessions[:6]}
    data = lab.Inputs(forecasts, {d: ["A"] for d in forecasts}, rows,
                      {d: days[i-1] for i, d in enumerate(days) if i}, {}, {})
    return spec, data, sessions


def replay(spec, data, scenario="75bp_full"):
    return lab.replay_group(data, spec, scenario, 0, lab.Budget())


def test_cost_region_keeps_small_advantage_but_exits_bad_holding(spec):
    spec, data, _ = fixture_market(spec)
    forecasts = {"A": .018, "B": .020}
    held = lab.targets("candidate", forecasts, {"A": 2, "B": 1}, [],
                       {"A": 1000}, {"A": 100, "B": 100}, 100000, spec)
    assert held[0] == {"A": 1.}
    empty = lab.targets("candidate", forecasts, {"A": 2, "B": 1}, [],
                        {}, {"A": 100, "B": 100}, 100000, spec)
    assert empty[0] == {"B": 1.}
    assert lab.targets("candidate", {"A": 0., "B": .001}, {"A": 2, "B": 1}, [],
                       {"A": 1000}, {"A": 100, "B": 100}, 100000, spec)[0] == {}


def test_held_forecast_missing_is_not_a_zero_forecast(spec):
    spec, _, _ = fixture_market(spec)
    with pytest.raises(ValueError, match="held_forecast_missing"):
        lab.targets("candidate", {"B": .03, "C": .02}, {"B": 2, "C": 1}, [],
                    {"A": 1}, {"A": 100}, 100000, spec)


def test_all_books_reconcile_and_terminal_submitted_once(spec):
    spec, data, sessions = fixture_market(spec)
    result = replay(spec, data)
    assert result["source_stop"] is None
    assert sum(bool(r["decision"] and r["decision"]["terminal"]) for r in result["trace"]) == 1
    for metric in result["metrics"].values():
        assert metric["complete"] and metric["net_pnl_krw"] < 0
        assert metric["net_pnl_krw"] == pytest.approx(-metric["fees_krw"])
        assert sum(metric["pnl_by_ticker"].values()) == pytest.approx(metric["net_pnl_krw"])
        assert not metric["end_state"]["holdings"] and not metric["end_state"]["settlements"]
        assert all(f["decision_on"] < f["on"] for f in metric["fills"])
    assert result["trace"][-1]["session"] == sessions[-1]
    for delta in result["deltas"].values():
        assert delta["net_pnl_krw"] == pytest.approx(delta["before_cost_price_pnl_krw"]-delta["fees_krw"])


@pytest.mark.parametrize("scenario", ["75bp_full", "120bp_full", "75bp_half", "75bp_delay1"])
def test_fill_cost_partial_delay_and_real_t2_settlements(spec, scenario):
    spec, data, sessions = fixture_market(spec)
    result = replay(spec, data, scenario)
    metric = result["metrics"]["candidate"]
    assert metric["complete"]
    first = metric["fills"][0]
    assert first["fee"] == pytest.approx(first["shares"]*first["price"]*int(scenario.split("bp")[0])/20000)
    expected = sessions[2] if scenario.endswith("delay1") else sessions[1]
    assert first["on"] == expected
    intended = result["trace"][0]["decision"]["plans"]["candidate"]["orders"][0]["shares"]
    assert first["shares"] == (intended//2 if scenario.endswith("half") else intended)
    on_fill = next(r for r in result["trace"] if r["session"] == first["on"])
    assert on_fill["books"]["candidate"]["settled_cash"] == 100000
    assert on_fill["books"]["candidate"]["buying_power"] < 100000


def test_future_open_cannot_resize_prior_orders_and_gap_limit_cancels(spec):
    spec, data, sessions = fixture_market(spec)
    original = replay(spec, data)
    changed = deepcopy(data)
    for t in ("A", "B"):
        changed.rows[sessions[1]][t]["TDD_OPNPRC"] = 50.
    cheaper = replay(spec, changed)
    assert original["trace"][0]["decision"] == cheaper["trace"][0]["decision"]
    first = cheaper["metrics"]["candidate"]["fills"][0]
    intent = cheaper["trace"][0]["decision"]["plans"]["candidate"]["orders"][0]
    assert first["shares"] == intent["shares"]
    for t in ("A", "B"):
        changed.rows[sessions[1]][t]["TDD_OPNPRC"] = 102.
    cancelled = replay(spec, changed)
    assert cancelled["trace"][0]["decision"] == original["trace"][0]["decision"]
    assert all(f["on"] != sessions[1] for m in cancelled["metrics"].values() for f in m["fills"])


def test_own_account_nav_sizes_each_arm(spec):
    spec, data, sessions = fixture_market(spec)
    for day in sessions[2:]:
        data.rows[day]["B"].update(TDD_OPNPRC=110., TDD_CLSPRC=110.)
    result = replay(spec, data)
    day = result["trace"][5]
    assert day["decision"]["plans"]["candidate"]["sizing_nav"] == day["books"]["candidate"]["nav"]
    assert day["decision"]["plans"]["legacy_selection"]["sizing_nav"] == day["books"]["legacy_selection"]["nav"]
    assert day["books"]["candidate"]["nav"] != day["books"]["legacy_selection"]["nav"]


def test_unknown_unit_change_stops_common_group_without_return(spec):
    spec, data, sessions = fixture_market(spec)
    data.rows[sessions[1]]["B"]["LIST_SHRS"] *= 2
    result = replay(spec, data)
    assert result["source_stop"]["field"] == "unreviewed_investment_unit_change"
    assert all(not m["complete"] and m["net_return_pct"] is None for m in result["metrics"].values())
    assert all(not m["fills"] for m in result["metrics"].values())
    assert all(d is None for d in result["deltas"].values())


def test_reviewed_count_change_never_scales_holder_shares(spec):
    spec, data, sessions = fixture_market(spec)
    for day in sessions[1:]:
        data.rows[day]["B"]["LIST_SHRS"] = 2000000
    data.reviewed[(sessions[1], "B")] = {"holder_quantity_effect": "unchanged",
        "previous_session": sessions[0], "listed_shares_before": 1000000, "listed_shares_after": 2000000}
    result = replay(spec, data)
    assert result["source_stop"] is None
    first = result["metrics"]["candidate"]["fills"][0]
    intended = result["trace"][0]["decision"]["plans"]["candidate"]["orders"][0]["shares"]
    assert first["shares"] == intended
    data.reviewed[(sessions[1], "B")]["listed_shares_after"] += 1
    assert replay(spec, data)["source_stop"]["field"] == "unreviewed_investment_unit_change"


def test_missing_close_preserves_confirmed_open_fills_but_not_nav(spec):
    spec, data, sessions = fixture_market(spec)
    data.rows[sessions[1]]["B"]["TDD_CLSPRC"] = None
    result = replay(spec, data)
    assert result["source_stop"]["field"] == "TDD_CLSPRC"
    assert result["metrics"]["candidate"]["fills"][0]["on"] == sessions[1]
    assert all(not m["complete"] and m["net_pnl_krw"] is None for m in result["metrics"].values())


def test_volume_limited_terminal_residue_is_incomplete_not_zero_return(spec):
    spec, data, sessions = fixture_market(spec)
    for day in sessions[10:]:
        for t in ("A", "B"):
            data.rows[day][t]["ACC_TRDVOL"] = 100
    result = replay(spec, data)
    assert result["source_stop"] is None
    assert result["metrics"]["candidate"]["end_state"]["holdings"]
    assert result["metrics"]["candidate"]["net_return_pct"] is None
    assert sum(bool(r["decision"] and r["decision"]["terminal"]) for r in result["trace"]) == 1


def test_mutated_frozen_input_is_rejected(tmp_path):
    path = tmp_path / "frozen.json"
    path.write_text('{"value":1}')
    expected = lab.sha(path.read_bytes())
    assert lab.fixed_bytes(path, expected)
    path.write_text('{"value":2}')
    with pytest.raises(ValueError, match="frozen_file_hash_differs"):
        lab.fixed_bytes(path, expected)


def passing_groups(spec):
    return [{"scenario": scenario, "phase": phase,
             "metrics": {a: {"complete": True, "net_return_pct": 2. if a == "candidate" else .5,
                              "price_mdd_pct": -2.} for a in spec["periods"][0]["arms"]}}
            for scenario in spec["execution"]["scenarios"] for phase in spec["schedule"]["phases"]]


def test_verdict_requires_full_grid_and_finite_complete_metrics(spec):
    groups = passing_groups(spec)
    assert lab.verdict(groups, spec)["decision"] == "conditional_development_support"
    for altered in ([], groups[:-1], groups[:-1]+[groups[0]]):
        with pytest.raises(ValueError, match="exact_frozen_grid"):
            lab.verdict(altered, spec)
    groups[0]["metrics"]["candidate"]["net_return_pct"] = None
    with pytest.raises(ValueError, match="finite_metrics"):
        lab.verdict(groups, spec)


def test_incomplete_path_never_disappears_from_mean_or_becomes_support(spec):
    groups = passing_groups(spec)
    groups[0]["metrics"]["candidate"].update(complete=False, net_return_pct=None, price_mdd_pct=None)
    verdict = lab.verdict(groups, spec)
    assert verdict["decision"] == "not_evaluable_source_or_tail"
    assert verdict["scenario_mean_price_return_pct"]["75bp_full"]["candidate"] is None
    groups[5]["metrics"]["candidate"]["net_return_pct"] = -.1
    assert lab.verdict(groups, spec)["decision"] == "observed_rejection"


def test_stress_uses_phase_mean_but_every_phase_drawdown_limit(spec):
    groups = passing_groups(spec)
    stress = next(g for g in groups if g["scenario"] == "75bp_half")
    stress["metrics"]["candidate"]["net_return_pct"] = -1.
    assert lab.verdict(groups, spec)["decision"] == "conditional_development_support"
    stress["metrics"]["candidate"]["price_mdd_pct"] = -10.01
    assert lab.verdict(groups, spec)["decision"] == "observed_rejection"
