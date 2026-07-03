"""뉴스·영상 수집 — 네이버 뉴스 검색 API + 유튜브 데이터 API v3.

키가 없으면 조용히 빈 리스트(그레이스풀 폴백). 원자료는 kb.py가 한 번 더 가공(요약·감성)해
KB로 적재한다. 여기서는 정규화된 원자료만 반환한다: {title, summary, url, source, published}.

증권 특화(2026-07 재설계): 종목명 단독 검색은 정치·산업 노이즈가 섞여(실측 확인) 종목명+"주가"로
증권 관련도를 높이고(securities_query), 수집 후 최근 N일 신선도 필터 + 증권 관련성 게이트로
비관련 기사를 컷한다. 유튜브는 화이트리스트 확보 전까지 파이프라인에서 보류.
"""

from __future__ import annotations

import datetime
import email.utils
import json
import logging
import re
import urllib.parse
import urllib.request

from signal_desk import config

log = logging.getLogger("signal_desk.ingest.news")

_TIMEOUT = 15
_TAG = re.compile(r"<[^>]+>")
_ENT = {"&quot;": '"', "&amp;": "&", "&lt;": "<", "&gt;": ">", "&#39;": "'", "&apos;": "'"}

# 증권 관련성 판정 키워드 — 제목/요약에 하나라도 있으면 증권 뉴스로 통과(관련성 게이트).
SECURITIES_TERMS = (
    "주가", "증시", "코스피", "코스닥", "실적", "영업이익", "매출", "순이익", "어닝", "컨센서스",
    "공시", "목표주가", "증권", "배당", "자사주", "수주", "계약", "인수", "합병", "지분",
    "상장", "유상증자", "무상증자", "투자", "상향", "하향", "매수", "매도", "급등", "급락",
    "반등", "신고가", "신저가", "시총", "시가총액", "리포트", "애널리스트", "밸류업",
)
_SECURITIES_SUFFIX = "주가"  # 종목명에 붙여 증권 관련도를 높이는 앵커(실측상 노이즈 최소)


def securities_query(name: str) -> str:
    """종목명 → 증권 특화 검색어. 종목명 단독은 정치/산업 기사가 섞여, '주가' 앵커를 붙인다."""
    return f"{name} {_SECURITIES_SUFFIX}"


def is_securities_relevant(item: dict) -> bool:
    """제목+요약에 증권 키워드가 하나라도 있으면 True(관련성 게이트)."""
    text = f"{item.get('title', '')} {item.get('summary', '')}"
    return any(term in text for term in SECURITIES_TERMS)


def _parse_dt(s: str) -> datetime.datetime | None:
    """네이버 pubDate(RFC822) / 유튜브 publishedAt(ISO8601) 모두 파싱 시도."""
    if not s:
        return None
    try:
        return email.utils.parsedate_to_datetime(s)  # 'Mon, 02 Jul 2026 14:30:00 +0900'
    except (TypeError, ValueError):
        pass
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _within_days(published: str, days: int) -> bool:
    """발행일이 최근 days일 이내인가. 파싱 실패 시 True(보수적으로 유지 — 필터로 버리지 않음)."""
    dt = _parse_dt(published)
    if dt is None:
        return True
    now = datetime.datetime.now(dt.tzinfo)
    return (now - dt).days <= days


def _clean(s: str) -> str:
    s = _TAG.sub("", s or "")
    for k, v in _ENT.items():
        s = s.replace(k, v)
    return s.strip()


def naver_news(query: str, n: int = 5) -> list[dict]:
    """네이버 뉴스 검색(최신순). CLIENT_ID/SECRET 없으면 []."""
    cid, secret = config.naver_search()
    if not (cid and secret):
        return []
    qs = urllib.parse.urlencode({"query": query, "display": n, "sort": "date"})
    req = urllib.request.Request(f"https://openapi.naver.com/v1/search/news.json?{qs}")
    req.add_header("X-Naver-Client-Id", cid)
    req.add_header("X-Naver-Client-Secret", secret)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log.warning("네이버 뉴스 실패(%s): %s", query, type(e).__name__)
        return []
    out = []
    for it in data.get("items", []):
        out.append({
            "title": _clean(it.get("title", "")),
            "summary": _clean(it.get("description", "")),
            "url": it.get("originallink") or it.get("link", ""),
            "source": "naver_news",
            "published": it.get("pubDate", ""),
        })
    return out


def youtube_search(query: str, n: int = 3) -> list[dict]:
    """유튜브 영상 검색(최신순). YOUTUBE_API_KEY 없으면 []."""
    key = config.youtube_key()
    if not key:
        return []
    qs = urllib.parse.urlencode({
        "part": "snippet", "q": query, "type": "video", "maxResults": n, "order": "date", "key": key,
    })
    try:
        with urllib.request.urlopen(f"https://www.googleapis.com/youtube/v3/search?{qs}", timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log.warning("유튜브 검색 실패(%s): %s", query, type(e).__name__)
        return []
    out = []
    for it in data.get("items", []):
        sn = it.get("snippet", {})
        vid = (it.get("id") or {}).get("videoId", "")
        out.append({
            "title": _clean(sn.get("title", "")),
            "summary": _clean(sn.get("description", "")),
            "url": f"https://www.youtube.com/watch?v={vid}" if vid else "",
            "source": "youtube",
            "published": sn.get("publishedAt", ""),
        })
    return out


def collect(name: str, news_n: int = 8, lookback_days: int = 7,
            gate: bool = True, include_video: bool = False) -> list[dict]:
    """종목의 증권 뉴스 수집(원자료). 종목명+'주가'로 검색 → 최근 lookback_days 신선도 필터 →
    증권 관련성 게이트. 유튜브는 화이트리스트 확보 전까지 기본 보류(include_video=False)."""
    items = naver_news(securities_query(name), news_n)
    items = [it for it in items if _within_days(it.get("published", ""), lookback_days)]
    if gate:
        items = [it for it in items if is_securities_relevant(it)]
    if include_video:
        items += youtube_search(securities_query(name), 3)
    return items
