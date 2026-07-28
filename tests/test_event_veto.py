"""Decision buy_blocked → 급락과 동일 하드 강등(HOLD + gate_blocked + [악재])."""

from signal_desk.signals import engine
from signal_desk.signals.decision import Decision
from signal_desk.signals.engine import SignalConfig


def test_apply_event_veto_demotes_buy():
    c = {"kind": "STRONG_BUY", "gated": False, "reasons": []}
    dec = Decision(True, "trim", 1, "serious", "유상증자 결정", "p2")
    assert engine._apply_event_veto(c, dec) is True
    assert c["kind"] == "HOLD" and c["gated"] is True
    assert any("[악재]" in r and "유상증자" in r for r in c["reasons"])


def test_apply_event_veto_skips_sells():
    c = {"kind": "SELL", "gated": False, "reasons": []}
    dec = Decision(True, "exit", 1, "critical", "감자", "p2")
    assert engine._apply_event_veto(c, dec) is False
    assert c["kind"] == "SELL" and c["gated"] is False


def test_apply_event_veto_noop_without_block():
    c = {"kind": "BUY", "gated": False, "reasons": []}
    dec = Decision(False, "none", None, None, "", "p2")
    assert engine._apply_event_veto(c, dec) is False
    assert c["kind"] == "BUY"


def test_evaluate_event_risk_not_buy_eligible():
    """상승 추세라도 buy_blocked면 매수권·승격 없음 + hold 사유 [악재]."""
    up = [100.0 + i * 0.5 for i in range(280)]
    uni = [{"ticker": "T", "name": "t"}]
    cfg = SignalConfig(selection_mode="rank", rank_top_pct=100.0, rank_min_score=-9.0,
                       crash_gate_1d_pct=0.0, crash_gate_2d_pct=0.0)
    sentiment = {"T": {
        "event_risk": True, "event_note": "리콜 사태", "event_severity": "serious",
    }}
    r = engine.evaluate(uni, {"T": up}, sentiment=sentiment, config=cfg)[0]
    assert r.event_risk is True
    assert r.gate_blocked is True
    assert r.kind == "HOLD"
    assert r.rank_eligible is False
    assert any("[악재]" in x for x in r.reasons)


def test_evaluate_absolute_mode_also_demotes():
    up = [100.0 + i * 0.5 for i in range(280)]
    uni = [{"ticker": "T", "name": "t"}]
    cfg = SignalConfig(selection_mode="absolute", buy_threshold=-9.0, strong_buy_threshold=9.0,
                       crash_gate_1d_pct=0.0, crash_gate_2d_pct=0.0)
    sentiment = {"T": {"decision": Decision(True, "exit", 9, "critical", "상장폐지", "p2")}}
    r = engine.evaluate(uni, {"T": up}, sentiment=sentiment, config=cfg)[0]
    assert r.kind == "HOLD" and r.gate_blocked and any("[악재]" in x for x in r.reasons)
