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


def _episode():
    s = {**_snapshot(loss=True), "version": rs.VERSION, "market": "kr",
         "session": "2026-09-23", "cost_assumptions": rs.execution.cost_assumptions("kr")}
    s["decisions"] = rs.decide(s, "balanced", review_due=True)
    for decision in s["decisions"].values():
        decision["fixed_orders"] = rs._fixed_orders(s, decision["pairs"])
    return s


def test_forward_paired_costs_hold_and_price_gap_block():
    s = _episode()
    assert s["decisions"]["champion_rotation_proxy"]["fixed_orders"] == []
    assert s["decisions"]["s0_rank_buffer"]["fixed_orders"]
    sessions = [f"2026-10-{i:02d}" for i in range(1, 21)]
    marks = {day: {"HELD": 80.0, "NEW": 100 + i} for i, day in enumerate(sessions)}
    result = rs.evaluate(s, marks, sessions, completed_session=sessions[19])
    assert result["complete"] and result["delta_net_pp"]["h20"] > 0
    assert result["metrics"]["s0_rank_buffer"]["cost_drag_pct"] > 0
    assert result["metrics"]["champion_rotation_proxy"]["orders"] == 0
    assert result["metrics"]["s0_rank_buffer"]["traded_notional_pct"] > 0
    assert result["metrics"]["s0_rank_buffer"]["h20"]["max_drawdown_pct"] <= 0
    assert rs.evaluate(s, marks, sessions, completed_session=sessions[19],
                       revision_halt="HELD:2026-09-23")["ready"] is False
    del marks[sessions[2]]
    assert "누락" in rs.evaluate(s, marks, sessions, completed_session=sessions[19])["reason"]
    marks[sessions[2]] = {"HELD": 80.0, "NEW": 102.0}
    marks[sessions[0]]["NEW"] = 2000.0
    assert "가격 갭" in rs.evaluate(s, marks, sessions, completed_session=sessions[19])["reason"]


def test_forward_marks_are_first_observed_and_revision_halts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = _episode()
    db.rotation_shadow_add_once(900002, "kr", s["session"], s)
    prices = {"HELD": [80.0, 80.0, 80.0], "NEW": [100.0, 101.0, 102.0]}
    dates = {ticker: ["2026-09-23", "2026-09-28", "2026-09-29"] for ticker in prices}
    monkeypatch.setattr(rs.store, "load_portfolio_close_bundle", lambda market: (prices, dates))
    first = rs.collect_forward("kr", dt.datetime(2026, 9, 28, 9, 30, tzinfo=dt.timezone.utc))
    assert first["marked"] == 2
    assert rs.collect_forward("kr", dt.datetime(2026, 9, 28, 9, 30, tzinfo=dt.timezone.utc))["marked"] == 0
    assert db.rotation_shadow_marks(900002, "kr", s["session"])["2026-09-28"]["NEW"] == 101
    prices["HELD"][0] = 79.0  # 최초 판단 종가가 사후 수정됨
    second = rs.collect_forward("kr", dt.datetime(2026, 9, 29, 9, 30, tzinfo=dt.timezone.utc))
    assert second["revision_halts"] == 1
    assert db.rotation_shadow_revision_halt(900002, "kr", s["session"]) == "HELD:2026-09-23"
    assert "2026-09-29" not in db.rotation_shadow_marks(900002, "kr", s["session"])


def test_a_missed_snapshot_names_the_holding_gap_alone():
    reason = rs._skip_reason(saved=0, account_seen=3, holding_mismatch=3, bad_cash=0, frozen=0)
    assert reason == "보유 종가가 마감 세션과 다른 계좌 3"
    assert "또는" not in reason
    assert rs._skip_reason(saved=1, account_seen=1, holding_mismatch=1, bad_cash=0, frozen=0) is None
    assert rs._skip_reason(saved=0, account_seen=0, holding_mismatch=0, bad_cash=0, frozen=2) == "이미 동결된 계좌 2"
