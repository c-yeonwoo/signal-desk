from signal_desk import db, kb_search, kb_search_eval


def test_labeled_search_eval_is_research_only_and_dense_opt_in(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    db.kb_document_add("AAA", "반도체 HBM 수요", "메모리 수요 증가",
                       "https://example.com/one", "news", "2026-09-28", "뉴스")
    kb_search._idx["sig"] = None
    cases = [{"query": "HBM 수요", "relevant_urls": ["https://example.com/one"]}]
    result = kb_search_eval.evaluate_labeled(cases, alpha=0.0)
    assert result["recall_at_k"] == 1.0 and result["mrr_at_k"] == 1.0
    assert result["live_eligible"] is False and result["backend"] == "bm25"
    try:
        kb_search_eval.evaluate_labeled(cases, alpha=0.5)
    except ValueError as exc:
        assert "비용" in str(exc)
    else:
        raise AssertionError("유료 dense 평가에 명시적 동의 필요")
