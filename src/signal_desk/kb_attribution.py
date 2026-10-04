"""Conservative issuer/role gate for short news-search snippets.

Search results are not full articles.  An ambiguous hit is useful as a lead for a
person, but it must not become a company fact in KB or a trading event.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit


POLICY_VERSION = "issuer-role-v1"
_CONTRACT = re.compile(r"계약|수주|공급|납품")
_SUPPLIER = re.compile(r"수주|공급\s*계약\s*체결|공급계약체결|납품\s*계약")
_BUYER = re.compile(r"공급받|납품받|발주|도입|로부터.{0,24}(?:공급|납품|계약)")
_SALES_RATIO = re.compile(r"매출액\s*(?:대비|의\s*\d)|매출\s*대비")
_LEAD_PREFIX = re.compile(r"^(?:\[[^\]]{1,24}\]\s*)+")
_AFTER_NAME = re.compile(r"^(?:$|[\s,·:：…'\"”’()\-]|(?:은|는|이|가|의|와|과|에|도|를|을|서|만|로|으로|에서)(?=$|[\s,·:：…'\"”’()\-]))")
_RELATED_SUBJECT = re.compile(r"^(?:\s+|의\s*)(?:협력사|계열사|자회사|납품사|공급사|고객사|대리점|관련주)(?=$|[\s,·:：…'\"”’()\-])")


def _target_leads(title: str, name: str) -> bool:
    headline = _LEAD_PREFIX.sub("", title.strip()).lstrip("‘“'\" ")
    if not headline.casefold().startswith(name.casefold()):
        return False
    tail = headline[len(name):]
    return bool(_AFTER_NAME.match(tail)) and not _RELATED_SUBJECT.match(tail)


def verify_news(name: str, item: dict) -> dict:
    """Return a fail-closed attribution verdict for a Naver search result.

    The lead issuer rule prevents a counterparty appearing later in a contract
    headline from inheriting the supplier's contract value or revenue ratio.
    A sales denominator is accepted only when the target issuer is named in it.
    """
    title = str(item.get("title") or "").strip()
    summary = str(item.get("summary") or "").strip()
    url = str(item.get("url") or "").strip()
    try:
        parsed = urlsplit(url)
    except ValueError:
        return {"ok": False, "reason": "원문 링크 없음"}
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return {"ok": False, "reason": "원문 링크 없음"}
    if not name or not _target_leads(title, name):
        return {"ok": False, "reason": "제목의 주체 기업을 확인할 수 없음"}
    text = f"{title} {summary}"
    if _CONTRACT.search(text):
        if _BUYER.search(text):
            return {"ok": False, "reason": "계약에서 이 회사가 공급자인지 확인할 수 없음"}
        if not _SUPPLIER.search(title):
            return {"ok": False, "reason": "계약에서 이 회사의 역할이 불명확함"}
        ratios = list(_SALES_RATIO.finditer(text))
        if ratios:
            issuer = re.escape(name)
            pattern = re.compile(
                rf"{issuer}(?:의\s*|\s+)(?:지난해\s*|작년\s*|전년도\s*|연결\s*)*매출액\s*(?:대비|의\s*\d)",
                re.IGNORECASE,
            )
            if any(not any(m.start() <= ratio.start() and m.end() >= ratio.end()
                           for m in pattern.finditer(text)) for ratio in ratios):
                return {"ok": False, "reason": "매출 비율의 기준 회사를 확인할 수 없음"}
        return {"ok": True, "role": "supplier", "amount_basis": name}
    if _SALES_RATIO.search(text):
        return {"ok": False, "reason": "매출 비율의 기준 회사를 확인할 수 없음"}
    return {"ok": True, "role": "subject", "amount_basis": None}
