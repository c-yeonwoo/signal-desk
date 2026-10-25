"""운영 매수 보류는 기본 켬: 모든 매수 경로를 막고 보유 위험 청산은 유지한다."""

import json
import time

import pytest

from signal_desk import bot, config, db
from signal_desk.broker import paper
from signal_desk.signals.engine import SignalConfig, SignalResult

UID = 17


def _sig(ticker, kind="BUY", score=2.0, **event):
    return SignalResult(ticker=ticker, name=ticker, score=score, kind=kind, confidence=0.5,
                        technical_score=0.0, fundamental_score=0.0, has_fundamental=False,
                        reasons=[], **event)


def _setup(monkeypatch, tmp_path, market, prices, signals, *, cash=100_000., positions=None,
           max_positions=10):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOT_KILL_SWITCH", "false")
    monkeypatch.setattr(paper.market_clock, "is_open", lambda *_args: True)
    monkeypatch.setattr(bot.store, "load_price_series", lambda: prices if market == "kr" else {})
    monkeypatch.setattr(bot.store, "load_us_price_series", lambda: prices if market == "us" else {})
    monkeypatch.setattr(bot.store, "load_warned_tickers", lambda: set())
    cfg = {"enabled": True, "trading_style": "balanced", "seed_cash": 100_000.,
           "seed_cash_us": 100_000., "max_positions": max_positions, "position_pct": 0.08,
           "min_buy_score": 1.0, "max_new_buys_per_run": 2}
    monkeypatch.setattr(bot, "_cfg", lambda uid: cfg)
    read = {"eff_cfg": SignalConfig(selection_mode="absolute"), "adapt": {},
            "context": {"regime": "중립", "exposure": 1.0}}
    monkeypatch.setattr(bot, "_market_read_for", lambda market: read)
    universe = [{"ticker": t, "name": t} for t in prices]
    snapshot = {"quotes": {t: closes[-1] for t, closes in prices.items()},
                "quote_updated": {t: time.time() for t in prices},
                "quote_meta": {t: {"observation_id": f"test-{t}"} for t in prices}}
    monkeypatch.setattr(bot, "_market_signals", lambda *_args: (
        universe, prices, signals, {t: t for t in prices}, {}, snapshot))
    db.kv_set(paper._key(UID, market), json.dumps({"cash": cash, "positions": positions or {}}))
    return cfg, read


