"""Synthetic boundary checks only; no preserved-market execution or network."""

import copy
import datetime as dt
import importlib.util
from pathlib import Path
import sys

import pytest

# Explicit staged source loading prevents accidentally testing controller/repo code.
TARGET = Path(__file__).resolve().parents[1] / "scripts/measure/kr_ridge_turnover_diagnostic.py"
loader = importlib.util.spec_from_file_location("staged_r3_turnover_diagnostic", TARGET)
diag = importlib.util.module_from_spec(loader)
sys.modules[loader.name] = diag
loader.loader.exec_module(diag)

DAY = "2024-10-02"


def book(scenario="75bp_full"):
    return diag.ExecutionBook({
        "scenarios": ["75bp_full", "75bp_buy_half", "75bp_none", "75bp_sell_half"],
        "initial_cash_krw": 100000, "evaluation_start": DAY,
        "evaluation_end": "2024-12-30", "liquidation_through": "2025-01-31",
        "max_positions": 6, "buy_limit_above_close": .01,
        "decision_one_way_cost": .006, "prior_volume_participation": .01,
        "sell_residual_attempts": 5}, scenario)


def decision(*, nav=100000, target=True):
    return diag.CommonDecision(DAY, nav, (("A", 1),) if target else (),
        (("A", 100),), (("A", 100000),), .1)


def observation(price=100):
    return {"A": {"price": price, "open_tradable": True, "source_id": "synthetic"}}


def pair(scenario="75bp_full", held=100):
    books = {arm: book(scenario) for arm in ("forced", "net")}
    for b in books.values():
        b.holdings = {"A": held} if held else {}
        b.settled_cash = 100000 - held * 100
    return books


def next_day():
    return diag.market_clock.next_sessions("kr", DAY, 1)[0]


def test_target_import_is_sibling_implementation():
    assert Path(diag.__file__).resolve() == TARGET.resolve()
    assert TARGET.name == "kr_ridge_turnover_diagnostic.py"


def test_100_to_98_cost_cash_quantities_exposure_and_t2():
    books = pair()
    initial = {a: b.state() for a, b in books.items()}
    orders = diag.submit_pair(books, decision())
    assert orders["net"] == []
    assert [(o["side"], o["shares"]) for o in orders["forced"]] == [("sell", 100), ("buy", 98)]
    for arm, b in books.items():
        b.open_session(next_day(), observation())
        assert diag.reconcile_book(b, initial[arm])
    forced, net = books["forced"], books["net"]
    assert forced.holdings == {"A": 98} and net.holdings == {"A": 100}
    assert forced.fees == pytest.approx(74.25) and net.fees == 0
    assert net.price_nav({"A": 100}) - forced.price_nav({"A": 100}) == pytest.approx(74.25)
    assert forced.settled_cash == 90000
    assert len(forced.settlements) == 2
    metrics = diag.book_metrics(forced, {"A": 100})
    assert metrics["exposure_krw"] == 9800
    assert metrics["turnover_initial_cash"] == pytest.approx(.198)
    for d in diag.market_clock.next_sessions("kr", next_day(), 2):
        forced.open_session(d, {})
    assert forced.settlements == []
    assert diag.reconcile_book(forced, initial["forced"])
    forced.settled_cash += 1
    with pytest.raises(ValueError, match="reconciliation"):
        diag.reconcile_book(forced, initial["forced"])


def test_common_nav_is_independent_of_arm_cash_and_future_open():
    books = pair(held=0)
    books["net"].settled_cash = 50000  # distinct capital does not resize intent
    submitted = diag.submit_pair(books, decision())
    assert submitted["forced"][0]["shares"] == submitted["net"][0]["shares"] == 98
    original = copy.deepcopy(submitted)
    for b in books.values():
        fills = b.open_session(next_day(), {"A": {**observation(50)["A"], "full_session_volume": 10**12}})
        assert fills[0]["shares"] == 98
    assert submitted == original


@pytest.mark.parametrize("obs", [observation(102), {"A": {"price": 100, "open_tradable": True}}])
def test_gap_limit_missing_source_cancel_without_fee(obs):
    books = pair(held=0)
    diag.submit_pair(books, decision())
    for b in books.values():
        assert b.open_session(next_day(), obs) == []
        assert b.pending == [] and b.fees == 0 and b.holdings == {}
        assert b.events[-1]["state"] == "cancelled_residual"


