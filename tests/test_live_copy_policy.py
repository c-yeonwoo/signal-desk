import time

import pytest
from fastapi.testclient import TestClient

from signal_desk import config, db
from signal_desk.broker import live, live_policy


def creds():
    return {"env": "real", "app_key": "key", "app_secret": "secret", "account_no": "12345678", "product_cd": "01"}


def policy(**values):
    return {**live_policy.validate({"source_style": "balanced"}), **values}


def wire_account(monkeypatch, *, total=1000, cash=1000):
    from signal_desk.broker import kis
    monkeypatch.setattr(kis, "balance", lambda *a, **k: {"complete": True, "cash": cash, "total_eval": total,
        "holdings": [{"ticker": "005930", "qty": 1, "price": 100, "sellable_qty": 1}]})
    monkeypatch.setattr(kis, "daily_orders", lambda *a, **k: {"complete": True, "orders": []})
    monkeypatch.setattr(kis, "buying_power", lambda *a, **k: {"cash_without_margin": cash, "qty_without_margin": 100})


def test_style_defaults_are_small_and_never_enable_transport():
    for style, expected in (("conservative", 25), ("balanced", 50), ("aggressive", 75)):
        out = live_policy.defaults(style)
        assert out["follow_pct"] == expected
        assert out["mode"] == "copy_preview_only" and out["order_transmission_enabled"] is False


@pytest.mark.parametrize("data", [
    {"follow_pct": 0}, {"follow_pct": 101}, {"follow_pct": float("nan")},
    {"max_order_pct": 30, "max_daily_buy_pct": 20},
    {"max_order_pct": 30, "max_daily_buy_pct": 40, "max_position_pct": 20},
    {"min_cash_pct": 100},
])
def test_policy_validation_rejects_invalid_or_contradictory_limits(data):
    with pytest.raises(ValueError):
        live_policy.validate(data)


def test_copy_scales_notional_not_share_count():
    out = live_policy.scaled_quantity({"qty": 10, "price": 100}, follow_pct=50, limit_price=125)
    assert out == {"source_notional": 1000.0, "follow_pct": 50, "requested_notional": 500.0,
                   "qty": 4, "unallocated_notional": 0.0}
    assert live_policy.scaled_quantity({"qty": 1, "price": 100}, follow_pct=25, limit_price=100)["qty"] == 0


def test_copy_preflight_enforces_order_and_position_caps(monkeypatch):
    wire_account(monkeypatch)
    p = policy(max_order_pct=5, max_daily_buy_pct=20, max_position_pct=20, min_cash_pct=10)
    out = live.copy_preview({"id": 1, "ticker": "005930", "name": "삼성", "side": "buy", "qty": 10,
                             "price": 100, "ts": int(time.time())}, limit_price=100, policy=p, creds=creds())
    assert out["ready"] is False and out["order_transmission_enabled"] is False
    assert out["copy"]["qty"] == 5
    assert out["preflight"]["broker_checks_passed"] is True
    assert out["preflight"]["copy_policy"]["caps_passed"] is False
    assert "한도를 초과" in out["reason"]


def test_copy_preflight_does_not_claim_daily_limit_without_reservation_ledger(monkeypatch):
    wire_account(monkeypatch, total=10_000, cash=10_000)
    p = policy(max_order_pct=20, max_daily_buy_pct=30, max_position_pct=30, min_cash_pct=10)
    out = live.copy_preview({"id": 1, "ticker": "005930", "name": "삼성", "side": "buy", "qty": 10,
                             "price": 100, "ts": int(time.time())}, limit_price=100, policy=p, creds=creds())
    assert out["preflight"]["copy_policy"]["caps_passed"] is True
    assert out["preflight"]["copy_policy"]["passed"] is False
    minimum_cash = next(c for c in out["preflight"]["copy_policy"]["checks"] if c["key"] == "minimum_cash")
    assert minimum_cash == {"key": "minimum_cash", "actual": 9_499.67, "limit": 1_000.0,
                            "unit": "KRW", "comparison": "at_least", "passed": True}
    assert any(c["key"] == "daily_buy_budget" and not c["passed"] for c in out["preflight"]["copy_policy"]["checks"])
    assert "예약 원장" in out["reason"]


