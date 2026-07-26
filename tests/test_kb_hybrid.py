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


def test_semantic_veto_fires_on_margin_not_absolute_cosine(monkeypatch):
    """의미 벡터에선 절대 cosine으로 문턱을 만들 수 없다(무관 문장끼리도 0.8~0.9).
    판정은 '악재 프로토타입 − 중립 앵커' 마진이어야 한다."""
    bad_axis = [1.0] + [0.0] * (kb_embed.DIM - 1)
    neutral_axis = [0.0, 1.0] + [0.0] * (kb_embed.DIM - 2)

    def fake_embed(texts):
        # 중립 앵커만 다른 축, 나머지(문서·악재 프로토타입)는 악재 축 → 마진 큼
        return [list(neutral_axis) if t in kb._NEUTRAL_ANCHORS else list(bad_axis) for t in texts]

    monkeypatch.setattr(kb_embed, "embed_texts", fake_embed)
    monkeypatch.setattr(kb_embed, "semantic_capable", lambda: True)
    flag, note = kb.detect_event([{"title": "전혀 다른 제목 XYZ", "summary": "본문", "source": "naver_news"}])
    assert flag is True and "중립대비" in note

    # 악재만큼 중립에도 가까우면(마진 0) 발화하지 않는다 — 절대값만 높은 건 근거가 아니다
    monkeypatch.setattr(kb_embed, "embed_texts", lambda texts: [list(bad_axis) for _ in texts])
    assert kb.detect_event([{"title": "전혀 다른 제목 XYZ", "summary": "본문",
                             "source": "naver_news"}])[0] is False


def test_neutral_news_not_vetoed():
    assert kb.detect_event([{"title": "B사 실적 발표", "summary": "영업이익 증가", "source": "naver_news"}])[0] is False