@pytest.mark.parametrize("value", [None, "", "  ", "1", "true", "yes", "on", "typo", "FALSEE"])
def test_buy_pause_defaults_and_unknown_values_fail_closed(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("BOT_BUY_PAUSED", raising=False)
    else:
        monkeypatch.setenv("BOT_BUY_PAUSED", value)
    assert config.bot_buy_paused() is True
    assert bot.buy_control_status()["buy_paused"] is True
    assert "BOT_BUY_PAUSED" in bot.buy_control_status()["buy_pause_reason"]


@pytest.mark.parametrize("value", ["0", "false", "no", "off", " FALSE ", "Off"])
def test_only_explicit_false_resumes(monkeypatch, value):
    monkeypatch.setenv("BOT_BUY_PAUSED", value)
    assert config.bot_buy_paused() is False
    assert bot.buy_control_status() == {"buy_paused": False, "buy_pause_reason": None}


def test_configured_llm_key_does_not_resume_default_pause(monkeypatch):
    monkeypatch.delenv("BOT_BUY_PAUSED", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-placeholder-not-a-real-key")
    assert config.bot_buy_paused() is True


@pytest.mark.parametrize("market", ["kr", "us"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_final_buy_authorizer_blocks_even_dry_run(monkeypatch, market, dry_run):
    monkeypatch.delenv("BOT_BUY_PAUSED", raising=False)
    # Guard precedes data reads: a caller cannot bypass it by directly authorizing a buy.
    qty, reason = bot._authorized_buy_qty(UID, market, "NEW", 100., 1, {}, 1.,
                                        balance={}, dry_run=dry_run)
    assert qty == 0 and "BOT_BUY_PAUSED" in reason


@pytest.mark.parametrize("market", ["kr", "us"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_default_pause_blocks_new_and_add_without_calling_advisor(tmp_path, monkeypatch, market, dry_run):
    _setup(monkeypatch, tmp_path, market, {"HELD": [100.], "NEW": [100.]},
           [_sig("HELD"), _sig("NEW")],
           positions={"HELD": {"name": "HELD", "qty": 1, "avg_price": 100.}})
    db.bot_position_upsert(UID, "HELD", "HELD", 1, 100., 100., "2020-01-01",
                           market=market, tranches_done=1, last_buy_date="2020-01-01")
    monkeypatch.delenv("BOT_BUY_PAUSED", raising=False)
    before = db.kv_get(paper._key(UID, market))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("paused run must not select or place a buy")
    monkeypatch.setattr(bot.advisor, "advise", forbidden)
    monkeypatch.setattr(paper, "place_order", forbidden)

    out = bot.run_once(UID, market=market, dry_run=dry_run)

    assert out["ok"] and out["buy_paused"] and "BOT_BUY_PAUSED" in out["buy_pause_reason"]
    assert out["buys"] == [] and out["sells"] == []
    assert db.kv_get(paper._key(UID, market)) == before
    state = bot._state(UID, market)
    assert state["buy_paused"] and state["buy_pause_reason"] == out["buy_pause_reason"]
    baseline = db.kv_get(f"bot_day_equity:{UID}:{market}:{bot._today(market)}")
    assert (baseline is None) is dry_run  # Pausing must not skip the daily-equity baseline.


@pytest.mark.parametrize("market", ["kr", "us"])
@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("exit_kind,price,event,expected_qty", [
    ("STOP_LOSS", 80., {}, 10),
    ("TAKE_PROFIT", 140., {}, 10),
    ("EVENT", 100., {"event_risk": True, "event_severity": "critical", "event_note": "event"}, 10),
    ("EVENT_TRIM", 100., {"event_risk": True, "event_severity": "serious", "event_note": "event"}, 5),
])
def test_pause_keeps_holding_exits(tmp_path, monkeypatch, market, dry_run,
                                  exit_kind, price, event, expected_qty):
    _setup(monkeypatch, tmp_path, market, {"HELD": [price]}, [_sig("HELD", "HOLD", **event)],
           positions={"HELD": {"name": "HELD", "qty": 10, "avg_price": 100.}})
    monkeypatch.delenv("BOT_BUY_PAUSED", raising=False)

    out = bot.run_once(UID, market=market, dry_run=dry_run)

    assert out["buy_paused"] and out["buys"] == []
    assert [(s["reason"], s["qty"]) for s in out["sells"]] == [(exit_kind, expected_qty)]
    if not dry_run:
        assert out["sells"][0]["ok"] is True
    remaining = paper.balance(UID, market)["holdings"]
    assert sum(h["qty"] for h in remaining) == (10 if dry_run else 10 - expected_qty)


@pytest.mark.parametrize("market", ["kr", "us"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_pause_cannot_sell_for_rotation(tmp_path, monkeypatch, market, dry_run):
    _setup(monkeypatch, tmp_path, market, {"WEAK": [100.], "STRONG": [50.]},
           [_sig("WEAK", "HOLD", .2), _sig("STRONG")], cash=0., max_positions=1,
           positions={"WEAK": {"name": "WEAK", "qty": 100, "avg_price": 100.}})
    db.bot_position_upsert(UID, "WEAK", "WEAK", 100, 100., 100., "2020-01-01", market=market)
    monkeypatch.delenv("BOT_BUY_PAUSED", raising=False)

    out = bot.run_once(UID, market=market, dry_run=dry_run)

    assert out["buy_paused"] and out["sells"] == [] and out["buys"] == []
    assert paper.balance(UID, market)["holdings"][0]["ticker"] == "WEAK"


@pytest.mark.parametrize("market", ["kr", "us"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_pause_preserves_pending_reservations_without_loading_signals(tmp_path, monkeypatch, market, dry_run):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOT_KILL_SWITCH", "false")
    monkeypatch.delenv("BOT_BUY_PAUSED", raising=False)
    db.bot_reservation_add(UID, "NEW", "NEW", "buy", 100., .02, "test", market=market)
    before = db.bot_reservations_pending(UID, market)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("paused reservation must remain untouched")
    monkeypatch.setattr(bot, "_market_read_for", forbidden)
    monkeypatch.setattr(db, "bot_reservation_resolve", forbidden)

    out = bot.execute_reservations(UID, market=market, dry_run=dry_run)

    assert out["ok"] and out["executed"] == [] and out["buy_paused"]
    assert "BOT_BUY_PAUSED" in out["reason"]
    assert db.bot_reservations_pending(UID, market) == before


@pytest.mark.parametrize("market", ["kr", "us"])
def test_reservation_final_pause_guard_does_not_resolve_pending(tmp_path, monkeypatch, market):
    _setup(monkeypatch, tmp_path, market, {"NEW": [100.]}, [_sig("NEW")])
    db.bot_reservation_add(UID, "NEW", "NEW", "buy", 100., .02, "test", market=market)
    original = bot._authorized_buy_qty

    def pause_before_final_check(*args, **kwargs):
        monkeypatch.setenv("BOT_BUY_PAUSED", "true")
        return original(*args, **kwargs)
    monkeypatch.setattr(bot, "_authorized_buy_qty", pause_before_final_check)
    before = db.bot_reservations_pending(UID, market)

    out = bot.execute_reservations(UID, market=market)

    assert out["buy_paused"] and out["executed"] == []
    assert db.bot_reservations_pending(UID, market) == before


@pytest.mark.parametrize("market", ["kr", "us"])
def test_explicit_resume_restores_selection_and_changes_execution_policy(tmp_path, monkeypatch, market):
    cfg, read = _setup(monkeypatch, tmp_path, market, {"NEW": [100.]}, [_sig("NEW")])
    monkeypatch.delenv("BOT_BUY_PAUSED", raising=False)
    paused_ids = bot._applied_policy_ids(market, cfg, read)
    monkeypatch.setenv("BOT_BUY_PAUSED", "false")
    resumed_ids = bot._applied_policy_ids(market, cfg, read)
    monkeypatch.setattr(bot.advisor, "advise", lambda *_args, **_kwargs: None)

    out = bot.run_once(UID, market=market)

    assert out["buy_paused"] is False and out["buy_pause_reason"] is None
    assert out["buys"][0]["ticker"] == "NEW" and out["buys"][0]["ok"]
    assert paused_ids[0] == resumed_ids[0] and paused_ids[1] != resumed_ids[1]
