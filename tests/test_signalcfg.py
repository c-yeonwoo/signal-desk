import pytest

from signal_desk import db, signalcfg
from signal_desk.signals.engine import SignalConfig


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


def _absolute() -> SignalConfig:
    """절대문턱 모드 base — 문턱 상향은 이 모드에서만 일어난다(기본은 rank=익스포저 조절)."""
    return SignalConfig(selection_mode="absolute")


def test_effective_config_raises_buy_threshold_in_weak_regime(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg, adapt = signalcfg.effective_config({"regime": "약세"}, {"bias": "비우호"}, base=_absolute())
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
    cfg, adapt = signalcfg.effective_config({"regime": "강세"}, {"bias": "우호"}, flow_result=flow,
                                            base=_absolute())
    assert cfg.buy_threshold == pytest.approx(1.2 + 0.3)  # 순매도(-3조) → +0.3
    assert any("순매도" in r for r in adapt["reasons"])


def test_effective_config_flow_strong_sell_bigger_bump(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    flow = {"KOSPI": {"smart_net_20d": -6.0, "foreign_net_20d": -6.0, "inst_net_20d": 0, "as_of": "d"}}
    cfg, _ = signalcfg.effective_config({"regime": "강세"}, {"bias": "우호"}, flow_result=flow,
                                        base=_absolute())
    assert cfg.buy_threshold == pytest.approx(1.2 + 0.5)  # 강한 순매도(≤-5조) → +0.5


def test_rank_mode_lowers_exposure_instead_of_raising_threshold(tmp_path, monkeypatch):
    """기본(rank) 모드에서 국면은 '자격'이 아니라 '크기'다 — 문턱은 그대로, 익스포저만 준다.

    문턱을 올리면 나쁜 시장에서 후보가 0이 되고(2026-07-26 진단: 10거래일 매수 1건),
    손실도 학습도 없는 상태가 된다. 그래서 문턱 불변 + 익스포저 축소로 바꿨다.
    """
    monkeypatch.chdir(tmp_path)
    flow = {"KOSPI": {"smart_net_20d": -6.0, "foreign_net_20d": -6.0, "inst_net_20d": 0, "as_of": "d"}}
    cfg, adapt = signalcfg.effective_config({"regime": "조정"}, {"bias": "비우호"}, flow_result=flow)
    assert cfg.buy_threshold == 1.2 and adapt["bump"] == 0.0      # 문턱 불변
    assert adapt["mode"] == "rank"
    # 조정 0.2 × 거시 비우호 0.8 × 강한 순매도 0.6 = 0.096 → 하한 0.15
    assert adapt["exposure"] == 0.15
    assert any("조정" in r for r in adapt["exposure_reasons"])


def test_exposure_never_reaches_zero(tmp_path, monkeypatch):
    """최악 국면에도 익스포저 하한이 있다 — 아무것도 안 사면 그 국면에서 무엇이 통하는지 못 배운다."""
    monkeypatch.chdir(tmp_path)
    from signal_desk.signals import regime
    flow = {"KOSPI": {"smart_net_20d": -50.0, "foreign_net_20d": -50.0, "inst_net_20d": 0, "as_of": "d"}}
    out = regime.target_exposure({"regime": "조정"}, {"bias": "비우호"}, flow)
    assert out["exposure"] == regime.EXPOSURE_FLOOR > 0


def test_exposure_full_in_bull(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from signal_desk.signals import regime
    assert regime.target_exposure({"regime": "강세"}, {"bias": "우호"})["exposure"] == 1.0
    # 국면 판정 불가는 '전액 투자'도 '전액 현금'도 아니다
    assert regime.target_exposure(None, None)["exposure"] == 0.7


def test_selection_mode_is_persisted(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert signalcfg.get_config().selection_mode == "rank"     # 기본
    signalcfg.set_dict({"selection_mode": "absolute", "rank_top_pct": 5})
    cfg = signalcfg.get_config()
    assert cfg.selection_mode == "absolute" and cfg.rank_top_pct == 5.0
    signalcfg.set_dict({"selection_mode": "없는모드"})           # 검증 실패 → 기본 유지
    assert signalcfg.get_config().selection_mode == "rank"


def test_effective_config_flow_net_buy_no_bump(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    flow = {"KOSPI": {"smart_net_20d": 4.0, "foreign_net_20d": 4.0, "inst_net_20d": 0, "as_of": "d"}}
    cfg, adapt = signalcfg.effective_config({"regime": "강세"}, {"bias": "우호"}, flow_result=flow,
                                            base=_absolute())
    assert cfg.buy_threshold == 1.2 and adapt["bump"] == 0.0  # 순매수는 문턱 안 낮춤
