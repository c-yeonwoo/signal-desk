"""Focused algebra/join checks; never read market files or rerun a strategy."""
from copy import deepcopy

import pytest

from scripts.measure import kr_frozen_forecast_policy as frozen
from scripts.measure import kr_holding_intent_diagnostic as audit


def fixture():
    candidate = {"return_buffer": .005, "decision_one_way_cost": .006,
        "risk_aversion": 0., "new_candidate_count": 6, "objective_tie_tolerance": 1e-12}
    spec = {"candidate": candidate}
    forecasts = {"A": .01, **{f"B{i}": .03 for i in range(6)}}
    book = {"nav": 1000., "holdings": {"A": 1}}
    previous = {"A": .1}
    plan = {"weights": previous, "choice": {"utility": audit.utility(previous, previous, forecasts, candidate)}, "orders": []}
    latest = {"A": {"filled_on": "2024-10-04", "decision_on": "2024-10-02", "forecast_gross": .022}}
    return spec, forecasts, book, plan, latest


def test_hysteresis_boundaries_follow_existing_objective_not_fitted_thresholds():
    spec, _, _, _, _ = fixture()
    c = spec["candidate"]
    for mu in (.004, .005, .01, .017, .03):
        forecasts = {"A": mu}
        hold = audit.utility({"A": .1}, {"A": .1}, forecasts, c)
        cash = audit.utility({}, {"A": .1}, forecasts, c)
        enter = audit.utility({"A": .1}, {}, forecasts, c)
        assert hold-cash == pytest.approx(.1*(mu-.005))
        assert enter == pytest.approx(.1*(mu-.017))


def test_weak_outside_rank_holding_explanation_does_not_predict_exit_fill():
    spec, forecasts, book, plan, latest = fixture()
    row, = audit.decision_rows("2024-11-01", book, plan, forecasts, {"A": 100.}, latest, spec)
    assert row["retained"] and row["outside_top6"] and row["below_new_cost_hurdle"]
    assert not row["below_hold_cash_boundary"] and not row["removal_improves_objective"]
    assert row["forecast_change_since_latest_buy_decision"] == pytest.approx(-.012)
    assert row["submitted_sell_shares"] == 0
    exit_plan = deepcopy(plan)
    exit_plan.update(weights={}, choice={"utility": audit.utility({}, {"A": .1}, forecasts, spec["candidate"])})
    exit_row, = audit.decision_rows("2024-11-01", book, exit_plan, forecasts, {"A": 100.}, latest, spec)
    assert not exit_row["retained"] and exit_row["shares"] == 1
    assert exit_row["submitted_sell_shares"] == 0  # e.g. volume limit, not a completed exit


def test_menu_constraint_diagnostic_is_objective_only_not_a_new_trade():
    spec, forecasts, book, plan, latest = fixture()
    forecasts["A"] = .001
    plan["choice"]["utility"] = audit.utility(plan["weights"], {"A": .1}, forecasts, spec["candidate"])
    row, = audit.decision_rows("2024-11-01", book, plan, forecasts, {"A": 100.}, latest, spec)
    assert row["removal_improves_objective"]
    assert row["remove_target_utility_delta"] == pytest.approx(.0004)
    assert plan["orders"] == [] and book["holdings"] == {"A": 1}


def test_missing_forecast_or_mismatched_choice_is_not_zero_signal():
    spec, forecasts, book, plan, latest = fixture()
    del forecasts["A"]
    with pytest.raises(ValueError, match="forecast_missing"):
        audit.decision_rows("2024-11-01", book, plan, forecasts, {"A": 100.}, latest, spec)
    forecasts["A"] = .011
    with pytest.raises(ValueError, match="utility_mismatch"):
        audit.decision_rows("2024-11-01", book, plan, forecasts, {"A": 100.}, latest, spec)


def test_missing_forward_prices_are_null_and_never_a_subset_mean():
    inputs = frozen.Inputs({}, {}, {}, {}, {}, {})
    label = audit.forward_label("2024-11-01", "A", inputs)
    assert label["price_return_pct"] is None and label["missing_reason"]
    out = audit.bucket([{"h5": label}, {"h5": {"price_return_pct": 3.}}])
    assert out == {"rows": 2, "known_h5": 1, "missing_h5": 1,
                   "negative_h5": 0, "mean_h5_price_return_pct": None}


def test_unit_change_is_not_silently_used_for_forward_return():
    days = ["2024-11-01", *frozen.market_clock.next_sessions("kr", "2024-11-01", 5)]
    rows = {d: {"A": {"LIST_SHRS": 100., "TDD_CLSPRC": 100.}} for d in days}
    rows[days[2]]["A"]["LIST_SHRS"] = 200.
    data = frozen.Inputs({}, {}, rows, {d: days[i-1] for i, d in enumerate(days) if i}, {}, {})
    label = audit.forward_label(days[0], "A", data)
    assert label["price_return_pct"] is None
    assert label["missing_reason"]["field"] == "unreviewed_investment_unit_change"
