"""FRED(미 세인트루이스 연은) 거시 시황 지표 수집 — CPI/기준금리/국채금리/나스닥/VIX.

FRED_API_KEY가 없으면 조용히 빈 값을 반환한다(그레이스풀 폴백). 한국 증시는 미국 물가·금리
발표(CPI·FOMC)와 나스닥 흐름에 강하게 연동되므로, 시장 국면(regime)과 함께 "시황" 판단의
거시 축으로 쓴다 — 개별 종목 팩터가 아니라 시장 전체에 걸리는 오버레이 성격이다.

series_id 참고:
- CPIAUCSL : 미 소비자물가지수(월간, 레벨) — YoY는 12개월 전 대비로 계산
- FEDFUNDS : 연방기금 실효금리(월간) — FOMC 기준금리 흐름
- DGS10    : 미 국채 10년물 금리(일간)
- NASDAQCOM: 나스닥 종합지수(일간)
- VIXCLS   : VIX 변동성지수(일간) — 공포/안도 심리
"""

from __future__ import annotations

import json
import logging
import math
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from signal_desk import config

log = logging.getLogger("signal_desk.ingest.fred")

BASE = "https://api.stlouisfed.org/fred/series/observations"
_TIMEOUT = 20
_MAX_RESPONSE_BYTES = 1024 * 1024

# (series_id, 화면 라벨, 단위, 최근 몇 개 관측을 받아올지 — 월간 시계열은 YoY 위해 넉넉히)
SERIES = [
    ("CPIAUCSL", "미 CPI", "% YoY", 16),
    ("FEDFUNDS", "미 기준금리", "%", 3),
    ("DGS10", "미 10년물", "%", 30),
    ("NASDAQCOM", "나스닥", "", 30),
    ("VIXCLS", "VIX", "", 30),
    ("DEXKOUS", "원/달러", "KRW", 30),
]


def _open(url: str):
    # The API key is in the query string; never follow a redirect to another host.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            return None

    return urllib.request.build_opener(NoRedirect()).open(url, timeout=_TIMEOUT)


def _observations(series_id: str, limit: int) -> list[tuple[str, float]]:
    key = config.fred_key()
    if not key:
        return []
    qs = urllib.parse.urlencode({
        "series_id": series_id,
        "api_key": key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": limit,
    })
    try:
        with _open(f"{BASE}?{qs}") as resp:
            raw = resp.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise ValueError("FRED response over limit")
        body = json.loads(raw)
        if not isinstance(body, dict) or not isinstance(body.get("observations"), list):
            raise ValueError("FRED response shape invalid")
    except Exception as e:
        # HTTP errors can contain the complete query URL, including the API key.
        log.error("FRED 요청 실패(%s): %s", series_id, type(e).__name__)
        return []
    out = []
    for o in body.get("observations", []):
        try:
            value = float(o.get("value"))
            day = str(o["date"])
            datetime.strptime(day, "%Y-%m-%d")
        except (TypeError, ValueError, KeyError, AttributeError):
            continue
        if math.isfinite(value):
            out.append((day, value))
    return out  # 최신 -> 과거 순


def macro_indicators() -> list[dict]:
    """각 시리즈의 최신값 + 변화(모멘텀)를 [{key,label,unit,value,change,dir,asof}]로.

    - CPI는 레벨을 YoY %로 환산해 value에 담는다(발표 관례가 전년동월비).
    - 그 외는 최신값을 value로, change는 직전 관측 대비(금리는 %p, 지수는 %)로 계산한다.
    - dir: +1(상승)/-1(하락)/0 — UI 화살표·색상용. 데이터 없으면 항목 자체를 생략한다.
    """
    out = []
    for series_id, label, unit, limit in SERIES:
        obs = _observations(series_id, limit)
        if not obs:
            continue
        asof, latest = obs[0]

        if series_id == "CPIAUCSL":
            if len(obs) <= 12:
                continue
            year_ago = obs[12][1]
            if latest <= 0 or year_ago <= 0:
                continue
            value = round((latest / year_ago - 1) * 100, 2)
            prev_year_ago = obs[13][1] if len(obs) > 13 else None
            prev = (round((obs[1][1] / prev_year_ago - 1) * 100, 2)
                    if prev_year_ago and prev_year_ago > 0 and obs[1][1] > 0 else None)
            change = round(value - prev, 2) if prev is not None else None
        elif unit == "%":  # 금리: 변화는 %p
            value = round(latest, 2)
            change = round(latest - obs[1][1], 2) if len(obs) > 1 else None
        else:  # 지수(나스닥/VIX): 변화는 % 등락
            value = round(latest, 2)
            change = round((latest / obs[1][1] - 1) * 100, 2) if len(obs) > 1 and obs[1][1] else None

        direction = 0 if not change else (1 if change > 0 else -1)
        out.append({
            "key": series_id,
            "label": label,
            "unit": unit,
            "value": value,
            "change": change,
            "dir": direction,
            "asof": asof,
            "source": "FRED",
            "source_url": f"https://fred.stlouisfed.org/series/{series_id}",
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "source_published_at": None,
            "strict_pit_eligible": False,
        })
    return out
