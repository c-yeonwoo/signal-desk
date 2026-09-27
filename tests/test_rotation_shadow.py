"""R11: 회전 판단은 첫 관측을 보존하고 주문 경로와 격리한다."""

import datetime as dt
import json

import pandas as pd

from signal_desk import db
from signal_desk.signals import rotation_shadow as rs


def _snapshot(*, loss=False):
    signals = [{"ticker": "HELD", "score": 0.1, "kind": "HOLD", "rank": 20,
                "event_risk": False, "price": 80 if loss else 100},
               {"ticker": "NEW", "score": 1.8, "kind": "BUY", "rank": 1,
                "event_risk": False, "price": 100}]
    signals += [{"ticker": f"X{i}", "score": 0, "kind": "HOLD", "rank": i + 2,
                 "event_risk": False, "price": 100} for i in range(28)]
    return {"signals": signals, "holdings": [{"ticker": "HELD", "qty": 10,
            "avg_price": 100, "price": 80 if loss else 100,
            "calendar_days_held": 15, "hold_sessions": 10}],
            "cash": 0, "tranche_alloc": 500, "max_positions": 1,
            "min_buy_score": 1.2, "warned": [], "recent_sold": []}


def test_s0_rank_buffer_and_loss_are_independent_of_champion():
    s = _snapshot(loss=True)
    decisions = rs.decide(s, "balanced", review_due=True)
    assert decisions["champion_rotation_proxy"]["action"] == "hold"
    assert decisions["s0_rank_buffer"]["pairs"][0]["out"] == "HELD"
    assert rs.decide(s, "balanced", review_due=False)["s0_rank_buffer"]["action"] == "hold"
    s["signals"][0]["rank"] = 2  # 계속 보유 버퍼 안으로 회복
    assert rs.decide(s, "balanced", review_due=True)["s0_rank_buffer"]["action"] == "hold"


def test_s0_keeps_unknown_holding_and_rejects_ineligible_candidate():
    s = _snapshot()
    s["signals"] = [row for row in s["signals"] if row["ticker"] != "HELD"]
    assert rs.decide(s, "balanced", review_due=True)["s0_rank_buffer"]["action"] == "hold"
    s = _snapshot()
    s["warned"] = ["NEW"]
    assert rs.decide(s, "balanced", review_due=True)["s0_rank_buffer"]["action"] == "hold"


def test_pit_capture_is_once_only_and_stale_bar_fails_closed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    session = "2026-09-23"
    now = dt.datetime(2026, 9, 23, 9, 30, tzinfo=dt.timezone.utc)
    db.kv_set("paper_account:900002", json.dumps({"cash": 0, "positions": {
        "HELD": {"qty": 10, "avg_price": 100}}}))
    db.bot_position_upsert(900002, "HELD", "Held", 10, 100, 100, "2026-09-01")
    rows = pd.DataFrame([{"date": session, "ticker": t, "score": score, "kind": kind,
                          "rank": rank, "event_risk": 0, "session_valid": True,
                          "bar_asof": session}
                         for t, score, kind, rank in
                         (("HELD", 0.1, "HOLD", 20), ("NEW", 1.8, "BUY", 1))])
    monkeypatch.setattr(rs.store, "load_signal_history", lambda market: rows)
    monkeypatch.setattr(rs.store, "load_portfolio_close_bundle", lambda market: (
        {"HELD": [100.0], "NEW": [100.0]}, {"HELD": [session], "NEW": [session]}))
    monkeypatch.setattr(rs.store, "load_warned_tickers", lambda: set())
    first = rs.capture("kr", now)
    assert first["saved"] == 1, first
    saved = db.rotation_shadow_recent(900002, "kr")[0]
    assert saved["decisions"]["s0_rank_buffer"]["action"] == "hold"
    assert saved["decisions"]["s0_rank_buffer"]["reason"] == "기존 보유 슬롯 여유"
    rows.loc[rows["ticker"] == "NEW", "score"] = -3.0
    assert rs.capture("kr", now)["saved"] == 0
    assert db.rotation_shadow_recent(900002, "kr")[0]["signals"][1]["score"] == 1.8
    assert db.rotation_shadow_add_once(900002, "kr", session, {"tampered": True}) is False
    assert "tampered" not in db.rotation_shadow_recent(900002, "kr")[0]

    # 다른 계좌/세션에서는 가격 바가 하루 오래되면 저장하지 않는다.
    db.kv_set("paper_account:900001", json.dumps({"cash": 10, "positions": {}}))
    monkeypatch.setattr(rs.store, "load_portfolio_close_bundle", lambda market: (
        {"HELD": [100.0], "NEW": [100.0]}, {"HELD": [session], "NEW": ["2026-09-24"]}))
    assert rs.capture("kr", now)["saved"] == 0
    assert db.rotation_shadow_recent(900001, "kr") == []


def test_capture_rejects_reconstructed_old_holding(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = rs.capture("kr", dt.datetime(2026, 9, 24, 8, 0, tzinfo=dt.timezone.utc))
    assert result["saved"] == 0
    assert "관측 지연" in result["reason"]


def test_rotation_shadow_api_is_admin_only_and_inputs_are_opt_in(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from signal_desk import api

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_EMAILS", "rotation-admin@example.com")
    db.rotation_shadow_add_once(900002, "kr", "2026-09-23", {
        "decisions": {"s0_rank_buffer": {"action": "hold", "pairs": []}},
        "signals": [{"ticker": "SECRET"}], "holdings": [{"ticker": "HELD"}]})
    guest = TestClient(api.app)
    assert guest.get("/api/admin/research/rotation-shadow").status_code == 401
    guest.post("/api/auth/signup", json={"email": "reader@example.com", "pw": "abcdef12"})
    assert guest.get("/api/admin/research/rotation-shadow").status_code == 403
    admin = TestClient(api.app)
    admin.post("/api/auth/signup", json={"email": "rotation-admin@example.com", "pw": "abcdef12"})
    response = admin.get("/api/admin/research/rotation-shadow?market=kr&style=balanced")
    assert response.status_code == 200
    assert response.json()["live_eligible"] is False
    assert "signals" not in response.json()["snapshots"][0]
    detailed = admin.get("/api/admin/research/rotation-shadow?include_inputs=true")
    assert detailed.json()["snapshots"][0]["signals"][0]["ticker"] == "SECRET"
