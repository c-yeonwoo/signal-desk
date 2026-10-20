from copy import deepcopy

import pytest

from scripts.measure import kr_bounded_momentum_ablation as lab
from scripts.measure import kr_score_tie_ablation as replay
from signal_desk.signals import engine


def signal(ticker="000000", *, raw=.5, score=2, coefficient=.6):
    norm = max(-1, min(1, raw / .5)) if raw is not None else 0
    return engine.SignalResult(ticker=ticker, name=ticker, score=score, kind="BUY", confidence=.9,
                               technical_score=0, fundamental_score=0, has_fundamental=True,
                               has_momentum=raw is not None, momentum_ret=raw, data_coverage=.9,
                               factor_scores={"momentum": norm},
                               score_contributions={"momentum": norm * coefficient})


def test_fixed_bounded_mapping_and_actual_fired_coefficient():
    data = [signal("A", raw=.5), signal("B", raw=1), signal("C", raw=1, coefficient=.9),
            signal("D", raw=.2), signal("E", raw=-.75), signal("F", raw=None)]
    values, saturated = lab.rank_values(data, engine.SignalConfig())
    assert values == pytest.approx({"A": 2, "B": 2.3, "C": 2.45, "D": 2, "E": 1.8, "F": 2})
    assert {r["ticker"] for r in saturated} == {"B", "C", "E"}


def test_strength_remains_monotone_but_extremes_cannot_dominate():
    raw_values = [-100, -2, -1, -.75, -.5, .5, .75, 1, 2, 100, 1e300]
    data = [signal(str(i), raw=raw) for i, raw in enumerate(raw_values)]
    values, saturated = lab.rank_values(data, engine.SignalConfig())
    ordered = [values[str(i)] for i in range(len(data))]
    assert ordered == sorted(ordered)
    assert all(a < b for a, b in zip(ordered, ordered[1:]) if (a, b) != (2, 2))
    for row in saturated:
        assert abs(row["bounded_increment"]) <= 1
        assert abs(row["research_rank_value"] - row["base_score"]) <= row["coefficient"] + 1e-12
    assert max(ordered) <= 2.6


def test_original_data_and_score_unchanged_without_recent20_combination():
    data = [signal("A", raw=1), signal("B", raw=2)]
    original = deepcopy(data)
    a = lab.bounded_rank(data, {"A": 10000, "B": -10000}, engine.SignalConfig())
    b = lab.bounded_rank(data, {"A": -10000, "B": 10000}, engine.SignalConfig())
    assert data == original
    assert [r.ticker for r in a] == [r.ticker for r in b] == ["B", "A"]
    assert {r.ticker: r.score for r in a} == {"A": 2, "B": 2}


def test_equal_values_preserve_original_order_and_existing_precision():
    data = [signal("B", raw=.5001), signal("A", raw=.5), signal("C", raw=2, coefficient=0)]
    values, audit = lab.rank_values(data, engine.SignalConfig())
    assert values == {"B": 2, "A": 2, "C": 2}
    assert len(audit) == 2
    assert [r.ticker for r in lab.bounded_rank(data, {}, engine.SignalConfig())] == ["B", "A", "C"]


@pytest.mark.parametrize("flag,value", [("gate_blocked", True), ("event_risk", True),
                                        ("data_coverage", .2), ("score", 1.1)])
def test_auxiliary_rank_cannot_bypass_floor_risk_gates_or_refill_slots(flag, value):
    data = [signal(f"{i:06d}", score=1.5 if i < 8 else 0) for i in range(200)]
    data[7].momentum_ret = 100
    setattr(data[7], flag, value)
    ranked = lab.bounded_rank(data, {}, engine.SignalConfig())
    assert ranked[0].ticker == data[7].ticker
    assert not engine.is_buy(ranked[0].kind)
    assert sum(engine.is_buy(r.kind) for r in ranked) == 5
    assert not engine.is_buy(next(r for r in ranked if r.ticker == data[5].ticker).kind)
    assert ranked[0].score == data[7].score


@pytest.mark.parametrize("field,value", [("momentum_ret", None), ("momentum_ret", float("nan")),
                                        ("momentum_ret", float("inf")), ("score", float("nan"))])
def test_unknown_nonfinite_or_overflowed_inputs_fail(field, value):
    item = signal(raw=1)
    setattr(item, field, value)
    with pytest.raises(ValueError):
        lab.rank_values([item], engine.SignalConfig())
    with pytest.raises(ValueError, match="raw normalization"):
        lab.rank_values([signal(raw=1e308)], engine.SignalConfig())


def test_invalid_denominator_scale_or_duplicate_inputs_fail():
    item = signal(raw=1)
    for contribution, factor in [(None, 1), (.6, -1), (-.6, 1)]:
        item.score_contributions = {"momentum": contribution}
        item.factor_scores = {"momentum": factor}
        with pytest.raises(ValueError):
            lab.rank_values([item], engine.SignalConfig())
    for scale in [0, -1, float("nan")]:
        with pytest.raises(ValueError, match="scale"):
            lab.rank_values([], engine.SignalConfig(momentum_scale=scale))
    with pytest.raises(ValueError, match="ranking inputs"):
        lab.rank_values([signal(), signal()], engine.SignalConfig())


def test_wrapper_dispatches_frozen_candidate_and_keeps_day_identity(monkeypatch):
    seen = {}
    def fake_replay(**kwargs):
        seen.update(kwargs)
        kwargs["ranker"]([signal(raw=1)], {}, engine.SignalConfig())
        return {"code_sha256": "shared-code", "windows": {
            "window": {"costs": {"25": {"daily": [{"date": "2024-04-01"}]}}}}}
    monkeypatch.setattr(replay, "run", fake_replay)
    result = lab.run()
    assert seen["plan_path"] == lab.PLAN
    assert seen["plan_sha"] == lab.PLAN_SHA
    assert seen["plan_commit"] == lab.PLAN_COMMIT
    assert seen["schema"] == "kr-bounded-momentum-ablation-v1"
    assert result["replay_code_sha256"] == "shared-code"
    assert result["saturated_ticker_days"] == 1
    assert result["saturation_audit"][0]["date"] == "2024-04-01"
    assert result["saturation_audit"][0]["window"] == "window"
    assert result["saturation_audit"][0]["saturated"][0]["bounded_increment"] == .5


def test_changed_contract_fails_before_input_loading(tmp_path, monkeypatch):
    changed = tmp_path / "plan.json"
    changed.write_text('{"changed":true}')
    monkeypatch.setattr(lab, "PLAN", changed)
    with pytest.raises(ValueError, match="frozen tie contract"):
        lab.run()
