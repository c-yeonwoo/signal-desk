"""유튜브 채널 수집 — 화이트리스트 채널의 최신 영상 목록(Data API v3) + 자막 전문.

채널 핸들 → uploads 재생목록 → 최근 영상(제목·설명·발행일)은 Data API(YOUTUBE_API_KEY).
영상 발화 내용은 자막(youtube-transcript-api)으로 받는다 — 유튜브가 asr 자막에 pot 토큰을
요구해 순수 urllib로는 막히므로 전용 라이브러리를 쓴다(그레이스풀: 없거나 실패 시 설명으로 폴백).
콘텐츠가 대부분 거시·시장 해설이라 상위(kb)에서 거시 KB로 적재한다.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
import urllib.request

from signal_desk import config

log = logging.getLogger("signal_desk.ingest.youtube")

_API = "https://www.googleapis.com/youtube/v3/"
_TIMEOUT = 15


def _api(path: str, **params) -> dict | None:
    key = config.youtube_key()
    if not key:
        return None
    params["key"] = key
    url = f"{_API}{path}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT) as resp:
            return json.load(resp)
    except Exception as e:
        log.warning("youtube Data API 실패(%s): %s", path, type(e).__name__)
        return None


def video_url(video_id: str) -> str:
    """영상 URL — KB 문서 고유키(증분 수집 dedup 기준)."""
    return f"https://www.youtube.com/watch?v={video_id}"


def channel_videos(handle: str, max_results: int = 10) -> dict:
    """채널 핸들(@없이)의 최신 업로드. 반환: {channel, videos:[{video_id,title,published,description}]}."""
    ch = _api("channels", part="snippet,contentDetails", forHandle=handle)
    items = (ch or {}).get("items") or []
    if not items:
        return {"channel": handle, "videos": []}
    c = items[0]
    uploads = c["contentDetails"]["relatedPlaylists"]["uploads"]
    pl = _api("playlistItems", part="snippet,contentDetails", playlistId=uploads,
              maxResults=min(max_results, 50))
    videos = []
    for it in (pl or {}).get("items", []):
        s = it["snippet"]
        videos.append({
            "video_id": it["contentDetails"]["videoId"],
            "title": (s.get("title") or "").strip(),
            "published": s.get("publishedAt"),
            "description": (s.get("description") or "").strip(),
        })
    return {"channel": c["snippet"].get("title") or handle, "videos": videos}


def transcript(video_id: str, languages: tuple[str, ...] = ("ko", "en")) -> str | None:
    """영상 자막 전문(공백 정리). 라이브러리 미설치·자막 없음·실패 시 None(설명 폴백은 호출측)."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        log.info("youtube-transcript-api 미설치 — 자막 스킵")
        return None
    try:
        fetched = YouTubeTranscriptApi().fetch(video_id, languages=list(languages))
    except Exception as e:
        log.info("자막 조회 실패(%s): %s", video_id, type(e).__name__)
        return None
    txt = " ".join(sn.text for sn in fetched)
    return re.sub(r"\s+", " ", txt).strip() or None
