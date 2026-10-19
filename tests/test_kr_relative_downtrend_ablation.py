import copy

import pytest

from scripts.measure.kr_relative_downtrend_ablation import (
    ARMS, compare_daily, evaluate, select, slot_result, summarize, support_checks,
)


def rows():
    return [{"ticker": f"{i:06d}", "name": str(i), "recent20_pct": float(i),
             "downtrend_confirmed": i == 0, "downtrend_blocking": False,
             "horizons": {"5": {"state": "matured", "net_pct": -6.0 if i == 0 else 3.0}}}
            for i in range(7)]


def test_one_ablation_does_not_refill_rejected_slot():
    arms, removed = select(rows(), ["000000", "000001"])
    assert removed == ["000000"]
    assert arms["legacy"] == ["000000", "000001"]
    assert arms["candidate"] == ["000001"]
    assert arms["simple_momentum"] == [f"{i:06d}" for i in range(6, 0, -1)]


def test_selection_does_not_use_outcomes():
    original = rows()
    modified = copy.deepcopy(original)
    for r in modified:
        r["horizons"] = {"5": {"state": "matured", "net_pct": 99999}}
    assert select(original, ["000000", "000001"]) == select(modified, ["000000", "000001"])


def test_ties_are_ticker_sorted():
    data = rows()
    for r in data:
        r["recent20_pct"] = 1
    arms, _ = select(list(reversed(data)), [])
    assert arms["simple_momentum"] == [f"{i:06d}" for i in range(6)]


@pytest.mark.parametrize("field", ["downtrend_confirmed", "downtrend_blocking", "recent20_pct"])
def test_unknown_decision_input_is_not_false_or_dropped(field):
    data = rows()
    data[0][field] = None
    with pytest.raises(ValueError, match="unknown"):
        select(data, ["000000"])


def test_gated_not_relaxed_stock_is_not_newly_removed():
    data = rows()
    data[0]["downtrend_blocking"] = True
    arms, removed = select(data, ["000000"])
    assert removed == []
    assert arms["candidate"] == ["000000"]


def test_cash_capacity_and_additional_cost_only_on_trades():
    by_ticker = {r["ticker"]: r for r in rows()}
    assert slot_result([], by_ticker, 75)["slot_net_pct"] == 0
    assert slot_result(["000001"], by_ticker, 25)["slot_net_pct"] == .5
    assert slot_result(["000001"], by_ticker, 75)["slot_net_pct"] == pytest.approx(2.5 / 6)


def test_missing_selected_outcome_prevents_full_mean_not_cash():
    data = rows()
    data[0]["horizons"]["5"] = {"state": "missing_entry_open"}
    by_ticker = {r["ticker"]: r for r in data}
    unresolved = slot_result(["000000", "000001"], by_ticker, 25)
    complete = slot_result([], by_ticker, 25)
    assert unresolved["slot_net_pct"] is None
    assert unresolved["missing"] == [{"ticker": "000000", "state": "missing_entry_open"}]
    daily = [{"arms": {"legacy": unresolved, "candidate": complete}},
             {"arms": {"legacy": complete, "candidate": complete}}]
    assert summarize(daily, "legacy")["equal_day_slot_net_pct"] is None
    comparison = compare_daily(daily, "legacy")
    assert comparison["equal_day_delta_pp"] is None
    assert comparison["complete_paired_dates"] == 1


def fixture():
    names = ["window"]
    day = "2024-04-01"
    scope = {"registered_verdict": False, "strict_pit_eligible": False,
             "windows": {"window": {"selection": {"rows": [{**r, "date": day} for r in rows()]}}}}
    selection = {"registered_verdict": False, "strict_pit_eligible": False,
                 "windows": {"window": {"daily": [{"date": day, "annual_financial": {"buy_tickers": ["000000", "000001"]}}]}}}
    plan = {"candidate_id": "test", "windows": names, "protected_start": "2026-08-05",
            "decision_dates": 1, "comparison": {"cost_bps": [25, 50, 75]}, "limits": []}
    return scope, selection, plan


def test_improvement_alone_is_not_support_or_production_promotion():
    report = evaluate(*fixture())
    assert report["affected_labels"] == 1
    assert report["avoided_loss_labels_25bp"] == 1
    assert report["summary"]["25"]["equal_window_slot_net_pct"]["candidate"] == .5
    assert report["summary"]["25"]["equal_window_slot_net_pct"]["simple_momentum"] == 3
    assert report["development_supported"] is False
    assert report["production_changed"] is False
    assert report["live_order_eligible"] is False


def test_missed_winners_and_unresolved_affected_are_preserved():
    scope, selection, plan = fixture()
    scope["windows"]["window"]["selection"]["rows"][0]["horizons"]["5"]["net_pct"] = 6
    report = evaluate(scope, selection, plan)
    assert report["missed_winner_labels_25bp"] == 1
    scope["windows"]["window"]["selection"]["rows"][0]["horizons"]["5"] = {"state": "price_gap"}
    report = evaluate(scope, selection, plan)
    assert report["affected_unresolved_labels"] == 1
    assert report["checks"]["complete_all_dates"] is False
    assert report["windows"]["window"]["costs"]["25"]["vs_legacy"]["equal_day_delta_pp"] is None


def test_registered_dates_are_rejected_before_selection():
    scope, selection, plan = fixture()
    for r in scope["windows"]["window"]["selection"]["rows"]:
        r["date"] = "2026-08-05"
    selection["windows"]["window"]["daily"][0]["date"] = "2026-08-05"
    with pytest.raises(ValueError, match="protected"):
        evaluate(scope, selection, plan)


def test_ticker_concentration_uses_same_window_weight_as_primary():
    windows = {}
    for name, dates in (("short", 10), ("long", 100)):
        windows[name] = {"dates": dates, "costs": {"25": {
            "arms": {arm: {"unresolved_dates": 0, "equal_day_slot_net_pct": 0} for arm in ARMS},
            "vs_legacy": {"equal_day_delta_pp": 0, "phase_deltas_pp": [0] * 5}}}}
    affected = [{"window": name, "ticker": name, "h5": {"state": "matured", "net_pct": 6}}
                for name in windows]
    _, summary = support_checks(windows, affected, [25])
    assert summary["25"]["largest_ticker_absolute_impact_share"] == pytest.approx(10 / 11)


def test_concentration_aggregates_signed_contributions_before_absolute_value():
    windows = {"one": {"dates": 10, "costs": {"25": {
        "arms": {arm: {"unresolved_dates": 0, "equal_day_slot_net_pct": 0} for arm in ARMS},
        "vs_legacy": {"equal_day_delta_pp": 0, "phase_deltas_pp": [0] * 5}}}}}
    affected = [{"window": "one", "ticker": ticker, "h5": {"state": "matured", "net_pct": net}}
                for ticker, net in (("A", 6), ("A", -6), ("B", 1))]
    _, summary = support_checks(windows, affected, [25])
    assert summary["25"]["largest_ticker_absolute_impact_share"] == 1
