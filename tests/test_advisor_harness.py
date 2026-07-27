"""advisor 하네스 — shadow kill · challenger veto · 실패 사유."""

import pytest

from signal_desk import db, store
from signal_desk.signals import advisor, advisor_shadow


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    advisor_shadow._SUMMARY_CACHE = None
    return tmp_path


def test_gate_kills_on_significant_negative_paired():
    summary = {
        "paired_verdict_ready": True,
        "paired_delta_pct": -3.5,
        "paired_n": 40,
        "by_style": {},
    }
    g = advisor_shadow.gate(summary=summary)
    assert g["active"] is False
    assert g["fallback"] == "abstain"
    assert "패배" in g["reason"]


def test_gate_style_specific_kill():
    summary = {
        "paired_verdict_ready": False,
        "paired_delta_pct": 1.0,
        "by_style": {
            "balanced": {
                "paired_verdict_ready": True,
                "paired_delta_pct": -2.0,
                "paired_n": 25,
            },
        },
    }
    assert advisor_shadow.gate(style="balanced", summary=summary)["active"] is False
    assert advisor_shadow.gate(style="aggressive", summary=summary)["active"] is True


def test_gate_manual_override():
    advisor_shadow.set_harness_config({"manual_override": "force_off"})
    assert advisor_shadow.gate(summary={"paired_verdict_ready": False})["active"] is False
    advisor_shadow.set_harness_config({"manual_override": "force_on"})
    bad = {"paired_verdict_ready": True, "paired_delta_pct": -9.0, "paired_n": 99}
    assert advisor_shadow.gate(summary=bad)["active"] is True


def test_advise_killed_abstains(monkeypatch):
    monkeypatch.setattr(advisor.llm, "available", lambda: True)
    monkeypatch.setattr(advisor.llm, "complete_json", lambda *a, **k: {
        "picks": [{"ticker": "A", "rationale": "x"}]})
    cands = [{"ticker": "A", "name": "a", "score": 2.0, "confidence": 0.7, "reasons": []}]
    adv = advisor.advise(cands, {}, {}, [], 1, challenge=False,
                         gate={"active": False, "fallback": "abstain", "reason": "test kill"})
    assert adv.killed and adv.picks == [] and adv.reason == "test kill"


def test_advise_killed_score_fallback(monkeypatch):
    cands = [{"ticker": "A", "name": "a", "score": 2.0, "confidence": 0.7, "reasons": []}]
    adv = advisor.advise(cands, {}, {}, [], 1, challenge=False,
                         gate={"active": False, "fallback": "score", "reason": "kill"})
    assert adv.killed and adv.picks is None


def test_challenger_vetoes_only(monkeypatch):
    calls = []

    def _llm(system, user, **k):
        calls.append(system)
        if "반론자" in system or "challenger" in system.lower() or "veto" in system:
            return {"veto": [{"ticker": "B", "why": "악재"}]}
        return {"picks": [
            {"ticker": "A", "rationale": "좋음"},
            {"ticker": "B", "rationale": "괜찮"},
        ]}

    monkeypatch.setattr(advisor.llm, "available", lambda: True)
    monkeypatch.setattr(advisor.llm, "complete_json", _llm)
    cands = [
        {"ticker": "A", "name": "a", "score": 2.0, "confidence": 0.7, "reasons": []},
        {"ticker": "B", "name": "b", "score": 1.9, "confidence": 0.6, "reasons": []},
    ]
    adv = advisor.advise(cands, {}, {}, [], 2, challenge=True,
                         gate={"active": True, "fallback": "abstain"})
    assert [p["ticker"] for p in adv.picks] == ["A"]
    assert adv.vetoed == ["B"]
    assert len(calls) == 2


def test_challenger_cannot_add_tickers(monkeypatch):
    def _llm(system, user, **k):
        if "veto" in system:
            # 악의적으로 새 종목을 veto에 넣어도 무시(픽에 없으면)
            return {"veto": [{"ticker": "ZZZ", "why": "x"}]}
        return {"picks": [{"ticker": "A", "rationale": "x"}]}

    monkeypatch.setattr(advisor.llm, "available", lambda: True)
    monkeypatch.setattr(advisor.llm, "complete_json", _llm)
    cands = [{"ticker": "A", "name": "a", "score": 2.0, "confidence": 0.7, "reasons": []}]
    adv = advisor.advise(cands, {}, {}, [], 1, challenge=True,
                         gate={"active": True})
    assert [p["ticker"] for p in adv.picks] == ["A"]
    assert adv.vetoed == []


def test_failure_reasons(monkeypatch):
    cands = [{"ticker": "A", "name": "a", "score": 2.0, "confidence": 0.7, "reasons": []}]
    monkeypatch.setattr(advisor.llm, "available", lambda: False)
    assert advisor.advise(cands, {}, {}, [], 1, challenge=False,
                          gate={"active": True}).reason == "no_key"

    monkeypatch.setattr(advisor.llm, "available", lambda: True)
    monkeypatch.setattr(advisor.llm, "complete_json", lambda *a, **k: None)
    assert advisor.advise(cands, {}, {}, [], 1, challenge=False,
                          gate={"active": True}).reason == "api_fail"

    monkeypatch.setattr(advisor.llm, "complete_json", lambda *a, **k: {"picks": "x"})
    assert advisor.advise(cands, {}, {}, [], 1, challenge=False,
                          gate={"active": True}).reason == "parse_fail"

    monkeypatch.setattr(advisor.llm, "complete_json",
                        lambda *a, **k: {"picks": [{"ticker": "ZZZ", "rationale": "x"}]})
    assert advisor.advise(cands, {}, {}, [], 1, challenge=False,
                          gate={"active": True}).reason == "out_of_pool"


def test_record_killed_excluded_from_scoring():
    pool = [{"ticker": "DOWN", "score": 2.0}, {"ticker": "UP", "score": 1.9}]
    advisor_shadow.record(uid=1, market="kr", pool=pool, picks=[], slots=1,
                          date="2026-01-01", outcome_override="killed",
                          detail={"reason": "shadow", "killed": True})
    out = advisor_shadow.summary({}, horizon=5)
    assert out["runs"] == 1 and out["advisor_used_runs"] == 0
