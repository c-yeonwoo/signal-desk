"""R15 evidence edges are sourced, versioned, historically available, and not orders."""

import pytest
from fastapi.testclient import TestClient

from signal_desk import db
from signal_desk.signals import relation_graph as graph


def _input():
    return {"us_customer": "NVDA", "kr_supplier": "000660",
            "revenue_exposure_pct": 12.5,
            "source_url": "https://www.sec.gov/Archives/edgar/data/example",
            "evidence_quote": "Customer NVDA accounted for approximately 12.5 percent of total revenue.",
            "source_published_date": "2026-09-20", "valid_from": "2026-09-20",
            "valid_until": "2027-09-20"}


def test_candidate_rejects_theme_map_and_unsourced_exposure():
    good = graph.candidate(_input())
    assert good["relation"] == "us_customer_of_kr_supplier"
    assert good["live_eligible"] is False and good["source_available_at_verified"] is False
    for change in ({"source_url": "https://example.com/theme"},
                   {"source_url": "http://www.sec.gov/filing"},
                   {"revenue_exposure_pct": None}, {"revenue_exposure_pct": float("nan")},
                   {"evidence_quote": "same sector"}, {"valid_from": "20260920"},
                   {"valid_until": "2026-09-19"}, {"valid_until": ""},
                   {"valid_until": "2028-12-31"}):
        with pytest.raises(ValueError):
            graph.candidate({**_input(), **change})


def test_relation_versions_and_reviews_are_prospective(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    proof = graph.candidate(_input())
    eid = db.relation_edge_add(proof, observed_at=100, submitted_by=7)
    assert db.relation_edge_get(eid)["evidence_hash"] == proof["evidence_hash"]
    with pytest.raises(ValueError):
        db.relation_edge_add(proof, observed_at=101, submitted_by=7)
    assert db.relation_edges_list()[0]["review"] is None
    db.relation_edge_review(eid, verdict="approved", reviewed_at=200,
                            reviewer_uid=7, note="공식 문서 원문과 매출 비율 확인")
    edge = db.relation_edge_get(eid)
    assert not graph.eligible_at(edge, observed_at=199, review=db.relation_edges_list()[0]["review"])
    # Date validity applies in KR time; the review is not backdated by the filing date.
    event = 1_800_000_000
    assert graph.eligible_at(edge, observed_at=event, review=db.relation_edges_list()[0]["review"])
    assert graph.active_edges(db.relation_edges_as_of("NVDA", event), observed_at=event)[0]["id"] == eid
    newer = graph.candidate({**_input(), "revenue_exposure_pct": 15.0})
    nid = db.relation_edge_add(newer, observed_at=event + 1, submitted_by=7, supersedes_id=eid)
    assert db.relation_edge_get(nid)["supersedes_id"] == eid
    with pytest.raises(ValueError):
        db.relation_edge_add(newer, observed_at=event + 2, submitted_by=7, supersedes_id=eid)
    with pytest.raises(ValueError):
        db.relation_edge_add(graph.candidate({**_input(), "kr_supplier": "005930"}),
                             observed_at=event + 2, submitted_by=7, supersedes_id=eid)
    db.relation_edge_review(eid, verdict="rejected", reviewed_at=event + 3,
                            reviewer_uid=8, note="원문이 관계 종료를 공시함")
    old_view = next(r for r in db.relation_edges_list(observed_before=event) if r["id"] == eid)
    assert old_view["review"]["verdict"] == "approved"
    assert db.relation_edges_list()[1]["review"]["verdict"] == "rejected"
    assert graph.active_edges(db.relation_edges_as_of("NVDA", event + 3),
                              observed_at=event + 3) == []
    db.relation_edge_review(nid, verdict="approved", reviewed_at=event + 4,
                            reviewer_uid=8, note="새 공시에서 노출 변경을 확인")
    assert graph.active_edges(db.relation_edges_as_of("NVDA", event + 4),
                              observed_at=event + 4)[0]["id"] == nid


def test_admin_registration_and_review_are_separate_and_guarded(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_EMAILS", "relation-admin@example.com")
    from signal_desk import api

    monkeypatch.setattr(api, "_rl_hits", {})
    guest = TestClient(api.app)
    assert guest.get("/api/admin/research/relations").status_code == 401
    guest.post("/api/auth/signup", json={"email": "ordinary@example.com", "pw": "abcdef12"})
    assert guest.get("/api/admin/research/relations").status_code == 403
    admin = TestClient(api.app)
    admin.post("/api/auth/signup", json={"email": "relation-admin@example.com", "pw": "abcdef12"})
    assert admin.post("/api/admin/research/relations", json=_input()).status_code == 403
    headers = {"X-Signal-Desk-Relation": "review"}
    assert admin.post("/api/admin/research/relations", json=_input(), headers={**headers,
                      "Origin": "https://evil.example"}).status_code == 403
    added = admin.post("/api/admin/research/relations", json=_input(), headers=headers)
    assert added.status_code == 200
    eid = added.json()["id"]
    assert added.json()["status"] == "candidate"
    assert admin.get("/api/admin/research/relations").json()["approved"] == 0
    url = "/api/admin/research/relations/review"
    payload = {"edge_id": eid, "verdict": "approved", "note": "SEC 원문과 고객 매출 비율 대조"}
    assert admin.post(url, json=payload, headers=headers).status_code == 400
    reviewed = admin.post(url, json={**payload, "source_checked": True}, headers=headers)
    assert reviewed.status_code == 200 and reviewed.json()["live_eligible"] is False
    result = admin.get("/api/admin/research/relations").json()
    assert result["approved"] == 1 and result["edges"][0]["review"]["verdict"] == "approved"
    assert result["live_eligible"] is False
