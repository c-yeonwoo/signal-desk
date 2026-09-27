from types import SimpleNamespace

import exchange_calendars as xcals

from signal_desk import strategy
from signal_desk.signals import portfolio_reference_shadow as shadow


def _history():
    days = [d.date().isoformat() for d in xcals.get_calendar("XNYS").sessions_in_range("2026-05-01", "2026-09-25")]
    prices = [100.0]
    for i in range(1, len(days)):
        prices.append(prices[-1] * (1.012 if i % 2 else 0.996))
    return days, prices


def test_three_profiles_use_same_alpha_but_preserve_existing_position_sizes():
    dates, prices = _history()
    balances = {style: {"cash": 10_000, "total_eval": 10_000, "holdings": []}
                for style in strategy.STYLES}
    out = shadow.compare(
        market="us", universe=[{"ticker": "AAA", "name": "A", "sector": "tech"}],
        prices={"AAA": prices}, dates_by={"AAA": dates},
        signal_by_ticker={"AAA": SimpleNamespace(kind="BUY", score=2.0, event_risk=False)},
        signal_policy_id="shared", balances=balances,
        reservations={style: [] for style in strategy.STYLES})
    assert out["live_eligible"] is False and out["same_alpha_snapshot"]
    profiles = out["profiles"]
    assert all(p["result"]["decision"]["signal_policy_id"] == "shared" for p in profiles.values())
    assert all(p["result"]["entry_candidates"]["candidates"][0]["ticker"] == "AAA" for p in profiles.values())
    weights = [profiles[style]["result"]["allocation"]["items"][0]["target_weight_pct"]
               for style in strategy.STYLES]
    assert weights == [6.0, 8.0, 14.0]
    assert [profiles[s]["profile"]["min_cash_pct"] for s in strategy.STYLES] == [28.0, 20.0, 16.0]
    assert [profiles[s]["profile"]["max_positions"] for s in strategy.STYLES] == [12, 10, 6]


def test_reference_reservation_quantity_is_estimated_not_treated_as_fill():
    balance = {"cash": 1_000, "total_eval": 10_000, "holdings": []}
    result = shadow.reservation_orders(
        [{"ticker": "AAA", "side": "buy", "target_price": 100, "max_chase_pct": 0.02}],
        balance=balance, style="balanced")
    assert result["orders"] == [{"ticker": "AAA", "side": "buy", "qty": 8, "limit_price": 102.0}]
    assert "estimated" in result["quantity_status"]
    unknown = shadow.reservation_orders(
        [{"ticker": "AAA", "side": "buy", "target_price": 0, "max_chase_pct": 0.02}],
        balance=balance, style="balanced")
    assert not unknown["ready"]


def test_reference_comparison_is_admin_only_and_cannot_send_orders(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from signal_desk import api

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_EMAILS", "reference-admin@example.com")
    guest = TestClient(api.app)
    assert guest.get("/api/admin/research/reference-allocation").status_code == 401
    guest.post("/api/auth/signup", json={"email": "reader@example.com", "pw": "abcdef12"})
    assert guest.get("/api/admin/research/reference-allocation").status_code == 403
    admin = TestClient(api.app)
    admin.post("/api/auth/signup", json={"email": "reference-admin@example.com", "pw": "abcdef12"})
    monkeypatch.setattr(api.store, "load_universe", lambda: [])
    monkeypatch.setattr(api.store, "load_portfolio_close_bundle", lambda market: ({}, {}))
    monkeypatch.setattr(api, "_signals", lambda: [])
    monkeypatch.setattr(api, "_signal_policy_id", lambda market, signals: "frozen")
    monkeypatch.setattr(api.paper, "balance", lambda uid, market: {"cash": 100, "total_eval": 100, "holdings": []})
    monkeypatch.setattr(api.db, "bot_reservations_pending", lambda uid, market: [])
    monkeypatch.setattr(api.portfolio_reference_shadow, "compare", lambda **kwargs: {"live_eligible": False, "mode": "shadow"})
    response = admin.get("/api/admin/research/reference-allocation")
    assert response.status_code == 200
    assert response.json() == {"live_eligible": False, "mode": "shadow"}
