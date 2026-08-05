"""증명 OS · 픽 이유 — 북극성 A 계약."""

from __future__ import annotations

import json

from signal_desk.signals import pick_reason, proof
from signal_desk.signals.engine import SignalResult
from signal_desk.signals.decision import Decision


def _sig(**kw):
    base = dict(
        ticker="005930", name="삼성전자", score=1.8, kind="BUY", confidence=0.6,
        technical_score=0.4, fundamental_score=0.2, has_fundamental=True,
        reasons=["[기술] RSI 28 — 과매도", "[선정] 시장 200종목 중 3위"],
        factor_scores={"technical": 0.5, "fundamental": 0.2},
        rank=3, rank_pct=1.5, rank_eligible=True, gate_blocked=False,
    )
    base.update(kw)
    return SignalResult(**base)


def test_from_signal_includes_reasons_and_decision():
    s = _sig(decision=Decision(True, "trim", 9, "serious", "실적 하향", "p2"),
             event_risk=True, gate_blocked=True)
    pr = pick_reason.from_signal(s)
    assert pr["ticker"] == "005930"
    assert pr["rank"] == 3 and pr["gate_blocked"] is True
    assert "[기술]" in pr["reasons"][0]
    assert pr["decision"]["severity"] == "serious"
    assert pr["decision"]["buy_blocked"] is True


