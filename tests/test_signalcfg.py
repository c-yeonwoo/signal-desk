import pytest

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
    signalcfg.set_dict({"weight_momentum": 0.5})
    assert signalcfg.get_config().weight_momentum == 0.5
    signalcfg.reset()
    assert signalcfg.get_config().weight_momentum == 0.30


def test_qualitative_weight_not_admin_tunable(tmp_path, monkeypatch):
    """KB 정성은 veto 전용 — FIELDS에 없어 set_dict로 덮어쓰지 않는다."""
    monkeypatch.chdir(tmp_path)
    assert "weight_qualitative" not in signalcfg.FIELDS
    signalcfg.set_dict({"weight_qualitative": 0.9, "weight_short": 0.25})
    cfg = signalcfg.get_config()
    assert cfg.weight_qualitative == 0.15  # 기본값 유지
    assert cfg.weight_short == 0.25


def test_effective_config_raises_buy_threshold_in_weak_regime(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg, adapt = signalcfg.effective_config({"regime": "약세"}, {"bias": "비우호"})
    assert cfg.buy_threshold == pytest.approx(1.2 + 0.7)  # 약세 0.4 + 거시 비우호 0.3
    assert cfg.strong_buy_threshold == pytest.approx(2.0 + 0.7)
    assert adapt["bump"] == pytest.approx(0.7) and adapt["reasons"]


def test_effective_config_no_change_when_favorable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg, adapt = signalcfg.effective_config({"regime": "강세"}, {"bias": "우호"})
    assert cfg.buy_threshold == 1.2 and adapt["bump"] == 0.0


def test_effective_config_off_when_regime_adaptive_disabled(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    signalcfg.set_dict({"regime_adaptive": 0})
    cfg, adapt = signalcfg.effective_config({"regime": "조정"}, {"bias": "비우호"})
    assert cfg.buy_threshold == 1.2 and adapt["bump"] == 0.0


def test_effective_config_flow_net_sell_raises_threshold(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    flow = {"KOSPI": {"smart_net_20d": -3.0, "foreign_net_20d": -3.0, "inst_net_20d": 0, "as_of": "d"}}
    cfg, adapt = signalcfg.effective_config({"regime": "강세"}, {"bias": "우호"}, flow_result=flow)
    assert cfg.buy_threshold == pytest.approx(1.2 + 0.3)  # 순매도(-3조) → +0.3
    assert any("순매도" in r for r in adapt["reasons"])


def test_effective_config_flow_strong_sell_bigger_bump(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    flow = {"KOSPI": {"smart_net_20d": -6.0, "foreign_net_20d": -6.0, "inst_net_20d": 0, "as_of": "d"}}
    cfg, _ = signalcfg.effective_config({"regime": "강세"}, {"bias": "우호"}, flow_result=flow)
    assert cfg.buy_threshold == pytest.approx(1.2 + 0.5)  # 강한 순매도(≤-5조) → +0.5


def test_effective_config_flow_net_buy_no_bump(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    flow = {"KOSPI": {"smart_net_20d": 4.0, "foreign_net_20d": 4.0, "inst_net_20d": 0, "as_of": "d"}}
    cfg, adapt = signalcfg.effective_config({"regime": "강세"}, {"bias": "우호"}, flow_result=flow)
    assert cfg.buy_threshold == 1.2 and adapt["bump"] == 0.0  # 순매수는 문턱 안 낮춤
