"""실계좌 보유주식 관측은 개인정보 격리와 계좌 수익률 오인을 함께 방지한다."""

import importlib

import pytest
from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TOSS_ACCOUNT_OWNER", "owner@x.com")
    from signal_desk import db
    importlib.reload(db)
    from signal_desk import api
    importlib.reload(api)
    return TestClient(api.app), db


def _holding(market, value, pnl):
    return {"marketCountry": market, "currency": "KRW" if market == "KR" else "USD",
            "symbol": "005930" if market == "KR" else "AAPL", "quantity": "1",
            "averagePurchasePrice": "100", "marketValue": {"amount": value},
            "dailyProfitLoss": {"amount": pnl}}


def test_observations_keep_markets_and_latest_daily_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from signal_desk import db, account_performance as perf
    importlib.reload(db)
    first = {"items": [_holding("KR", "1000", "100"), _holding("US", "20", "-2")]}
    perf.capture_toss(7, first, observed_at=1_800_000_000)
    assert perf.history(7, "kr")["points"][0]["daily_return_pct"] == pytest.approx(100 / 900 * 100)
    assert perf.history(7, "us")["points"][0]["currency"] == "USD"
    perf.capture_toss(7, first, observed_at=1_800_000_000)  # 동일 재조회는 중복 금지
    c = db.conn()
    assert c.execute("SELECT COUNT(*) FROM account_observations WHERE uid=7").fetchone()[0] == 2
    c.close()
    perf.capture_toss(7, {"items": [_holding("KR", "1100", "50")]}, observed_at=1_800_000_100)
    assert perf.history(7, "kr")["points"][-1]["holdings_value"] == "1100"
    assert perf.history(7, "us")["points"][-1]["quality"] == "holdings_empty"
    assert perf.history(7, "us")["points"][-1]["holdings_value"] == "0"
    assert perf.history(7, "kr")["account_return_available"] is False


def test_invalid_broker_response_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from signal_desk import db, account_performance as perf
    importlib.reload(db)
    with pytest.raises(ValueError):
        perf.capture_toss(8, {"items": [_holding("KR", "100", "10"),
                                       _holding("US", "nan", "1")]})
    with pytest.raises(ValueError):
        perf.capture_toss(8, {"items": [{**_holding("KR", "100", "10"),
                                        "marketValue": "invalid"}]})
    assert perf.history(8, "kr")["points"] == []
    assert perf.history(8, "us")["points"] == []


def test_owner_only_api_and_get_captures_without_import(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    from signal_desk.ingest import toss
    monkeypatch.setattr(toss, "holdings", lambda account="1": {"items": [_holding("KR", "100", "10")]})
    client.post("/api/auth/signup", json={"email": "other@x.com", "pw": "abcdef"})
    assert client.get("/api/my-performance").status_code == 403
    client.post("/api/auth/signup", json={"email": "owner@x.com", "pw": "abcdef"})
    assert client.get("/api/my-holdings").json()["performance_recorded"] is True
    data = client.get("/api/my-performance?market=kr").json()
    assert data["scope"] == "holdings_only" and len(data["points"]) == 1
    assert data["points"][0]["holdings_value"] == "100"
    assert client.get("/api/holdings").json()["holdings"] == []