def test_history_meta_roundtrip_in_snapshot(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data/cache").mkdir(parents=True)
    from signal_desk import store

    s = _sig(gate_blocked=True, rank=2, rank_eligible=False,
             decision=Decision(True, "exit", 1, "critical", "상폐 위험", "p2"))
    store.snapshot_signals([s], date="2026-08-01")
    df = store.load_signal_history()
    row = df.iloc[0].to_dict()
    assert int(row["gate_blocked"]) == 1
    assert int(row["rank"]) == 2
    assert row["decision_severity"] == "critical"
    reasons = pick_reason.parse_reasons_json(row["reasons_json"])
    assert any("RSI" in r for r in reasons)
    rebuilt = pick_reason.from_history_row(row)
    assert rebuilt["gate_blocked"] and rebuilt["decision"]["severity"] == "critical"


def test_postmortem_ready_and_forward():
    rows = [{
        "date": "2026-07-01", "ticker": "AAA", "score": 1.5, "kind": "BUY",
        "technical": 0.3, "fundamental": 0.1, "valuation": 80, "reversion": 0.0,
        "qualitative": None, "flow": None, "quality": None, "momentum": None,
        "short": None, "rank": 1, "rank_eligible": 1, "gate_blocked": 0,
        "event_risk": 0, "decision_severity": None, "decision_blocked": 0,
        "decision_summary": None,
        "reasons_json": json.dumps(["[기술] 골든크로스"], ensure_ascii=False),
    }]
    # 시그널일 다음 거래일 진입 → h5 성숙하려면 날짜 6개+
    dates = [f"2026-07-{d:02d}" for d in range(1, 12)]
    closes = [100.0 + i for i in range(len(dates))]
    out = pick_reason.postmortem(
        "2026-07-01", "AAA",
        history_rows=rows,
        closes_by_ticker={"AAA": (dates, closes)},
        bot_decisions=[],
    )
    assert out["ready"] is True
    assert out["pick"]["reasons"][0].startswith("[기술]")
    assert out["forward_ret_pct"]["h5"] is not None


def test_latest_picks_most_recent_date():
    rows = [
        {"date": "2026-07-01", "ticker": "AAA", "score": 1.0, "kind": "BUY",
         "technical": 0.1, "fundamental": None, "valuation": None, "reversion": None,
         "qualitative": None, "flow": None, "quality": None, "momentum": None,
         "short": None, "rank": 2, "rank_eligible": 1, "gate_blocked": 0,
         "event_risk": 0, "decision_severity": None, "decision_blocked": 0,
         "decision_summary": None, "reasons_json": "[]"},
        {"date": "2026-07-08", "ticker": "AAA", "score": 1.8, "kind": "STRONG_BUY",
         "technical": 0.2, "fundamental": None, "valuation": None, "reversion": None,
         "qualitative": None, "flow": None, "quality": None, "momentum": None,
         "short": None, "rank": 1, "rank_eligible": 1, "gate_blocked": 0,
         "event_risk": 0, "decision_severity": None, "decision_blocked": 0,
         "decision_summary": None,
         "reasons_json": json.dumps(["[모멘텀] 상위"], ensure_ascii=False)},
    ]
    out = pick_reason.latest(
        "AAA", history_rows=rows, closes_by_ticker={}, bot_decisions=None)
    assert out["ready"] and out["date"] == "2026-07-08"
    slim = pick_reason.slim_for_detail(out)
    assert slim["date"] == "2026-07-08" and slim["rank"] == 1
    assert slim["reasons"][0].startswith("[모멘텀]")
    assert pick_reason.slim_for_detail({"ready": False}) is None


def test_proof_build_marks_a_primary():
    payload = proof.build(
        accuracy={"ready": True, "buy_lift_pp": 4.0, "factor_ic": {"score": 0.1},
                  "coverage": {"rows": 100, "matured_primary": 40}},
        advisor_shadow={"verdict_ready": True, "paired_delta_pct": 1.2,
                        "paired_verdict_ready": True, "paired_n": 25},
        climate_shadow={"verdict_ready": False, "blocked_reason": "표본 미달"},
        kb_coverage_shadow={"verdict_ready": False, "blocked_reason": "대기"},
        harness_last={"ready": True, "verdict": "판별력 있음",
                      "vs_random": {"percentile": 97.0}, "saved_at": "t"},
        paper_scorecard={"resolved": 3, "win_rate": 66.7},
    )
    assert payload["north_star"] == "selection"
    assert payload["A"]["name"] == "시그널 판별력"
    assert payload["A"]["primary"] is True
    assert payload["B"]["primary"] is False
    assert payload["contract"]["qualitative_in_combine"] is False
    assert payload["A"]["harness"]["verdict"] == "판별력 있음"
    assert "advisor" in payload["A"]["shadows_verdict_ready"]


def test_save_load_harness_last(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data/cache").mkdir(parents=True)
    from signal_desk import store
    store.save_harness_last(
        {"ready": True, "verdict": "판별력 있음", "vs_random": {"percentile": 96}},
        market="kr",
    )
    got = store.load_harness_last()
    assert got["ready"] and got["verdict"] == "판별력 있음"
    assert got["market"] == "kr" and got.get("saved_at")


def test_only_preregistered_locked_runs_reach_the_board(tmp_path, monkeypatch):
    """API/마감루프가 쓰는 store.run_harness — 패널만 스텁해도 저장 경로를 검증."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data/cache").mkdir(parents=True)
    from signal_desk import store
    from signal_desk.signals import harness as hz

    monkeypatch.setattr(store, "is_ready", lambda: True)
    monkeypatch.setattr(store, "load_universe",
                        lambda: [{"ticker": "A"}, {"ticker": "B"}])
    dates = [f"2025-01-{i:02d}" for i in range(1, 28)] + [
        f"2025-02-{i:02d}" for i in range(1, 28)]
    n = len(dates)
    closes = {
        "A": (dates, [100.0 + i * 0.1 for i in range(n)]),
        "B": (dates, [50.0 + i * 0.05 for i in range(n)]),
    }
    monkeypatch.setattr(store, "load_all_dated_closes", lambda: closes)
    # 짧은 패널은 min_periods에 걸릴 수 있어 run을 스텁한다.
    monkeypatch.setattr(hz, "build_panel",
                        lambda dc, tickers=None: hz.Panel(dates=dates, closes={
                            "A": closes["A"][1], "B": closes["B"][1]}))
    monkeypatch.setattr(
        hz, "run",
        lambda panel, cfg=None, regimes=None, scores=None, score_source="price": {
            "ready": True, "verdict": "판별력 있음", "verdict_why": "stub",
            "vs_random": {"percentile": 97.0}, "strategy": {}, "benchmark": {},
            "warnings": [], "coverage_pct": 80.0, "fired_pct": {},
            "periods": 40, "empty_periods": 0, "effective_periods": 40,
        })

    # (1) 사전등록 없는 탐색 실행 — 결과는 돌려주지만 **보드 정본은 건드리지 않는다**.
    #     2026-08-05 이전에는 여기서도 harness_last.json을 덮었다. 8조합 스윕의 마지막 칸이
    #     보드에 남는 경로가 그것이었다(우연 통과 확률 33.7%) — 초록 칸을 고르는 건 측정이 아니다.
    out = store.run_harness(market="kr", trials=10)
    assert out["ready"] and out["verdict"] == "판별력 있음"
    assert out["vs_random"]["percentile"] == 97.0
    assert out["board_updated"] is False
    assert not store.load_harness_last().get("ready"), "탐색 실행이 보드를 덮었다"

    # (2) 사전등록 + 요건 충족 확정만 보드를 갱신한다.
    locked = store.run_harness(market="kr", trials=10,
                               preregistered_id="test-look", lock=True)
    assert locked["verdict"] == "판별력 있음"
    assert store.load_harness_last()["verdict"] == "판별력 있음"
    assert store.load_harness_last()["preregistered_id"] == "test-look"

    # (3) 이력에는 둘 다 남고, 탐색 실행은 절대 잠기지 않는다.
    from signal_desk import db
    runs = db.harness_runs_recent(10)
    assert len(runs) == 2
    assert [r["is_locked"] for r in runs] == [1, 0]          # 최신순
    assert runs[1]["preregistered_id"] is None
