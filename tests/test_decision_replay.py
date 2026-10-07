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


def test_captured_inputs_require_the_same_gate_prices(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    prices, dates, inputs, gate, results = _sample()
    gate = {**gate, "status": "applied", "closes_by": prices, "dates_by": dates}
    captured = {"engine_inputs": inputs, "gate_inputs": gate, "results": results}
    refs = decision_snapshot.persist_captured_decision("kr", (prices, dates, {}), captured)
    assert decision_snapshot.replay_signal_decision("kr", refs["signal_output_id"])["match"]

    gate["closes_by"] = {"AAA": [999.0]}
    try:
        decision_snapshot.persist_captured_decision("kr", (prices, dates, {}), captured)
    except ValueError as exc:
        assert "price generation" in str(exc)
    else:
        raise AssertionError("mismatched gate price was accepted")


def test_failed_gate_capture_cannot_be_called_replayable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    prices, dates, inputs, _gate, results = _sample()
    captured = {"engine_inputs": inputs,
                "gate_inputs": {"status": "failed_partial", "closes_by": prices,
                                "dates_by": dates, "today": "2026-10-07"},
                "results": results}
    try:
        decision_snapshot.persist_captured_decision("kr", (prices, dates, {}), captured)
    except ValueError as exc:
        assert "not captured successfully" in str(exc)
    else:
        raise AssertionError("failed gate was accepted")
    assert db.decision_artifact_storage() == []


def test_admin_can_replay_recent_output_without_raw_inputs(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from signal_desk import api

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_EMAILS", "replay-admin@example.com")
    prices, dates, inputs, gate, results = _sample()
    refs = decision_snapshot.persist_signal_decision(
        "kr", prices=prices, dates=dates, quote_snapshot={}, engine_inputs=inputs,
        gate_inputs=gate, results=results)
    recent = db.decision_artifact_recent_outputs()
    assert recent == [{"id": refs["signal_output_id"], "market": "kr",
                       "first_observed": recent[0]["first_observed"]}]

    params = {"market": "kr", "signal_output_id": refs["signal_output_id"]}
    guest = TestClient(api.app)
    assert guest.get("/api/admin/decision-replay", params=params).status_code == 401
    guest.post("/api/auth/signup", json={"email": "reader@example.com", "pw": "abcdef12"})
    assert guest.get("/api/admin/decision-replay", params=params).status_code == 403
    admin = TestClient(api.app)
    admin.post("/api/auth/signup", json={"email": "replay-admin@example.com", "pw": "abcdef12"})
    storage = admin.get("/api/admin/storage-breakdown")
    assert storage.status_code == 200
    assert storage.json()["recent_decisions"] == recent
    before = db.decision_artifact_storage()
    response = admin.get("/api/admin/decision-replay", params=params)
    assert response.status_code == 200
    assert response.json()["match"] is True
    assert "rows" not in response.json() and "prices" not in response.json()
    assert db.decision_artifact_storage() == before
    assert admin.get("/api/admin/decision-replay", params={**params, "market": "us"}).status_code == 404
    assert admin.get("/api/admin/decision-replay", params={**params, "signal_output_id": "invalid"}).status_code == 400

    c = db.conn()
    try:
        c.execute("UPDATE decision_artifacts SET payload=? WHERE id=?",
                  (b"damaged", refs["signal_output_id"]))
        c.commit()
    finally:
        c.close()
    assert admin.get("/api/admin/decision-replay", params=params).status_code == 409