@pytest.mark.parametrize("scenario,filled", [("75bp_buy_half", 49), ("75bp_none", 0)])
def test_partial_and_unfilled_buys_charge_only_filled(scenario, filled):
    books = pair(scenario, held=0)
    initial = {a: b.state() for a, b in books.items()}
    diag.submit_pair(books, decision())
    for arm, b in books.items():
        b.open_session(next_day(), observation())
        assert b.holdings == ({"A": filled} if filled else {})
        assert b.fees == pytest.approx(filled * 100 * .00375)
        assert b.pending == [] and b.events[-1]["remaining"] == 98-filled
        assert diag.reconcile_book(b, initial[arm])


def test_partial_sell_halts_bound_residual_and_missing_rights_null():
    books = pair("75bp_sell_half", held=3)
    initial = {a: b.state() for a, b in books.items()}
    diag.submit_pair(books, decision(target=False))
    days = diag.market_clock.next_sessions("kr", DAY, 6)
    for arm, b in books.items():
        for i, day in enumerate(days):
            b.open_session(day, observation() if i == 0 else {})
        assert b.holdings == {"A": 2} and b.pending == []
        assert b.events[-1]["attempts"] == 6
        assert diag.reconcile_book(b, initial[arm])
        assert diag.book_metrics(b, {})["price_nav_krw"] is None
        state = diag.book_metrics(b, {"A": 100}, rights_reason="holder_rights_unknown:2024-10-11:A")
        assert state["exposure_krw"] is None and state["price_nav_krw"] is None
        assert all(state[k] is None for k in ("total_return", "net_return", "mdd", "adoption"))


def test_saved_fill_baseline_is_cost_arithmetic_and_unequal_prices_are_null():
    def fill(side, quantity, price):
        return {"session": "2024-10-04", "ticker": "A", "side": side, "shares": quantity, "price": price}
    row = diag.saved_fill_baseline([{"fills": [fill("sell", 100, 100), fill("buy", 98, 100)]}])[0]
    assert row["overlap_count"] == 1
    assert row["overlap_assumed_cost_krw"] == pytest.approx(73.5)
    assert row["assumed_fill_cost_krw"] == pytest.approx(74.25)
    changed = diag.saved_fill_baseline([{"fills": [fill("sell", 100, 100), fill("buy", 98, 99)]}])[0]
    assert changed["overlap_assumed_cost_krw"] is None


def synthetic_result():
    day = dt.date.fromisoformat(DAY)
    end = dt.date(2024, 12, 30)
    dates = []
    while day <= end:
        if diag.market_clock.is_session("kr", day):
            dates.append(day.isoformat())
        day += dt.timedelta(days=1)
    result = {"evaluation_decision_dates": dates, "selections": {}, "predictions": [],
        "scenarios": {"75bp_full": {"ridge": [{"fills": []} for _ in range(5)]}},
        "plan_sha256": "synthetic"}
    for d in dates:
        result["selections"][d] = {"feature_names": 1, "selected": {"ridge": [["A", .02]]}}
        result["predictions"].append({"date": d, "ticker": "A", "predicted_gross": .02})
    return result


def test_row_reconciliation_rejects_rank_and_date_tampering():
    cfg = {"max_positions": 6, "predicted_gross_entry_min": .0125, "min_price_feature_names": 1}
    result = synthetic_result()
    assert diag.reconcile_saved_rows(result, cfg)["dates"] == 61
    result["selections"][DAY]["selected"]["ridge"] = []
    with pytest.raises(ValueError, match="selection_mismatch"):
        diag.reconcile_saved_rows(result, cfg)
    result = synthetic_result()
    result["evaluation_decision_dates"] = result["evaluation_decision_dates"][:-1]
    with pytest.raises(ValueError, match="calendar_mismatch"):
        diag.reconcile_saved_rows(result, cfg)


def test_sell_exit_id_can_use_liquidation_tail_without_new_selection():
    result = synthetic_result()
    cfg = {"max_positions": 6, "predicted_gross_entry_min": .0125, "min_price_feature_names": 1}
    day = "2025-01-02"
    result["scenarios"]["75bp_full"]["ridge"][0]["fills"] = [{
        "side": "sell", "ticker": "OLD", "shares": 1, "price": 100,
        "session": diag.market_clock.next_sessions("kr", day, 1)[0],
        "decision_input_id": "r3/ridge/0/" + day + "/synthetic"}]
    assert diag.reconcile_saved_rows(result, cfg)["dates"] == 61
    result["scenarios"]["75bp_full"]["ridge"][0]["fills"][0]["side"] = "buy"
    with pytest.raises(ValueError, match="buy_selection"):
        diag.reconcile_saved_rows(result, cfg)


