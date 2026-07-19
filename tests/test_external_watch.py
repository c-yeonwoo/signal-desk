"""외부 후보 조사 큐 — 점수 가산 없음 · KB 우선 타깃."""

import importlib

from fastapi.testclient import TestClient


def _reload_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from signal_desk import db as db_module
    importlib.reload(db_module)
    return db_module


def test_add_resolve_remove_and_kb_priority(tmp_path, monkeypatch):
    _reload_db(tmp_path, monkeypatch)
    from signal_desk import external_watch, store

    monkeypatch.setattr(store, "load_universe", lambda: [
        {"ticker": "005930", "name": "삼성전자"},
        {"ticker": "000660", "name": "SK하이닉스"},
    ])
    monkeypatch.setattr(store, "load_us_universe", lambda: [
        {"ticker": "AAPL", "name": "Apple Inc."},
    ])

    out = external_watch.add_items(
        "005930\n삼성전자\nAAPL\n없는종목XYZ",
        source="serenity", note="테스트 관심",
    )
    assert out["ok"]
    assert "005930" in out["added"]
    assert "AAPL" in out["added"]
    assert "없는종목XYZ" in out["unresolved"]
    # 삼성전자는 이미 005930로 들어갔으면 updated
    assert external_watch.status()["total"] >= 2

    pri = external_watch.kb_priority_targets(limit=10)
    assert pri[0]["ticker"] in ("005930", "AAPL", "000660")
    assert "005930" in {x["ticker"] for x in pri}

    external_watch.mark_kb_collected(["005930"])
    items = {x["ticker"]: x for x in external_watch.list_items()}
    assert items["005930"].get("kb_collected_at")

    rem = external_watch.remove("AAPL")
    assert rem["ok"]
    assert "AAPL" not in external_watch.ticker_set()

    external_watch.clear()
    assert external_watch.status()["total"] == 0


def test_api_admin_gate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from signal_desk import db as db_module
    importlib.reload(db_module)
    from signal_desk import api as api_module
    importlib.reload(api_module)
    client = TestClient(api_module.app)

    assert client.get("/api/external-watch").status_code == 401

    client.post("/api/auth/signup", json={"email": "u@e.com", "pw": "abcdef12"})
    r = client.get("/api/external-watch")
    assert r.status_code == 403

    html = client.get("/").text
    assert 'id="admin-watch"' in html
    assert "조사 후보" in html
    assert "ext-badge" in html
