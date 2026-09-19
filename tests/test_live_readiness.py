import copy
import time
import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from signal_desk import config
from signal_desk.broker import kis, live


def creds(**values):
    return {"env": "real", "app_key": "test-key", "app_secret": "test-secret",
            "account_no": "12345678", "product_cd": "01", **values}


@pytest.mark.parametrize("flag", ["", "true"])
def test_live_order_locked_even_when_legacy_flag_is_enabled(monkeypatch, flag):
    monkeypatch.setenv("ALLOW_REAL_ORDERS", flag)
    monkeypatch.setattr(kis, "_request", lambda *a, **k: pytest.fail("must not reach transport"))
    with pytest.raises(PermissionError):
        kis.place_order("005930", "buy", 1, price=70000, creds=creds())


def test_transport_rejects_real_mutation_before_authentication(monkeypatch):
    monkeypatch.setattr(kis, "get_token", lambda *a: pytest.fail("must not authenticate"))
    with pytest.raises(PermissionError):
        kis._request("/uapi/domestic-stock/v1/trading/order-cash", "TTTC0012U", creds(), {}, method="POST")


def test_unknown_environment_is_not_treated_as_real():
    with pytest.raises(ValueError):
        kis.get_token(creds(env="prod"))


def test_token_cache_is_private_and_account_environment_scoped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    kis._save_token("mock-token", time.time() + 3600, creds())
    assert kis._load_cached_token(creds()) == "mock-token"
    assert kis._load_cached_token(creds(env="demo")) is None
    assert kis._load_cached_token(creds(account_no="87654321")) is None
    assert kis._load_cached_token(creds(app_secret="rotated")) is None
    assert kis._token_path(creds()).stat().st_mode & 0o777 == 0o600


def test_paginated_reads_require_all_pages(monkeypatch):
    bodies = iter([
        {"rt_cd": "0", "output1": [{"id": 1}], "_tr_cont": "M", "ctx_area_fk100": "f", "ctx_area_nk100": "n"},
        {"rt_cd": "0", "output1": [{"id": 2}], "_tr_cont": "D"},
    ])
    calls = []
    def read(*a, **k):
        calls.append(k)
        return next(bodies)
    monkeypatch.setattr(kis, "_request", read)
    assert kis._read_all("path", "tr", creds(), {})["output1"] == [{"id": 1}, {"id": 2}]
    assert calls[1]["tr_cont"] == "N"


def test_paginated_read_failure_does_not_return_partial_holdings(monkeypatch):
    bodies = iter([
        {"rt_cd": "0", "output1": [{"id": 1}], "_tr_cont": "M", "ctx_area_fk100": "f", "ctx_area_nk100": "n"},
        None])
    monkeypatch.setattr(kis, "_request", lambda *a, **k: next(bodies))
    assert kis._read_all("path", "tr", creds(), {}) is None


def test_buying_power_never_falls_back_to_margin_capacity(monkeypatch):
    monkeypatch.setattr(kis, "_request", lambda *a, **k: {"rt_cd": "0", "output": {
        "max_buy_amt": "10000000", "max_buy_qty": "100"}})
    assert kis.buying_power("005930", 70000, creds()) is None


def test_daily_order_partial_fill_is_not_marked_completed(monkeypatch):
    body = {"rt_cd": "0", "output1": [{"odno": "test-order", "pdno": "005930", "ord_qty": "3",
                                         "tot_ccld_qty": "1", "rmn_qty": "2", "cncl_yn": "N"}]}
    monkeypatch.setattr(kis, "_read_all", lambda *a: body)
    today = datetime.datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
    out = kis.daily_orders(today, creds())
    assert out["orders"][0]["status"] == "partial"
    body["output1"][0].pop("rmn_qty")
    assert kis.daily_orders(today, creds()) is None


@pytest.fixture
def account(monkeypatch):
    balance = {"complete": True, "cash": 1000, "total_eval": 1100,
               "holdings": [{"ticker": "005930", "qty": 1, "price": 100}]}
    orders = {"complete": True, "orders": []}
    monkeypatch.setattr(kis, "balance", lambda *a, **k: copy.deepcopy(balance))
    monkeypatch.setattr(kis, "daily_orders", lambda *a, **k: copy.deepcopy(orders))
    monkeypatch.setattr(kis, "buying_power", lambda *a, **k: {"cash_without_margin": 1000, "qty_without_margin": 10})
    monkeypatch.setattr(kis, "place_order", lambda *a, **k: pytest.fail("readiness never submits orders"))
    return orders


def test_preflight_pass_is_never_a_live_order_authorization(account):
    out = live.preflight({"ticker": "005930", "side": "buy", "qty": 1, "limit_price": 100}, creds())
    assert out["broker_checks_passed"] is True
    assert out["ready"] is False and out["order_transmission_enabled"] is False
    assert out["blockers"]


@pytest.mark.parametrize("status", ["open", "partial", "unknown"])
def test_unreconciled_orders_block_preflight(account, status):
    account["orders"] = [{"status": status}]
    out = live.preflight({"ticker": "005930", "side": "buy", "qty": 1, "limit_price": 100}, creds())
    assert out["broker_checks_passed"] is False


def test_fees_cannot_overdraw_broker_buying_power(account):
    out = live.preflight({"ticker": "005930", "side": "buy", "qty": 10, "limit_price": 100}, creds())
    assert out["broker_checks_passed"] is False


def test_held_quantity_is_not_assumed_to_be_sellable(account):
    out = live.preflight({"ticker": "005930", "side": "sell", "qty": 1, "limit_price": 100}, creds())
    assert out["broker_checks_passed"] is False
    assert "매도가능수량" in out["reason"]


@pytest.mark.parametrize("qty,price", [(True, 100), (1.5, 100), (0, 100), (1, 0), (1, float("inf"))])
def test_preflight_invalid_order_never_queries_broker(monkeypatch, qty, price):
    monkeypatch.setattr(live, "snapshot", lambda *a: pytest.fail("invalid input must not query account"))
    with pytest.raises(ValueError):
        live.preflight({"ticker": "005930", "side": "buy", "qty": qty, "limit_price": price}, creds())


@pytest.mark.parametrize("owner", ["", "another@example.com"])
def test_account_ownership_checked_before_broker_access(tmp_path, monkeypatch, owner):
    monkeypatch.chdir(tmp_path)
    from signal_desk import api
    monkeypatch.setenv("KIS_ACCOUNT_OWNER", owner)
    monkeypatch.setattr(api.auth, "current_user", lambda *a: {"id": 7, "email": "user@example.com"})
    monkeypatch.setattr(config, "kis_credentials", lambda: pytest.fail("unauthorized broker access"))
    client = TestClient(api.app)
    for endpoint in ("account", "preflight"):
        assert client.post("/api/live/" + endpoint, json={}).status_code == 403


def test_owner_can_inspect_but_cannot_submit_orders(tmp_path, monkeypatch, account):
    monkeypatch.chdir(tmp_path)
    from signal_desk import api
    monkeypatch.setenv("KIS_ACCOUNT_OWNER", "owner@example.com")
    monkeypatch.setattr(api.auth, "current_user", lambda *a: {"id": 7, "email": "owner@example.com"})
    monkeypatch.setattr(config, "kis_credentials", creds)
    client = TestClient(api.app)
    assert client.get("/api/live/status").json()["order_transmission_enabled"] is False
    assert client.post("/api/live/account").json()["ready"] is True
    assert client.post("/api/live/orders", json={}).status_code == 404
