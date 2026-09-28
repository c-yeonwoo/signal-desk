"""미 경제 발표 surprise 연구 원장. 예상치는 발표 전 서버 시각으로만 동결한다.

FRED의 월간 FEDFUNDS는 FOMC 발표 목표금리가 아니고, CPIAUCSL의 최신
수정값은 발표 당시 값이 아니다. 둘을 이 원장의 실제값으로 대체하지 않는다.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import math
import re
from urllib.parse import urlparse

VERSION = "macro-release-research-v1"
METRICS = {
    "us_cpi_mom_sa_pct": {"label": "미 CPI 전월비(계절조정)", "unit": "%p", "min": -5.0, "max": 5.0},
    "us_fomc_target_mid_pct": {"label": "FOMC 목표금리 구간 중간값", "unit": "%p", "min": 0.0, "max": 20.0},
}
OFFICIAL_HOSTS = {"www.bls.gov", "bls.gov", "www.federalreserve.gov", "federalreserve.gov"}


def _utc(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("UTC 오프셋이 있는 시각이 필요합니다")
    return parsed.astimezone(dt.timezone.utc)


def _number(value, metric: str) -> float:
    try:
        out = float(value)
    except (ValueError, TypeError):
        raise ValueError("예상·실제 값은 숫자여야 합니다") from None
    spec = METRICS[metric]
    if not math.isfinite(out) or not spec["min"] <= out <= spec["max"]:
        raise ValueError("지표 범위 밖 값입니다")
    return out


def _url(value: str, *, official: bool) -> str:
    parsed = urlparse(str(value or ""))
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("근거는 공개 HTTPS 주소여야 합니다")
    if official and parsed.hostname.lower() not in OFFICIAL_HOSTS:
        raise ValueError("실제값 근거는 BLS 또는 연준 공식 주소여야 합니다")
    if len(value) > 1000:
        raise ValueError("근거 주소가 너무 깁니다")
    return value


def release_id(metric: str, period: str, scheduled_at: str) -> str:
    return hashlib.sha256(f"{metric}|{period}|{_utc(scheduled_at).isoformat()}".encode()).hexdigest()[:24]


def validate_forecast(data: dict, *, observed_at: dt.datetime) -> dict:
    metric = str(data.get("metric") or "")
    if metric not in METRICS:
        raise ValueError("지원하지 않는 발표 지표입니다")
    period = str(data.get("period") or "")
    pattern = r"\d{4}-\d{2}" if metric == "us_cpi_mom_sa_pct" else r"\d{4}-\d{2}-\d{2}"
    if not re.fullmatch(pattern, period):
        raise ValueError("지표 기간 형식이 맞지 않습니다")
    try:
        dt.date.fromisoformat(period + "-01" if metric == "us_cpi_mom_sa_pct" else period)
    except ValueError:
        raise ValueError("실제 존재하는 대상 기간을 입력하세요") from None
    scheduled = _utc(data.get("scheduled_at"))
    observed = observed_at.astimezone(dt.timezone.utc)
    if observed >= scheduled:
        raise ValueError("발표 예정시각 이후에는 예상치를 동결할 수 없습니다")
    return {"id": release_id(metric, period, data["scheduled_at"]), "metric": metric,
            "period": period, "scheduled_at": scheduled.isoformat(),
            "expected_value": _number(data.get("expected_value"), metric),
            "forecast_source_url": _url(str(data.get("forecast_source_url") or ""), official=False),
            "forecast_observed_at": observed.isoformat(), "version": VERSION}


def validate_actual(data: dict, forecast: dict, *, observed_at: dt.datetime) -> dict:
    if not forecast:
        raise ValueError("발표 전 동결된 예상치가 없습니다")
    observed = observed_at.astimezone(dt.timezone.utc)
    scheduled = _utc(forecast["scheduled_at"])
    published = _utc(data.get("source_published_at"))
    if observed < scheduled or published > observed or published < scheduled - dt.timedelta(minutes=15):
        raise ValueError("발표·원천 공개·최초 관측시각의 순서가 맞지 않습니다")
    if data.get("source_checked") is not True:
        raise ValueError("공식 원문의 기간·지표·실제값 수동 대조 확인이 필요합니다")
    return {"release_id": forecast["id"], "actual_value": _number(data.get("actual_value"), forecast["metric"]),
            "source_published_at": published.isoformat(), "actual_observed_at": observed.isoformat(),
            "actual_source_url": _url(str(data.get("actual_source_url") or ""), official=True),
            "source_quality": "operator_attested_official", "version": VERSION}


def evaluate(release: dict | None, *, as_of: dt.datetime | None = None) -> dict:
    """시장 반응/수익 예측이 아니라 수치 surprise 부호만 설명한다."""
    if not release or release.get("actual_value") is None:
        return {"verdict": "unavailable", "surprise_pp": None,
                "reason": "발표 전 예상치와 공식 실제값을 모두 확보하지 못함"}
    decision = (as_of or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    if _utc(release["actual_observed_at"]) > decision:
        return {"verdict": "unavailable", "surprise_pp": None,
                "reason": "해당 판단 시각에는 실제값을 아직 관측하지 못함"}
    delta = round(float(release["actual_value"]) - float(release["expected_value"]), 3)
    if abs(delta) < 0.001:
        verdict, desc = "hold", "예상과 동일"
    elif delta > 0:
        verdict, desc = "hold", "예상보다 높음"
    else:
        verdict, desc = "pass", "예상보다 낮음"
    return {"verdict": verdict, "surprise_pp": delta,
            "reason": f"{METRICS[release['metric']]['label']} {desc} ({delta:+.3f}%p). 시장 반응·수익 효과는 미검증"}
