from signal_desk.signals import execution_twin, risk


def test_replay_uses_the_same_gain_only_trailing_rule_as_live_bot():
    cfg = risk.RiskConfig(stop_loss_pct=-0.10, take_profit_pct=0.30,
                          trailing_from_peak_pct=-0.05, trailing_protects_gains_only=True)
    ticks = [
        execution_twin.QuoteTick(1, 98),   # 손실 구간 -2%: trailing 아님
        execution_twin.QuoteTick(2, 110),  # peak 갱신
        execution_twin.QuoteTick(3, 104),  # 고점 대비 -5.45%: 수익 보호 trailing
    ]
    replay = execution_twin.replay(100, ticks, config=cfg)

    assert replay.reason == "TRAILING"
    assert replay.exit_tick == ticks[-1]
    assert replay.peak == 110
    assert replay.observed == 3


def test_step_and_replay_agree_on_stop_loss():
    cfg = risk.RiskConfig(stop_loss_pct=-0.07, take_profit_pct=0.15, trailing_from_peak_pct=-0.05)
    step = execution_twin.evaluate_quote(100, 93, 100, cfg)
    replay = execution_twin.replay(100, [execution_twin.QuoteTick(1000, 93)], config=cfg)

    assert step.reason == replay.reason == "STOP_LOSS"
    assert step.peak == replay.peak == 100
