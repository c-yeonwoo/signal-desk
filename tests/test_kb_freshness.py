"""종목 KB는 원문 발행시각과 마지막 수집 실패를 구분해 현재 근거를 닫는다."""

import datetime
import sqlite3
import time

from signal_desk import api, db, kb
from signal_desk.ingest import news


def test_digest_freshness_uses_source_time_not_rebuild_time():
    now = 1_800_000_000
    dg = {"summary": "과거 호재", "newest_ts": now - 4 * 86400, "updated": now,
          "policy_version": db.KB_DIGEST_POLICY_VERSION}
    assert kb.digest_freshness(dg, now=now)["status"] == "stale"
    assert kb.digest_freshness({**dg, "newest_ts": None}, now=now)["status"] == "undated"
    assert kb.digest_freshness({**dg, "newest_ts": now + 3600}, now=now)["status"] == "future"
    assert kb.digest_freshness({**dg, "newest_ts": now - 3600}, now=now)["current"]
    assert kb.digest_freshness({**dg, "newest_ts": now - 3600}, now=now,
                               check={"status": "failed"})["status"] == "source_failed"
    assert kb.digest_freshness({**dg, "newest_ts": now - 3600, "policy_version": None},
                               now=now)["status"] == "unverified_legacy"


def test_old_digest_is_not_current_in_status_advisor_or_signal_context(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    old = int(time.time()) - 4 * 86400
    db.kb_digest_set("005930", "삼성전자", 0.8, "과거 호재", ["오래된 사실"], 1, newest_ts=old)
    assert kb.advisor_digest("005930") is None
    status = kb.refresh_status([{"ticker": "005930", "name": "삼성전자"}])
    assert status["fresh"] == 0 and status["stale"] == 1
    assert kb.sentiment_map()["005930"]["reasons"] == []
    assert kb.sentiment_map()["005930"]["stale"] is True


def test_news_request_failure_quarantines_fresh_digest_but_not_official_event(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    now = int(time.time())
    db.kb_digest_set("005930", "삼성전자", 0.8, "방금 나온 기사", [], 1, newest_ts=now)
    monkeypatch.setattr(kb.ingest_dart, "corp_codes", lambda: {"005930": "corp"})
    monkeypatch.setattr(kb, "_disclosure_items", lambda corp: [{
        "title": "[공시] 상장폐지", "source": "dart", "published": time.strftime("%Y-%m-%d"),
        "url": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20261004900001",
        "rcept_no": "20261004900001",
    }])
    monkeypatch.setattr(news.config, "naver_search", lambda: ("", ""))
    out = kb.refresh([{"ticker": "005930", "name": "삼성전자"}])
    assert out["failed"] and out["failed"][0]["ticker"] == "005930"
    assert kb.digest_checks()["005930"]["status"] == "failed"
    assert kb.advisor_digest("005930") is None
    assert kb.refresh_status([{"ticker": "005930", "name": "삼성전자"}])["fresh"] == 0
    sm = kb.sentiment_map()["005930"]
    assert sm["freshness_status"] == "source_failed"
    assert sm["score"] == 0.0 and sm["decision"].buy_blocked


def test_successful_check_with_no_new_article_clears_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    db.kb_digest_set("005930", "삼성전자", 0.1, "최근 기사", [], 1, newest_ts=int(time.time()))
    db.kv_set("kb_digest_checks", {"005930": {"status": "failed", "checked_at": int(time.time())}})
    monkeypatch.setattr(kb.ingest_dart, "corp_codes", lambda: {})
    monkeypatch.setattr(kb, "_disclosure_items", lambda corp: [])
    monkeypatch.setattr(news, "naver_news", lambda query, n: [])
    out = kb.refresh([{"ticker": "005930", "name": "삼성전자"}])
    assert out["failed"] == [] and out["updated"] == 0
    assert kb.digest_checks()["005930"]["status"] == "ok"
    assert kb.advisor_digest("005930") is not None


def test_recent_article_cannot_launder_old_article_into_current_digest(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    today = datetime.date.today()
    raw = [
        {"title": "지난주 호재", "source": "naver_news", "published": (today - datetime.timedelta(days=5)).isoformat(),
         "url": "https://n.example/old", "summary": "지난 사실"},
        {"title": "오늘 실적", "source": "naver_news", "published": today.isoformat(),
         "url": "https://n.example/new", "summary": "새 사실"},
    ]
    monkeypatch.setattr(kb.news, "collect", lambda *a, **k: raw)
    monkeypatch.setattr(kb.ingest_dart, "corp_codes", lambda: {})
    monkeypatch.setattr(kb, "_disclosure_items", lambda corp: [])
    monkeypatch.setattr(kb, "sync_candidate_events", lambda *a, **k: 0)
    seen = []
    monkeypatch.setattr(kb, "build_digest", lambda name, items: (
        seen.extend(it["title"] for it in items) or
        {"sentiment": 0.2, "summary": "오늘 실적", "points": []}
    ))
    out = kb.refresh([{"ticker": "005930", "name": "삼성전자"}])
    assert out["updated"] == 1 and seen == ["오늘 실적"]
    assert {d["title"] for d in db.kb_entries_recent("005930")} == {"지난주 호재", "오늘 실적"}


def test_stale_news_cannot_become_new_auto_confirmed_event(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    monkeypatch.setattr(kb, "_extract_candidate_event", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("오래된 뉴스에 LLM 후보 추출 금지")))
    old = (datetime.date.today() - datetime.timedelta(days=5)).isoformat()
    assert kb.sync_candidate_events("005930", [{
        "title": "검찰 압수수색", "source": "naver_news", "published": old,
        "url": "https://n.example/old-event", "summary": "횡령 혐의",
    }]) == 0
    assert db.kb_events_list() == []


def test_legacy_digest_is_quarantined_until_rebuilt_from_current_sources(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    today = datetime.date.today().isoformat()
    item = {"title": "오늘 실적", "summary": "새 사실", "source": "naver_news",
            "published": today, "url": "https://n.example/same"}
    db.kb_document_add("005930", item["title"], item["summary"], item["url"],
                       item["source"], item["published"], "뉴스")
    db.kb_digest_set("005930", "삼성전자", 0.8, "예전 방식의 혼합 요약", [], 1,
                     newest_ts=int(time.time()), policy_version=None)
    assert kb.advisor_digest("005930") is None
    monkeypatch.setattr(kb.news, "collect", lambda *a, **k: [item])
    monkeypatch.setattr(kb.ingest_dart, "corp_codes", lambda: {})
    monkeypatch.setattr(kb, "_disclosure_items", lambda corp: [])
    monkeypatch.setattr(kb, "sync_candidate_events", lambda *a, **k: 0)
    monkeypatch.setattr(kb, "build_digest", lambda name, items: {
        "sentiment": 0.2, "summary": "오늘 실적 확인", "points": []})
    out = kb.refresh([{"ticker": "005930", "name": "삼성전자"}])
    assert out["updated"] == 1  # 같은 URL이어도 배포 전 요약은 한 번 다시 만들기
    assert kb.advisor_digest("005930")["summary"] == "오늘 실적 확인"


def test_existing_database_migrates_legacy_digest_without_certifying_it(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE kb_digest(ticker TEXT PRIMARY KEY, name TEXT, sentiment REAL, summary TEXT, "
                     "points TEXT, n_sources INTEGER, updated INTEGER, newest_ts INTEGER, "
                     "event_flag INTEGER, event_note TEXT)")
        conn.execute("INSERT INTO kb_digest VALUES(?,?,?,?,?,?,?,?,?,?)",
                     ("005930", "삼성전자", 0.8, "이전 방식 요약", "[]", 1,
                      int(time.time()), int(time.time()), 0, ""))
    monkeypatch.setattr(db, "DB", path)
    legacy = db.kb_digest_get("005930")
    assert legacy["policy_version"] is None
    assert kb.digest_freshness(legacy)["status"] == "unverified_legacy"


def test_public_kb_endpoint_separates_current_from_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    old = int(time.time()) - 5 * 86400
    db.kb_digest_set("005930", "삼성전자", 0.5, "오래된 호재", [], 1, newest_ts=old)
    result = api.kb_get("005930")
    assert result["digest"] is None
    assert result["archived_digest"]["summary"] == "오래된 호재"
    assert result["freshness"]["status"] == "stale"
