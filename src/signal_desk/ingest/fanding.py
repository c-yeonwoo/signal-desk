"""fanding.kr 미주은 포스트 수집 — 앱 REST API(JSON). 브라우저 불필요 → headless 자동수집 가능.

인증은 config.fanding_cookie()(device_uid + tt 세션 토큰). 토큰 만료 시 .env(FANDING_TT) 갱신.
본문 하단 홍보 템플릿(시킹알파 할인·에세이·초이스스탁 등)은 잘라내고 본문만 반환한다.
유료 창작물이므로 상위(kb)에서 원문 저장 없이 LLM 다이제스트 + 출처만 적재한다.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.request

from signal_desk import config

log = logging.getLogger("signal_desk.ingest.fanding")

_BASE = "https://fanding.kr/rest"
_UA = "RN_FDAPP_ios-V:6.0.2;bundle:v18;"
_TIMEOUT = 20
# 본문 뒤에 붙는 홍보/공지 블록 시작 마커 — 이 지점 이후는 광고라 버린다(토큰·오염 방지)
_PROMO_MARKERS = ("📢", "📍 미주은 에세이", "💸 특별 할인", "시킹알파 멤버십", "초이스스탁", "노력의 배신")
_TAG = re.compile(r"<[^>]+>")


def _get(path: str) -> dict | None:
    cookie = config.fanding_cookie()
    if not cookie:
        return None
    req = urllib.request.Request(f"{_BASE}/{path}", headers={
        "cookie": cookie, "accept": "application/json, text/plain, */*",
        "lang": "ko", "user-agent": _UA,
    })
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log.warning("fanding 요청 실패(%s): %s", path, type(e).__name__)
        return None
    return body if body.get("bIsResult") else None


def post_list(member: str = "mijooeun", limit: int = 20) -> list[dict]:
    """최신순 포스트 목록 [{post_no, title, type, content_type, published}]."""
    body = _get(f"post_list?iLimit={limit}&sMemberUrl={member}&sVisibleOnlyOption=F&sSortOrder=recent")
    if not body:
        return []
    out = []
    for p in (body.get("aData") or {}).get("aPostList", []):
        out.append({"post_no": p.get("iPostNo"), "title": (p.get("sTitle") or "").strip(),
                    "type": p.get("sType"), "content_type": p.get("sContentType"),
                    "published": p.get("sInsDatetime")})
    return out


def post_url(post_no: int, member: str = "mijooeun") -> str:
    """포스트 공개 URL — KB 문서의 고유키(증분 수집 dedup 기준)."""
    return f"https://fanding.kr/@{member}/post/{post_no}/"


def _clean_body(html: str) -> str:
    """HTML 태그 제거 + 공백 정리 + 하단 홍보 블록 컷."""
    txt = _TAG.sub(" ", html or "")
    txt = txt.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").replace("&#39;", "'").replace("&quot;", '"')
    txt = re.sub(r"\s+", " ", txt).strip()
    cut = len(txt)
    for m in _PROMO_MARKERS:
        i = txt.find(m)
        if i != -1:
            cut = min(cut, i)
    return txt[:cut].strip()


def post_detail(post_no: int) -> dict | None:
    """단일 포스트 본문. 반환: {post_no, title, content(광고 제외), published, is_paid, url}. 실패 시 None."""
    body = _get(f"post?iPostNo={post_no}")
    if not body:
        return None
    p = ((body.get("aData") or {}).get("oPostData")) or {}
    content = _clean_body(p.get("sContent") or "")
    if not content:
        return None
    return {"post_no": post_no, "title": (p.get("sTitle") or "").strip(), "content": content,
            "published": p.get("sInsDatetime"), "is_paid": p.get("sIsPaid") == "T",
            "url": post_url(post_no)}
