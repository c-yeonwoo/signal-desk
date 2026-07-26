"""KB 검색의 최신성 — 오래된 문서는 순위에서 밀리고, 나이는 항상 함께 나온다.

시점 없는 근거는 근거가 아니다: 오전에 사실이던 시황이 오후엔 아닐 수 있는데, 관련도만 보는
검색기는 3주 전 기사를 오늘 기사와 같은 순위에 올린다.
"""

import datetime
import time

from signal_desk import db, kb_search


def _add(ticker, title, summary, url, days_ago, doc_class="뉴스"):
    pub = (datetime.datetime.now() - datetime.timedelta(days=days_ago)).isoformat()
    return db.kb_document_add(ticker, title, summary, url, "news", pub, doc_class)


def test_old_doc_loses_to_fresh_doc_with_same_words(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    _add("005930", "삼성전자 HBM 수요 급증", "HBM 수요가 급증하고 있다는 분석.", "http://old", days_ago=30)
    _add("000660", "SK하이닉스 HBM 수요 급증", "HBM 수요가 급증하고 있다는 분석.", "http://new", days_ago=0)
    kb_search._idx["sig"] = None

    hits = kb_search.retrieve("HBM 수요 급증", k=2)
    assert [h["ticker"] for h in hits] == ["000660", "005930"], "같은 문구면 최신이 위여야 한다"
    # 관련도는 사실상 동급인데 최신성으로 갈렸다는 게 보여야 한다(왜 밀렸는지 설명 가능해야 함).
    assert hits[0]["relevance"] > 0 and hits[1]["relevance"] > 0
    assert hits[0]["score"] > hits[1]["score"]


def test_every_hit_carries_its_timestamp(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    _add("005930", "삼성전자 실적 서프라이즈", "영업이익이 컨센서스를 상회했다.", "http://a", days_ago=2)
    kb_search._idx["sig"] = None

    hits = kb_search.retrieve("실적 서프라이즈", k=1)
    assert hits
    h = hits[0]
    assert h["age_days"] is not None and 1.5 <= h["age_days"] <= 2.5
    assert h["as_of"], "시점 문자열이 없으면 소비자가 나이를 알 수 없다"
    assert h["stale"] is False
    assert h["half_life_days"] == kb_search.HALF_LIFE_DAYS["뉴스"]


def test_stale_flag_and_fresh_only_filter(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    _add("005930", "삼성전자 시황 코멘트", "지수 흐름과 수급을 짚는다.", "http://s", days_ago=20, doc_class="시황")
    kb_search._idx["sig"] = None

    hits = kb_search.retrieve("시황 수급", k=3)
    assert hits and hits[0]["stale"] is True, "반감기 3배를 넘으면 오래됨으로 표시"
    # 오래됐다고 지우지는 않는다(맥락은 남는다). 단 속보성 질의는 걸러낼 수 있어야 한다.
    assert kb_search.retrieve("시황 수급", k=3, fresh_only=True) == []


def test_disclosure_decays_slower_than_market_commentary(tmp_path, monkeypatch):
    """공시·실적은 사건 자체가 사실로 남고, 시황은 전제가 매일 바뀐다."""
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    _add("005930", "삼성전자 공급계약 체결 공시", "단일판매 공급계약을 체결했다.", "http://d", days_ago=10, doc_class="공시")
    _add("000660", "SK하이닉스 공급계약 관련 시황", "단일판매 공급계약을 체결했다.", "http://m", days_ago=10, doc_class="시황")
    kb_search._idx["sig"] = None

    hits = kb_search.retrieve("단일판매 공급계약 체결", k=2)
    assert [h["ticker"] for h in hits] == ["005930", "000660"]


def test_unknown_timestamp_is_not_treated_as_newest(tmp_path, monkeypatch):
    """시점 불명을 최신으로 대우하면 날짜 없는 업로드 문서가 항상 상위를 먹는다."""
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    eid = db.kb_document_add("005930", "삼성전자 목표주가 상향", "밸류에이션 매력을 강조한다.",
                             "http://nodate", "manual", "", "리포트")
    c = db.conn()
    c.execute("UPDATE kb_entries SET fetched=NULL WHERE id=?", (eid,))
    c.commit()
    c.close()
    _add("000660", "SK하이닉스 목표주가 상향", "밸류에이션 매력을 강조한다.", "http://dated", days_ago=1, doc_class="리포트")
    kb_search._idx["sig"] = None

    hits = kb_search.retrieve("목표주가 상향 밸류에이션", k=2)
    assert [h["ticker"] for h in hits] == ["000660", "005930"]
    nodate = [h for h in hits if h["ticker"] == "005930"][0]
    assert nodate["age_days"] is None and nodate["as_of"] is None
    assert 0.4 < kb_search.recency_weight({"published": "", "fetched": None}) < 0.6


def test_recency_weight_bounded_and_monotonic():
    now = time.time()
    doc = {"doc_class": "뉴스", "fetched": now}
    assert kb_search.recency_weight(doc, now=now) == 1.0
    older = {"doc_class": "뉴스", "fetched": now - 400 * 86400}
    assert kb_search.recency_weight(older, now=now) == kb_search.RECENCY_FLOOR, "하한 아래로는 안 간다"
    half = {"doc_class": "뉴스", "fetched": now - kb_search.HALF_LIFE_DAYS["뉴스"] * 86400}
    assert abs(kb_search.recency_weight(half, now=now) - 0.5) < 0.01
