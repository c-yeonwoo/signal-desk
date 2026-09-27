"""R13b fixed-look inference cannot peek, replace missing blocks or promote live."""

import datetime as dt

from fastapi.testclient import TestClient

from signal_desk import db
from signal_desk.signals import revision_price_freeze as fr, revision_price_verdict as gate


def _episodes(n=12, value=1.0):
    start = dt.date(2026, 9, 28)
    return [{"version": fr.VERSION, "session": (start + dt.timedelta(days=21 * i)).isoformat(),
             "policies": {"eps_revision_only": ["A"], "revision_unreacted_price": ["B"]},
             "forward": {"ready": True, "status": "complete", "delta_net_pp": value}}
            for i in range(n)]


def _calendar(monkeypatch):
    monkeypatch.setattr(gate.market_clock, "next_sessions", lambda market, day, count: [
        (dt.date.fromisoformat(day) + dt.timedelta(days=i)).isoformat() for i in range(1, count + 1)])


def test_positive_result_is_only_a_research_signal(monkeypatch):
    _calendar(monkeypatch)
    rows = _episodes(13)
    rows[-1]["forward"]["delta_net_pp"] = -100
    result = gate.assess(rows, completed_session="2027-07-31")
    assert result["status"] == "positive_research_signal"
    assert result["look"] == 12 and result["net_delta_mean_pp"] == 1
    assert result["auto_promote"] is False and result["live_eligible"] is False
    assert result["source_available_at_verified"] is False
    assert gate.assess(rows, completed_session="2027-02-01")["status"] == "awaiting_oos"


def test_blocked_preselected_episode_stays_in_coverage(monkeypatch):
    _calendar(monkeypatch)
    rows = _episodes()
    rows[0]["forward"] = {"ready": False, "status": "blocked"}
    rows.append({**rows[0], "session": "2026-10-01", "forward": {"ready": True,
                 "status": "complete", "delta_net_pp": 100}})
    result = gate.assess(rows, completed_session="2027-07-31")
    assert result["status"] == "blocked_data_quality"
    assert result["matured_blocks"] == 12 and result["effective_blocks"] == 11
    assert result["blocked_sessions"] == ["2026-09-28"]


def test_admin_verdict_route_is_read_only_and_uses_full_history(tmp_path, monkeypatch):
    from signal_desk import api

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_EMAILS", "revision-verdict@example.com")
    monkeypatch.setattr(api, "_rl_hits", {})
    for row in _episodes(35):
        assert db.revision_price_add_once(row["session"], row)
    assert len(db.revision_price_all(fr.START_SESSION)) == 35
    assert TestClient(api.app).get("/api/admin/research/revision-price/verdict").status_code == 401
    admin = TestClient(api.app)
    assert admin.post("/api/auth/signup", json={"email": "revision-verdict@example.com", "pw": "abcdef12"}).status_code == 200
    response = admin.get("/api/admin/research/revision-price/verdict")
    assert response.status_code == 200
    assert response.json()["auto_promote"] is False
