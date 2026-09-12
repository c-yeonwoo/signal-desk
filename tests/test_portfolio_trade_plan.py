from signal_desk.signals import portfolio_trade_plan as ptp


def test_plan_sells_before_buying_and_includes_execution_costs():
    allocation = {"ready": True, "items": [
        {"ticker": "SELL", "name": "매도", "action": "축소 검토", "delta_value": -500,
         "target_weight_pct": 20},
        {"ticker": "BUY", "name": "매수", "action": "확대 검토", "delta_value": 200,
         "target_weight_pct": 20},
    ]}
    rows = [
        {"ticker": "SELL", "qty": 10, "price": 100, "value": 1000},
        {"ticker": "BUY", "qty": 1, "price": 100, "value": 100},
    ]
    out = ptp.plan(allocation, rows, cash=0, market="kr")
    assert out["ready"] is True and out["execution_order"] == "sell_then_buy"
    assert [(x["ticker"], x["side"], x["qty"]) for x in out["instructions"]] == [("SELL", "sell", 5), ("BUY", "buy", 2)]
    assert out["estimated"]["fees"] > 0 and out["estimated"]["slippage"] > 0
    assert out["estimated"]["cash_after"] > 0


def test_fractional_share_position_blocks_integer_execution_plan():
    out = ptp.plan({"ready": True, "items": []}, [{"ticker": "A", "qty": 1.5, "price": 100}], cash=0, market="us")
    assert out["ready"] is False and "분할주" in out["reason"]


def test_partial_buy_is_reported_when_cash_is_insufficient():
    allocation = {"ready": True, "items": [{"ticker": "A", "name": "A", "action": "확대 검토",
                                                 "delta_value": 1_000, "target_weight_pct": 50}]}
    out = ptp.plan(allocation, [{"ticker": "A", "qty": 1, "price": 100, "value": 100}], cash=150, market="us")
    assert out["instructions"][0]["qty"] == 1
    assert out["unfunded_buys"] == [{"ticker": "A", "remaining_qty": 9}]


def test_expansion_is_blocked_without_current_buy_signal():
    allocation = {"ready": True, "items": [{"ticker": "A", "name": "A", "action": "확대 검토",
                                                 "delta_value": 500, "target_weight_pct": 50}]}
    out = ptp.plan(allocation, [{"ticker": "A", "qty": 1, "price": 100, "value": 100,
                                 "entry_allowed": False, "entry_block_reason": "현재 BUY 시그널 없음"}],
                   cash=1_000, market="us")
    assert out["instructions"] == []
    assert out["blocked_buys"] == [{"ticker": "A", "name": "A", "reason": "현재 BUY 시그널 없음"}]
