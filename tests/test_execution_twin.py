from signal_desk.signals import execution_audit, execution_twin, risk


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


def test_execution_audit_replays_the_recorded_risk_rule():
    entry = {"ticker": "A", "price": 100, "ts": 10, "event_type": "filled_buy",
             "payload": {"risk": {"stop_loss_pct": -0.07, "take_profit_pct": 0.15,
                                  "trailing_from_peak_pct": -0.05,
                                  "trailing_protects_gains_only": True}}}
    exit_event = {"ticker": "A", "price": 93, "ts": 20, "event_type": "filled_sell",
                  "payload": {"reason": "STOP_LOSS"}}
    row = execution_audit.audit_round_trip(entry, exit_event, [{"ts": 15, "price": 101}, {"ts": 20, "price": 93}])

    assert row["auditable"] is True
    assert row["expected_reason"] == "STOP_LOSS"
    assert row["match"] is True


def test_execution_audit_preserves_remaining_lot_after_partial_sell():
    risk_payload = {"risk": {"stop_loss_pct": -0.07, "take_profit_pct": 0.15,
                             "trailing_from_peak_pct": -0.05, "trailing_protects_gains_only": True}}
    events = [
        {"ticker": "A", "price": 100, "ts": 10, "event_type": "filled_buy",
         "payload": {**risk_payload, "qty": 10}},
        {"ticker": "A", "price": 110, "ts": 20, "event_type": "filled_sell",
         "payload": {**risk_payload, "qty": 4, "reason": "EVENT_TRIM", "entry_price": 100}},
        {"ticker": "A", "price": 93, "ts": 30, "event_type": "filled_sell",
         "payload": {**risk_payload, "qty": 6, "reason": "STOP_LOSS", "entry_price": 100}},
    ]
    rows = execution_audit.audit_events(
        events, lambda _ticker, _after, _before: [{"ts": 15, "price": 101}, {"ts": 30, "price": 93}])

    assert [r["quantity"] for r in rows] == [4, 6]
    assert rows[0]["match"] is None  # 이벤트 부분청산은 risk replay 비교 대상이 아니다.
    assert rows[1]["match"] is True
