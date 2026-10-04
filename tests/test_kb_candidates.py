"""P1b: 비-DART Sonnet 후보 추출 → 자동 판정(명확 악재만 Decision)."""

import datetime
import time

from signal_desk import db, kb, kb_attribution

_TODAY = datetime.date.today().isoformat()


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


def test_sync_candidate_auto_confirms_clear_negative(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    monkeypatch.setattr(kb.llm, "available", lambda: True)
    monkeypatch.setattr(kb, "_extract_candidate_event", _fake_extract)
    items = [{
        "title": "삼성전자, 검찰 압수수색", "source": "naver_news",
        "published": _TODAY, "url": "https://n.example/cand1",
        "summary": "횡령 혐의 수사",
        "attribution_version": kb_attribution.POLICY_VERSION,
    }]
    assert kb.sync_candidate_events("005930", items) == 1
    assert db.kb_events_list(status="candidate") == []
    confirmed = db.kb_events_list(status="confirmed")
    assert len(confirmed) == 1
    ev = confirmed[0]
    assert ev["decision_eligible"] is False
    assert ev["decision_action"] == "attention"
    assert ev["policy_version"] == "p1b"
    assert ev["trust_tier"] == "medium"
    assert db.kb_event_evidence(ev["id"])
    assert db.kb_events_active("005930", decision_only=True) == []
    db.kb_digest_set("005930", "삼성전자", 0.1, "요약", [], 1, newest_ts=int(time.time()))
    sm = kb.sentiment_map()["005930"]
    assert sm["event_risk"] is False
    assert sm.get("event_id") is None


def test_sync_candidate_auto_rejects_ambiguous(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    monkeypatch.setattr(kb.llm, "available", lambda: True)

    def soft(ticker, item):
        return {**_fake_extract(ticker, item), "severity": "watch", "confidence": 0.9}

    monkeypatch.setattr(kb, "_extract_candidate_event", soft)
    assert kb.sync_candidate_events("005930", [{
        "title": "관측 이슈", "source": "naver_news", "published": _TODAY,
        "url": "https://n.example/soft", "summary": "소송 언급",
        "attribution_version": kb_attribution.POLICY_VERSION,
    }]) == 1
    assert db.kb_events_list(status="candidate") == []
    assert db.kb_events_list(status="rejected")
    assert db.kb_events_active("005930", decision_only=True) == []


def test_candidate_requires_url_and_skips_dart(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    monkeypatch.setattr(kb.llm, "available", lambda: True)
    calls = []

    def track(ticker, item):
        calls.append(item)
        return _fake_extract(ticker, item)

    monkeypatch.setattr(kb, "_extract_candidate_event", track)
    assert kb.sync_candidate_events("005930", [
        {"title": "x", "source": "naver_news", "url": "", "published": _TODAY},
        {"title": "공시", "source": "dart", "url": "https://dart.example/1", "published": _TODAY},
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
    items = [{"title": "과징금 부과 이슈", "source": "naver_news", "url": "https://n.example/dup",
              "published": _TODAY, "summary": "공정위 제재",
              "attribution_version": kb_attribution.POLICY_VERSION}]
    assert kb.sync_candidate_events("005930", items) == 1
    assert kb.sync_candidate_events("005930", items) == 0
    assert n["c"] == 1


def test_refresh_candidates_only_on_new_urls(tmp_path, monkeypatch):
    monkeypatch.setattr(kb.db, "DB", tmp_path / "app.db")
    monkeypatch.setattr(kb.news, "collect", lambda *a, **k: [
        {"title": "삼성전자, 검찰 압수수색 보도", "source": "naver_news", "published": _TODAY,
         "url": "https://n.example/new1", "summary": "횡령 혐의 수사"},
    ])
    monkeypatch.setattr(kb.ingest_dart, "corp_codes", lambda: {"005930": "00126380"})
    monkeypatch.setattr(kb.ingest_dart, "disclosures", lambda *a, **k: [])
    monkeypatch.setattr(kb, "build_digest", lambda name, items: {
        "sentiment": 0.0, "summary": "s", "points": []})
    monkeypatch.setattr(kb.llm, "available", lambda: True)
    monkeypatch.setattr(kb, "_extract_candidate_event", _fake_extract)
    out = kb.refresh([{"ticker": "005930", "name": "삼성전자"}])
    assert out["updated"] == 1
    assert db.kb_events_list(status="candidate") == []
    assert len(db.kb_events_list(status="confirmed")) == 1
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


def test_extract_rejects_hallucinated_quote_and_attributes_cost(monkeypatch):
    monkeypatch.setattr(kb.llm, "available", lambda: True)
    seen = {}

    def fake_complete(*args, **kwargs):
        seen.update(kwargs)
        return {"event": True, "event_type": "litigation", "direction": "negative",
                "severity": "serious", "confidence": 0.99, "summary": "압수수색",
                "rationale": "법적 위험", "evidence_text": "거래정지 결정"}

    monkeypatch.setattr(kb.llm, "complete_json", fake_complete)
    item = {"title": "검찰, 압수수색", "url": "https://n.example/e2", "summary": "횡령 혐의 수사"}
    assert kb._extract_candidate_event("005930", item) is None
    assert seen["purpose"] == "kb_event"
    assert kb._supported_event_quote("검찰 압수수색", item["title"], item["summary"])


def test_candidate_event_key_deduplicates_tracking_and_source():
    first = kb._candidate_event_key("naver_news", "https://n.example/story?id=3&utm_source=x")
    second = kb._candidate_event_key("other_feed", "https://N.EXAMPLE/story?fbclid=y&id=3")
    assert first == second
    assert kb._candidate_event_key("naver_news", "https://n.example/story?id=4") != first
