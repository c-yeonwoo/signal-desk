"""정책 식별자와 점수 강도 의미가 화면·페이퍼·재생에서 유지되는지 검사."""

from dataclasses import replace
from types import SimpleNamespace

from signal_desk import api, bot
from signal_desk.signals import policy_contract, portfolio_decision
from signal_desk.signals.engine import SignalConfig


def test_signal_policy_id_tracks_effective_config_and_market():
    cfg = SignalConfig()
    kr = policy_contract.signal_policy_id("kr", cfg)
    assert kr == policy_contract.signal_policy_id("kr", replace(cfg))
    assert kr != policy_contract.signal_policy_id("us", cfg)
    assert kr != policy_contract.signal_policy_id("kr", replace(cfg, buy_threshold=cfg.buy_threshold + 0.1))
    assert policy_contract.SCORE_SEMANTICS == "uncalibrated_score_strength"


def test_bot_policy_ids_reuse_signal_id_but_split_style_and_exposure():
    cfg = SignalConfig()
    base = {"trading_style": "balanced", "max_positions": 10,
            "position_pct": 0.08, "max_new_buys_per_run": 2}
    mr = {"eff_cfg": cfg, "context": {"exposure": 0.6}}
    signal_id, execution_id = bot._applied_policy_ids("kr", base, mr)
    assert signal_id == policy_contract.signal_policy_id("kr", cfg)
    assert execution_id == bot._applied_policy_ids("kr", base, mr)[1]
    assert execution_id != bot._applied_policy_ids(
        "kr", base, {**mr, "context": {"exposure": 0.8}})[1]
    assert execution_id != bot._applied_policy_ids(
        "kr", {**base, "trading_style": "aggressive"}, mr)[1]


def test_api_prefers_policy_frozen_on_cached_signal():
    assert api._signal_policy_id("kr", [SimpleNamespace(signal_policy_id="frozen-123")]) == "frozen-123"


def test_bot_market_read_includes_same_kr_macro_input_as_api(monkeypatch):
    seen = {}
    monkeypatch.setattr(bot.regime, "classify", lambda prices: {"regime": "중립"})
    monkeypatch.setattr(bot.store, "load_macro", lambda: {"fed": 1})
    monkeypatch.setattr(bot.store, "load_macro_kr", lambda: {"ecos": 2})
    monkeypatch.setattr(bot.store, "load_market_flow", lambda: {})
    monkeypatch.setattr(bot.macro, "read", lambda indicators, *, extra: seen.update(extra=extra) or {"bias": "중립"})
    monkeypatch.setattr(bot.cycle, "position", lambda indicators: {})
    monkeypatch.setattr(bot.signalcfg, "effective_config", lambda *args, **kwargs: (SignalConfig(), {}))
    monkeypatch.setattr(bot.signalcfg, "get_dict", lambda: {})
    monkeypatch.setattr(bot.kb, "macro_digest", lambda: None)
    bot._market_read({"005930": [100.0]})
    assert seen["extra"] == {"ecos": 2}


def test_shadow_decision_distinguishes_policy_from_input_decision_id():
    args = {"rows": [{"ticker": "A", "value": None}], "universe": [],
            "signal_by_ticker": {}, "prices": {}, "dates_by": {},
            "profile": {"cash": 1000.0, "max_single_position_pct": 40.0},
            "market": "kr", "signal_policy_id": "signal-123"}
    one = portfolio_decision.decide(**args)
    two = portfolio_decision.decide(**{**args, "profile": {
        "cash": 1000.0, "max_single_position_pct": 20.0}})
    cash_only = portfolio_decision.decide(**{**args, "profile": {
        "cash": 2000.0, "max_single_position_pct": 40.0}})
    assert one["decision"]["signal_policy_id"] == "signal-123"
    assert one["decision"]["policy_id"] != two["decision"]["policy_id"]
    assert one["decision"]["policy_id"] == cash_only["decision"]["policy_id"]
    assert one["decision"]["live_eligible"] is False
