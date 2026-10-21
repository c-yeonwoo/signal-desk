"""Synthetic missing-data bounds, exact target clock and no future reranking."""
from copy import deepcopy
from itertools import product
from statistics import median

import pytest

from scripts.measure import kr_forecast_error_attribution as audit
from scripts.measure import kr_frozen_forecast_policy as frozen


@pytest.mark.parametrize("known,missing", [([1., 3., 5.], 1), ([1., 3., 5., 7.], 1),
    ([1., 3., 5.], 2), ([1., 3., 5., 7.], 2), ([1., 2., 3.], 0)])
def test_median_bounds_contain_all_missing_completions(known, missing):
    lo, hi = audit.median_bounds(known, missing)
    for unknown in product([-100., 0., 2., 4., 8., 100.], repeat=missing):
        actual = median([*known, *unknown])
        assert lo <= actual <= hi
    assert lo == median([*known, *([-100.]*missing)])
    assert hi == median([*known, *([100.]*missing)])


def test_unbounded_median_and_invalid_inputs_are_not_zero():
    assert audit.median_bounds([], 2) == [None, None]
    assert audit.median_bounds([1.], 2) == [None, None]
    with pytest.raises(ValueError):
        audit.median_bounds([float("nan")], 0)
    with pytest.raises(ValueError):
        audit.median_bounds([1.], -1)


def test_pairwise_bounds_keep_unknown_pairs_and_half_ties():
    out = audit.pairwise_bounds([.2, .1], [.3, .1, None])
    assert out["pairs"] == 6 and out["unknown_pairs"] == 2 and out["ties"] == 1
    assert out["bounds"] == pytest.approx([.25, 3.5/6])
    assert audit.pairwise_bounds([.1], [None])["bounds"] == [0., 1.]
    assert audit.pairwise_bounds([], [.1])["bounds"] == [None, None]


def test_center_relative_errors_cancel_at_opposite_ends():
    out = audit.error_bounds(.02, -.04, .005, [-.03, -.02])
    assert out["total_error"] == pytest.approx(.06)
    assert out["center_error_bounds"] == pytest.approx([.025, .035])
    assert out["relative_error_bounds"] == pytest.approx([.025, .035])
    assert out["center_error_bounds"][0]+out["relative_error_bounds"][1] == pytest.approx(.06)
    assert audit.category(-.01, [-.03, -.02]) == "negative_proxy_but_relative_outperformance"
    assert audit.category(-.04, [-.03, -.02]) == "proxy_and_selection_weak"
    assert audit.category(-.01, [.001, .002]) == "selection_loss_in_positive_proxy"
    assert audit.category(-.01, [-.02, .02]) == "ambiguous_negative"
    assert audit.category(None, [-.02, .02]) == "unknown_top6"


def test_missing_top_label_never_reranks_or_averages_only_five():
    forecasts = {str(i): .03-i*.001 for i in range(8)}
    caps = {str(i): 100-i for i in range(8)}
    labels = {str(i): {"gross_return": i*.01} for i in range(8)}
    first = audit.day_record("2024-11-01", forecasts, caps, labels)
    changed = deepcopy(labels)
    changed["0"]["gross_return"] = None
    changed["7"]["gross_return"] = 100.
    out = audit.day_record("2024-11-01", forecasts, caps, changed)
    assert out["top6"] == first["top6"] == [str(i) for i in range(6)]
    assert out["cap6"] == first["cap6"]
    assert out["top6_return_mean"] is None and out["cap6_return_mean"] is None
    summary = audit.summarize_days([first, out])
    assert summary["top6_return_mean"] is None


def test_unknown_bin_mean_is_explicit_and_negative_fraction_includes_unknown():
    rows = [{"forecast": .02, "label": {"gross_return": r}} for r in (-.1, .1, None)]
    out = audit.summarize_rows(rows)
    assert out["full_mean_gross_return"] is None
    assert out["known_only_mean_gross_return"] == pytest.approx(0.)
    assert out["known_only_mean_error"] == pytest.approx(.02)
    assert out["negative_fraction_bounds"] == pytest.approx([1/3, 2/3])


def market():
    day = "2024-11-01"
    sessions = [day, *frozen.market_clock.next_sessions("kr", day, 6)]
    rows = {d: {"A": {"LIST_SHRS": 100., "TDD_OPNPRC": 100., "TDD_CLSPRC": 500.}} for d in sessions}
    rows[sessions[5]]["A"]["TDD_OPNPRC"] = 130.
    rows[sessions[6]]["A"]["TDD_OPNPRC"] = 120.
    return day, sessions, frozen.Inputs({}, {}, rows, {d: sessions[i-1] for i, d in enumerate(sessions) if i}, {}, {})


def test_original_open_to_open_clock_not_decision_close_or_t_plus5():
    day, sessions, data = market()
    out = audit.target_label(data, day, "A")
    assert out["entry_on"] == sessions[1] and out["exit_on"] == sessions[6]
    assert out["gross_return"] == pytest.approx(.2)
    data.rows[sessions[6]]["A"]["TDD_OPNPRC"] = 0.
    assert audit.target_label(data, day, "A")["gross_return"] is None


def test_entry_after_unit_change_is_not_a_holding_conversion():
    day, sessions, data = market()
    for d in sessions[1:]:
        data.rows[d]["A"]["LIST_SHRS"] = 200.
    assert audit.target_label(data, day, "A")["gross_return"] == pytest.approx(.2)
    # A change after entering requires the exact already-reviewed unchanged-holder fact.
    for d in sessions[3:]:
        data.rows[d]["A"]["LIST_SHRS"] = 300.
    assert audit.target_label(data, day, "A")["gross_return"] is None
    data.reviewed[sessions[3], "A"] = {"holder_quantity_effect": "unchanged",
        "previous_session": sessions[2], "listed_shares_before": 200., "listed_shares_after": 300.}
    assert audit.target_label(data, day, "A")["gross_return"] == pytest.approx(.2)
