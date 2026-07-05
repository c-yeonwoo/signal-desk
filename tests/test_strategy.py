from signal_desk import db, strategy


def test_presets_cover_three_styles():
    assert set(strategy.PRESETS) == {"conservative", "balanced", "aggressive"}
    # 공격형은 소수 집중(종목 적고 비중 큼), 안정형은 분산(종목 많고 비중 작음)
    assert strategy.PRESETS["aggressive"]["max_positions"] < strategy.PRESETS["conservative"]["max_positions"]
    assert strategy.PRESETS["aggressive"]["position_pct"] > strategy.PRESETS["conservative"]["position_pct"]
    # 안정형이 더 엄격한 매수 기준 + 타이트한 손절
    assert strategy.PRESETS["conservative"]["min_buy_score"] > strategy.PRESETS["aggressive"]["min_buy_score"]
    assert strategy.PRESETS["conservative"]["stop_loss_pct"] > strategy.PRESETS["aggressive"]["stop_loss_pct"]


def test_normalize_and_risk_config():
    assert strategy.normalize("weird") == "balanced"
    rc = strategy.risk_config("aggressive")
    assert rc.stop_loss_pct == -0.10 and rc.take_profit_pct == 0.25


def test_set_style_applies_preset(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from signal_desk import bot
    bot.set_style(1, "aggressive")
    cfg = bot._cfg(1)  # user_bot 설정 + 성향 프리셋 파라미터 병합
    assert cfg["trading_style"] == "aggressive"
    assert cfg["max_positions"] == 6 and cfg["position_pct"] == 0.14 and cfg["min_buy_score"] == 1.3


def test_risk_config_tightens_take_profit_in_choppy_regime():
    # 추세(강세)면 넓은 익절(0.15), 횡보·약세면 중간 실현(0.09) — 균형형 기준
    assert strategy.risk_config("balanced", "강세").take_profit_pct == 0.15
    assert strategy.risk_config("balanced", "과열").take_profit_pct == 0.15
    assert strategy.risk_config("balanced", "약세").take_profit_pct == 0.09
    assert strategy.risk_config("balanced", "중립").take_profit_pct == 0.09
    # regime 미지정이면 기본 익절 유지
    assert strategy.risk_config("balanced").take_profit_pct == 0.15


def test_entry_tranches_by_style():
    assert strategy.entry_tranches("conservative") == 4
    assert strategy.entry_tranches("balanced") == 3
    assert strategy.entry_tranches("aggressive") == 2
