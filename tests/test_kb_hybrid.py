"""KB 하이브리드 검색·임베딩·시맨틱/구문 veto."""

from signal_desk import db, kb, kb_embed, kb_search


def _seed(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    db.kb_document_add("005930", "삼성전자 HBM 수요 급증", "고대역폭 메모리 HBM 수요가 AI 서버 확대로 급증하고 있다는 분석.",
                       "http://a", "news", "2026-07-01", "뉴스")
    db.kb_document_add("000660", "SK하이닉스 반도체 업황 반등", "메모리 가격 반등과 감산 효과로 반도체 업황이 개선되고 있다.",
                       "http://b", "news", "2026-07-02", "뉴스")
    db.kb_document_add("005380", "현대차 전기차 판매 부진", "전기차 수요 둔화로 현대차 판매가 주춤하고 있다는 우려.",
                       "http://c", "news", "2026-07-03", "뉴스")
    kb_search._idx["sig"] = None


def test_embed_on_document_add(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    eid = db.kb_document_add("005930", "테스트 임베드", "요약 본문", "http://e1", "news", "", "뉴스")
    assert eid > 0
    vecs = kb_embed.load_vectors([eid])
    assert eid in vecs and len(vecs[eid]) == kb_embed.DIM


def test_hybrid_retrieve_keeps_bm25_ranking(tmp_path, monkeypatch):
    _seed(monkeypatch, tmp_path)
    hits = kb_search.retrieve("HBM 메모리 수요", k=3)
    assert hits and hits[0]["ticker"] == "005930"
    assert "bm25" in hits[0] and "dense" in hits[0]


def test_hybrid_alpha_zero_is_pure_bm25(tmp_path, monkeypatch):
    _seed(monkeypatch, tmp_path)
    a = kb_search.retrieve("전기차 판매", k=1, alpha=0.0)
    assert a and a[0]["ticker"] == "005380"


def test_phrase_expansion_veto_without_exact_term():
    # '횡령' 글자 없이도 프로토타입 구문 '회사 자금 유용'으로 veto
    flag, note = kb.detect_event([{"title": "회사 자금 유용 정황 포착", "summary": "", "source": "naver_news"}])
    assert flag is True
    assert "횡령" in note
    assert kb.event_severity(note) == "critical"


def test_semantic_veto_with_mock_embed(monkeypatch):
    """cosine 경로 — 임베딩을 강제로 일치시켜 시맨틱 veto 발화."""
    unit = [1.0] + [0.0] * (kb_embed.DIM - 1)

    def fake_embed(texts):
        return [list(unit) for _ in texts]

    monkeypatch.setattr(kb_embed, "embed_texts", fake_embed)
    monkeypatch.setattr(kb_embed, "semantic_capable", lambda: True)
    monkeypatch.setattr(kb_embed, "EVENT_SEMANTIC_TAU", 0.99)
    # 키워드·구문에도 안 걸리게 무관한 문장 + mock으로 cosine=1
    flag, note = kb.detect_event([{"title": "전혀 다른 제목 XYZ", "summary": "본문", "source": "naver_news"}])
    assert flag is True
    assert "의미근접" in note


def test_neutral_news_not_vetoed():
    assert kb.detect_event([{"title": "B사 실적 발표", "summary": "영업이익 증가", "source": "naver_news"}])[0] is False
