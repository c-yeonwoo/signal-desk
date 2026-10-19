from copy import deepcopy
from types import SimpleNamespace

import pytest

from scripts.measure import kr_score_tie_ablation as lab
from signal_desk.signals import engine


def signals():
    data = [engine.SignalResult(ticker=f"{i:05d}0", name=str(i), score=3.0 if i < 8 else 0.0,
                                kind="BUY" if i < 8 else "HOLD", confidence=.9,
                                technical_score=0, fundamental_score=0, has_fundamental=True,
                                data_coverage=.9) for i in range(200)]
    return engine.apply_cross_sectional(data, engine.SignalConfig())


def buys(data):
    return [r.ticker for r in data if engine.is_buy(r.kind)]


def test_exact_score_tie_uses_recent_return_and_does_not_mutate_original():
    data = signals()
    original = deepcopy(data)
    ranked = lab.tie_rank(data, {r.ticker: float(i) for i, r in enumerate(data)}, engine.SignalConfig())
    assert buys(data) == [f"{i:05d}0" for i in range(6)]
    assert buys(ranked) == [f"{i:05d}0" for i in range(7, 1, -1)]
    assert data == original
    assert {r.ticker: r.score for r in ranked} == {r.ticker: r.score for r in original}


def test_lower_score_cannot_jump_higher_score_and_double_ties_remain_stable():
    data = signals()
    data[7].score = 2.99
    recent = {r.ticker: 0 for r in data}
    recent[data[7].ticker] = 100000
    ranked = lab.tie_rank(data, recent, engine.SignalConfig())
    assert buys(ranked) == buys(data)
    assert [r.ticker for r in ranked[:7]] == [r.ticker for r in data[:7]]


@pytest.mark.parametrize("flag,value", [("gate_blocked", True), ("event_risk", True), ("data_coverage", .2)])
def test_same_gates_keep_vacant_slot_not_backfilled(flag, value):
    data = signals()
    setattr(data[7], flag, value)
    ranked = lab.tie_rank(data, {r.ticker: float(i) for i, r in enumerate(data)}, engine.SignalConfig())
    assert ranked[0].ticker == data[7].ticker
    assert not engine.is_buy(ranked[0].kind)
    assert len(buys(ranked)) == 5
    assert data[1].ticker not in buys(ranked)  # outside the original six-place score window


@pytest.mark.parametrize("value", [None, float("nan"), float("inf")])
def test_missing_tie_input_is_not_neutralized(value):
    data = signals()
    recent = {r.ticker: 0 for r in data}
    recent[data[0].ticker] = value
    with pytest.raises(ValueError, match="missing tie"):
        lab.tie_rank(data, recent, engine.SignalConfig())


def daily():
    return [{"date": str(i), "arms": {arm: {"slot_net_pct": float(i) if arm == "candidate" else 0}
                                        for arm in lab.ARMS}} for i in range(6)]


def test_predeclared_baseline_exclusion_does_not_rephase_the_sample():
    result = lab.simple_pair(daily(), {"1"})
    assert result["dates"] == 5
    assert result["phase_deltas_pp"] == [2.5, None, 2, 3, 4]


def test_new_candidate_missing_return_is_not_an_additional_exclusion():
    data = daily()
    data[2]["arms"]["candidate"]["slot_net_pct"] = None
    result = lab.simple_pair(data, {"1"})
    assert result["dates"] == 5
    assert result["delta_pp"] is None
    assert result["paired_means_pct"]["candidate"] is None


def test_assessment_has_a_positive_control_and_rejects_missing_candidate_prices():
    windows = {}
    for name in range(7):
        metrics = {"arms": {arm: {"equal_day_slot_net_pct": value, "unresolved_dates": 0}
                            for arm, value in (("legacy", 1), ("candidate", 3), ("simple_momentum", 2), ("cash", 0))},
                   "vs_legacy": {"equal_day_delta_pp": 2, "phase_deltas_pp": [2] * 5},
                   "simple_pair": {"dates": 10, "coverage": 1, "delta_pp": 1,
                                   "paired_means_pct": {arm: value for arm, value in
                                                        (("legacy", 1), ("candidate", 3), ("simple_momentum", 2), ("cash", 0))}}}
        windows[str(name)] = {"dates": 10, "changed_buy_dates": 6, "costs": {"25": metrics}}
    changes = [{"window": "0", "ticker": str(i), "side": "removed", "h5": {"state": "matured", "net_pct": -1}}
               for i in range(10)]
    checks, _ = lab.assess(windows, changes, [25])
    assert all(checks.values())
    windows["0"]["costs"]["25"]["arms"]["candidate"]["unresolved_dates"] = 1
    checks, _ = lab.assess(windows, changes, [25])
    assert checks["all_legacy_candidate_prices_known"] is False


def test_future_prices_never_enter_asof_replay(monkeypatch):
    names = [r.ticker for r in signals()]
    sessions = [f"historical-{i}" for i in range(252)] + ["2024-04-01", "2024-04-02"]
    bars = {(s, ticker): (100, 100) for s in sessions for ticker in names}
    panel = SimpleNamespace(sessions=sessions, index={"2024-04-01": 252}, bars=bars)
    rows = [{"ticker": t, "recent20_pct": 0} for t in names]
    days = {"2024-04-01": [{"ISU_CD": t, "ISU_NM": t, "MKTCAP": 1000 - i} for i, t in enumerate(names)]}
    captured = []
    def evaluate(universe, prices, **kwargs):
        captured.append(deepcopy(prices))
        return signals()
    monkeypatch.setattr(lab, "metrics_asof", lambda *a, **k: ({}, {}))
    monkeypatch.setattr(engine, "evaluate", evaluate)
    a = lab.replay_day("2024-04-01", rows, days, panel, {"records": []}, {})
    for ticker in names:
        bars["2024-04-02", ticker] = (999999, 999999)
    b = lab.replay_day("2024-04-01", rows, days, panel, {"records": []}, {})
    assert captured[0] == captured[1]
    assert a[2] == b[2]
    assert buys(a[1]) == buys(b[1])


def test_contract_tampering_is_rejected_before_loading_any_data(tmp_path, monkeypatch):
    changed = tmp_path / "plan.json"
    changed.write_text('{"changed":true}')
    monkeypatch.setattr(lab, "PLAN", changed)
    with pytest.raises(ValueError, match="frozen tie contract"):
        lab.run()
