"""KB 저장 효율화 — raw_text 절단 + 뉴스/pending prune. 큐레이션 문서는 보존."""

import time

from signal_desk import db


def _news(ticker, n, fetched_days_ago=0, source="naver_news"):
    now = int(time.time())
    c = db.conn()
    for i in range(n):
        c.execute("INSERT INTO kb_entries(ticker,title,summary,url,source,published,fetched,doc_class,status) "
                  "VALUES(?,?,?,?,?,?,?,?,?)",
                  (ticker, f"t{i}", "s", f"http://x/{ticker}/{i}/{source}", source, "",
                   now - fetched_days_ago * 86400, "뉴스", "confirmed"))
    c.commit()
    c.close()


def test_raw_text_truncated_on_write(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    monkeypatch.setattr(db, "KB_RAW_TEXT_KEEP", 100)
    db.kb_document_add("005930", "리포트", "요약", "http://r/1", "report", "", "리포트",
                       raw_text="가" * 5000, status="confirmed")
    c = db.conn()
    (rt,) = c.execute("SELECT raw_text FROM kb_entries WHERE url='http://r/1'").fetchone()
    c.close()
    assert len(rt) == 100  # 5000자 원문 → 100자로 절단 보관


def test_prune_caps_news_per_ticker(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    _news("005930", 40)                      # 최신 40건
    out = db.kb_prune(news_per_ticker=30, news_ttl_days=90)
    assert out["news_deleted"] == 10         # 30건 초과분 삭제
    c = db.conn()
    (cnt,) = c.execute("SELECT COUNT(*) FROM kb_entries WHERE ticker='005930'").fetchone()
    c.close()
    assert cnt == 30


def test_prune_keeps_digest_floor_but_drops_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    _news("000660", 20, fetched_days_ago=200)  # 20건 전부 200일 경과(만료)
    out = db.kb_prune(news_per_ticker=30, news_ttl_days=90)
    c = db.conn()
    (cnt,) = c.execute("SELECT COUNT(*) FROM kb_entries WHERE ticker='000660'").fetchone()
    c.close()
    assert cnt == 12 and out["news_deleted"] == 8   # 하한 12건은 보존, 초과 8건은 만료 삭제


def test_prune_preserves_curated_uploads(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    _news("035420", 40, source="upload")     # 업로드(큐레이션) 40건 — 오래돼도 보존
    out = db.kb_prune(news_per_ticker=30)
    c = db.conn()
    (cnt,) = c.execute("SELECT COUNT(*) FROM kb_entries WHERE ticker='035420'").fetchone()
    c.close()
    assert cnt == 40 and out["news_deleted"] == 0


def test_prune_drops_stale_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    now = int(time.time())
    c = db.conn()
    c.execute("INSERT INTO kb_entries(ticker,title,summary,url,source,fetched,status) "
              "VALUES('068270','p','s','http://p/old','report',?, 'pending')", (now - 20 * 86400,))
    c.execute("INSERT INTO kb_entries(ticker,title,summary,url,source,fetched,status) "
              "VALUES('068270','p','s','http://p/new','report',?, 'pending')", (now - 2 * 86400,))
    c.commit(); c.close()
    out = db.kb_prune(pending_ttl_days=14)
    assert out["pending_deleted"] == 1       # 20일 지난 pending만 삭제, 2일짜리는 유지
