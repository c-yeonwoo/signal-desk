from signal_desk.signals import portfolio_trade_plan as ptp
from signal_desk.signals import portfolio_pending


def test_plan_sells_before_buying_and_includes_execution_costs():
    allocation = {"ready": True, "items": [
        {"ticker": "SELL", "name": "매도", "action": "축소 검토", "delta_value": -500,
         "target_weight_pct": 20},
        {"ticker": "BUY", "name": "매수", "action": "확대 검토", "delta_value": 200,
         "target_weight_pct": 20},
    ]}
    rows = [
        {"ticker": "SELL", "qty": 10, "price": 100, "value": 1000},
        {"ticker": "BUY", "qty": 1, "price": 100, "value": 100, "entry_allowed": True},
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
    out = ptp.plan(allocation, [{"ticker": "A", "qty": 1, "price": 100, "value": 100, "entry_allowed": True}], cash=150, market="us")
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


def test_pending_buy_reserves_limit_cash_and_blocks_duplicate_action():
    rows = [{"ticker": "A", "qty": 0, "price": 100, "value": 0, "entry_allowed": True}]
    pending = portfolio_pending.project(rows, cash=500, market="us",
                                        orders=[{"ticker": "A", "side": "buy", "qty": 2,
                                                 "limit_price": 110}])
    assert pending["ready"] and pending["rows"][0]["qty"] == 2
    assert pending["cash"] == 280  # US default commission = 0; limit includes no extra slippage.
    allocation = {"ready": True, "items": [{"ticker": "A", "name": "A", "action": "확대 검토",
                                               "delta_value": 300, "target_weight_pct": 50}]}
    plan = ptp.plan(allocation, pending["rows"], cash=pending["cash"], market="us")
    assert plan["ready"] and plan["instructions"] == []
    assert "미체결" in plan["blocked_buys"][0]["reason"]
    assert plan["post_trade"]["position_values"]["A"] == 200


def test_pending_sell_does_not_credit_cash_or_create_duplicate_sell():
    rows = [{"ticker": "A", "qty": 5, "price": 100, "value": 500}]
    pending = portfolio_pending.project(rows, cash=10, market="us",
                                        orders=[{"ticker": "A", "side": "sell", "qty": 3,
                                                 "limit_price": 100}])
    assert pending["ready"] and pending["cash"] == 10
    allocation = {"ready": True, "items": [{"ticker": "A", "name": "A", "action": "축소 검토",
                                               "delta_value": -500, "target_weight_pct": 0}]}
    plan = ptp.plan(allocation, pending["rows"], cash=pending["cash"], market="us")
    assert plan["instructions"] == []
    assert plan["post_trade"]["position_values"]["A"] == 500


def test_unknown_or_oversubscribed_pending_order_abstains():
    rows = [{"ticker": "A", "qty": 2, "price": 100, "value": 200}]
    for order in ({"ticker": "A", "side": "buy", "qty": None, "limit_price": 100},
                  {"ticker": "A", "side": "buy", "qty": 3, "limit_price": 100},
                  {"ticker": "A", "side": "sell", "qty": 3, "limit_price": 100}):
        assert not portfolio_pending.project(rows, cash=200, market="us", orders=[order])["ready"]


def test_pending_limit_not_close_is_used_for_concentration_cap():
    rows = [{"ticker": "A", "qty": 0, "price": 100, "value": 0, "sector": "tech"}]
    projected = portfolio_pending.project(rows, cash=500, market="us",
                                          orders=[{"ticker": "A", "side": "buy", "qty": 1,
                                                   "limit_price": 200}])
    profile = {"min_cash_pct": 0, "max_single_position_pct": 30,
               "max_sector_pct": 100, "max_cluster_pct": 100}
    out = ptp.plan({"ready": True, "items": []}, projected["rows"], cash=projected["cash"],
                   market="us", profile=profile)
    assert out["ready"] is False
    assert "종목 한도: A" in out["violations"]
