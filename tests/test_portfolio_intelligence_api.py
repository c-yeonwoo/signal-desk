"""포트폴리오 분석은 주문 경로와 분리된, 사용자별 스냅샷 API여야 한다."""

import importlib

from fastapi.testclient import TestClient


def _fresh_client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from signal_desk import db as db_module
    importlib.reload(db_module)
    from signal_desk import api as api_module
    importlib.reload(api_module)
    return TestClient(api_module.app), api_module


def test_profile_and_analysis_are_user_market_scoped(tmp_path, monkeypatch):
    client, api = _fresh_client(tmp_path, monkeypatch)
    client.post("/api/auth/signup", json={"email": "portfolio@x.com", "pw": "abcdef"})
    saved = client.post("/api/portfolio/profile", json={
        "market": "kr", "cash": 500, "max_single_position_pct": 40,
        "max_sector_pct": 60, "max_cluster_pct": 70, "min_cash_pct": 10,
    })
    assert saved.status_code == 200 and saved.json()["profile"]["cash"] == 500
    client.post("/api/holdings", json={"ticker": "005930", "qty": 10, "avg_price": 90})
    dates = [f"2026-01-{i:02d}" for i in range(1, 62)]
    api.store.load_universe = lambda: [{"ticker": "005930", "name": "삼성전자", "sector": "전자"}]
    api.store.load_price_series = lambda: {"005930": [100 + i for i in range(61)]}
    api.store.load_dates_by_ticker = lambda: {"005930": dates}
    result = client.post("/api/portfolio/analyze", json={"market": "kr"})
    assert result.status_code == 200
    out = result.json()
    assert out["snapshot_id"] > 0 and out["currency"] == "KRW"
    assert out["summary"]["total_value"] == 2100.0  # 평가 1,600 + 현금 500
    assert out["holdings"][0]["weight_pct"] == 76.2


def test_profile_rejects_invalid_percentages(tmp_path, monkeypatch):
    client, _ = _fresh_client(tmp_path, monkeypatch)
    client.post("/api/auth/signup", json={"email": "invalid@x.com", "pw": "abcdef"})
    response = client.post("/api/portfolio/profile", json={"market": "us", "max_sector_pct": 101})
    assert response.status_code == 400
