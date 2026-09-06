"""트레일링이 손절·익절을 덮어써 죽은 파라미터로 만들던 문제.

2026-09-06 진단: 레퍼런스 3봇 최근 20거래의 매도 사유가 **100% TRAILING** 이었다
(STOP_LOSS 0건 · TAKE_PROFIT 0건). 원인은 통계가 아니라 기하다 —
진입 시 `peak = 진입가` 이고 트레일링 폭이 손절 폭보다 좁아서, 주가가 한 번도 오르지
않아도 트레일링 발동가가 늘 손절가보다 **위**에 있다. 5분틱으로 연속 관측하면
트레일링이 항상 먼저 닿는다.
"""

from __future__ import annotations

from signal_desk import strategy
from signal_desk.signals import risk


def test_trailing_price_sits_above_stop_price_in_every_preset():
    """세 성향 모두 트레일링 발동가 > 손절가 — 이게 죽은 파라미터의 기하학적 원인이다."""
    for style in strategy.STYLES:
        for regime in ("강세", "약세"):
            c = strategy.risk_config(style, regime)
            trail_px = 1 + c.trailing_from_peak_pct     # peak = 진입가
            stop_px = 1 + c.stop_loss_pct
            assert trail_px > stop_px, (
                f"{style}/{regime}: 트레일링 {trail_px:.3f} <= 손절 {stop_px:.3f} — "
                "전제가 바뀌었으면 이 파일의 설명을 갱신할 것")


def test_trailing_no_longer_fires_while_the_position_is_losing():
    """오른 적 없는 포지션의 -4% 되돌림은 트레일링이 아니다 — 그냥 손실이다."""
    c = risk.RiskConfig(stop_loss_pct=-0.07, take_profit_pct=0.15,
                        trailing_from_peak_pct=-0.05)
    # 진입 100, 한 번도 안 오름(peak=100), 현재 95 → 예전엔 TRAILING
    assert risk.check_exit(100, 95, 100, c) is None
    # 손절선까지 내려가면 손절이 판정한다 — 이제 도달 가능하다
    assert risk.check_exit(100, 93, 100, c) == "STOP_LOSS"


def test_trailing_still_protects_a_gain():
    c = risk.RiskConfig(stop_loss_pct=-0.07, take_profit_pct=0.50,
                        trailing_from_peak_pct=-0.05)
    # 진입 100 → 고점 130 → 현재 120: 고점 대비 -7.7%, 진입 대비 +20% → 이익을 지킨다
    assert risk.check_exit(100, 120, 130, c) == "TRAILING"


def test_trailing_never_realizes_a_loss():
    """고점에서 크게 밀려도 손익분기 아래면 트레일링이 아니라 손절 소관이다."""
    c = risk.RiskConfig(stop_loss_pct=-0.07, take_profit_pct=0.50,
                        trailing_from_peak_pct=-0.05)
    # 고점 130 → 현재 99: 고점 대비 -23.8%지만 진입가 아래다
    assert risk.check_exit(100, 99, 130, c) is None
    assert risk.check_exit(100, 93, 130, c) == "STOP_LOSS"


def test_breakeven_is_the_boundary():
    c = risk.RiskConfig(stop_loss_pct=-0.07, take_profit_pct=0.50,
                        trailing_from_peak_pct=-0.05)
    assert risk.check_exit(100, 100, 130, c) == "TRAILING"   # 손익분기는 발동
    assert risk.check_exit(100, 99.99, 130, c) is None       # 그 아래는 손절 소관


def test_old_behaviour_is_still_reachable_for_measurement():
    """옛 동작을 끌 수 있어야 하네스에서 A/B로 잰다 — 못 재는 변경은 근거가 없다."""
    c = risk.RiskConfig(stop_loss_pct=-0.07, take_profit_pct=0.15,
                        trailing_from_peak_pct=-0.05,
                        trailing_protects_gains_only=False)
    assert risk.check_exit(100, 95, 100, c) == "TRAILING"


def test_stop_loss_becomes_reachable_under_continuous_monitoring():
    """5분틱처럼 촘촘히 보면 예전엔 손절이 원리적으로 도달 불가였다는 것을 재현한다."""
    old = risk.RiskConfig(stop_loss_pct=-0.07, take_profit_pct=0.15,
                          trailing_from_peak_pct=-0.05,
                          trailing_protects_gains_only=False)
    new = risk.RiskConfig(stop_loss_pct=-0.07, take_profit_pct=0.15,
                          trailing_from_peak_pct=-0.05)
    # 100 → 99 → 98 → ... 로 연속 하락(갭 없음). peak은 100에 머문다.
    fired_old = next((risk.check_exit(100, px, 100, old)
                      for px in [100 - i * 0.5 for i in range(1, 30)]
                      if risk.check_exit(100, px, 100, old)), None)
    fired_new = next((risk.check_exit(100, px, 100, new)
                      for px in [100 - i * 0.5 for i in range(1, 30)]
                      if risk.check_exit(100, px, 100, new)), None)
    assert fired_old == "TRAILING", "옛 동작에서는 트레일링이 먼저 닿아야 한다"
    assert fired_new == "STOP_LOSS", "이제는 손절이 제 일을 해야 한다"


def test_take_profit_still_wins_over_trailing():
    """우선순위(손절 → 익절 → 트레일링)는 그대로다."""
    c = risk.RiskConfig(stop_loss_pct=-0.07, take_profit_pct=0.09,
                        trailing_from_peak_pct=-0.05)
    # +10%이면서 고점(120) 대비 -8.3% — 익절이 이긴다
    assert risk.check_exit(100, 110, 120, c) == "TAKE_PROFIT"


def test_change_is_recorded_as_unproven(tmp_path, monkeypatch):
    """게이트를 우회한 소스 변경일수록 이력에 남아야 한다 — 관리자 미검증 배너가 읽는다."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data/cache").mkdir(parents=True)
    from signal_desk import db, signalcfg
    db._CONN = None
    assert strategy.record_unproven_trailing_change() is True
    assert strategy.record_unproven_trailing_change() is False, "중복 기록은 배너를 도배한다"
    hist = signalcfg.history()
    rec = next(h for h in hist if "트레일링" in (h.get("reason") or ""))
    assert rec["unproven"] is True
    assert "확인 실패" in rec["harness"], "하네스가 확인 못 했다는 사실이 기록에 남아야 한다"


def test_boot_records_the_change():
    src = open("src/signal_desk/api.py", encoding="utf-8").read()
    assert "record_unproven_trailing_change()" in src, "기록 함수가 아무 데서도 안 불린다"
