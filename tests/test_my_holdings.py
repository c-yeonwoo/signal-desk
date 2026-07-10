"""토스 실계좌 보유내역 — owner 격리(다른 계정 절대 조회 불가) + 파싱 + 챗봇 도구 게이트."""

import importlib

from fastapi.testclient import TestClient


def _fresh_client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from signal_desk import db as db_module
    importlib.reload(db_module)
    from signal_desk import api as api_module
    importlib.reload(api_module)
    return TestClient(api_module.app), api_module


def test_owner_default_none_denies_everyone(monkeypatch):
    from signal_desk import config
    monkeypatch.delenv("TOSS_ACCOUNT_OWNER", raising=False)
    assert config.toss_account_owner() is None   # 미설정 → 아무도 조회 불가(안전 기본)


def test_non_owner_forbidden(tmp_path, monkeypatch):
    monkeypatch.setenv("TOSS_ACCOUNT_OWNER", "owner@x.com")
    client, _ = _fresh_client(tmp_path, monkeypatch)
    client.post("/api/auth/signup", json={"email": "someoneelse@x.com", "pw": "abcdef"})
    r = client.get("/api/my-holdings")   # 다른 계정 → 격리
    assert r.status_code == 403 and r.json().get("forbidden") is True


def test_owner_passes_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("TOSS_ACCOUNT_OWNER", "owner@x.com")
    client, api = _fresh_client(tmp_path, monkeypatch)
    client.post("/api/auth/signup", json={"email": "owner@x.com", "pw": "abcdef"})
    from signal_desk.ingest import toss
    monkeypatch.setattr(toss, "holdings", lambda account="1": {"items": [{"symbol": "005930", "name": "삼성전자",
        "quantity": "100", "lastPrice": "72000", "averagePurchasePrice": "65000", "profitLoss": {"rate": "0.1"}}],
        "marketValue": {"amount": {"krw": "7200000"}}, "totalPurchaseAmount": {"krw": "6500000"},
        "profitLoss": {"rate": "0.1077"}})
    r = client.get("/api/my-holdings")
    assert r.status_code == 200 and r.json()["ready"] is True
    assert r.json()["items"][0]["symbol"] == "005930"


def test_toss_holdings_parses(monkeypatch):
    from signal_desk.ingest import toss

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"result":{"items":[{"symbol":"AAPL","quantity":"10"}]}}'
    monkeypatch.setattr(toss, "_access_token", lambda: "tok")
    monkeypatch.setattr(toss.urllib.request, "urlopen", lambda *a, **k: _Resp())
    res = toss.holdings("1")
    assert res and res["items"][0]["symbol"] == "AAPL"
    monkeypatch.setattr(toss, "_access_token", lambda: None)
    assert toss.holdings("1") is None


def test_import_forbidden_for_non_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("TOSS_ACCOUNT_OWNER", "owner@x.com")
    client, _ = _fresh_client(tmp_path, monkeypatch)
    client.post("/api/auth/signup", json={"email": "guest@x.com", "pw": "abcdef"})
    r = client.post("/api/my-holdings/import")
    assert r.status_code == 403 and r.json().get("forbidden") is True


def test_import_populates_holdings_store(tmp_path, monkeypatch):
    monkeypatch.setenv("TOSS_ACCOUNT_OWNER", "owner@x.com")
    client, api = _fresh_client(tmp_path, monkeypatch)
    client.post("/api/auth/signup", json={"email": "owner@x.com", "pw": "abcdef"})
    from signal_desk.ingest import toss
    monkeypatch.setattr(toss, "holdings", lambda account="1": {"items": [
        {"symbol": "005930", "quantity": "100", "averagePurchasePrice": "65000"},
        {"symbol": "AAPL", "quantity": "10", "averagePurchasePrice": "155.3"}]})
    r = client.post("/api/my-holdings/import")
    assert r.status_code == 200 and r.json()["imported"] == 2
    hs = client.get("/api/holdings").json()["holdings"]
    tks = {h["ticker"] for h in hs}
    assert tks == {"005930", "AAPL"}                    # 실계좌 → 수동 스토어(히트맵·리밸런싱이 읽음)


def test_chat_tool_owner_gate(tmp_path, monkeypatch):
    _, api = _fresh_client(tmp_path, monkeypatch)
    import json
    d_guest = api._make_chat_dispatch(1, is_toss_owner=False)
    assert "본인만" in json.loads(d_guest("get_real_holdings", {}))["error"]
    monkeypatch.setattr(api, "_toss_holdings_summary", lambda: {"총평가_원": "7200000", "보유": []})
    d_owner = api._make_chat_dispatch(1, is_toss_owner=True)
    assert json.loads(d_owner("get_real_holdings", {}))["총평가_원"] == "7200000"
