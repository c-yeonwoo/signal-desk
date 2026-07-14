"""범용 RSS/Atom 피드 파서 — 거시·시장 KB 고품질 소스(해외 전문가·기관 블로그) 수집용.

표준 라이브러리(urllib+xml.etree)만 사용, 추가 의존성 0. RSS 2.0(<item>)·Atom(<entry>) 모두 처리.
네임스페이스는 로컬명으로 무시. 본문은 HTML 태그 제거 후 요약(상위 kb에서 LLM으로 시장관점 재요약).
실패(네트워크·파싱)는 조용히 빈 리스트 — KB 수집은 best-effort.
"""

from __future__ import annotations

import html
import logging
import re
import urllib.request
import xml.etree.ElementTree as ET

log = logging.getLogger("signal_desk.ingest.rss")

_UA = "Mozilla/5.0 (compatible; signal-desk/1.0; +macro-kb)"
_TIMEOUT = 20
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(el, names: set[str]) -> str:
    for ch in el:
        if _local(ch.tag) in names and (ch.text or "").strip():
            return ch.text.strip()
    return ""


def _link(el) -> str:
    """RSS: <link>text</link>. Atom: <link rel='alternate' href=.../>."""
    fallback = ""
    for ch in el:
        if _local(ch.tag) != "link":
            continue
        href = ch.get("href")
        if href:
            if ch.get("rel", "alternate") == "alternate":
                return href
            fallback = fallback or href
        elif (ch.text or "").strip():
            return ch.text.strip()
    return fallback


def _clean(raw: str, limit: int = 1200) -> str:
    # 태그 제거 → HTML 엔티티(&nbsp; &amp; 등) 복원 → 공백 정리
    return _WS.sub(" ", html.unescape(_TAG.sub(" ", raw or ""))).strip()[:limit]


def feed_entries(url: str, limit: int = 5) -> list[dict]:
    """피드에서 최신 항목 최대 limit개 → [{title, url, published, summary}]. 실패 시 []."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            root = ET.fromstring(resp.read())
    except Exception as e:
        log.warning("RSS 조회 실패(%s): %r", url, e)
        return []

    # RSS 2.0: rss>channel>item / Atom: feed>entry
    items = [el for el in root.iter() if _local(el.tag) in ("item", "entry")]
    out = []
    for el in items[:limit]:
        title = _text(el, {"title"})
        if not title:
            continue
        summary = _clean(_text(el, {"description", "summary", "content", "encoded"}))
        out.append({
            "title": title,
            "url": _link(el),
            "published": _text(el, {"pubDate", "published", "updated", "date"}),
            "summary": summary,
        })
    return out
