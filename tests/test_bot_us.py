"""해외(US) 페이퍼 봇 — 시장 격리(KR/US 계좌·포지션 분리), USD 시드."""

import json

from signal_desk import bot, db
from signal_desk.broker import paper
from signal_desk.signals.engine import SignalResult

UID = 11


def _sig(t, n, kind, score=0.0):
    return SignalResult(ticker=t, name=n, score=score, kind=kind, confidence=0.6,
                        technical_score=0.0, fundamental_score=0.0, has_fundamental=False, reasons=[])


def _setup(monkeypatch, **cfg):
    us_uni = [{"ticker": "AAPL", "name": "Apple"}, {"ticker": "NVDA", "name": "Nvidia"}]
    monkeypatch.setattr(bot.store, "load_us_universe", lambda: us_uni)
    monkeypatch.setattr(bot.store, "load_us_price_series", lambda: {"AAPL": [200.0], "NVDA": [120.0]})
    monkeypatch.setattr(bot.store, "load_price_series", lambda: {})  # KR 없음(us만)
    monkeypatch.setattr(bot, "us_signals", lambda: [_sig("AAPL", "Apple", "BUY", 2.0), _sig("NVDA", "Nvidia", "BUY", 1.8)])
    monkeypatch.setattr(bot, "_cfg", lambda uid: {
        "enabled": True, "trading_style": "balanced", "seed_cash": 10_000_000, "seed_cash_us": 10_000,
        "max_positions": 10, "position_pct": 0.08, "min_buy_score": 1.0, "max_new_buys_per_run": 5, **cfg})


def test_us_paper_buys_and_isolated_from_kr(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _setup(monkeypatch)
    db.user_bot_set_seed(UID, 10_000, market="us")
    out = bot.run_once(UID, market="us")
    assert out["ok"] and out["buys"], "US 매수가 실행돼야"
    us_pos = db.bot_positions_all(UID, "us")
    assert us_pos and all(p["ticker"] in ("AAPL", "NVDA") for p in us_pos)
    assert db.bot_positions_all(UID, "kr") == []          # KR 계좌엔 영향 없음(격리)
    b_us = paper.balance(UID, "us")
    assert b_us["cash"] < 10_000                          # USD 시드에서 차감
    assert paper.balance(UID, "kr")["cash"] == 10_000_000  # KR 계좌 그대로


def test_us_state_and_reset(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _setup(monkeypatch)
    bot.run_once(UID, market="us")
    st = bot.get_state(UID, "us")
    assert st["market"] == "us" and st["currency"] == "USD" and st["positions"]
    bot.reset(UID)  # 초기화는 양 시장 모두
    assert db.bot_positions_all(UID, "us") == [] and paper.balance(UID, "us")["cash"] == 10_000
