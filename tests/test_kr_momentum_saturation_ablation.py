from copy import deepcopy

import pytest

from scripts.measure import kr_momentum_saturation_ablation as lab
from scripts.measure import kr_score_tie_ablation as replay
from signal_desk.signals import engine


def signal(ticker="000000", *, raw=.5, score=2, coefficient=.6):
    norm = max(-1, min(1, raw / .5)) if raw is not None else 0
    return engine.SignalResult(ticker=ticker, name=ticker, score=score, kind="BUY", confidence=.9,
                               technical_score=0, fundamental_score=0, has_fundamental=True,
                               has_momentum=raw is not None, momentum_ret=raw, data_coverage=.9,
                               factor_scores={"momentum": norm},
                               score_contributions={"momentum": norm * coefficient})


def test_restores_only_clipped_information_using_actual_engine_coefficient():
    data = [signal("A", raw=.5), signal("B", raw=1), signal("C", raw=1, coefficient=.9),
            signal("D", raw=.2), signal("E", raw=-.75), signal("F", raw=None)]
    values, saturated = lab.rank_values(data, engine.SignalConfig())
    assert values == pytest.approx({"A": 2, "B": 2.6, "C": 2.9, "D": 2, "E": 1.7, "F": 2})
    assert {r["ticker"] for r in saturated} == {"B", "C", "E"}


def test_original_score_factors_and_inputs_are_not_modified_or_ret20_used():
    data = [signal("A", raw=1), signal("B", raw=2)]
    original = deepcopy(data)
    a = lab.uncapped_rank(data, {"A": 10000, "B": -10000}, engine.SignalConfig())
    b = lab.uncapped_rank(data, {"A": -10000, "B": 10000}, engine.SignalConfig())
    assert data == original
    assert [r.ticker for r in a] == [r.ticker for r in b] == ["B", "A"]
    assert {r.ticker: r.score for r in a} == {"A": 2, "B": 2}
    assert a[0].research_rank_value > a[1].research_rank_value


def test_equal_auxiliary_values_keep_original_order():
    data = [signal("B", raw=.5), signal("A", raw=.5)]
    ranked = lab.uncapped_rank(data, {}, engine.SignalConfig())
    assert [r.ticker for r in ranked] == ["B", "A"]


def cohort():
    data = [signal(f"{i:05d}0", raw=.5 if i < 8 else None, score=3 if i < 8 else 0) for i in range(200)]
    return engine.apply_cross_sectional(data, engine.SignalConfig())


@pytest.mark.parametrize("flag,value", [("gate_blocked", True), ("event_risk", True), ("data_coverage", .2)])
def test_restored_rank_does_not_bypass_gates_or_fill_vacancy(flag, value):
    data = cohort()
    data[7].momentum_ret = 2
    setattr(data[7], flag, value)
    ranked = lab.uncapped_rank(data, {}, engine.SignalConfig())
    assert ranked[0].ticker == data[7].ticker
    assert not engine.is_buy(ranked[0].kind)
    assert sum(engine.is_buy(r.kind) for r in ranked) == 5
    assert not engine.is_buy(next(r for r in ranked if r.ticker == data[5].ticker).kind)


def test_auxiliary_value_above_three_cannot_raise_base_score_through_buy_floor():
    data = cohort()
    data[7].score = 1.1
    data[7].momentum_ret = 5
    ranked = lab.uncapped_rank(data, {}, engine.SignalConfig())
    first = ranked[0]
    assert first.ticker == data[7].ticker
    assert first.research_rank_value > 3
    assert first.score == 1.1
    assert not engine.is_buy(first.kind)


def test_existing_normalization_precision_and_zero_weight_are_retained():
    data = [signal("A", raw=.5001), signal("B", raw=2, coefficient=0)]
    values, saturated = lab.rank_values(data, engine.SignalConfig())
    assert values == {"A": 2, "B": 2}
    assert len(saturated) == 2  # tiny overflow is still rounded to the existing 3 decimals


@pytest.mark.parametrize("field,value", [("momentum_ret", None), ("momentum_ret", float("nan")),
                                        ("momentum_ret", float("inf")), ("score", float("nan"))])
def test_unknown_or_nonfinite_inputs_fail_not_impute(field, value):
    item = signal(raw=1)
    setattr(item, field, value)
    with pytest.raises(ValueError):
        lab.rank_values([item], engine.SignalConfig())


def test_missing_contribution_or_inconsistent_sign_cannot_invent_a_denominator():
    item = signal(raw=1)
    item.score_contributions = {}
    with pytest.raises(ValueError, match="contribution mismatch"):
        lab.rank_values([item], engine.SignalConfig())
    item.score_contributions = {"momentum": .6}
    item.factor_scores = {"momentum": -1}
    with pytest.raises(ValueError, match="contribution mismatch"):
        lab.rank_values([item], engine.SignalConfig())


def test_wrapper_dispatches_only_approved_ranker_and_preserves_day_identity(monkeypatch):
    seen = {}
    def fake_replay(**kwargs):
        seen.update(kwargs)
        kwargs["ranker"]([signal(raw=1)], {}, engine.SignalConfig())
        return {"decision_dates": 1, "code_sha256": "shared-code",
                "windows": {"window": {"costs": {"25": {"daily": [{"date": "2024-04-01"}]}}}}}
    monkeypatch.setattr(replay, "run", fake_replay)
    result = lab.run()
    assert seen["plan_path"] == lab.PLAN
    assert seen["plan_sha"] == lab.PLAN_SHA
    assert seen["schema"] == "kr-momentum-saturation-ablation-v1"
    assert result["replay_code_sha256"] == "shared-code"
    assert result["saturated_ticker_days"] == 1
    assert result["saturation_audit"][0]["date"] == "2024-04-01"
    assert result["saturation_audit"][0]["window"] == "window"


def test_tampered_contract_is_rejected_before_input_loading(tmp_path, monkeypatch):
    changed = tmp_path / "plan.json"
    changed.write_text('{"changed":true}')
    monkeypatch.setattr(lab, "PLAN", changed)
    with pytest.raises(ValueError, match="frozen tie contract"):
        lab.run()
