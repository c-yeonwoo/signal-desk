"""후보 이벤트 사람 검토 — confirm/attention/reject. 자동 승격 없음."""

from signal_desk import db, kb


def _seed_candidate(tmp_path, monkeypatch, *, severity="serious", direction="negative"):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    eid = db.kb_event_upsert(
        {
            "event_key": "news:test:1",
            "scope_type": "stock",
            "ticker": "005930",
            "event_type": "litigation",
            "direction": direction,
            "severity": severity,
            "confidence": 0.8,
            "trust_tier": "medium",
            "status": "candidate",
            "decision_eligible": False,
            "decision_action": "none",
            "summary": "검찰 수사 보도",
            "rationale": "후보",
            "policy_version": "p1b",
        },
        evidence={"url": "https://n.example/r1", "evidence_text": "수사", "source_key": "naver_news"},
    )
    return eid


def test_confirm_serious_negative_is_decision_eligible(tmp_path, monkeypatch):
    eid = _seed_candidate(tmp_path, monkeypatch)
    out = kb.review_candidate_event(eid, "confirm")
    assert out["ok"] and out["decision_eligible"] is True
    assert out["decision_action"] == "buy_block"
    ev = db.kb_event_get(eid)
    assert ev["status"] == "confirmed"
    assert ev["decision_eligible"] is True
    assert db.kb_events_active("005930", decision_only=True)


def test_confirm_positive_stays_attention_only(tmp_path, monkeypatch):
    eid = _seed_candidate(tmp_path, monkeypatch, severity="serious", direction="positive")
    out = kb.review_candidate_event(eid, "confirm")
    assert out["ok"] and out["decision_eligible"] is False
    assert out["decision_action"] == "attention"


def test_reject_removes_from_active(tmp_path, monkeypatch):
    eid = _seed_candidate(tmp_path, monkeypatch)
    assert kb.review_candidate_event(eid, "reject")["ok"]
    ev = db.kb_event_get(eid)
    assert ev["status"] == "rejected"
    assert db.kb_events_active("005930") == []


def test_attention_confirmed_not_eligible(tmp_path, monkeypatch):
    eid = _seed_candidate(tmp_path, monkeypatch, severity="critical")
    out = kb.review_candidate_event(eid, "attention")
    assert out["ok"]
    ev = db.kb_event_get(eid)
    assert ev["status"] == "confirmed"
    assert ev["decision_eligible"] is False
    assert db.kb_events_active("005930", decision_only=True) == []


def test_non_candidate_rejected(tmp_path, monkeypatch):
    eid = _seed_candidate(tmp_path, monkeypatch)
    kb.review_candidate_event(eid, "confirm")
    out = kb.review_candidate_event(eid, "confirm")
    assert out["ok"] is False