def test_preflight_restores_intent_without_stored_decisions_or_quantity(monkeypatch, tmp_path):
    model, result = {"model": "synthetic"}, synthetic_result()
    result.update({"model": model, "model_sha256": diag.EXPECTED[diag.MODEL]})
    cfg = {"max_positions": 6, "predicted_gross_entry_min": .0125, "min_price_feature_names": 1,
        "evaluation_start": DAY, "evaluation_end": "2024-12-30", "liquidate_through": "2025-01-31",
        "starting_cash": 10_000_000, "phase_count": 5, "horizon_open_to_open_sessions": 5}
    monkeypatch.setattr(diag, "digest", lambda p: diag.EXPECTED.get(str(p.relative_to(tmp_path)), "synthetic")
                        if p.is_relative_to(tmp_path) else "synthetic")
    monkeypatch.setattr(diag, "checked_inputs", lambda root: (model, result, cfg, diag.EXPECTED.copy()))
    monkeypatch.setattr(diag, "EXECUTION_CODE", {})
    monkeypatch.setattr(diag, "reproduce_fixed_predictions", lambda *a, **k: ({"state": "matched"}, {}, None))
    monkeypatch.setattr(diag, "checked_reviewed", lambda *a: {})
    result["reviewed_share_changes"] = []
    calls = []
    def restore(*a, **k):
        calls.append("restored")
        return [[] for _ in range(5)]
    monkeypatch.setattr(diag, "restore_old_intents", restore)
    monkeypatch.setattr(diag, "replay_pair", lambda phase, *a, **k: {
        "phase": phase, "stop": None, "comparable_prefix_difference": {"cost_saved_krw": 1}})
    out = diag.diagnose(tmp_path)
    assert out["state"] == "price_only_complete" and len(out["paths"]) == 5
    assert out["new_execution_paths"] == 10 and calls == ["restored"]
    assert out["old_intent_reconciliation"]["state"] == "matched"
    def rejected(*a, **k):
        raise ValueError("cash_quantity_fee_reconciliation_failed")
    monkeypatch.setattr(diag, "replay_pair", rejected)
    failed = diag.diagnose(tmp_path)
    assert failed["state"] == "rejected_execution" and failed["active_phase"] == 0
    assert failed["paths"] == []


def test_hash_mismatch_does_not_read_model_or_start_paths(tmp_path):
    for name in diag.EXPECTED:
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("deliberate hash mismatch, not JSON")
    out = diag.diagnose(tmp_path)
    assert out["reason"] == "fixed_input_hash_mismatch" and out["paths"] == []
    assert out["total_return"] is None


def market_fixture(count=15):
    from types import SimpleNamespace
    dates = [DAY] + diag.market_clock.next_sessions("kr", DAY, count-1)
    prior = "2024-09-30"
    sessions = [prior] + dates
    panel = SimpleNamespace(sessions=sessions, index={d: i for i, d in enumerate(sessions)},
                            bars={(d, "A"): (100., 100.) for d in sessions})
    by_day = {d: {"A": {"LIST_SHRS": "1,000,000", "ACC_TRDVOL": "100,000"}} for d in sessions}
    cfg = {"candidate_id": "synthetic", "max_positions": 6, "starting_cash": 100000,
           "horizon_open_to_open_sessions": 2, "evaluation_start": DAY,
           "evaluation_end": dates[min(5, len(dates)-1)], "liquidate_through": dates[-1]}
    return dates, panel, by_day, cfg


def test_old_intent_reconstruction_links_fills_and_maturity_without_preopen_quantity():
    dates, panel, by_day, cfg = market_fixture(5)
    buy_cash = 100000 - 10*100*1.00375
    sell_cash = buy_cash + 10*100*.99625
    equity = [100000, buy_cash+1000, buy_cash+1000, sell_cash, sell_cash]
    def fill(side, on, origin):
        return {"side": side, "ticker": "A", "session": on, "price": 100., "shares": 10,
                "decision_input_id": "r3/ridge/0/"+origin+"/synthetic"}
    result = {"plan_sha256": "synthetic", "evaluation_decision_dates": [DAY],
        "selections": {DAY: {"selected": {"ridge": [["A", .02]]}}},
        "scenarios": {"75bp_full": {"ridge": [{
            "fills": [fill("buy", dates[1], DAY), fill("sell", dates[3], dates[2])],
            "equity_curve": [{"session": d, "equity": e} for d, e in zip(dates, equity)]}]}}}
    restored = diag.restore_old_intents(result, cfg, by_day, panel, {})[0]
    actions = [r["actions"] for r in restored]
    assert [a[0]["action"] if a else None for a in actions] == ["ENTER", "HOLD", "EXIT", None, None]
    assert actions[0][0]["candidate_rank"] == 1
    assert actions[0][0]["risk_budget_fraction"] == 1
    assert actions[0][0]["horizon_sessions"] == 2
    assert actions[0][0]["valid_until"] == dates[1]
    assert "shares" not in actions[0][0]
    assert restored[0]["holdings"] == {} and restored[1]["holdings"] == {"A": 10}
    bad = copy.deepcopy(result)
    bad["scenarios"]["75bp_full"]["ridge"][0]["fills"][1]["decision_input_id"] = "r3/ridge/0/"+DAY+"/synthetic"
    with pytest.raises(ValueError, match="old_fill_action_link"):
        diag.restore_old_intents(bad, cfg, by_day, panel, {})


