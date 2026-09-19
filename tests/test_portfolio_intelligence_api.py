"""포트폴리오 분석은 주문 경로와 분리된, 사용자별 스냅샷 API여야 한다."""

import importlib
from datetime import datetime, timezone

import exchange_calendars as xcals

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
    dates = [d.date().isoformat() for d in xcals.get_calendar('XKRX').sessions_in_range('2026-01-01', '2026-09-18')][-61:]
    monkeypatch.setattr(api.portfolio_audit, 'utc_now', lambda: datetime(2026, 9, 19, tzinfo=timezone.utc))
    monkeypatch.setattr(api.store, 'load_universe', lambda: [{"ticker": "005930", "name": "삼성전자", "sector": "전자"}])
    monkeypatch.setattr(api.store, 'load_price_series', lambda: {"005930": [100 + i for i in range(61)]})
    monkeypatch.setattr(api.store, 'load_dates_by_ticker', lambda: {"005930": dates})
    monkeypatch.setattr(api.store, 'load_portfolio_close_bundle', lambda market: ({"005930": [100 + i for i in range(61)]}, {"005930": dates}))
    result = client.post("/api/portfolio/analyze", json={"market": "kr"})
    assert result.status_code == 200
    out = result.json()
    assert out["snapshot_id"] > 0 and out["currency"] == "KRW"
    assert out["summary"]["total_value"] == 2100.0  # 평가 1,600 + 현금 500
    assert out["holdings"][0]["weight_pct"] == 76.2
    assert out["recommendation_id"]
    history = client.get("/api/portfolio/recommendations?market=kr").json()
    assert history["ready"] is True and history["coverage"]["items"] == 1
    artifact_id = out['audit']['id']
    assert out['audit']['timing']['aligned']
    assert client.post(f'/api/portfolio/decisions/{artifact_id}/replay?market=kr').json()['matched']
    assert client.get(f'/api/portfolio/decisions/{artifact_id}?market=us').status_code == 404
    assert client.post(f'/api/portfolio/decisions/{artifact_id}/compare?market=kr').json()['ready'] is False
    assert client.get(f'/api/portfolio/decisions/{artifact_id}/comparison?market=kr').json()['result']
    assert len(client.get('/api/portfolio/decisions?market=kr').json()['items']) == 1
    client.post('/api/auth/signup', json={'email': 'other@x.com', 'pw': 'abcdef'})
    assert client.get(f'/api/portfolio/decisions/{artifact_id}?market=kr').status_code == 404
    assert client.post(f'/api/portfolio/decisions/{artifact_id}/compare?market=kr').status_code == 404
    assert client.post(f'/api/portfolio/decisions/{artifact_id}/replay?market=kr').status_code == 404


def test_profile_rejects_invalid_percentages(tmp_path, monkeypatch):
    client, _ = _fresh_client(tmp_path, monkeypatch)
    client.post("/api/auth/signup", json={"email": "invalid@x.com", "pw": "abcdef"})
    response = client.post("/api/portfolio/profile", json={"market": "us", "max_sector_pct": 101})
    assert response.status_code == 400
