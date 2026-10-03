"""SEC watchlist evidence never escapes contact, identity, or request budgets."""

import datetime as dt
import json

import pytest

from signal_desk.ingest import edgar, sec_issuer_map as secmap, sec_refresh


NOW = dt.datetime(2026, 10, 5, 8, tzinfo=dt.timezone.utc)


def _map(path):
    rows = {str(i): {"ticker": f"T{i:04d}", "cik_str": 100000 + i,
                     "title": f"Test issuer {i}"} for i in range(1000)}
    rows["1000"] = {"ticker": "AAPL", "cik_str": 320193, "title": "Apple Inc."}
    return {"requested": 1, **secmap.archive(path, json.dumps(rows).encode(),
                                                observed_at=NOW.isoformat())}


def _state():
    state = {}
    def reserve(key):
        used = state.get(key, 0)
        if used >= sec_refresh.MAX_REQUESTS_PER_MONTH:
            return False
        state[key] = used + 1
        return True
    return state, reserve


def test_no_real_contact_never_reserves_or_fetches(tmp_path, monkeypatch):
    monkeypatch.setattr(edgar, "available", lambda: False)
    state, reserve = _state()
    result = sec_refresh.run(tmp_path / "m.db", tmp_path / "f.db", ["AAPL"], now=NOW,
                             state_get=state.get, state_set=state.__setitem__, reserve=reserve,
                             map_collect=lambda _: (_ for _ in ()).throw(AssertionError("map")),
                             facts_collect=lambda *_: (_ for _ in ()).throw(AssertionError("facts")))
    assert result["status"] == "missing_real_contact" and result["requested"] == 0
    assert state == {}


def test_bounded_map_and_one_fact_then_cooldown(tmp_path, monkeypatch):
    monkeypatch.setattr(edgar, "available", lambda: True)
    monkeypatch.setattr(secmap, "_now", lambda: NOW.isoformat())
    state, reserve = _state()
    seen = []
    def facts(_path, cik):
        seen.append(cik)
        return {"status": "ok", "response_bytes": 19}
    opts = dict(now=NOW, state_get=state.get, state_set=state.__setitem__,
                reserve=reserve, map_collect=_map, facts_collect=facts)
    first = sec_refresh.run(tmp_path / "m.db", tmp_path / "f.db",
                            ["005930", "AAPL", "AAPL"], **opts)
    assert first["status"] == "ok" and first["requested"] == 2 and first["ok"] == 2
    assert seen == ["0000320193"]
    assert state[sec_refresh._month_key(NOW)] == 2
    again = sec_refresh.run(tmp_path / "m.db", tmp_path / "f.db", ["AAPL"], **opts)
    assert again["status"] == "not_due" and again["requested"] == 0
    assert state[sec_refresh._month_key(NOW)] == 2


def test_failed_mapping_never_falls_back_to_old_or_guessed_cik(tmp_path, monkeypatch):
    monkeypatch.setattr(edgar, "available", lambda: True)
    state, reserve = _state()
    item = sec_refresh.run(tmp_path / "m.db", tmp_path / "f.db", ["AAPL"], now=NOW,
                           state_get=state.get, state_set=state.__setitem__, reserve=reserve,
                           map_collect=lambda _: {"status": "collection_failed", "requested": 1},
                           facts_collect=lambda *_: (_ for _ in ()).throw(AssertionError("facts")))
    assert item["status"] == "partial_failure" and item["requested"] == 1
    assert not (tmp_path / "f.db").exists()


@pytest.mark.parametrize("step", ["map_start", "map_final", "fact_start", "fact_final"])
def test_state_failure_preserves_actual_sec_call_accounting(tmp_path, monkeypatch, step):
    monkeypatch.setattr(edgar, "available", lambda: True)
    monkeypatch.setattr(secmap, "_now", lambda: NOW.isoformat())
    map_path, facts_path = tmp_path / "m.db", tmp_path / "f.db"
    if step.startswith("fact"):
        _map(map_path)
    state, reserve = _state()
    seen = []

    def save(key, value):
        source = "map" if key == "sec_evidence_map_attempt" else "fact"
        if source == step.split("_")[0] and (value["status"] == "started") == step.endswith("start"):
            raise OSError("state storage unavailable")
        state[key] = value

    def collect_map(path):
        seen.append("map")
        return _map(path)

    def collect_fact(_path, _cik):
        seen.append("fact")
        return {"status": "ok", "response_bytes": 37}

    result = sec_refresh.run(map_path, facts_path, ["AAPL"], now=NOW,
                             state_get=state.get, state_set=save, reserve=reserve,
                             map_collect=collect_map, facts_collect=collect_fact)
    assert result["status"] == "state_failure"
    expected = 0 if step.endswith("start") else 1
    assert result["requested"] == result["ok"] == expected
    assert result["failed"] == 0 and len(seen) == expected
    assert state[sec_refresh._month_key(NOW)] == 1
    if step == "fact_final":
        assert result["response_bytes"] == 37 and result["facts_status"] == "ok"
    if step == "map_final":
        assert result["map_status"] == "ok" and "fact" not in seen


def test_monthly_limit_and_non_us_favorite(tmp_path, monkeypatch):
    monkeypatch.setattr(edgar, "available", lambda: True)
    state, reserve = _state()
    item = sec_refresh.run(tmp_path / "m.db", tmp_path / "f.db", ["005930"], now=NOW,
                           state_get=state.get, state_set=state.__setitem__, reserve=reserve)
    assert item["status"] == "no_us_favorites" and not state
    state[sec_refresh._month_key(NOW)] = sec_refresh.MAX_REQUESTS_PER_MONTH
    item = sec_refresh.run(tmp_path / "m.db", tmp_path / "f.db", ["AAPL"], now=NOW,
                           state_get=state.get, state_set=state.__setitem__, reserve=reserve)
    assert item["status"] == "budget_exhausted" and item["requested"] == 0


def test_malformed_cik_does_not_coerce_to_real_issuer():
    rows = {str(i): {"ticker": f"T{i:04d}", "cik_str": 100000 + i,
                     "title": f"Test issuer {i}"} for i in range(1000)}
    rows["1000"] = {"ticker": "AAPL", "cik_str": 320193.9, "title": "Wrong"}
    mapping, _ambiguous = secmap.parse(json.dumps(rows).encode())
    assert "AAPL" not in mapping


def test_api_refresh_records_sec_operation_without_touching_score(tmp_path, monkeypatch):
    from signal_desk import api
    from signal_desk.ingest import evidence_ops

    state = {}
    observed = []
    monkeypatch.setattr(api.db, "fav_tickers_all", lambda: {"AAPL"})
    monkeypatch.setattr(api.db, "kv_get", state.get)
    monkeypatch.setattr(api.db, "kv_set", state.__setitem__)
    monkeypatch.setattr(sec_refresh, "run", lambda *_args, **_kwargs: {
        "status": "missing_real_contact", "requested": 0, "ok": 0, "failed": 0,
        "response_bytes": 0})
    monkeypatch.setattr(evidence_ops, "record", lambda source, **kwargs: observed.append((source, kwargs)))
    api._refresh_sec_evidence_daily(NOW)
    assert state["sec_evidence_refresh_last"]["status"] == "missing_real_contact"
    assert observed == [("sec", {"when": NOW, "result": state["sec_evidence_refresh_last"]})]
