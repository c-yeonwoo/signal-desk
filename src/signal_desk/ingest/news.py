"""뉴스·영상 수집 — 네이버 뉴스 검색 API + 유튜브 데이터 API v3.

키가 없으면 조용히 빈 리스트(그레이스풀 폴백). 원자료는 kb.py가 한 번 더 가공(요약·감성)해
KB로 적재한다. 여기서는 정규화된 원자료만 반환한다: {title, summary, url, source, published}.
"""

from __future__ import annotations

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


def collect(query: str, news_n: int = 5, video_n: int = 3) -> list[dict]:
    """뉴스+영상 통합 수집(원자료)."""
    return naver_news(query, news_n) + youtube_search(query, video_n)
