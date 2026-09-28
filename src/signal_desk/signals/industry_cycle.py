"""반도체 업황 근거의 보수적 연구 판정. 매매 점수와 무관하다.

업종 전체 매출(WSTS)을 메모리·파운드리·장비의 증거로 전가하지 않는다.
복합 사업 회사나 설계사는 이 세 분야에 임의 배정하지 않는다.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from urllib.parse import urlparse

VERSION = "semi-evidence-research-v1"
SEGMENTS = {"memory": "메모리", "foundry": "파운드리", "equipment": "장비"}
DIMENSIONS = {"demand": "수요", "inventory": "재고", "pricing": "가격",
              "capex": "설비투자", "guidance": "가이던스"}
DIRECTIONS = {"improving": "개선", "deteriorating": "악화", "neutral": "중립"}
SOURCE_HOSTS = {"www.sec.gov", "sec.gov", "dart.fss.or.kr", "opendart.fss.or.kr"}
# 좁은 범위의 대표 순수 노출만 지도화. 복합 사업(삼성전자), 팹리스(NVDA)는 제외.
EXPOSURE = {"000660": "memory", "MU": "memory", "TSM": "foundry",
            "ASML": "equipment", "AMAT": "equipment", "LRCX": "equipment", "KLAC": "equipment"}
MIN_DIMENSIONS = 3
MIN_ISSUERS = 2
MAX_AGE_DAYS = 120


def _utc(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("UTC 오프셋이 있는 원천 공개시각이 필요합니다")
    return parsed.astimezone(dt.timezone.utc)


def candidate(data: dict, *, observed_at: dt.datetime) -> dict:
    segment = str(data.get("segment") or "")
    dimension = str(data.get("dimension") or "")
    direction = str(data.get("direction") or "")
    issuer = str(data.get("issuer") or "").strip().upper()
    if segment not in SEGMENTS or dimension not in DIMENSIONS or direction not in DIRECTIONS:
        raise ValueError("분야·근거 유형·방향이 유효하지 않습니다")
    if not issuer or len(issuer) > 24 or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-" for c in issuer):
        raise ValueError("공시 발행 회사 티커가 필요합니다")
    period = str(data.get("period") or "")
    try:
        dt.date.fromisoformat(period)
    except ValueError:
        raise ValueError("근거 기준일은 YYYY-MM-DD여야 합니다") from None
    url = str(data.get("source_url") or "").strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in SOURCE_HOSTS or len(url) > 1000:
        raise ValueError("SEC·DART 공식 공시 HTTPS 주소가 필요합니다")
    quote = " ".join(str(data.get("evidence_quote") or "").split())
    if not 20 <= len(quote) <= 500:
        raise ValueError("원문과 대조할 20~500자 근거 문구가 필요합니다")
    observed = observed_at.astimezone(dt.timezone.utc)
    published = _utc(data.get("source_published_at"))
    if published > observed or (observed - published).days > MAX_AGE_DAYS:
        raise ValueError("공시 공개시각이 미래이거나 120일보다 오래됐습니다")
    material = f"{segment}|{dimension}|{issuer}|{period}|{url}|{quote}"
    return {"evidence_key": hashlib.sha256(material.encode()).hexdigest(),
            "segment": segment, "dimension": dimension, "direction": direction,
            "issuer": issuer, "period": period, "source_url": url,
            "source_published_at": published.isoformat(), "observed_at": observed.isoformat(),
            "evidence_quote": quote, "version": VERSION}


def assess(segment: str | None, evidence: list[dict], *, as_of: dt.datetime) -> dict:
    """충분한 독립 근거가 아니면 pass 금지. 결과는 수익 예측이 아니다."""
    if segment not in SEGMENTS:
        return {"verdict": "unavailable", "reason": "세부 분야 노출 확인 전", "evidence_ids": [],
                "coverage": None, "source_quality": "unmapped_exposure"}
    now = as_of.astimezone(dt.timezone.utc)
    fresh = []
    for row in evidence:
        if row.get("segment") != segment or row.get("review_verdict") != "approved":
            continue
        try:
            observed = _utc(row["observed_at"])
            published = _utc(row["source_published_at"])
        except (ValueError, KeyError):
            continue
        if observed <= now and published <= observed and dt.timedelta(0) <= now - published <= dt.timedelta(days=MAX_AGE_DAYS):
            fresh.append(row)
    # 같은 공시 URL의 여러 인용은 독립 원천으로 세지 않는다.
    dimensions = {r["dimension"] for r in fresh}
    issuers = {r["issuer"] for r in fresh}
    sources = {r["source_url"] for r in fresh}
    coverage = round(len(dimensions) / len(DIMENSIONS), 2)
    ids = [f"industry_evidence:{r['id']}" for r in fresh]
    if len(dimensions) < MIN_DIMENSIONS or len(issuers) < MIN_ISSUERS or len(sources) < MIN_ISSUERS:
        return {"verdict": "unavailable", "reason": f"{SEGMENTS[segment]} 근거 부족 · 유형 {len(dimensions)}/5, 독립 회사 {len(issuers)}/2",
                "evidence_ids": ids, "coverage": coverage, "source_quality": "insufficient_independent_evidence"}
    directions = {r["direction"] for r in fresh}
    if "deteriorating" in directions:
        verdict, reason = "hold", "악화 근거가 있어 분야 개선 단정 불가"
    elif "improving" in directions and len({r["dimension"] for r in fresh if r["direction"] == "improving"}) >= MIN_DIMENSIONS:
        verdict, reason = "pass", "독립 공시의 여러 유형에서 개선 관측(주가 수익 효과 미검증)"
    else:
        verdict, reason = "hold", "근거가 중립·상충하여 개선 단정 불가"
    return {"verdict": verdict, "reason": f"{SEGMENTS[segment]} · {reason}",
            "evidence_ids": ids, "coverage": coverage, "source_quality": "operator_approved_official"}
