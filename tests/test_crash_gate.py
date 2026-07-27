"""단기 급락 게이트 — 모멘텀·MA가 못 막는 당일 폭락 후 매수권/분위 승격 차단."""

from signal_desk.signals import engine
from signal_desk.signals.engine import SignalConfig, SignalResult


def test_crash_gate_demotes_buy_on_one_day_drop():
    cfg = SignalConfig(crash_gate_1d_pct=-8.0, crash_gate_2d_pct=0.0)
    closes = [100.0, 92.0]  # -8%
    c = {"kind": "STRONG_BUY", "gated": False, "reasons": []}
    assert engine._apply_crash_gate(c, closes, 1, cfg) is True
    assert c["kind"] == "HOLD" and c["gated"] is True
    assert any("[급락]" in r and "1일" in r for r in c["reasons"])


def test_crash_gate_two_day_window():
    cfg = SignalConfig(crash_gate_1d_pct=0.0, crash_gate_2d_pct=-12.0)
    closes = [100.0, 95.0, 87.0]  # 2일 -13%
    c = {"kind": "BUY", "gated": False, "reasons": []}
    assert engine._apply_crash_gate(c, closes, 2, cfg) is True
    assert c["kind"] == "HOLD" and "[급락]" in c["reasons"][-1]


def test_crash_gate_blocks_rank_promotion_even_from_hold():
    """절대 BUY가 아니어도 gated면 분위가 우선매수로 올리지 못한다(HD현대 케이스)."""
    cfg = SignalConfig(selection_mode="rank", rank_top_pct=50.0, rank_min_score=0.0,
                       crash_gate_1d_pct=-8.0)
    # 점수만 보면 상위인데 급락 게이트
    a = SignalResult(ticker="A", name="A", score=2.0, kind="HOLD", confidence=0.5,
                     technical_score=0, fundamental_score=0, has_fundamental=False,
                     gate_blocked=True,
                     reasons=["[급락] 1일 -17.8% — 단기 급락으로 신규 매수 보류(관망)"])
    b = SignalResult(ticker="B", name="B", score=1.0, kind="HOLD", confidence=0.5,
                     technical_score=0, fundamental_score=0, has_fundamental=False)
    engine.apply_cross_sectional([a, b], cfg)
    assert a.rank_eligible is False and a.kind == "HOLD"
    assert b.rank_eligible is True and engine.is_buy(b.kind)


def test_crash_gate_does_not_touch_sells():
    cfg = SignalConfig(crash_gate_1d_pct=-8.0)
    c = {"kind": "SELL", "gated": False, "reasons": []}
    assert engine._apply_crash_gate(c, [100.0, 90.0], 1, cfg) is False
    assert c["kind"] == "SELL" and c["gated"] is False


def test_crash_gate_disabled_when_thresholds_non_negative():
    cfg = SignalConfig(crash_gate_1d_pct=0.0, crash_gate_2d_pct=0.0)
    c = {"kind": "BUY", "gated": False, "reasons": []}
    assert engine._apply_crash_gate(c, [100.0, 50.0], 1, cfg) is False
    assert c["kind"] == "BUY"


def test_evaluate_crash_day_not_buy_eligible():
    """실제 evaluate 경로: 전일 대비 −18%면 매수권·승격 없음."""
    # 충분히 긴 상승 후 급락 — 모멘텀은 살아 있을 수 있음
    up = [100.0 + i * 0.5 for i in range(280)]
    up.append(up[-1] * 0.82)  # -18%
    uni = [{"ticker": "T", "name": "t"}]
    cfg = SignalConfig(selection_mode="rank", rank_top_pct=100.0, rank_min_score=-9.0,
                       crash_gate_1d_pct=-8.0, crash_gate_2d_pct=-12.0)
    res = engine.evaluate(uni, {"T": up}, config=cfg)
    assert len(res) == 1
    r = res[0]
    assert r.gate_blocked is True
    assert r.kind == "HOLD"
    assert r.rank_eligible is False
    assert any("[급락]" in x for x in r.reasons)
