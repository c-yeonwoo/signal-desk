"""R11: 비중첩 사전등록 판정은 실주문 권한이 아니다."""

import datetime as dt

from signal_desk.signals import rotation_shadow, rotation_verdict as gate


def _episode(day: str, *, net: float = 1.0, turnover: float = -5.0,
             cost: float = -0.1, mdd: float = 0.0, complete: bool = True) -> dict:
    champion = {"traded_notional_pct": 10.0, "cost_drag_pct": 0.2,
                "h20": {"max_drawdown_pct": -5.0}}
    challenger = {"traded_notional_pct": 10.0 + turnover, "cost_drag_pct": 0.2 + cost,
                  "h20": {"max_drawdown_pct": -5.0 + mdd}}
    return {"version": rotation_shadow.VERSION, "session": day,
            "decisions": {"champion_rotation_proxy": {"fixed_orders": []},
                          "s0_rank_buffer": {"fixed_orders": [{"side": "buy", "qty": 1}]}},
            "forward": {"ready": complete, "complete": complete,
                        "delta_net_pp": {"h20": net},
                        "metrics": {"champion_rotation_proxy": champion,
                                    "s0_rank_buffer": challenger}}}


def _sessions(monkeypatch):
    monkeypatch.setattr(gate.market_clock, "next_sessions", lambda market, day, n: [
        (dt.date.fromisoformat(day) + dt.timedelta(days=i)).isoformat() for i in range(1, n + 1)])


def _blocks(n=12):
    start = dt.date(2026, 9, 28)
    return [_episode((start + dt.timedelta(days=21 * i)).isoformat()) for i in range(n)]


def test_gate_needs_fixed_nonoverlap_look_and_never_auto_promotes(monkeypatch):
    _sessions(monkeypatch)
    episodes = _blocks()
    episodes.append(_episode("2026-10-03", net=1000.0))  # 첫 블록과 겹침 — 성적 좋아도 배제
    result = gate.assess(episodes, market="kr", completed_session="2027-06-30")
    assert result["status"] == "manual_review_candidate"
    assert result["look"] == 12 and result["effective_blocks"] == 12
    assert result["net_delta_mean_pp"] == 1.0
    assert result["live_eligible"] is False and result["auto_promote"] is False
    assert result["divergent_episodes"] == 13 and result["nonoverlap_blocks"] == 12
    assert gate.assess(episodes, market="kr", completed_session="2027-02-01")["status"] == "awaiting_oos"


def test_gate_keeps_failed_preselected_block_in_coverage(monkeypatch):
    _sessions(monkeypatch)
    episodes = _blocks()
    episodes[3]["forward"]["ready"] = False
    episodes.append(_episode("2026-12-06", net=999.0))  # 빈 곳을 겹치는 표본으로 메우지 않음
    result = gate.assess(episodes, market="kr", completed_session="2027-06-30")
    assert result["status"] == "blocked_data_quality"
    assert result["effective_blocks"] == 11
    assert episodes[3]["session"] in result["blocked_sessions"]


def test_gate_requires_net_cost_turnover_and_risk_together(monkeypatch):
    _sessions(monkeypatch)
    mixed = _blocks()
    for i, episode in enumerate(mixed):
        episode["forward"]["delta_net_pp"]["h20"] = 1.0 if i % 2 else -1.0
    assert gate.assess(mixed, market="us", completed_session="2027-06-30")["status"] == "inconclusive"
    more_trades = _blocks()
    for episode in more_trades:
        episode["forward"]["metrics"]["s0_rank_buffer"]["traded_notional_pct"] = 12.0
    assert gate.assess(more_trades, market="kr", completed_session="2027-06-30")["status"] == "risk_or_cost_failed"
    more_drawdown = _blocks()
    more_drawdown[0]["forward"]["metrics"]["s0_rank_buffer"]["h20"]["max_drawdown_pct"] = -9.0
    assert gate.assess(more_drawdown, market="kr", completed_session="2027-06-30")["status"] == "risk_or_cost_failed"


def test_gate_only_peeks_at_registered_counts(monkeypatch):
    _sessions(monkeypatch)
    episodes = _blocks(13)
    episodes[-1]["forward"]["delta_net_pp"]["h20"] = -100.0
    result = gate.assess(episodes, market="kr", completed_session="2027-07-31")
    assert result["look"] == 12 and result["net_delta_mean_pp"] == 1.0
    assert result["next_look"] == 24


def test_verdict_route_is_admin_only_and_starts_without_samples(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from signal_desk import api

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_EMAILS", "rotation-gate@example.com")
    monkeypatch.setattr(api, "_rl_hits", {})  # 앞선 API 테스트의 TestClient IP별 회원가입 제한과 격리
    guest = TestClient(api.app)
    assert guest.get("/api/admin/research/rotation-shadow/verdict").status_code == 401
    guest.post("/api/auth/signup", json={"email": "reader@example.com", "pw": "abcdef12"})
    assert guest.get("/api/admin/research/rotation-shadow/verdict").status_code == 403
    admin = TestClient(api.app)
    signup = admin.post("/api/auth/signup", json={"email": "rotation-gate@example.com", "pw": "abcdef12"})
    assert signup.status_code == 200, signup.text
    result = admin.get("/api/admin/research/rotation-shadow/verdict")
    assert result.status_code == 200
    assert result.json()["status"] == "awaiting_oos"
    assert result.json()["auto_promote"] is False


def test_gate_history_is_chronological_and_not_recent_truncated(tmp_path, monkeypatch):
    from signal_desk import db

    monkeypatch.chdir(tmp_path)
    for i in range(35):
        day = (dt.date(2026, 9, 28) + dt.timedelta(days=i)).isoformat()
        db.rotation_shadow_add_once(900002, "kr", day, {
            "version": rotation_shadow.VERSION, "decisions": {"s0_rank_buffer": {"fixed_orders": [i]}},
            "signals": [{"ticker": "private-input"}]})
    rows = db.rotation_shadow_gate_rows(900002, "kr", gate.START_SESSION)
    assert len(rows) == 35 and rows[0]["session"] == "2026-09-28"
    assert "signals" not in rows[0]
    assert db.rotation_shadow_get(900002, "kr", "2026-09-28")["signals"][0]["ticker"] == "private-input"
