"""KB 소스 레지스트리·ingest gate (P1)."""

import time

from signal_desk import db, kb, kb_attribution


def test_kb_sources_seeded(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    srcs = {s["source_key"]: s for s in db.kb_sources_list()}
    assert "dart" in srcs and srcs["dart"]["trust_tier"] == "official"
    assert srcs["dart"]["decision_event_mode"] == "rule_official"
    assert srcs["naver_news"]["trust_tier"] == "medium"
    assert srcs["manual"]["enabled"] is True
    for key in ("youtube", "rss", "fanding", "outstanding"):
        assert key in srcs


def test_ingest_rejects_unknown_and_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    bad = kb.ingest_document(
        source_key="no_such_source", ticker="005930", title="t", summary="s" * 20,
    )
    assert bad["ok"] is False and "등록" in bad["reason"]

    c = db.conn()
    c.execute("UPDATE kb_sources SET enabled=0 WHERE source_key='naver_news'")
    c.commit()
    c.close()
    out = kb.ingest_document(
        source_key="naver_news", ticker="005930", title="뉴스", summary="본문 " * 10,
    )
    assert out["ok"] is False and "비활성" in out["reason"]
    src = db.kb_source_get("naver_news")
    assert src["rejected_count"] >= 1


def test_ingest_accepts_manual_and_lazy_child(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    ok = kb.ingest_document(
        source_key="manual", ticker="005930", title="리포트", summary="분석 " * 20,
        doc_class="리포트", scope="stock",
    )
    assert ok["ok"] is True and ok["entry_id"]
    assert ok["trust_tier"] == "high"

    child = kb.ingest_document(
        source_key="youtube:@testchan", ticker=kb.MACRO_TICKER,
        title="영상", summary="시황 설명 " * 10, scope="market",
        parent_key="youtube", display_name="@testchan",
    )
    assert child["ok"] is True
    ch = db.kb_source_get("youtube:@testchan")
    assert ch and ch["source_family"] == "youtube" and ch["trust_tier"] == "medium"
    assert ch["lifecycle"] == "probation"


def test_ingest_stock_batch_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    items = [
        {"title": "공시", "source": "dart", "published": "2026-07-01", "url": "https://dart.example/1", "attribution_version": kb_attribution.POLICY_VERSION},
        {"title": "뉴스", "source": "naver_news", "published": "2026-07-01", "url": "https://n.example/1", "attribution_version": kb_attribution.POLICY_VERSION},
    ]
    n = kb.ingest_stock_batch("005930", items)
    assert n == 2
    assert db.kb_source_get("dart")["accepted_count"] >= 1

    c = db.conn()
    c.execute("UPDATE kb_sources SET enabled=0 WHERE source_key='dart'")
    c.commit()
    c.close()
    n2 = kb.ingest_stock_batch("005930", [
        {"title": "공시2", "source": "dart", "published": "2026-07-02", "url": "https://dart.example/2", "attribution_version": kb_attribution.POLICY_VERSION},
        {"title": "뉴스2", "source": "naver_news", "published": "2026-07-02", "url": "https://n.example/2", "attribution_version": kb_attribution.POLICY_VERSION},
    ])
    assert n2 == 1  # naver only


def test_scope_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    # dart is stock-only
    out = kb.ingest_document(
        source_key="dart", ticker="_MARKET", title="x", summary="y" * 20, scope="market",
    )
    assert out["ok"] is False and "scope" in out["reason"]


def test_refresh_still_writes_with_gate(tmp_path, monkeypatch):
    import datetime
    monkeypatch.setattr(kb.db, "DB", tmp_path / "app.db")
    monkeypatch.setattr(kb.news, "collect", lambda *a, **k: [
        {"title": "삼성전자, 일반 뉴스", "source": "naver_news", "published": datetime.date.today().isoformat(),
         "url": "https://n.example/r", "summary": "내용"},
    ])
    monkeypatch.setattr(kb.ingest_dart, "corp_codes", lambda: {"005930": "00126380"})
    monkeypatch.setattr(kb.ingest_dart, "disclosures", lambda *a, **k: [])
    monkeypatch.setattr(kb, "build_digest", lambda name, items: {
        "sentiment": 0.1, "summary": "s", "points": []})
    out = kb.refresh([{"ticker": "005930", "name": "삼성전자"}])
    assert out["updated"] == 1
    assert db.kb_source_get("naver_news")["accepted_count"] >= 1