def paired_fixture():
    dates, panel, by_day, cfg = market_fixture()
    result = {"evaluation_decision_dates": dates[:6],
              "selections": {d: {"selected": {"ridge": [["A", .02]]}} for d in dates[:6]}}
    old = [{"session": d, "reference_equity": 100000,
            "actions": [{"action": "ENTER", "ticker": "A", "candidate_rank": 1,
                         "valid_until": diag.market_clock.next_sessions("kr", d, 1)[0],
                         "horizon_sessions": 5, "risk_budget_fraction": 1}]} for d in dates]
    return dates, panel, by_day, cfg, result, old


def test_paired_adapter_schedule_terminal_costs_and_future_price_invariance():
    dates, panel, by_day, cfg, result, old = paired_fixture()
    first = diag.replay_pair(0, result, cfg, by_day, panel, {}, old)
    assert first["state"] == "price_only_complete"
    assert first["liquidation_state"] == "simulated_closed"
    assert [r["common"]["day"] for r in first["submissions"]] == [dates[0], dates[5], dates[10]]
    assert first["submissions"][-1]["terminal"]
    assert first["arms"]["forced"]["metrics"]["holdings"] == first["arms"]["net"]["metrics"]["holdings"] == {}
    assert first["comparable_prefix_difference"]["cost_saved_krw"] > 0
    assert first["total_return"] is None and first["adoption"] is None
    changed = copy.deepcopy(panel)
    changed.bars[dates[1], "A"] = (50., 100.)
    changed_days = copy.deepcopy(by_day)
    changed_days[dates[1]]["A"]["ACC_TRDVOL"] = "999999999"
    second = diag.replay_pair(0, result, cfg, changed_days, changed, {}, old)
    assert first["submissions"][0] == second["submissions"][0]


def test_future_close_gap_preserves_known_open_fills_but_not_valuation():
    dates, panel, by_day, cfg, result, old = paired_fixture()
    panel.bars[dates[1], "A"] = (100., None)
    out = diag.replay_pair(0, result, cfg, by_day, panel, {}, old)
    assert out["stop"]["field"] == "close"
    assert out["stop"]["session"] == dates[1]
    assert all(row["forced"] is None and row["net"] is None for row in out["trace"][1:])
    for arm in out["arms"].values():
        assert len(arm["fills"]) == 1 and arm["fills"][0]["on"] == dates[1]
        assert arm["confirmed_execution_prefix"]["assumed_fill_cost_krw"] > 0
        assert arm["metrics"]["price_nav_krw"] is None


@pytest.mark.parametrize("field,value", [("action", "WAIT"), ("candidate_rank", 2),
    ("risk_budget_fraction", 0), ("valid_until", "2024-10-07"), ("horizon_sessions", 20)])
def test_paired_selection_requires_matching_restored_entry(field, value):
    _, panel, by_day, cfg, result, old = paired_fixture()
    old[0]["actions"][0][field] = value
    with pytest.raises(ValueError, match="restored_action_selection_mismatch"):
        diag.replay_pair(0, result, cfg, by_day, panel, {}, old)


@pytest.mark.parametrize("field", ["price", "unit"])
def test_paired_source_stop_nulls_both_arms_and_preserves_prefix(field):
    dates, panel, by_day, cfg, result, old = paired_fixture()
    if field == "price":
        panel.bars[dates[3], "A"] = (None, None)
    else:
        by_day[dates[3]]["A"]["LIST_SHRS"] = "2,000,000"
    out = diag.replay_pair(0, result, cfg, by_day, panel, {}, old)
    assert out["state"] == "partial_source_stop" and out["stop"]["session"] == dates[3]
    assert out["stop"]["ticker"] == "A"
    assert all(r["forced"] is None and r["net"] is None for r in out["trace"][3:])
    for arm in out["arms"].values():
        assert arm["metrics"]["fees"] is None and arm["metrics"]["price_nav_krw"] is None
        assert arm["confirmed_execution_prefix"]["assumed_fill_cost_krw"] > 0
        assert all(f["on"] < dates[3] for f in arm["fills"])
