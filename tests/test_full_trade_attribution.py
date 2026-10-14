"""Complete paper history is not silently reduced to the new execution ledger."""

import json

from signal_desk.signals import full_trade_attribution as audit


def _trade(id, side, qty, price, cash=None, reason="SIGNAL", ticker="ABC"):
    return {"id": id, "ticker": ticker, "side": side, "qty": qty,
            "price": price, "cash_change": cash, "reason": reason}


def test_full_journal_uses_paper_average_cost_and_separates_unknown_costs():
    trades = [_trade(1, "buy", 10, 100, -1005),
              _trade(2, "buy", 10, 80, -804, "ADD"),
              _trade(3, "sell", 5, 90, 449, "STOP_LOSS"),
              _trade(4, "sell", 15, 110, None, "TAKE_PROFIT")]
    account = {"cash": 8640 + 1650, "positions": {}}
    out = audit.analyze(trades, seed_cash=10000, account=account)
    assert out["trade_rows"] == 4
    assert out["closed_sell_rows"] == 2
    assert out["price_only_pnl"] == 300
    assert out["booked_net_pnl"] == -3.25
    assert out["booked_net_sell_rows"] == 1
    assert out["coverage_gaps"]["missing_or_invalid_cash_rows"] == 1
    assert out["account_reconciled"] is True
    assert out["account_realized_pnl"] == 290
    assert out["by_exit_reason"][0]["key"] == "STOP_LOSS"
    assert out["by_entry_pattern"][0]["key"] == "추가매수 포함"


def test_old_price_only_trades_are_not_called_booked_net():
    out = audit.analyze([_trade(1, "buy", 2, 100),
                         _trade(2, "sell", 2, 90, reason="STOP_LOSS")],
                        seed_cash=1000, account={"cash": 980, "positions": {}})
    assert out["price_only_pnl"] == -20
    assert out["booked_net_sell_rows"] == 0
    assert out["booked_net_pnl"] == 0
    assert out["account_reconciled"] is True
    assert out["by_exit_reason"][0]["cost_unknown_rows"] == 1


def test_unmatched_sell_does_not_create_fictitious_basis():
    out = audit.analyze([_trade(1, "sell", 2, 90),
                         _trade(2, "buy", 1, 80),
                         _trade(3, "sell", 1, 100)],
                        seed_cash=1000, account={"cash": 1000, "positions": {}})
    assert out["closed_sell_rows"] == 0
    assert out["account_reconciled"] is False
    assert out["coverage_gaps"]["unmatched_sell_qty"] == 2
    assert out["blocked_tickers"] == ["ABC"]


def test_account_quantity_or_basis_gap_withholds_reconciliation():
    trades = [_trade(1, "buy", 2, 100), _trade(2, "sell", 1, 90)]
    out = audit.analyze(trades, seed_cash=1000,
                        account={"cash": 890, "positions": {"ABC": {"qty": 2, "avg_price": 100}}})
    assert out["position_mismatches"] == [{"ticker": "ABC", "journal_qty": 1, "account_qty": 2}]
    assert out["account_reconciled"] is False
    assert out["price_only_pnl"] == -10


def test_snapshot_reads_all_trades_and_account_without_event_limit(tmp_path, monkeypatch):
    from signal_desk import db
    monkeypatch.chdir(tmp_path)
    db.user_bot_get(77)
    c = db.conn()
    try:
        c.executemany(
            "INSERT INTO bot_trades(uid,market,ticker,side,qty,price,reason,ts) VALUES(77,'kr','ABC',?,?,?,?,?)",
            [("buy", 1, 100, "SIGNAL", 1), ("sell", 1, 90, "STOP_LOSS", 2)],
        )
        c.execute("INSERT INTO kv(k,v,ts) VALUES(?,?,0)",
                  ("paper_account:77", json.dumps({"cash": 9999990, "positions": {}})))
        c.commit()
    finally:
        c.close()
    snapshot = db.bot_trade_audit_snapshot(77, "kr")
    assert len(snapshot["trades"]) == 2
    assert snapshot["last_trade_id"] == snapshot["trades"][-1]["id"]
    assert snapshot["account"]["cash"] == 9999990


def test_admin_endpoint_reports_full_journal_separately_from_recent_cost_slice(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from signal_desk import api, auth, bot, db
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_EMAILS", "journal-admin@example.com")
    uid = next(uid for uid, style in bot.REFERENCE_BOTS.items() if style == "balanced")
    db.user_bot_get(uid)
    c = db.conn()
    try:
        c.executemany(
            "INSERT INTO bot_trades(uid,market,ticker,side,qty,price,reason,ts) VALUES(?,'kr','ABC',?,?,?,?,?)",
            [(uid, "buy", 1, 100, "SIGNAL", 1),
             (uid, "sell", 1, 90, "STOP_LOSS", 2)],
        )
        c.execute("INSERT INTO kv(k,v,ts) VALUES(?,?,0)",
                  (f"paper_account:{uid}", json.dumps({"cash": 9999990, "positions": {}})))
        c.commit()
    finally:
        c.close()
    admin = TestClient(api.app)
    token, error = auth.signup("journal-admin@example.com", "abcdef12")
    assert error is None
    admin.cookies.set(auth.COOKIE, token)
    response = admin.get("/api/admin/research/execution-cost")
    assert response.status_code == 200
    report = response.json()
    assert report["closed_fragments"] == 0
    assert report["full_trade_attribution"]["trade_rows"] == 2
    assert report["full_trade_attribution"]["price_only_pnl"] == -10
    assert report["full_trade_attribution"]["booked_net_sell_rows"] == 0
    assert report["full_trade_attribution"]["account_reconciled"] is True
