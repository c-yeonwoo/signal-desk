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
    db.bot_config_set_style("aggressive", strategy.bot_params("aggressive"))
    cfg = db.bot_config_get()
    assert cfg["trading_style"] == "aggressive"
    assert cfg["max_positions"] == 6 and cfg["position_pct"] == 0.14 and cfg["min_buy_score"] == 1.3
