import datetime

from signal_desk.ingest import news


def test_securities_query_appends_price_anchor():
    assert news.securities_query("삼성전자") == "삼성전자 주가"


def test_is_securities_relevant_gate():
    assert news.is_securities_relevant({"title": "삼성전자 목표주가 상향", "summary": ""})
    assert news.is_securities_relevant({"title": "무관", "summary": "2분기 영업이익 급증"})
    assert not news.is_securities_relevant({"title": "삼성전자 노란봉투법 논란", "summary": "정치권 공방"})


def test_within_days_filters_old_and_keeps_unparseable():
    now = datetime.datetime.now(datetime.timezone.utc)
    recent = now.strftime("%a, %d %b %Y %H:%M:%S +0000")
    old = (now - datetime.timedelta(days=30)).strftime("%a, %d %b %Y %H:%M:%S +0000")
    assert news._within_days(recent, 7) is True
    assert news._within_days(old, 7) is False
    assert news._within_days("", 7) is True  # 파싱 불가 → 보수적으로 유지


def test_collect_applies_freshness_and_gate(monkeypatch):
    now = datetime.datetime.now(datetime.timezone.utc)
    recent = now.strftime("%a, %d %b %Y %H:%M:%S +0000")
    old = (now - datetime.timedelta(days=30)).strftime("%a, %d %b %Y %H:%M:%S +0000")
    raw = [
        {"title": "삼성전자 주가 급등", "summary": "", "published": recent, "url": "u1"},   # 통과
        {"title": "삼성전자 신제품 광고", "summary": "홍보", "published": recent, "url": "u2"},  # 게이트 컷(증권 무관)
        {"title": "삼성전자 실적 발표", "summary": "", "published": old, "url": "u3"},       # 신선도 컷
    ]
    monkeypatch.setattr(news, "naver_news", lambda q, n: raw)
    out = news.collect("삼성전자", news_n=10, lookback_days=7)
    assert [it["url"] for it in out] == ["u1"]


def test_collect_skips_youtube_by_default(monkeypatch):
    monkeypatch.setattr(news, "naver_news", lambda q, n: [])
    called = {"yt": False}
    monkeypatch.setattr(news, "youtube_search", lambda q, n: called.__setitem__("yt", True) or [])
    news.collect("삼성전자")
    assert called["yt"] is False  # 유튜브 보류 — 기본 미호출
