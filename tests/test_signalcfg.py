from signal_desk import db, signalcfg


def test_default_matches_engine_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = signalcfg.get_config()
    assert cfg.weight_technical == 0.35 and cfg.buy_threshold == 1.2


def test_set_and_get_override(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = signalcfg.set_dict({"weight_technical": 0.5, "buy_threshold": 1.5, "ignored": 9})
    assert out["weight_technical"] == 0.5 and out["buy_threshold"] == 1.5
    assert "ignored" not in out
    assert signalcfg.get_config().weight_technical == 0.5


def test_reset_restores_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    signalcfg.set_dict({"weight_qualitative": 0.4})
    assert signalcfg.get_config().weight_qualitative == 0.4
    signalcfg.reset()
    assert signalcfg.get_config().weight_qualitative == 0.15
