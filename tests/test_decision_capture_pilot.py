"""자동 판단 보존은 작게, 한 번만, 재생 가능한 경우에만 성공으로 센다."""

import datetime
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from signal_desk import api, bot, db, store
from signal_desk.signals import decision_capture_pilot as pilot
from signal_desk.signals import engine


def _captured_bundle(monkeypatch):
    days = [(datetime.date(2025, 1, 1) + datetime.timedelta(days=i)).isoformat()
            for i in range(260)]
    bundle = ({"005930": [100.0 + i * 0.1 for i in range(260)]},
              {"005930": days}, {"captured_at": 1000, "quotes": {}, "quote_updated": {}})
    monkeypatch.setattr(store, "load_universe", lambda: [{"ticker": "005930", "name": "삼성전자"}])
    monkeypatch.setattr(store, "load_fundamentals", lambda: {})
    monkeypatch.setattr(store, "kr_engine_inputs", lambda: {})
    monkeypatch.setattr(store, "load_signal_history", lambda: [])
    monkeypatch.setattr(db, "kb_events_active", lambda: [])
    read = {"eff_cfg": engine.SignalConfig(), "_price_bundle": bundle}
    bot._market_signals("kr", read)
    return bundle, read["_decision_capture"]


def test_pilot_saves_and_replays_only_first_run_per_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bundle, capture = _captured_bundle(monkeypatch)
    monkeypatch.setattr(pilot.shutil, "disk_usage", lambda path: SimpleNamespace(
        total=1024 * pilot._MIB, free=500 * pilot._MIB))

    first = pilot.capture_once("kr", "2026-10-07", bundle, capture)
    second = pilot.capture_once("kr", "2026-10-07", bundle, capture)

    assert first["status"] == "saved" and first["replay_match"] is True
    assert first["elapsed_ms"] >= 0 and first["artifact_bytes_added"] > 0
    assert second == {"status": "already_claimed"}
    assert len(db.decision_artifact_recent_outputs()) == 1
    recent = db.decision_pilot_recent()
    assert recent[0]["status"] == "saved" and recent[0]["session"] == "2026-10-07"
    assert "owner" not in recent[0]


def test_us_pilot_replays_the_cached_api_batch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    days = [(datetime.date(2025, 1, 1) + datetime.timedelta(days=i)).isoformat()
            for i in range(260)]
    bundle = ({"AAPL": [100.0 + i * 0.1 for i in range(260)]}, {"AAPL": days},
              {"captured_at": 1000, "quotes": {}, "quote_updated": {}})
    monkeypatch.setattr(store, "load_engine_price_bundle", lambda market: bundle)
    monkeypatch.setattr(store, "load_us_universe", lambda: [{"ticker": "AAPL", "name": "Apple"}])
    monkeypatch.setattr(store, "us_marketcaps", lambda prices: {})
    monkeypatch.setattr(store, "attach_us_quality", lambda fundamentals: None)
    monkeypatch.setattr(store, "load_us_earnings_calendar", lambda: {})
    monkeypatch.setattr(api.kb, "sentiment_map", lambda: {})
    monkeypatch.setattr(api, "_sync_episode_state", lambda results, **kwargs: None)
    monkeypatch.setattr(store, "load_signal_history", lambda: [])
    monkeypatch.setattr(db, "kb_events_active", lambda: [])
    monkeypatch.setattr(pilot.shutil, "disk_usage", lambda path: SimpleNamespace(
        total=1024 * pilot._MIB, free=500 * pilot._MIB))
    api._us_signals.cache_clear()
    try:
        snapshot = api._us_signals()
        result = pilot.capture_once("us", "2026-10-07",
                                    (snapshot.prices, snapshot.price_dates, snapshot.quote_snapshot),
                                    snapshot.decision_capture)
        assert result["status"] == "saved" and result["replay_match"] is True
    finally:
        api._us_signals.cache_clear()


def test_pilot_skips_low_free_space_without_writing_inputs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bundle, capture = _captured_bundle(monkeypatch)
    monkeypatch.setattr(pilot.shutil, "disk_usage", lambda path: SimpleNamespace(
        total=1024 * pilot._MIB, free=100 * pilot._MIB))

    result = pilot.capture_once("kr", "2026-10-07", bundle, capture)

    assert result == {"status": "skipped", "reason": "volume_free_low"}
    assert db.decision_artifact_storage() == []


def test_pilot_replay_mismatch_is_failure_not_verified(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bundle, capture = _captured_bundle(monkeypatch)
    capture["results"][0].score = -999.0
    monkeypatch.setattr(pilot.shutil, "disk_usage", lambda path: SimpleNamespace(
        total=1024 * pilot._MIB, free=500 * pilot._MIB))

    result = pilot.capture_once("kr", "2026-10-07", bundle, capture)

    assert result["status"] == "failed" and result["reason"] == "replay_mismatch"
    assert result["replay_match"] is False
    assert pilot.capture_once("kr", "2026-10-07", bundle, capture)["status"] == "already_claimed"


def test_pilot_claim_is_atomic_and_finished_state_is_immutable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def claim(i):
        return db.decision_pilot_claim("us", "2026-10-07", f"owner-{i}", now=1000)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(claim, range(8)))
    assert results.count(True) == 1
    winner = f"owner-{results.index(True)}"
    assert db.decision_pilot_finish("us", "2026-10-07", winner,
                                    {"status": "saved", "replay_match": True}, now=1001)
    assert not db.decision_pilot_claim("us", "2026-10-07", "later", now=2000)
    assert not db.decision_pilot_finish("us", "2026-10-07", "later",
                                        {"status": "failed"}, now=2001)


def test_pilot_rejects_large_input_before_any_artifact(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bundle, capture = _captured_bundle(monkeypatch)
    capture["engine_inputs"]["universe"][0]["name"] = "A" * (pilot._MAX_RAW_INPUT + 1)
    monkeypatch.setattr(pilot.shutil, "disk_usage", lambda path: SimpleNamespace(
        total=1024 * pilot._MIB, free=500 * pilot._MIB))

    result = pilot.capture_once("kr", "2026-10-07", bundle, capture)

    assert result == {"status": "skipped", "reason": "input_too_large"}
    assert db.decision_artifact_storage() == []


def test_pilot_stops_at_artifact_budget(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bundle, capture = _captured_bundle(monkeypatch)
    monkeypatch.setattr(pilot.shutil, "disk_usage", lambda path: SimpleNamespace(
        total=1024 * pilot._MIB, free=500 * pilot._MIB))
    monkeypatch.setattr(db, "decision_artifact_storage", lambda: [
        {"stored_bytes": pilot._MAX_STORED - pilot._BATCH_RESERVE + 1}])

    result = pilot.capture_once("kr", "2026-10-07", bundle, capture)

    assert result == {"status": "skipped", "reason": "artifact_budget_reached"}


def test_unavailable_generation_is_visible_and_not_retried(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    pilot.record_unavailable("us", "2026-10-07", "generation_changed")
    pilot.record_unavailable("us", "2026-10-07", "capture_missing")

    assert db.decision_pilot_recent() == [{
        "market": "us", "session": "2026-10-07", "status": "skipped",
        "reason": "generation_changed", "signal_output_id": None, "replay_match": None,
        "at": db.decision_pilot_recent()[0]["at"], "elapsed_ms": None,
        "artifact_bytes_added": None}]
