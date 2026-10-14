"""Observed cost attribution must not invent zero-cost legacy fills."""

from signal_desk.signals import execution_cost_shadow


def _event(key, ticker, kind, qty, reference, fill, fees):
    return {"event_key": key, "ticker": ticker, "event_type": kind, "price": fill,
            "payload": {"qty": qty, "reference_price": reference, "fees": fees}}


def test_partial_fifo_uses_only_closed_quantity_and_allocates_fees():
    rows = [_event("b", "ABC", "filled_buy", 10, 100, 101, 2),
            _event("s", "ABC", "filled_sell", 4, 110, 109, 1)]
    out = execution_cost_shadow.analyze(rows)
    assert out["closed_fragments"] == 1
    assert out["open_lots"] == 1
    assert out["gross_reference_pnl"] == 40
    assert out["net_booked_pnl"] == 30.2
    assert out["observed_cost_drag"] == 9.8
    assert out["two_x_cost_return_pct"] == 5.1
    assert out["live_eligible"] is False


def test_missing_basis_is_coverage_gap_not_zero_cost():
    rows = [_event("old", "ABC", "filled_buy", 3, 100, 101, 1),
            _event("old-sell", "ABC", "filled_sell", 1, 110, 109, None),
            _event("later", "ABC", "filled_sell", 2, 110, 109, 1)]
    out = execution_cost_shadow.analyze(rows)
    assert out["closed_fragments"] == 0
    assert out["coverage_gaps"]["incomplete_sell"] == 1
    assert out["coverage_gaps"]["unknown_basis_qty"] == 2
    assert out["net_booked_return_pct"] is None


def test_unknown_buy_quantity_blocks_future_ticker_pairs():
    rows = [_event("old", "ABC", "filled_buy", None, 100, 101, 1),
            _event("later", "ABC", "filled_buy", 1, 100, 101, 1),
            _event("sell", "ABC", "filled_sell", 1, 110, 109, 1)]
    out = execution_cost_shadow.analyze(rows)
    assert out["closed_fragments"] == 0
    assert out["blocked_tickers"] == ["ABC"]


def test_favorable_fill_does_not_turn_two_x_stress_into_a_bonus():
    out = execution_cost_shadow.analyze([
        _event("b", "ABC", "filled_buy", 1, 100, 99, 0),
        _event("s", "ABC", "filled_sell", 1, 110, 111, 0),
    ])
    assert out["net_booked_pnl"] == 12
    assert out["gross_reference_pnl"] == 10
    assert out["two_x_cost_return_pct"] == 10


def test_recent_event_page_keeps_order_and_exposes_truncated_basis(tmp_path, monkeypatch):
    from signal_desk import db
    monkeypatch.chdir(tmp_path)
    for i in range(5):
        db.execution_event_add(str(i), uid=77, market="kr", ticker="ABC",
                               event_type="filled_buy", price=100, payload={"qty": 1}, ts=i)
    events = db.execution_events_for_uid(77, "kr", limit=2)
    assert [e["event_key"] for e in events] == ["3", "4"]
    assert db.execution_events_count_for_uid(77, "kr") == 5


def test_closed_trade_groups_use_booked_cash_and_report_partial_lots():
    buy = _event("b", "ABC", "filled_buy", 10, 100, 101, 2)
    buy["payload"]["reason"] = "SIGNAL"
    buy["ts"] = 1000
    first = _event("s1", "ABC", "filled_sell", 4, 110, 109, 1)
    first["payload"]["reason"] = "STOP_LOSS"
    first["ts"] = 1000 + 86400
    second = _event("s2", "ABC", "filled_sell", 6, 90, 89, 1.5)
    second["payload"]["reason"] = "TRAILING"
    second["ts"] = 1000 + 2 * 86400
    out = execution_cost_shadow.analyze([buy, first, second])
    assert out["closed_fragments"] == 2
    assert out["net_booked_pnl"] == -44.5
    assert out["by_entry_reason"][0]["key"] == "SIGNAL"
    assert out["by_entry_reason"][0]["distinct_buy_orders"] == 1
    assert out["by_entry_reason"][0]["distinct_sell_orders"] == 2
    assert out["by_entry_reason"][0]["median_holding_days"] == 1.5
    assert [r["key"] for r in out["by_exit_reason"]] == ["TRAILING", "STOP_LOSS"]
    assert out["by_exit_reason"][0]["net_booked_pnl"] == -74.7
    assert out["by_ticker"][0]["key"] == "ABC"


def test_admin_api_only_exposes_shadow_costs(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from signal_desk import api
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_EMAILS", "cost-admin@example.com")
    guest = TestClient(api.app)
    assert guest.get("/api/admin/research/execution-cost").status_code == 401
    guest.post("/api/auth/signup", json={"email": "reader@example.com", "pw": "abcdef12"})
    assert guest.get("/api/admin/research/execution-cost").status_code == 403
    admin = TestClient(api.app)
    admin.post("/api/auth/signup", json={"email": "cost-admin@example.com", "pw": "abcdef12"})
    response = admin.get("/api/admin/research/execution-cost")
    assert response.status_code == 200
    assert response.json()["live_eligible"] is False
    assert response.json()["full_event_history"] is True
