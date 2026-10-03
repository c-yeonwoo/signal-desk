"""Official evidence runs only after the KR close and never blocks peers."""

import datetime as dt

from signal_desk import api
from signal_desk.ingest import fed_g17


KST = dt.timezone(dt.timedelta(hours=9))


def test_official_collectors_wait_for_session_close_and_daily_snapshot(monkeypatch):
    state = {}
    calls = []
    monkeypatch.setattr(api.db, "kv_get", lambda key: state.get(key))
    monkeypatch.setattr(api.db, "kv_set", lambda key, value: state.__setitem__(key, value))
    monkeypatch.setattr(api, "_kst_today", lambda: "2026-10-06")
    monkeypatch.setattr(api, "_refresh_financial_evidence_daily", lambda: calls.append("dart"))
    monkeypatch.setattr(fed_g17, "refresh", lambda *a, **k: (calls.append("g17"), {"status": "not_due"})[1])
    monkeypatch.setattr(api.db, "kv_transform", lambda *a, **k: calls.append("sec_gate"))
    monkeypatch.setattr(api, "_record_official_evidence_ops", lambda *a: None)

    api._collect_official_evidence_after_close(dt.datetime(2026, 10, 5, 16, 0, tzinfo=KST))
    api._collect_official_evidence_after_close(dt.datetime(2026, 10, 6, 15, 39, tzinfo=KST))
    api._collect_official_evidence_after_close(dt.datetime(2026, 10, 6, 15, 40, tzinfo=KST))
    assert calls == []

    state["bot_daily_snap"] = "2026-10-06"
    api._collect_official_evidence_after_close(dt.datetime(2026, 10, 6, 15, 40, tzinfo=KST))
    assert calls == ["dart", "g17", "sec_gate"]
    assert state["financial_evidence_refresh_date"] == "2026-10-06"


def test_official_collector_exceptions_are_recorded_without_blocking_peers(monkeypatch):
    day = "2026-10-06"
    now = dt.datetime(2026, 10, 6, 15, 40, tzinfo=KST)
    state = {"bot_daily_snap": day}
    events = []
    calls = []

    def transform(key, fn):
        value, result = fn(state.get(key))
        if value is not None:
            state[key] = value
        return result

    def fail(label):
        calls.append(label)
        raise RuntimeError(label)

    monkeypatch.setattr(api, "_kst_today", lambda: day)
    monkeypatch.setattr(api.db, "kv_get", lambda key: state.get(key))
    monkeypatch.setattr(api.db, "kv_set", lambda key, value: state.__setitem__(key, value))
    monkeypatch.setattr(api.db, "kv_transform", transform)
    monkeypatch.setattr(api, "_refresh_financial_evidence_daily", lambda: fail("dart"))
    monkeypatch.setattr(fed_g17, "refresh", lambda *a, **k: fail("g17"))
    monkeypatch.setattr(api, "_refresh_sec_evidence_daily", lambda at: fail("sec"))
    monkeypatch.setattr(api, "_record_official_evidence_ops",
                        lambda source, when, result: events.append((source, when, result)))

    api._collect_official_evidence_after_close(now)

    assert calls == ["dart", "g17", "sec"]
    assert [source for source, _, _ in events] == ["dart", "fed_g17", "sec"]
    assert all(when == now and result["status"] == "collection_failed" and result["requested"] == 0
               for _, when, result in events)
    assert state["financial_evidence_refresh_date"] == day
    assert state["sec_evidence_refresh_date"] == day


def test_g17_no_request_poll_does_not_hide_later_same_day_request(monkeypatch):
    day = "2026-10-06"
    now = dt.datetime(2026, 10, 6, 15, 40, tzinfo=KST)
    state = {"bot_daily_snap": day, "financial_evidence_refresh_date": day,
             "sec_evidence_refresh_date": day}
    results = iter([{"status": "not_due", "requested": 0},
                    {"status": "ok", "requested": 1, "response_bytes": 123}])
    events = []
    monkeypatch.setattr(api, "_kst_today", lambda: day)
    monkeypatch.setattr(api.db, "kv_get", lambda key: state.get(key))
    monkeypatch.setattr(api.db, "kv_set", lambda key, value: state.__setitem__(key, value))
    monkeypatch.setattr(api.db, "kv_transform", lambda *a: False)
    monkeypatch.setattr(fed_g17, "refresh", lambda *a, **k: next(results))
    monkeypatch.setattr(api, "_record_official_evidence_ops",
                        lambda source, when, result: events.append((source, result)))

    api._collect_official_evidence_after_close(now)
    api._collect_official_evidence_after_close(now + dt.timedelta(minutes=30))

    assert events == [("fed_g17", {"status": "ok", "requested": 1, "response_bytes": 123})]
