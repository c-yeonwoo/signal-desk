"""R15 research-only US customer -> KR supplier evidence contract.

Curated industry maps and LLM assertions are never relationship proof. An
approved edge still has only first-observed availability, not source release
timing or a demonstrated return effect.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
from urllib.parse import urlparse

VERSION = "r15-relation-evidence-v1"
_US = re.compile(r"[A-Z][A-Z0-9.\-]{0,9}\Z")
_KR = re.compile(r"[0-9A-Z]{6}\Z")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_HOSTS = {"www.sec.gov", "sec.gov", "dart.fss.or.kr", "kind.krx.co.kr"}


def candidate(data: dict) -> dict:
    """Validate an admin-submitted source assertion; no automatic approval."""
    if not isinstance(data, dict):
        raise ValueError("관계 입력 형식 오류")
    us = str(data.get("us_customer") or "").strip().upper()
    kr = str(data.get("kr_supplier") or "").strip().upper()
    if not _US.fullmatch(us) or not _KR.fullmatch(kr):
        raise ValueError("미국/국내 종목코드 형식 오류")
    url = str(data.get("source_url") or "").strip()
    parsed = urlparse(url)
    if (parsed.scheme != "https" or parsed.hostname not in _HOSTS or
            parsed.username or parsed.password or parsed.port not in (None, 443) or len(url) > 1200):
        raise ValueError("SEC·DART·KRX 공식 HTTPS 문서만 허용")
    quote = " ".join(str(data.get("evidence_quote") or "").split())
    if not 24 <= len(quote) <= 1200:
        raise ValueError("관계와 매출노출을 확인할 근거 문장 필요")
    try:
        exposure = float(data.get("revenue_exposure_pct"))
    except (TypeError, ValueError):
        raise ValueError("근거가 있는 매출노출 비율 필요") from None
    if not math.isfinite(exposure) or not 0 < exposure <= 100:
        raise ValueError("매출노출 비율 범위 오류")
    start = str(data.get("valid_from") or "")
    end = str(data.get("valid_until") or "")
    try:
        if not _DATE.fullmatch(start) or not _DATE.fullmatch(end):
            raise ValueError
        start_date = dt.date.fromisoformat(start)
        end_date = dt.date.fromisoformat(end)
    except ValueError:
        raise ValueError("관계 유효일은 YYYY-MM-DD") from None
    if end_date < start_date:
        raise ValueError("종료일이 시작일보다 빠름")
    published = str(data.get("source_published_date") or "")
    try:
        if not _DATE.fullmatch(published):
            raise ValueError
        dt.date.fromisoformat(published)
    except ValueError:
        raise ValueError("근거 문서 발행일 필요") from None
    if end_date > dt.date.fromisoformat(published) + dt.timedelta(days=370):
        raise ValueError("발행 후 370일 이내에 관계 재검토 필요")
    proof = {"version": VERSION, "us_customer": us, "kr_supplier": kr,
             "relation": "us_customer_of_kr_supplier", "revenue_exposure_pct": exposure,
             "exposure_basis": "kr_supplier_revenue_from_us_customer",
             "source_url": url, "evidence_quote": quote,
             "source_published_date": published, "valid_from": start,
             "valid_until": end, "source_available_at_verified": False,
             "live_eligible": False}
    proof["evidence_hash"] = hashlib.sha256(
        json.dumps(proof, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return proof


def eligible_at(edge: dict, *, observed_at: int, review: dict | None) -> bool:
    """Historical availability uses *our* observation and human review times."""
    if not review or review.get("verdict") != "approved":
        return False
    if max(int(edge["observed_at"]), int(review["reviewed_at"])) > observed_at:
        return False
    day = dt.datetime.fromtimestamp(observed_at, dt.timezone.utc).astimezone(
        dt.timezone(dt.timedelta(hours=9))).date().isoformat()
    return edge["valid_from"] <= day and (not edge.get("valid_until") or day <= edge["valid_until"])


def active_edges(rows: list[dict], *, observed_at: int) -> list[dict]:
    """Latest approved version per pair at the event's *known* time.

    An unapproved replacement does not hide a proven old version. An approved
    replacement that has expired cannot resurrect the old version.
    """
    latest: dict[tuple[str, str], dict] = {}
    for row in sorted(rows, key=lambda r: r["id"]):
        review = row.get("review")
        if (review and review.get("verdict") == "approved" and
                max(int(row["observed_at"]), int(review["reviewed_at"])) <= observed_at):
            latest[(row["us_customer"], row["kr_supplier"])] = row
    return [row for row in latest.values() if eligible_at(row, observed_at=observed_at,
                                                           review=row.get("review"))]
