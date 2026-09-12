from signal_desk import bot, db


def test_execution_cost_summary_does_not_treat_legacy_rows_as_zero_cost(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db.bot_trade_log(900002, "A", "A", "buy", 1, 100, "SIGNAL", "old", market="kr")
    db.bot_trade_log(900002, "B", "B", "buy", 1, 100, "SIGNAL", "new", market="kr",
                     reference_price=100, fees=1.2, slippage_cost=0.5, cash_change=-101.7)
    costs = db.bot_execution_costs(900002, "kr")

    assert costs == {"trades": 2, "cost_recorded_trades": 1, "coverage_pct": 50.0,
                     "fees": 1.2, "slippage_cost": 0.5, "total_execution_cost": 1.7}


def test_execution_performance_holds_pre_cost_pnl_until_coverage_complete(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bot, "ledger_state", lambda style, market: {
        "style": "balanced", "currency": "KRW", "total_pnl": -10.0})
    db.bot_trade_log(900002, "A", "A", "buy", 1, 100, "SIGNAL", "old", market="kr")
    out = bot.execution_performance("balanced", "kr")

    assert out["estimated_pre_cost_pnl"] is None
    assert out["full_cost_coverage"] is False
