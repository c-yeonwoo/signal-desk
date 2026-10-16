"""Paper policy sensitivities do not become historical fills or live rules."""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from signal_desk import market_clock
from signal_desk.signals import paper_policy_counterfactual as study


def _trade(id, side, qty, price, day="2026-07-14", reason="SIGNAL"):
    ts = int(dt.datetime.fromisoformat(day + "T10:00:00").replace(
        tzinfo=ZoneInfo("Asia/Seoul")).timestamp())
    return {"id": id, "ticker": "ABC", "side": side, "qty": qty,
            "price": price, "ts": ts, "reason": reason}


def _panel(entry="2026-07-14", price=120):
    days = market_clock.next_sessions("kr", entry, 20)
    return {"ABC": ([entry, *days], [100, *([price] * 20)])}


def test_no_add_and_fixed_exits_use_same_first_entry_but_not_same_notional():
    trades = [_trade(1, "buy", 10, 100), _trade(2, "buy", 10, 80, reason="ADD"),
              _trade(3, "sell", 5, 90, day="2026-07-15"),
              _trade(4, "sell", 15, 110, day="2026-07-16")]
    out = study.analyze(trades, _panel(), market="kr", close_basis_verified=True,
                        now=dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc))
    row = out["episodes"][0]
    assert row["actual_net_25bps_pct"] == 16.417
    assert row["no_add_net_25bps_pct"] == 4.75
    assert row["no_add_minus_actual_pp"] == -11.667
    assert row["h5_net_25bps_pct"] == 19.75
    assert row["h20_net_25bps_pct"] == 19.75
    assert row["added_notional"] == 800
    assert out["summary"]["h5_net_25bps_pct"]["eligible"] == 1
    assert out["summary"]["h5_net_25bps_pct"]["mean_delta_vs_actual_same_episodes_pp"] == 3.333
    assert out["live_eligible"] is False
    assert out["total_return_verified"] is False


def test_missing_intermediate_close_blocks_both_horizons_without_compression():
    panel = _panel()
    days, prices = panel["ABC"]
    panel["ABC"] = (days[:2] + days[3:], prices[:2] + prices[3:])
    out = study.analyze([_trade(1, "buy", 1, 100),
                         _trade(2, "sell", 1, 90, day="2026-07-15")], panel,
                        market="kr", close_basis_verified=True,
                        now=dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc))
    row = out["episodes"][0]
    assert row["h5_status"] == "missing_close"
    assert row["h20_status"] == "missing_close"
    assert out["coverage_gaps"]["missing_close_h5"] == 1
    assert out["summary"]["h5_net_25bps_pct"]["eligible"] == 0


def test_missing_entry_close_blocks_horizon_even_if_future_prices_exist():
    panel = _panel()
    days, prices = panel["ABC"]
    panel["ABC"] = (days[1:], prices[1:])
    out = study.analyze([_trade(1, "buy", 1, 100),
                         _trade(2, "sell", 1, 90, day="2026-07-15")], panel,
                        market="kr", close_basis_verified=True,
                        now=dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc))
    assert out["episodes"][0]["h5_status"] == "missing_entry_close"
    assert out["episodes"][0]["h20_net_25bps_pct"] is None


def test_unverified_close_basis_blocks_numeric_exit_arms_by_default():
    out = study.analyze([_trade(1, "buy", 1, 100),
                         _trade(2, "sell", 1, 90, day="2026-07-15")],
                        _panel(), market="kr",
                        now=dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc))
    assert out["episodes"][0]["actual_net_25bps_pct"] == -10.25
    assert out["episodes"][0]["h5_status"] == "price_basis_unverified"
    assert out["summary"]["h5_net_25bps_pct"]["eligible"] == 0


def test_unmatured_close_stays_pending_and_later_episode_does_not_disappear():
    trades = [_trade(1, "buy", 1, 100), _trade(2, "sell", 1, 90, day="2026-07-15"),
              _trade(3, "buy", 2, 80, day="2026-07-16")]
    out = study.analyze(trades, _panel(), market="kr", close_basis_verified=True,
                        now=dt.datetime(2026, 7, 16, 0, 0, tzinfo=dt.timezone.utc))
    assert out["closed_episodes"] == 1
    assert out["open_episodes"] == 1
    assert out["episodes"][0]["h5_status"] == "pending"
    assert out["episodes"][0]["h5_net_25bps_pct"] is None


def test_invalid_or_unmatched_trade_blocks_ticker_instead_of_making_a_winner():
    trades = [_trade(1, "buy", 1, 100), _trade(2, "sell", 2, 120),
              _trade(3, "buy", 1, 80)]
    out = study.analyze(trades, _panel(), market="kr", close_basis_verified=True,
                        now=dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc))
    assert out["closed_episodes"] == 0
    assert out["blocked_tickers"] == ["ABC"]
    assert out["coverage_gaps"]["unmatched_sell"] == 1
    with pytest.raises(ValueError, match="journal IDs"):
        study.analyze([trades[0], trades[0]], {}, market="kr")


def test_admin_endpoint_is_read_only_and_restricts_market_style(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from signal_desk import api, auth, bot, db, store

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_EMAILS", "sensitivity-admin@example.com")
    uid = next(u for u, style in bot.REFERENCE_BOTS.items() if style == "balanced")
    db.user_bot_get(uid)
    c = db.conn()
    try:
        c.executemany(
            "INSERT INTO bot_trades(uid,market,ticker,side,qty,price,reason,ts) "
            "VALUES(?,'kr','ABC',?,?,?,?,?)",
            [(uid, "buy", 1, 100, "SIGNAL", _trade(1, "buy", 1, 100)["ts"]),
             (uid, "sell", 1, 90, "SIGNAL", _trade(2, "sell", 1, 90, day="2026-07-15")["ts"])],
        )
        c.commit()
    finally:
        c.close()
    monkeypatch.setattr(store, "load_market_dated_closes", lambda market: _panel())
    client = TestClient(api.app)
    assert client.get("/api/admin/research/paper-policy-sensitivity").status_code == 401
    token, error = auth.signup("sensitivity-admin@example.com", "abcdef12")
    assert error is None
    client.cookies.set(auth.COOKIE, token)
    response = client.get("/api/admin/research/paper-policy-sensitivity")
    assert response.status_code == 200
    assert response.json()["closed_episodes"] == 1
    assert response.json()["episodes"][0]["actual_net_25bps_pct"] == -10.25
    assert response.json()["episodes"][0]["h5_net_25bps_pct"] is None
    assert response.json()["episodes"][0]["h5_status"] == "price_basis_unverified"
    assert response.json()["close_basis_state"] == "unverified_adjustment_provenance"
    assert client.get("/api/admin/research/paper-policy-sensitivity?style=invalid").status_code == 422
    assert client.get("/api/admin/research/paper-policy-sensitivity?market=other").status_code == 422
