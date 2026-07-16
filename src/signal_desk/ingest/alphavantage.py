"""Alpha Vantage OVERVIEW — 미국 종목의 발행주식수·PER·섹터. 시총순 정렬·US PER 활성화용.

무료 티어가 하루 25콜·초당 ~1콜로 매우 빠듯하다 → 발행주식수(거의 고정)를 한 번 캐시해 두고,
시총은 매일 `주식수 × 현재가`로 무료 재계산한다(store에서). 이 모듈은 backfill(신규 종목만 소량씩)
전용. 스로틀/한도 초과 응답은 조용히 스킵(다음 실행에서 이어서 채움)."""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request

from signal_desk import config

log = logging.getLogger("signal_desk.ingest.alphavantage")

_URL = "https://www.alphavantage.co/query"
_TIMEOUT = 15


def overview(ticker: str) -> dict | None:
    """단일 종목 개요. 반환: {shares, per, sector, name} 또는 None(키 없음·한도·실패).
    한도 초과 시 AV가 'Note'/'Information' 필드를 주는데, 그 경우 None으로 처리한다."""
    key = config.alphavantage_key()
    if not key:
        return None
    qs = urllib.parse.urlencode({"function": "OVERVIEW", "symbol": ticker, "apikey": key})
    try:
        with urllib.request.urlopen(f"{_URL}?{qs}", timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log.warning("AV overview 실패(%s): %s", ticker, type(e).__name__)
        return None
    if not data or "Symbol" not in data:  # Note/Information(스로틀) 또는 빈 응답
        if data.get("Note") or data.get("Information"):
            log.info("AV 한도/스로틀 — %s 스킵", ticker)
        return None

    def _num(k):
        v = data.get(k)
        try:
            return float(v) if v not in (None, "", "None", "-") else None
        except (TypeError, ValueError):
            return None

    return {"shares": _num("SharesOutstanding"), "per": _num("PERatio"),
            "sector": data.get("Sector") or None, "name": data.get("Name") or None,
            "industry": data.get("Industry") or None,
            "description": (data.get("Description") or "").strip() or None}  # 영문 사업설명(사업 개요 요약용)


def earnings_calendar(horizon: str = "3month") -> dict[str, str]:
    """미국 종목 실적발표 '예정일' — EARNINGS_CALENDAR(CSV 벌크 1콜로 전 종목). 반환: {symbol: 'YYYY-MM-DD'}
    (종목별 가장 이른 예정일). 키 없음·스로틀·비CSV 응답이면 {} (그레이스풀). 무료 티어도 지원."""
    key = config.alphavantage_key()
    if not key:
        return {}
    qs = urllib.parse.urlencode({"function": "EARNINGS_CALENDAR", "horizon": horizon, "apikey": key})
    try:
        with urllib.request.urlopen(f"{_URL}?{qs}", timeout=_TIMEOUT) as resp:
            text = resp.read().decode("utf-8", "replace")
    except Exception as e:
        log.warning("AV earnings_calendar 실패: %s", type(e).__name__)
        return {}
    lines = [ln for ln in text.splitlines() if ln.strip()]
    # 정상 응답은 CSV(헤더: symbol,name,reportDate,...). 스로틀 시 JSON({"Information":...}) → 헤더 불일치
    if not lines or not lines[0].lower().startswith("symbol"):
        log.info("AV earnings_calendar 한도/비정상 응답 — 스킵")
        return {}
    out: dict[str, str] = {}
    for ln in lines[1:]:
        cols = ln.split(",")
        if len(cols) < 3:
            continue
        sym, date = cols[0].strip(), cols[2].strip()
        if not sym or len(date) != 10:
            continue
        if sym not in out or date < out[sym]:  # 종목별 가장 이른 예정일
            out[sym] = date
    return out
