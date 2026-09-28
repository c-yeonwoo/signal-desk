"""조회 전용 투자 관점. 원래 점수·매수권·주문 정책을 절대 변경하지 않는다.

서버가 받은 하나의 시그널 목록에서 결과와 스냅샷 ID를 만든다. 토글은 같은
스냅샷의 결과를 재조합할 뿐 새로운 시세/뉴스 요청이나 주문을 실행하지 않는다.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import Any
from zoneinfo import ZoneInfo

from signal_desk.signals import industry_cycle, macro_release

VERSION = "read-only-lenses-v3"
HORIZON = "수일~수주 참고"
LENS_CATALOG = (
    {"key": "quant", "label": "가격·실적", "available": True, "affects": ["candidate"]},
    {"key": "event", "label": "확인된 소식", "available": True, "affects": ["candidate"]},
    {"key": "entry", "label": "지금 진입", "available": True, "affects": ["timing"]},
    {"key": "macro_release", "label": "경제 발표", "available": True, "affects": ["size", "timing"],
     "reason": "발표 전 예상치·공식 실제값이 둘 다 있을 때만 연구용으로 표시"},
    {"key": "industry_cycle", "label": "반도체 업황", "available": True, "affects": ["candidate"],
     "reason": "세부 분야별 공식 공시·독립 회사 근거가 부족하면 자료 없음"},
    {"key": "portfolio", "label": "내 포트폴리오", "available": False, "reason": "개인 보유·한도와 연결 전"},
)


@dataclass(frozen=True)
class LensResult:
    lens: str
    version: str
    ticker: str
    market: str
    horizon: str
    as_of: str | None
    valid_until: str | None
    verdict: str  # pass | hold | exclude | unavailable
    coverage: float | None
    evidence_ids: list[str]
    source_quality: str
    affects: list[str]
    research_only: bool
    reason: str


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and len(value) >= 10 and value[4] == "-":
        try:
            return datetime.date.fromisoformat(value[:10]).isoformat()
        except ValueError:
            return None
    try:
        return datetime.datetime.fromtimestamp(int(value), ZoneInfo("Asia/Seoul")).date().isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _base(key: str, row: dict, market: str, as_of: str | None, *,
          verdict: str, reason: str, evidence_ids: list[str] | None = None,
          valid_until: str | None = None, source_quality: str = "unverified",
          affects: list[str] | None = None, coverage: float | None = None) -> dict:
    return asdict(LensResult(
        lens=key, version=VERSION, ticker=str(row.get("ticker") or ""), market=market,
        horizon=HORIZON, as_of=as_of, valid_until=valid_until, verdict=verdict,
        coverage=_finite(row.get("data_coverage")) if key == "quant" else coverage,
        evidence_ids=evidence_ids or [], source_quality=source_quality,
        affects=affects or [], research_only=True, reason=reason,
    ))


def _quant(row: dict, market: str, as_of: str | None) -> dict:
    factors = row.get("factor_scores") or {}
    evidence = [f"factor:{key}" for key in sorted(factors) if _finite(factors[key]) is not None]
    if row.get("decision_buy_blocked") or row.get("gate_blocked"):
        return _base("quant", row, market, as_of, verdict="exclude",
                     reason="기본 엔진 안전 조건으로 신규 매수 보류", evidence_ids=evidence,
                     source_quality="engine_gate", affects=["candidate"])
    if not evidence or as_of is None or _finite(row.get("data_coverage")) is None or _finite(row.get("price")) is None:
        return _base("quant", row, market, as_of, verdict="unavailable",
                     reason="가격 또는 정량 근거의 시점·커버리지를 확인할 수 없음",
                     evidence_ids=evidence, source_quality="incomplete")
    kind = row.get("kind")
    if kind in ("BUY", "STRONG_BUY"):
        verdict, reason = "pass", "현재 기본 엔진의 매수권에 있음"
    elif kind in ("SELL", "STRONG_SELL"):
        verdict, reason = "exclude", "현재 기본 엔진은 매도 방향"
    else:
        verdict, reason = "hold", "현재 기본 엔진은 관망"
    return _base("quant", row, market, as_of, verdict=verdict, reason=reason,
                 evidence_ids=evidence, source_quality="bar_date_unverified", affects=["candidate"])


def _event(row: dict, market: str, events: list[dict]) -> dict:
    current = [ev for ev in events if ev.get("ticker") == row.get("ticker")]
    current.sort(key=lambda ev: int(ev.get("detected_at") or 0), reverse=True)
    evidence = [f"event:{ev['id']}" for ev in current if ev.get("id") is not None]
    latest = current[0] if current else {}
    as_of = _date(latest.get("detected_at"))
    valid_until = _date(latest.get("expires_at"))
    quality = str(latest.get("trust_tier") or "unverified")
    if row.get("decision_buy_blocked"):
        return _base("event", row, market, as_of, verdict="exclude",
                     reason="확인된 악재가 기본 엔진의 신규 매수를 차단 중",
                     evidence_ids=evidence, valid_until=valid_until,
                     source_quality=quality if evidence else "engine_decision", affects=["candidate"])
    # confirmed와 투자 판단 사용 승인(decision_eligible)은 다르다. 출처만 확인된
    # 미승인 사건을 통과 근거로 격상하지 않는다.
    eligible = [ev for ev in current if ev.get("decision_eligible") is True]
    negative = next((ev for ev in eligible if ev.get("direction") == "negative"), None)
    if negative:
        return _base("event", row, market, _date(negative.get("detected_at")), verdict="hold",
                     reason="확인된 부정적 사건이 있으나 현재 매수 차단 근거는 아님",
                     evidence_ids=evidence, valid_until=_date(negative.get("expires_at")),
                     source_quality=str(negative.get("trust_tier") or "unverified"), affects=["candidate"])
    positive = next((ev for ev in eligible if ev.get("direction") == "positive"), None)
    if positive:
        return _base("event", row, market, _date(positive.get("detected_at")), verdict="pass",
                     reason="확인된 긍정적 사건 관측(수익 효과는 검증 전)",
                     evidence_ids=evidence, valid_until=_date(positive.get("expires_at")),
                     source_quality=str(positive.get("trust_tier") or "unverified"), affects=["candidate"])
    if current:
        return _base("event", row, market, as_of, verdict="unavailable",
                     reason="사건은 있으나 투자 판단 근거로 승인되지 않음",
                     evidence_ids=evidence, valid_until=valid_until, source_quality=quality)
    return _base("event", row, market, None, verdict="unavailable",
                 reason="확인된 새 사건 없음 — 뉴스 공백은 안전하다는 뜻이 아님",
                 source_quality="no_observation")


def _entry(row: dict, market: str, as_of: str | None) -> dict:
    entry = row.get("entry") or {}
    priced = row.get("priced_in") or {}
    if not entry or as_of is None:
        return _base("entry", row, market, as_of, verdict="unavailable",
                     reason="매수권·발동가 관측이 없어 진입 품질을 평가하지 않음",
                     source_quality="no_entry_observation")
    evidence = ["entry:" + str(entry.get("fire_date"))] if entry.get("fire_date") else []
    if priced.get("flag"):
        return _base("entry", row, market, as_of, verdict="hold",
                     reason="호재 전 상승으로 선반영 의심 — 추격 전 확인 필요",
                     evidence_ids=evidence, source_quality="price_observation", affects=["timing"])
    quality = entry.get("quality")
    if quality in ("fresh", "ok"):
        return _base("entry", row, market, as_of, verdict="pass",
                     reason="발동가 대비 진입 품질이 신선·여유 구간(성과 검증 전)",
                     evidence_ids=evidence, source_quality="price_observation", affects=["timing"])
    if quality in ("extended", "late"):
        return _base("entry", row, market, as_of, verdict="hold",
                     reason="발동가 대비 상승폭이 커 추격 위험 관측",
                     evidence_ids=evidence, source_quality="price_observation", affects=["timing"])
    return _base("entry", row, market, as_of, verdict="unavailable",
                 reason="진입 품질 등급을 해석할 수 없음", source_quality="unknown_grade")


def _macro_release(row: dict, market: str, releases: list[dict], observed_at: int) -> dict:
    decision_time = datetime.datetime.fromtimestamp(observed_at, datetime.timezone.utc)
    candidates = [r for r in releases if r.get("actual_value") is not None
                  and r.get("actual_observed_at")]
    candidates.sort(key=lambda r: r["actual_observed_at"], reverse=True)
    release = candidates[0] if candidates else None
    result = macro_release.evaluate(release, as_of=decision_time)
    if not release:
        return _base("macro_release", row, market, None, verdict="unavailable",
                     reason=result["reason"], source_quality="no_frozen_release", affects=["size", "timing"])
    observed = macro_release._utc(release["actual_observed_at"])
    expires = observed + datetime.timedelta(hours=72)
    if observed > decision_time or decision_time > expires:
        return _base("macro_release", row, market, _date(release["actual_observed_at"]),
                     verdict="unavailable", reason="발표 관측 전이거나 72시간 관찰창이 지남",
                     evidence_ids=["macro_release:" + release["id"]], valid_until=expires.date().isoformat(),
                     source_quality="outside_observation_window", affects=["size", "timing"])
    return _base("macro_release", row, market, _date(release["actual_observed_at"]),
                 verdict=result["verdict"], reason=result["reason"],
                 evidence_ids=["macro_release:" + release["id"]], valid_until=expires.date().isoformat(),
                 source_quality=str(release.get("source_quality") or "unverified"),
                 affects=["size", "timing"])


def _industry_cycle(row: dict, market: str, evidence: list[dict], observed_at: int) -> dict:
    segment = industry_cycle.EXPOSURE.get(str(row.get("ticker") or "").upper())
    relevant = [r for r in evidence if r.get("segment") == segment and r.get("review_verdict") == "approved"]
    assessed = industry_cycle.assess(segment, relevant,
                                     as_of=datetime.datetime.fromtimestamp(observed_at, datetime.timezone.utc))
    used = set(assessed["evidence_ids"])
    dates = [industry_cycle._utc(r["source_published_at"]) for r in relevant
             if f"industry_evidence:{r.get('id')}" in used]
    as_of = max(dates).date().isoformat() if dates else None
    # 충분한 근거로 pass/hold인 경우 가장 먼저 만료되는 문서 기준을 표시한다.
    valid_until = (min(dates) + datetime.timedelta(days=industry_cycle.MAX_AGE_DAYS)).date().isoformat() \
        if dates and assessed["verdict"] != "unavailable" else None
    return _base("industry_cycle", row, market, as_of, verdict=assessed["verdict"],
                 reason=assessed["reason"], evidence_ids=assessed["evidence_ids"],
                 valid_until=valid_until, source_quality=assessed["source_quality"],
                 affects=["candidate"], coverage=assessed["coverage"])


def build_snapshot(rows: list[dict], *, market: str, signal_policy_id: str | None,
                   dates_by: dict[str, list[str]] | None = None,
                   events: list[dict] | None = None,
                   macro_releases: list[dict] | None = None,
                   industry_evidence: list[dict] | None = None,
                   observed_at: int | None = None) -> dict:
    """한 목록의 동일 입력에서 렌즈를 산출한다. rows는 복사본이어야 한다."""
    if market not in ("kr", "us"):
        raise ValueError("unsupported lens market")
    dates_by = dates_by or {}
    events = list(events or [])
    observed_at = int(observed_at if observed_at is not None else
                      datetime.datetime.now(datetime.timezone.utc).timestamp())
    events_by: dict[str, list[dict]] = {}
    for ev in events:
        if isinstance(ev, dict) and ev.get("ticker"):
            events_by.setdefault(str(ev["ticker"]), []).append(ev)
    result_rows = []
    digest_rows = []
    for row in rows:
        ticker = str(row.get("ticker") or "")
        as_of = _date((dates_by.get(ticker) or [None])[-1])
        related = events_by.get(ticker) or []
        results = {
            "quant": _quant(row, market, as_of),
            "event": _event(row, market, related),
            "entry": _entry(row, market, as_of),
            "macro_release": _macro_release(row, market, macro_releases or [], observed_at),
            "industry_cycle": _industry_cycle(row, market, industry_evidence or [], observed_at),
        }
        row["lens_results"] = results
        result_rows.append({"ticker": ticker, "name": row.get("name"), "kind": row.get("kind"),
                            "score": _finite(row.get("score")), "price": _finite(row.get("price")),
                            "rank": row.get("rank"), "lenses": results})
        digest_rows.append({"ticker": ticker, "name": row.get("name"), "kind": row.get("kind"),
                            "score": _finite(row.get("score")), "price": _finite(row.get("price")),
                            "rank": row.get("rank"), "lenses": results})
    material = {"version": VERSION, "market": market, "signal_policy_id": signal_policy_id,
                "rows": digest_rows}
    raw = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                     allow_nan=False).encode()
    snapshot_id = hashlib.sha256(raw).hexdigest()[:24]
    return {"id": snapshot_id, "version": VERSION, "market": market,
            "signal_policy_id": signal_policy_id, "observed_at": observed_at,
            "rows": result_rows, "row_count": len(result_rows), "mode": "read_only",
            "order_eligible": False, "catalog": list(LENS_CATALOG),
            "note": "같은 입력의 관점 비교만 합니다. 기본 시그널·봇·실주문은 바꾸지 않습니다."}
