"""청산 폭을 σ 배수로 — 같은 −4%가 시장마다 다른 뜻이었다.

2026-01~07 일간 σ: 미국 2.51% · 국내 4.57%(1.8배). 트레일링 −4%는 미국에서 1.6σ,
국내에서 0.9σ다. 같은 기간 같은 엔진으로 미국 봇은 벤치마크를 이기고(균형 +0.24%p ·
공격 +3.04%p) 국내 봇만 크게 졌다(−5.26 · −10.19 · −10.57%p).
"""

from __future__ import annotations

import dataclasses

from signal_desk import strategy
from signal_desk.signals import harness, risk


def test_multiples_are_derived_not_invented():
    """배수는 새로 고른 값이 아니라 **현재 퍼센트 ÷ 미국 σ** 다."""
    for style, p in strategy.PRESETS.items():
        sg = strategy.EXIT_SIGMA[style]
        assert abs(sg["stop"] - abs(p["stop_loss_pct"]) / strategy._US_SIGMA_ANCHOR) < 0.01
        assert abs(sg["trailing"]
                   - abs(p["trailing_from_peak_pct"]) / strategy._US_SIGMA_ANCHOR) < 0.01


def test_us_sigma_reproduces_todays_percentages():
    """미국 변동성에서는 폭이 지금과 같아야 한다 — 통하던 설정을 그대로 두는 환산이다."""
    for style in strategy.STYLES:
        scaled = strategy.risk_config(style, "약세",
                                      sigma=strategy._US_SIGMA_ANCHOR).effective()
        fixed = strategy.risk_config(style, "약세")
        assert abs(scaled.stop_loss_pct - fixed.stop_loss_pct) < 0.0005
        assert abs(scaled.trailing_from_peak_pct - fixed.trailing_from_peak_pct) < 0.0005


def test_korean_volatility_widens_the_stops():
    c = strategy.risk_config("balanced", "약세", sigma=0.0457).effective()
    assert c.stop_loss_pct < -0.12, "국내 σ에서는 폭이 넓어져야 한다"
    assert c.trailing_from_peak_pct < -0.08


def test_unknown_sigma_changes_nothing():
    """모르면 바꾸지 않는다 — σ를 못 재는 종목에서 폭을 0으로 만들면 0으로 나누기다."""
    for sig in (None, 0.0, -1.0):
        c = strategy.risk_config("balanced", "약세", sigma=sig).effective()
        fixed = strategy.risk_config("balanced", "약세")
        assert c.stop_loss_pct == fixed.stop_loss_pct
        assert c.trailing_from_peak_pct == fixed.trailing_from_peak_pct


def test_default_is_off():
    """기본은 고정 퍼센트 — 켜는 것은 별도 결정이고 사전등록 대상이다."""
    assert strategy.risk_config("balanced").sigma is None
    assert risk.RiskConfig().sigma is None


def test_check_exit_applies_the_scaling():
    c = risk.RiskConfig(stop_loss_pct=-0.07, take_profit_pct=0.50,
                        trailing_from_peak_pct=-0.05,
                        sigma=0.05, stop_loss_sigma=2.79, trailing_sigma=1.99)
    # σ=5% → 손절 -14.0%. -10%에서는 아직 안 나간다(고정 -7%였다면 나갔다).
    assert risk.check_exit(100, 90, 100, c) is None
    assert risk.check_exit(100, 85, 100, c) == "STOP_LOSS"


def test_harness_measures_sigma_before_entry_only():
    """진입 직전까지의 변동성만 쓴다 — 진입일 이후를 보면 룩어헤드다."""
    row = [100.0] * 30 + [100.0, 200.0, 50.0]      # 진입 이후에만 큰 변동
    quiet = harness._realized_sigma(row, 29)
    assert quiet is not None and quiet < 0.001, "진입 전 조용한 구간의 σ가 커졌다 = 룩어헤드"


def test_harness_sigma_needs_enough_samples():
    assert harness._realized_sigma([100.0, 101.0, 102.0], 2) is None


def test_harness_uses_sigma_when_multiples_are_set():
    """배수가 있으면 하네스가 종목·시점별 σ를 채운다 — 안 채우면 σ 모드가 죽는다."""
    src = open(harness.__file__, encoding="utf-8").read()
    assert "_realized_sigma(row, i)" in src
    assert "dataclasses.replace(rules, sigma=" in src


def test_sigma_mode_actually_changes_the_result():
    """양성 대조군 — 같은 경로에서 고정 %와 σ 모드가 다른 결과를 내야 한다."""
    n = 60
    row = [100.0 * (1.0 + 0.03 * ((-1) ** i)) for i in range(n)]   # ±3% 톱니
    p = harness.Panel(dates=[f"2026-01-{i + 1:03d}" for i in range(n)],
                      closes={"A": row})
    base = risk.RiskConfig(stop_loss_pct=-0.07, take_profit_pct=0.50,
                           trailing_from_peak_pct=-0.05)
    fixed = harness.HarnessConfig(rebalance_days=10, override_selection=True,
                                  exit_rules=base)
    scaled = harness.HarnessConfig(
        rebalance_days=10, override_selection=True,
        exit_rules=dataclasses.replace(base, stop_loss_sigma=2.79, trailing_sigma=1.99))
    a = harness._period_return(p, ["A"], 30, fixed)
    b = harness._period_return(p, ["A"], 30, scaled)
    assert a != b, "σ 모드가 아무것도 바꾸지 않는다 — 배수가 안 걸린 것이다"


def test_preregistration_declares_the_exit_layer():
    """등록에서 빠진 파라미터는 검증된 적이 없다 — `exits` 가 해시 대상이어야 한다."""
    from signal_desk import prereg
    assert "exits" in prereg._HARNESS_KEYS
    reg = prereg.load()
    assert reg["ok"], reg["reason"]
    for lk in reg["looks"]:
        assert "exits" in lk["harness"], f"{lk['id']}: 청산 레이어 선언이 없다"


def test_registered_looks_currently_measure_no_exit_holding():
    """현재 등록 3+1개는 전부 무청산이다 — 그게 사실이고, 사실이 파일에 적혀 있어야 한다."""
    from signal_desk import prereg
    for lk in prereg.load()["looks"]:
        assert lk["harness"]["exits"] == "none"


def test_run_preregistered_honours_the_declaration():
    """선언만 하고 안 읽으면 같은 id가 두 전략을 잰다."""
    src = open("src/signal_desk/store.py", encoding="utf-8").read()
    assert 'hzc.get("exits")' in src, "run_preregistered가 등록된 청산 레이어를 안 읽는다"
    assert "exit_rules=_exit_rules" in src, "읽어 놓고 하네스에 안 넘긴다"
