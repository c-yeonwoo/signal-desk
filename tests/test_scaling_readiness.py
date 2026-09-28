"""인프라 증설은 운영 지연·대표 라벨·작업 실패 근거가 없으면 보류한다."""

from signal_desk import db, kb_search
from signal_desk.signals import scaling_readiness


def test_search_latency_sample_has_no_query_text(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(kb_search.random, "random", lambda: 0)
    c = db.conn()
    try:
        c.execute("INSERT INTO kb_entries(ticker,title,summary,url) VALUES(?,?,?,?)",
                  ("AAA", "성장", "공식 실적", "https://example.com/one"))
        c.commit()
    finally:
        c.close()
    kb_search._idx["sig"] = None
    assert kb_search.retrieve("성장")
    summary = db.kb_search_latency_summary()
    assert summary["samples"] == 1 and summary["p95_ms"] >= 0
    c = db.conn()
    try:
        columns = [r[1] for r in c.execute("PRAGMA table_info(kb_search_latency_samples)")]
    finally:
        c.close()
    assert "query" not in columns and "user" not in columns
    assert "corpus_size" in columns


def test_search_evaluation_with_explicit_alpha_does_not_pollute_operational_latency(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(kb_search.random, "random", lambda: 0)
    kb_search._idx["sig"] = None
    kb_search.retrieve("연구 질의", alpha=0)
    assert db.kb_search_latency_summary()["samples"] == 0


def test_ml_vector_and_queue_are_deferred_without_proof():
    report = scaling_readiness.assess(document_count=1500,
        latency={"samples": 20, "p95_ms": 12}, prospective_cohorts={"kr": 3, "us": 2},
        review_pending=7)
    assert report["live_eligible"] is False
    assert all(report[key]["status"] == "defer" for key in ("ml", "vector_database", "distributed_queue"))
    assert report["metrics"]["labeled_search_queries"] is None
    assert report["metrics"]["producer_retry_lag_p95_ms"] is None
    assert "20/100" in report["vector_database"]["reason"]


def test_even_high_latency_without_quality_labels_cannot_justify_vector_db():
    report = scaling_readiness.assess(document_count=20000,
        latency={"samples": 200, "p95_ms": 1000}, prospective_cohorts={"kr": 60, "us": 60},
        review_pending=0)
    assert report["vector_database"]["status"] == "defer"
    assert "대표 질의" in report["vector_database"]["reason"]
