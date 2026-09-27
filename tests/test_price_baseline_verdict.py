"""R12 statistical alert needs non-overlapping paired net outcomes and never promotes."""

import datetime as dt

from fastapi.testclient import TestClient

from signal_desk import db
from signal_desk.signals import price_baseline_shadow as pb, price_baseline_verdict as gate


def _rows(n=12):
    start = dt.date(2026, 9, 28)
    return [{"version": pb.VERSION, "session": (start + dt.timedelta(days=21 * i)).isoformat(),
             "policies": {"price_3factor": ["A"], "sector_momentum": ["B"]},
             "forward": {"ready": True, "status": "complete", "delta_net_pp": 1.0}}
            for i in range(n)]


def _calendar(monkeypatch):
    monkeypatch.setattr(gate.market_clock, "next_sessions", lambda market, day, count: [
        (dt.date.fromisoformat(day) + dt.timedelta(days=i)).isoformat() for i in range(1, count + 1)])


def test_fixed_look_corrected_signal_never_promotes(monkeypatch):
    _calendar(monkeypatch)
    rows = _rows(13)
    rows[-1]["forward"]["delta_net_pp"] = -100
    result = gate.assess(rows, market="kr", completed_session="2027-07-31")
    assert result["status"] == "positive_research_signal"
    assert result["look"] == 12 and result["net_delta_mean_pp"] == 1
    assert result["live_eligible"] is False and result["auto_promote"] is False
    assert gate.assess(rows, market="kr", completed_session="2027-02-01")["status"] == "awaiting_oos"


def test_missing_block_is_not_replaced_and_overlap_is_skipped(monkeypatch):
    _calendar(monkeypatch)
    rows = _rows()
    rows[0]["forward"] = {"ready": False, "status": "blocked"}
    rows.append({**rows[0], "session": "2026-10-01", "forward": {"ready": True,
                       "status": "complete", "delta_net_pp": 100}})
    result = gate.assess(rows, market="us", completed_session="2027-07-31")
    assert result["status"] == "blocked_data_quality"
    assert result["effective_blocks"] == 11
    assert result["matured_blocks"] == 12
    assert result["blocked_sessions"] == ["2026-09-28"]


def test_gate_api_reads_all_history_and_requires_admin(tmp_path, monkeypatch):
    from signal_desk import api

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_EMAILS", "price-gate@example.com")
    monkeypatch.setattr(api, "_rl_hits", {})
    for row in _rows(35):
        assert db.price_baseline_add_once("kr", row["session"], row)
    assert len(db.price_baseline_all("kr", gate.START_SESSION)) == 35
    assert TestClient(api.app).get("/api/admin/research/price-baseline/verdict").status_code == 401
    admin = TestClient(api.app)
    assert admin.post("/api/auth/signup", json={"email": "price-gate@example.com", "pw": "abcdef12"}).status_code == 200
    response = admin.get("/api/admin/research/price-baseline/verdict")
    assert response.status_code == 200
    assert response.json()["auto_promote"] is False