def test_policy_is_user_scoped_and_never_persists_an_execution_switch(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "copy.db")
    saved = db.live_copy_policy_set(1, {"source_style": "aggressive", "follow_pct": 60,
        "max_order_pct": 10, "max_daily_buy_pct": 20, "max_position_pct": 20, "min_cash_pct": 10})
    assert saved["configured"] and saved["order_transmission_enabled"] is False
    assert db.live_copy_policy_get(1)["source_style"] == "aggressive"
    assert db.live_copy_policy_get(2)["configured"] is False


def test_owner_routes_only_preview_and_resolve_server_side_source(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "routes.db")
    from signal_desk import api, live_routes
    monkeypatch.setenv("KIS_ACCOUNT_OWNER", "owner@example.com")
    monkeypatch.setattr(api.auth, "current_user", lambda *a: {"id": 7, "email": "owner@example.com"})
    monkeypatch.setattr(config, "kis_credentials", creds)
    monkeypatch.setattr(live_routes.bot, "ensure_reference_bots", lambda: None)
    source = {"id": 9, "ticker": "005930", "name": "삼성", "side": "buy", "qty": 10, "price": 100,
              "ts": int(time.time()), "reason": "SIGNAL"}
    monkeypatch.setattr(live_routes.db, "bot_trade_get", lambda *a: source)
    monkeypatch.setattr(live_routes.db, "bot_trades_recent", lambda *a: [source])
    seen = {}
    monkeypatch.setattr(live_routes.live, "copy_preview", lambda source, **kw: seen.update(source=source, **kw) or {
        "ready": False, "order_transmission_enabled": False, "reason": "locked"})
    client = TestClient(api.app)
    assert client.get("/api/live/copy-policy").json()["configured"] is False
    payload = {"source_style": "balanced", "follow_pct": 50, "max_order_pct": 10,
               "max_daily_buy_pct": 20, "max_position_pct": 15, "min_cash_pct": 25}
    assert client.put("/api/live/copy-policy", json=payload).json()["configured"] is True
    assert client.get("/api/live/copy-events?style=balanced").json()["events"][0]["id"] == 9
    r = client.post("/api/live/copy-preview", json={"source_style": "balanced", "source_event_id": 9,
                                                       "limit_price": 100})
    assert r.status_code == 200 and r.json()["order_transmission_enabled"] is False
    assert seen["source"] == source and "creds" in seen
    assert client.post("/api/live/orders", json={}).status_code == 404
    assert client.post("/api/live/copy-preview", json={"source_style": "aggressive", "source_event_id": 9,
                                                          "limit_price": 100}).status_code == 409


def test_stale_reference_event_is_blocked_before_broker_read(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "stale.db")
    from signal_desk import api, live_routes
    monkeypatch.setenv("KIS_ACCOUNT_OWNER", "owner@example.com")
    monkeypatch.setattr(api.auth, "current_user", lambda *a: {"id": 7, "email": "owner@example.com"})
    monkeypatch.setattr(config, "kis_credentials", creds)
    monkeypatch.setattr(live_routes.bot, "ensure_reference_bots", lambda: None)
    monkeypatch.setattr(live_routes.db, "bot_trade_get", lambda *a: {"id": 9, "ticker": "005930", "side": "buy",
        "qty": 1, "price": 100, "ts": int(time.time()) - 901})
    monkeypatch.setattr(live_routes.live, "copy_preview", lambda *a, **k: pytest.fail("stale source must not query broker"))
    client = TestClient(api.app)
    client.put("/api/live/copy-policy", json={"source_style": "balanced", "follow_pct": 50, "max_order_pct": 10,
        "max_daily_buy_pct": 20, "max_position_pct": 15, "min_cash_pct": 25})
    out = client.post("/api/live/copy-preview", json={"source_style": "balanced", "source_event_id": 9, "limit_price": 100}).json()
    assert out["ready"] is False and "15분" in out["reason"]
