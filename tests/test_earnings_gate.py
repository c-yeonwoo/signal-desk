"""실적발표 임박 게이트 — 발표 전 신규 매수 신호를 관망으로 강등(매도는 유지). US 전용."""

import datetime

from signal_desk.signals import engine
from signal_desk.signals.engine import SignalConfig, evaluate

TODAY = datetime.date(2026, 7, 11)


def test_days_until():
    assert engine._days_until("2026-07-15", TODAY) == 4
    assert engine._days_until("2026-07-11", TODAY) == 0
    assert engine._days_until("2026-07-01", TODAY) == -10
    assert engine._days_until(None, TODAY) is None
    assert engine._days_until("not-a-date", TODAY) is None


def test_gate_demotes_buy_within_window():
    cfg = SignalConfig(earnings_gate_days=7)
    for kind in ("BUY", "STRONG_BUY"):
        c = {"kind": kind, "reasons": []}
        soon = engine._apply_earnings_gate(c, 3, cfg)
        assert soon is True
        assert c["kind"] == "HOLD"
        assert any("실적" in r for r in c["reasons"])


def test_gate_keeps_sell_and_hold():
    cfg = SignalConfig(earnings_gate_days=7)
    for kind in ("SELL", "STRONG_SELL", "HOLD"):
        c = {"kind": kind, "reasons": []}
        soon = engine._apply_earnings_gate(c, 2, cfg)
        assert soon is True          # 임박 창 이내(표시용 True)
        assert c["kind"] == kind     # 매도·관망은 강등하지 않음


def test_gate_outside_window_and_past():
    cfg = SignalConfig(earnings_gate_days=7)
    c1 = {"kind": "BUY", "reasons": []}
    assert engine._apply_earnings_gate(c1, 10, cfg) is False and c1["kind"] == "BUY"
    c2 = {"kind": "BUY", "reasons": []}
    assert engine._apply_earnings_gate(c2, -2, cfg) is False and c2["kind"] == "BUY"  # 이미 지난 발표


def test_gate_disabled_when_days_zero():
    cfg = SignalConfig(earnings_gate_days=0)
    c = {"kind": "STRONG_BUY", "reasons": []}
    assert engine._apply_earnings_gate(c, 1, cfg) is False and c["kind"] == "STRONG_BUY"


def test_evaluate_attaches_earnings_fields():
    closes = [100.0 + i * 0.1 for i in range(80)]
    universe = [{"ticker": "SOON", "name": "임박"}, {"ticker": "FAR", "name": "여유"},
                {"ticker": "NONE", "name": "없음"}]
    prices = {"SOON": closes, "FAR": closes, "NONE": closes}
    ed = {"SOON": "2026-07-14", "FAR": "2026-09-01"}   # SOON=D-3(임박), FAR=D-52
    res = {r.ticker: r for r in evaluate(universe, prices, earnings_dates=ed, today=TODAY)}
    assert res["SOON"].earnings_soon is True and res["SOON"].earnings_date == "2026-07-14"
    assert res["FAR"].earnings_soon is False and res["FAR"].earnings_date == "2026-09-01"
    assert res["NONE"].earnings_soon is False and res["NONE"].earnings_date is None
