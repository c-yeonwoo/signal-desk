"""아웃스탠딩(outstanding.kr) 전문가 기고 수집 — WPGraphQL API(POST JSON). 브라우저 불필요.

작가 화이트리스트(config.outstanding_authors)로 신뢰 전문가 기고만 골라 수집한다(오염 방지).
콘텐츠는 대부분 거시·산업 해설이라 상위(kb)에서 거시 KB로 적재한다.
공개(isPrivate=False) 기고는 목록의 text 필드가 사실상 전문이라 단건 조회 없이 바로 쓴다.
유료(isPrivate=True) 기고 본문은 로그인 쿠키(config.outstanding_cookie)가 있어야 하며, 없으면 건너뛴다.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request

from signal_desk import config

log = logging.getLogger("signal_desk.ingest.outstanding")

_GQL = "https://wp.outstanding.kr/api/next/index.php"
_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 "
       "(KHTML, like Gecko) Mobile/15E148 outsapp/ios/20240413")
_TIMEOUT = 20
_TAG = re.compile(r"<[^>]+>")

# postAuthorList — 작가별 기고 목록(공개 text 포함). 필요한 필드만 선택.
_AUTHOR_QUERY = (
    "query postAuthorList($author_login_id: String!, $page: Int, $item_per_page: Int){"
    " postAuthorList(author_login_id:$author_login_id, page:$page, item_per_page:$item_per_page){"
    " author{ name loginId authorPosition }"
    " postListResult{ pagingItemCount posts{ id title uri datetime isPrivate type"
    " postCategorys{ name } text html } } } }"
)


def _post(query: str, variables: dict) -> dict | None:
    headers = {"content-type": "application/json", "accept": "*/*",
               "origin": "https://outstanding.kr", "referer": "https://outstanding.kr/",
               "user-agent": _UA}
    cookie = config.outstanding_cookie()
    if cookie:
        headers["cookie"] = cookie
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(_GQL, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            out = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 2026-07 outstanding.kr이 WordPress→Next.js로 개편되며 이 WP GraphQL 엔드포인트(index.php)가
        # 폐기됨(모든 요청 500). 새 기고 API는 미확인 → graceful 스킵(전체 수집은 계속 진행).
        log.warning("outstanding 수집 불가 — 사이트 개편으로 기존 API 폐기(HTTP %s, 재연동 필요)", e.code)
        return None
    except Exception as e:
        log.warning("outstanding 요청 실패: %s", type(e).__name__)
        return None
    if out.get("errors"):
        log.warning("outstanding GraphQL 오류: %s", str(out["errors"])[:200])
        return None
    return out.get("data")


def post_url(uri: str) -> str:
    """공개 기고 URL — KB 문서 고유키(증분 수집 dedup 기준)."""
    return f"https://outstanding.kr/{uri}"


def _html_to_text(html: str) -> str:
    txt = _TAG.sub(" ", html or "")
    txt = (txt.replace("&nbsp;", " ").replace("&lt;", "<").replace("&gt;", ">")
           .replace("&amp;", "&").replace("&#39;", "'").replace("&quot;", '"'))
    return re.sub(r"\s+", " ", txt).strip()


def author_posts(login_id: str, page: int = 1, item_per_page: int = 20) -> dict:
    """작가 최신 기고 목록. 반환: {author:{name,position}, total, posts:[{...}]}.
    posts 각 항목: {post_id, uri, title, datetime, is_private, categories, body}."""
    data = _post(_AUTHOR_QUERY, {"author_login_id": login_id, "page": page, "item_per_page": item_per_page})
    if not data or not data.get("postAuthorList"):
        return {"author": {}, "total": 0, "posts": []}
    node = data["postAuthorList"]
    a = node.get("author") or {}
    lr = node.get("postListResult") or {}
    posts = []
    for p in lr.get("posts") or []:
        # 공개글은 text가 사실상 전문. 비면 html에서 텍스트 추출.
        body = (p.get("text") or "").strip() or _html_to_text(p.get("html") or "")
        posts.append({
            "post_id": p.get("id"), "uri": p.get("uri"), "title": (p.get("title") or "").strip(),
            "datetime": p.get("datetime"), "is_private": bool(p.get("isPrivate")),
            "categories": [c.get("name") for c in (p.get("postCategorys") or [])],
            "body": body,
        })
    return {"author": {"name": a.get("name"), "login_id": a.get("loginId"),
                       "position": a.get("authorPosition")},
            "total": lr.get("pagingItemCount"), "posts": posts}
