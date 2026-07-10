"""KB 문서 RAG 검색(BM25) — 한글 2-그램 토크나이즈, 관련도 랭킹, 재색인."""

from signal_desk import db, kb_search


def _seed(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    db.kb_document_add("005930", "삼성전자 HBM 수요 급증", "고대역폭 메모리 HBM 수요가 AI 서버 확대로 급증하고 있다는 분석.",
                       "http://a", "news", "2026-07-01", "뉴스")
    db.kb_document_add("000660", "SK하이닉스 반도체 업황 반등", "메모리 가격 반등과 감산 효과로 반도체 업황이 개선되고 있다.",
                       "http://b", "news", "2026-07-02", "뉴스")
    db.kb_document_add("005380", "현대차 전기차 판매 부진", "전기차 수요 둔화로 현대차 판매가 주춤하고 있다는 우려.",
                       "http://c", "news", "2026-07-03", "뉴스")
    kb_search._idx["sig"] = None  # 캐시 무효화


def test_tokenize_hangul_bigrams():
    t = kb_search._tokenize("반도체 AAPL 3")
    assert "반도" in t and "도체" in t and "aapl" in t and "3" in t


def test_retrieve_ranks_relevant_top(tmp_path, monkeypatch):
    _seed(monkeypatch, tmp_path)
    hits = kb_search.retrieve("HBM 메모리 수요", k=3)
    assert hits, "관련 문서가 나와야 함"
    assert hits[0]["ticker"] == "005930"          # HBM 수요 문서가 최상위
    assert hits[0]["score"] > 0
    # 무관한 질의는 해당 문서를 상위에 올리지 않음
    ev = kb_search.retrieve("전기차 판매", k=3)
    assert ev and ev[0]["ticker"] == "005380"


def test_retrieve_empty_on_no_match(tmp_path, monkeypatch):
    _seed(monkeypatch, tmp_path)
    assert kb_search.retrieve("비트코인 규제 소송", k=3) == []   # 코퍼스에 없는 주제


def test_reindex_on_corpus_change(tmp_path, monkeypatch):
    _seed(monkeypatch, tmp_path)
    assert len(kb_search.retrieve("반도체", k=10)) >= 1
    db.kb_document_add("035420", "네이버 클라우드 반도체 투자", "네이버가 반도체·클라우드 인프라 투자를 확대한다.",
                       "http://d", "news", "2026-07-04", "뉴스")
    # 시그니처 변경 → 자동 재색인되어 새 문서도 검색됨
    hits = kb_search.retrieve("네이버 클라우드", k=5)
    assert any(h["ticker"] == "035420" for h in hits)
