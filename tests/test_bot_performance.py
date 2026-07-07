"""봇 track record — 일별 자산 스냅샷 기록 + 성과(수익률·MDD·자산곡선) 집계."""

import json

from signal_desk import bot, db
from signal_desk.broker import paper
from signal_desk.signals.engine import SignalResult

UID = 21


def _setup(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    uni = [{"ticker": "AAA", "name": "가"}]
    monkeypatch.setattr(bot.store, "load_universe", lambda: uni)
    monkeypatch.setattr(bot.store, "load_price_series", lambda: {"AAA": [100.0, 110.0]})
    monkeypatch.setattr(bot.store, "load_us_price_series", lambda: {})
    monkeypatch.setattr(bot.store, "load_fundamentals", lambda: {})
    monkeypatch.setattr(bot.engine, "evaluate", lambda *a, **k: [
        SignalResult(ticker="AAA", name="가", score=2.0, kind="BUY", confidence=0.6,
                     technical_score=0.0, fundamental_score=0.0, has_fundamental=False, reasons=[])])
    monkeypatch.setattr(bot, "_market_read", lambda p: {"eff_cfg": None, "adapt": {}, "context": {"regime": "중립"}})
    monkeypatch.setattr(bot, "_cfg", lambda uid: {
        "enabled": True, "trading_style": "balanced", "seed_cash": 1_000_000, "seed_cash_us": 10_000,
        "max_positions": 10, "position_pct": 0.1, "min_buy_score": 1.0, "max_new_buys_per_run": 5})


def test_run_records_daily_equity(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    db.kv_set(f"paper_account:{UID}", json.dumps({"cash": 1_000_000.0, "positions": {}}))
    bot.run_once(UID, market="kr")
    curve = db.bot_equity_curve(UID, "kr")
    assert len(curve) == 1 and curve[0]["total_eval"] > 0    # 오늘 자산 1점 기록됨


def test_performance_summary(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    db.kv_set(f"paper_account:{UID}", json.dumps({"cash": 1_000_000.0, "positions": {}}))
    # 자산곡선 수동 시딩(수익 후 낙폭)
    for d, te in [("2026-07-01", 1_000_000), ("2026-07-02", 1_100_000), ("2026-07-03", 1_045_000)]:
        db.bot_equity_record(UID, "kr", d, te, te, 0)
    perf = bot.performance(UID, "kr")
    assert perf["seed"] == 1_000_000 and perf["days"] == 3
    assert perf["max_drawdown_pct"] == -5.0                   # 110만 → 104.5만 = -5%
    assert perf["return_pct"] is not None and perf["currency"] == "KRW"
