from signal_desk.signals import portfolio_outcomes as po


def test_buy_outcome_uses_costed_exit_and_keeps_raw_return_separate():
    dates = [f"2026-01-{i:02d}" for i in range(1, 23)]
    prices = [100.0] + [101.0] * 19 + [110.0, 111.0]
    item = {"side": "buy", "qty": 1, "reference_price": 100.0, "reference_date": dates[0],
            "entry_cash": 100.05}
    out = po.evaluate(item, dates=dates, closes=prices, market="us")
    h20 = next(x for x in out if x["horizon_days"] == 20)
    assert h20["raw_return_pct"] == 10.0 and h20["directional_return_pct"] == 10.0
    assert h20["cost_adjusted_return_pct"] < 10.0


def test_sell_outcome_is_directional_counterfactual_not_fake_realized_pnl():
    dates = [f"2026-02-{i:02d}" for i in range(1, 23)]
    prices = [100.0] + [101.0] * 19 + [110.0, 111.0]
    out = po.evaluate({"side": "sell", "qty": 1, "reference_price": 100.0, "reference_date": dates[0]},
                      dates=dates, closes=prices, market="kr")
    h20 = next(x for x in out if x["horizon_days"] == 20)
    assert h20["raw_return_pct"] == 10.0 and h20["directional_return_pct"] == -10.0
    assert h20["cost_adjusted_return_pct"] is None
