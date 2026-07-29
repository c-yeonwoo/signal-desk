"""KB 다이제스트 비용 가드 — 신규 없을 때 LLM 스킵 · 후보 프리필터."""

from signal_desk import db, kb


def test_refresh_skips_digest_when_urls_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(kb.db, "DB", tmp_path / "app.db")
    item = {
        "title": "주가 상승 마감", "source": "naver_news", "published": "2026-07-18",
        "url": "https://n.example/same1", "summary": "외국인 순매수",
    }
    monkeypatch.setattr(kb.news, "collect", lambda *a, **k: [item])
    monkeypatch.setattr(kb.ingest_dart, "corp_codes", lambda: {})
    monkeypatch.setattr(kb, "_disclosure_items", lambda *a, **k: [])
    n = {"digest": 0}

    def counting_digest(name, items):
        n["digest"] += 1
        return {"sentiment": 0.1, "summary": "요약", "points": ["p"]}

    monkeypatch.setattr(kb, "build_digest", counting_digest)
    monkeypatch.setattr(kb.llm, "available", lambda: True)
    monkeypatch.setattr(kb, "sync_candidate_events", lambda *a, **k: 0)

    first = kb.refresh([{"ticker": "005930", "name": "삼성전자"}])
    assert first["updated"] == 1 and n["digest"] == 1
    second = kb.refresh([{"ticker": "005930", "name": "삼성전자"}])
    assert second["updated"] == 0 and n["digest"] == 1  # LLM 재호출 없음


def test_candidate_prefilter_skips_noise(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    monkeypatch.setattr(kb.llm, "available", lambda: True)
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        return None

    monkeypatch.setattr(kb, "_extract_candidate_event", boom)
    assert kb.sync_candidate_events("005930", [{
        "title": "외국인 순매수에 주가 상승 마감", "source": "naver_news",
        "url": "https://n.example/noise", "summary": "코스피 강세",
        "published": "2026-07-18",
    }]) == 0
    assert calls["n"] == 0


def test_candidate_prefilter_allows_material(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    monkeypatch.setattr(kb.llm, "available", lambda: True)
    monkeypatch.setattr(kb, "_extract_candidate_event", lambda *a, **k: {
        "event_type": "litigation", "direction": "negative", "severity": "serious",
        "confidence": 0.8, "summary": "압수수색", "rationale": "수사",
        "evidence_text": "검찰 압수수색",
    })
    assert kb.sync_candidate_events("005930", [{
        "title": "검찰, 압수수색", "source": "naver_news",
        "url": "https://n.example/mat", "summary": "횡령 혐의",
        "published": "2026-07-18",
    }]) == 1


def test_build_digest_uses_haiku(monkeypatch):
    seen = {}

    def fake_complete(system, user, max_tokens=500, model=None):
        seen["model"] = model
        return {"sentiment": 0.2, "summary": "함의", "points": ["a"]}

    monkeypatch.setattr(kb.llm, "available", lambda: True)
    monkeypatch.setattr(kb.llm, "complete_json", fake_complete)
    out = kb.build_digest("삼성전자", [{
        "source": "naver_news", "title": "수주 공시", "summary": "공급계약 체결",
    }])
    assert out["sentiment"] == 0.2
    assert seen["model"] == kb.llm.DIGEST_MODEL
