"""P2 오프라인 입력·전체 후보 재생. 라이브 주문이나 등록 연구 판정은 실행하지 않는다."""

import datetime

from signal_desk import db
from signal_desk.signals import decision_snapshot, engine, execution_gate
from signal_desk.signals.decision import empty_decision


def _sample():
    days = [(datetime.date(2025, 1, 1) + datetime.timedelta(days=i)).isoformat()
            for i in range(260)]
    prices = {"AAA": [100.0 + i * 0.1 for i in range(260)]}
    dates = {"AAA": days}
    universe = [{"ticker": "AAA", "name": "가"}, {"ticker": "BBB", "name": "나"}]
    today = datetime.date(2026, 10, 7)
    cfg = engine.SignalConfig()
    inputs = {"universe": universe, "fundamentals": {},
              "sentiment": {"AAA": {"score": 0.0, "decision": empty_decision()}},
              "flows": {}, "shorts": {}, "earnings_dates": {}, "unavailable": (),
              "config": cfg, "today": today, "signal_policy_id": "test-policy"}
    gate = {"hist_by": {}, "events_by": {}, "today": today.isoformat(),
            "config": execution_gate.ExecutionGateConfig()}
    results = engine.evaluate(universe, prices, fundamentals={}, config=cfg,
                              sentiment=inputs["sentiment"], today=today)
    for result in results:
        result.signal_policy_id = "test-policy"
    execution_gate.apply(results, hist_by={}, events_by={}, dates_by=dates,
                         closes_by=prices, today=today.isoformat())
    return prices, dates, inputs, gate, results


def test_full_universe_inputs_and_gate_replay_exactly(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    prices, dates, inputs, gate, results = _sample()
    refs = decision_snapshot.persist_signal_decision(
        "kr", prices=prices, dates=dates, quote_snapshot={}, engine_inputs=inputs,
        gate_inputs=gate, results=results, observed_at=100)
    again = decision_snapshot.persist_signal_decision(
        "kr", prices=prices, dates=dates, quote_snapshot={}, engine_inputs=inputs,
        gate_inputs=gate, results=results, observed_at=200)

    assert refs["signal_output_id"] == again["signal_output_id"]
    assert refs["replay_attemptable"] and refs["strict_pit_eligible"] is False
    saved = db.decision_artifact_get(refs["signal_output_id"])["data"]
    assert saved["universe_size"] == 2 and len(saved["rows"]) == 1
    replay = decision_snapshot.replay_signal_decision("kr", refs["signal_output_id"])
    assert replay["match"] and replay["mismatched_tickers"] == []
    assert replay["universe_size"] == 2 and replay["replayed_rows"] == 1


def test_replay_reports_difference_without_rewriting_original(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    prices, dates, inputs, gate, results = _sample()
    results[0].score = -9.0
    refs = decision_snapshot.persist_signal_decision(
        "kr", prices=prices, dates=dates, quote_snapshot={}, engine_inputs=inputs,
        gate_inputs=gate, results=results)
    replay = decision_snapshot.replay_signal_decision("kr", refs["signal_output_id"])
    assert replay["match"] is False and replay["mismatched_tickers"] == ["AAA"]
    assert db.decision_artifact_get(refs["signal_output_id"])["data"]["rows"][0]["score"] == -9.0


def test_replay_requires_explicit_engine_day_and_market_match(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    prices, dates, inputs, gate, results = _sample()
    inputs["today"] = None
    try:
        decision_snapshot.persist_signal_decision(
            "kr", prices=prices, dates=dates, quote_snapshot={}, engine_inputs=inputs,
            gate_inputs=gate, results=results)
    except ValueError as exc:
        assert "exact date" in str(exc)
    else:
        raise AssertionError("missing evaluation day was accepted")
    inputs["today"] = datetime.date(2026, 10, 7)
    refs = decision_snapshot.persist_signal_decision(
        "kr", prices=prices, dates=dates, quote_snapshot={}, engine_inputs=inputs,
        gate_inputs=gate, results=results)
    try:
        decision_snapshot.replay_signal_decision("us", refs["signal_output_id"])
    except ValueError as exc:
        assert "market mismatch" in str(exc)
    else:
        raise AssertionError("cross-market replay was accepted")
