import pytest

from scripts.measure.research_entry_edge_audit import summarize_window


def _pilot():
    day = "2024-04-01"
    rows = [{"date": day, "ticker": f"{index:06d}",
             "kind": "BUY" if index == 0 else "HOLD",
             "outcomes": {"1": {"state": "matured", "net_pct": 1.0 if index == 0 else 0.0}}}
            for index in range(150)]
    return {"registered_verdict": False,
            "source_level": "C_offline_price_only_current_engine",
            "signal_dates": [day], "rows": rows,
            "cohort_comparison": {f"{day}:5": {"n": 150, "mean_net_pct": 0.5}},
            "buy_episodes": {"count": 1, "episodes": [{
                "ticker": "000000", "first_buy_date": day, "left_censored": False,
                "h5": {"state": "matured", "net_pct": -2.0},
                "h20": {"state": "matured", "net_pct": 4.0},
                "h5_share_status": "listed_shares_unchanged_actions_unverified",
                "h20_share_status": "listed_shares_unchanged_actions_unverified"}]}}


def test_repeated_label_and_first_entry_are_separate_measures():
    report = summarize_window(_pilot())
    assert report["daily_h1"]["buy_labels"] == 1
    assert report["daily_h1"]["equal_day_buy_mean_net_pct"] == 1.0
    assert report["daily_h1"]["days_buy_mean_ge_3pct"] == 0
    assert report["first_buy_episodes"]["count"] == 1
    assert report["first_buy_episodes"]["h5_mean_excess_vs_same_day_cohort_pp"] == -2.5
    assert report["share_count_stable_subset_actions_unverified"]["count"] == 1


def test_missing_share_flag_is_reported_not_silently_dropped():
    pilot = _pilot()
    pilot["buy_episodes"]["episodes"][0]["h5_share_status"] = "listed_shares_changed"
    report = summarize_window(pilot)
    assert report["first_buy_episodes"]["count"] == 1
    assert report["share_count_stable_subset_actions_unverified"]["count"] == 0
    assert report["share_count_changed_or_missing"] == 1


def test_registered_period_is_never_scored():
    pilot = _pilot()
    pilot["signal_dates"] = ["2026-08-05"]
    with pytest.raises(ValueError, match="development scope"):
        summarize_window(pilot)


def test_incomplete_daily_comparison_is_not_averaged():
    pilot = _pilot()
    pilot["rows"] = pilot["rows"][:149]
    with pytest.raises(ValueError, match="incomplete daily"):
        summarize_window(pilot)
