"""P1b: 비-DART Sonnet candidate 이벤트 — Decision·sentiment_map 격리."""

import time

from signal_desk import db, kb


def _fake_extract(ticker, item):
    return {
        "event_type": "litigation",
        "direction": "negative",
        "severity": "serious",
        "confidence": 0.72,
        "summary": "검찰 수사 관련 보도",
        "rationale": "법적 리스크",
        "evidence_text": item.get("title") or "수사",
    }


def test_sync_candidate_creates_non_eligible(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    monkeypatch.setattr(kb.llm, "available", lambda: True)
    monkeypatch.setattr(kb, "_extract_candidate_event", _fake_extract)
    items = [{
        "title": "검찰, ○○ 압수수색", "source": "naver_news",
        "published": "2026-07-18", "url": "https://n.example/cand1",
        "summary": "횡령 혐의 수사",
    }]
    assert kb.sync_candidate_events("005930", items) == 1
    cands = db.kb_events_list(status="candidate")
    assert len(cands) == 1
    ev = cands[0]
    assert ev["decision_eligible"] is False
    assert ev["decision_action"] == "none"
    assert ev["status"] == "candidate"
    assert ev["policy_version"] == "p1b"
    assert ev["trust_tier"] == "medium"
    assert db.kb_event_evidence(ev["id"])
    # Decision 경로 격리
    assert db.kb_events_active("005930", decision_only=True) == []
    db.kb_digest_set("005930", "삼성전자", 0.1, "요약", [], 1, newest_ts=int(time.time()))
    sm = kb.sentiment_map()["005930"]
    assert sm["event_risk"] is False
    assert sm.get("event_id") is None


def test_candidate_requires_url_and_skips_dart(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    monkeypatch.setattr(kb.llm, "available", lambda: True)
    calls = []

    def track(ticker, item):
        calls.append(item)
        return _fake_extract(ticker, item)

    monkeypatch.setattr(kb, "_extract_candidate_event", track)
    assert kb.sync_candidate_events("005930", [
        {"title": "x", "source": "naver_news", "url": "", "published": "2026-07-01"},
        {"title": "공시", "source": "dart", "url": "https://dart.example/1", "published": "2026-07-01"},
    ]) == 0
    assert calls == []


def test_candidate_dedup_no_second_extract(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    monkeypatch.setattr(kb.llm, "available", lambda: True)
    n = {"c": 0}

    def once(ticker, item):
        n["c"] += 1
        return _fake_extract(ticker, item)

    monkeypatch.setattr(kb, "_extract_candidate_event", once)
    items = [{"title": "이슈", "source": "naver_news", "url": "https://n.example/dup",
              "published": "2026-07-18", "summary": "s"}]
    assert kb.sync_candidate_events("005930", items) == 1
    assert kb.sync_candidate_events("005930", items) == 0
    assert n["c"] == 1


def test_refresh_candidates_only_on_new_urls(tmp_path, monkeypatch):
    monkeypatch.setattr(kb.db, "DB", tmp_path / "app.db")
    monkeypatch.setattr(kb.news, "collect", lambda *a, **k: [
        {"title": "신규 악재 보도", "source": "naver_news", "published": "2026-07-18",
         "url": "https://n.example/new1", "summary": "내용"},
    ])
    monkeypatch.setattr(kb.ingest_dart, "corp_codes", lambda: {"005930": "00126380"})
    monkeypatch.setattr(kb.ingest_dart, "disclosures", lambda *a, **k: [])
    monkeypatch.setattr(kb, "build_digest", lambda name, items: {
        "sentiment": 0.0, "summary": "s", "points": []})
    monkeypatch.setattr(kb.llm, "available", lambda: True)
    monkeypatch.setattr(kb, "_extract_candidate_event", _fake_extract)
    out = kb.refresh([{"ticker": "005930", "name": "삼성전자"}])
    assert out["updated"] == 1
    assert len(db.kb_events_list(status="candidate")) == 1
    # 재 refresh — URL 이미 있음 → Sonnet 경로 0
    n = {"c": 0}

    def boom(*a, **k):
        n["c"] += 1
        return _fake_extract(*a, **k)

    monkeypatch.setattr(kb, "_extract_candidate_event", boom)
    kb.refresh([{"ticker": "005930", "name": "삼성전자"}])
    assert n["c"] == 0


def test_extract_rejects_missing_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    monkeypatch.setattr(kb.llm, "available", lambda: True)
    monkeypatch.setattr(kb.llm, "complete_json", lambda *a, **k: {
        "event": True, "event_type": "earnings", "direction": "positive",
        "severity": "info", "confidence": 0.9, "summary": "실적",
        "rationale": "x", "evidence_text": "",
    })
    assert kb._extract_candidate_event("005930", {
        "title": "실적 호조", "url": "https://n.example/e", "summary": "영업이익",
    }) is None
