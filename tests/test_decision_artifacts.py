"""P2 공용 판단 자료: 내용 주소·중복 제거·손상 검출·저장량 계측."""

import pytest

from signal_desk import db


def test_artifact_is_idempotent_and_keeps_first_observation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    first = db.decision_artifact_put("kr", "engine_input", {"b": 2, "a": [1.0, 2.0]},
                                     observed_at=100)
    second = db.decision_artifact_put("kr", "engine_input", {"a": [1.0, 2.0], "b": 2},
                                      observed_at=200)
    assert first == second
    assert db.decision_artifact_get(first) == {
        "schema_version": 1, "market": "kr", "kind": "engine_input",
        "data": {"a": [1.0, 2.0], "b": 2},
    }
    c = db.conn()
    try:
        assert c.execute("SELECT COUNT(*),first_observed FROM decision_artifacts").fetchone() == (1, 100)
    finally:
        c.close()


def test_artifact_revision_and_market_have_distinct_ids(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = db.decision_artifact_put("kr", "price_base", {"prices": [100.0]})
    b = db.decision_artifact_put("kr", "price_base", {"prices": [101.0]})
    c = db.decision_artifact_put("us", "price_base", {"prices": [100.0]})
    assert len({a, b, c}) == 3
    assert db.decision_artifact_get(a)["data"]["prices"] == [100.0]
    assert db.decision_artifact_storage("kr")[0]["count"] == 2


def test_artifact_rejects_nonfinite_or_unknown_payload(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError):
        db.decision_artifact_put("kr", "engine_input", {"score": float("nan")})
    with pytest.raises(ValueError):
        db.decision_artifact_put("kr", "order", {"uid": 7})
    with pytest.raises(ValueError):
        db.decision_artifact_put("other", "price_base", {})
    assert db.decision_artifact_storage() == []


def test_artifact_detects_corruption_instead_of_reusing_it(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    artifact_id = db.decision_artifact_put("us", "quote_delta", {"AAPL": 201.0})
    c = db.conn()
    try:
        c.execute("UPDATE decision_artifacts SET payload=? WHERE id=?", (b"broken", artifact_id))
        c.commit()
    finally:
        c.close()
    with pytest.raises(RuntimeError, match="corrupt"):
        db.decision_artifact_get(artifact_id)
    with pytest.raises(RuntimeError, match="corrupt"):
        db.decision_artifact_put("us", "quote_delta", {"AAPL": 201.0})


def test_artifact_storage_reports_actual_compressed_bytes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db.decision_artifact_put("kr", "price_base", {"prices": [100.0] * 1000})
    row = db.decision_artifact_storage()[0]
    assert row["kind"] == "price_base" and row["count"] == 1
    assert 0 < row["stored_bytes"] < row["raw_bytes"]


def test_admin_storage_breakdown_includes_decision_artifact_usage(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from signal_desk import api

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_EMAILS", "storage-admin@example.com")
    db.decision_artifact_put("kr", "price_base", {"prices": [100.0] * 1000})
    guest = TestClient(api.app)
    assert guest.get("/api/admin/storage-breakdown").status_code == 401
    guest.post("/api/auth/signup", json={"email": "reader@example.com", "pw": "abcdef12"})
    assert guest.get("/api/admin/storage-breakdown").status_code == 403
    admin = TestClient(api.app)
    admin.post("/api/auth/signup", json={"email": "storage-admin@example.com", "pw": "abcdef12"})
    response = admin.get("/api/admin/storage-breakdown")
    assert response.status_code == 200
    assert response.json()["decision_artifacts"] == db.decision_artifact_storage()
    assert response.json()["decision_artifacts"][0]["stored_bytes"] > 0
