"""미국 주식(S&P500) 수집 — 유니버스는 datahub CSV, 시세는 KIS 해외주식 API.

KIS 모의계좌로 해외주식 현재가/일봉 조회가 가능함을 실증 확인(2026-07, AAPL). 새 키·yfinance
불필요 — 기존 KIS 인증(broker.kis)을 그대로 재사용한다. 일봉은 호출당 100영업일이라 더 긴
히스토리는 BYMD(기준일)로 페이지네이션한다. 거래소 코드(EXCD)는 종목마다 달라 NAS→NYS→AMS
순으로 탐지한다(결과 캐시).

한국물(KOSPI)과 스케일(통화)이 달라 유니버스·시세는 별도 캐시로 격리한다 — regime/백테스트가
시장을 섞지 않도록. 기술·낙폭 팩터는 스케일 불변이라 그대로 쓰이고, 미국 재무가 없어 저평가·
기본 팩터는 engine이 자동 제외한다(그레이스풀).
"""

from __future__ import annotations

import io
import csv
import logging
import time
import urllib.request

from signal_desk import config
from signal_desk.broker import kis

log = logging.getLogger("signal_desk.ingest.us")

_SP500_CSV = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
_EXCHANGES = ("NAS", "NYS", "AMS")  # KIS EXCD 후보 — 나스닥→뉴욕→아멕스 순 탐지
_TIMEOUT = 20
_PRICE_TR = "HHDFS76240000"       # 해외주식 기간별시세
_PRICE_PATH = "/uapi/overseas-price/v1/quotations/dailyprice"


def sp500_constituents() -> list[dict]:
    """datahub S&P500 구성종목 → [{ticker, name, sector}]. 실패 시 []."""
    try:
        with urllib.request.urlopen(_SP500_CSV, timeout=_TIMEOUT) as resp:
            text = resp.read().decode("utf-8")
    except Exception as e:
        log.error("S&P500 리스트 수집 실패: %s", e)
        return []
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        sym = (row.get("Symbol") or "").strip()
        if not sym:
            continue
        out.append({"ticker": sym.replace(".", "-"),  # BRK.B→BRK-B 등 KIS 심볼 표기
                    "name": (row.get("Security") or sym).strip(),
                    "sector": (row.get("GICS Sector") or "").strip()})
    return out


def _fetch_page(ticker: str, excd: str, creds: dict, bymd: str, retries: int = 3) -> list[dict] | None:
    """단일 페이지(≤100영업일) 조회. bymd='' 면 최신부터. 응답 output2 원자료 반환(None=오류).

    KIS 해외 시세는 간헐적 HTTP 500을 냄(실측 ~1/3 확률) — 짧은 백오프로 재시도한다."""
    for attempt in range(retries):
        body = kis._request(_PRICE_PATH, _PRICE_TR, creds,
                            {"AUTH": "", "EXCD": excd, "SYMB": ticker, "GUBN": "0", "BYMD": bymd, "MODP": "1"})
        if body and body.get("rt_cd") == "0":
            return body.get("output2") or []
        if body is None:  # HTTP 오류(500 등) — 재시도
            time.sleep(0.3 * (attempt + 1))
            continue
        return None  # rt_cd != 0 (정상 응답인데 조회 실패 — 잘못된 거래소/심볼) → 재시도 무의미
    return None


def detect_exchange(ticker: str, creds: dict | None = None) -> str | None:
    """종목의 KIS 거래소코드(NAS/NYS/AMS)를 최신 시세가 잡히는 곳으로 탐지. 못 찾으면 None."""
    creds = creds or config.kis_credentials()
    if not creds:
        return None
    for excd in _EXCHANGES:
        rows = _fetch_page(ticker, excd, creds, "")
        if rows:
            return excd
    return None


def us_ohlcv(ticker: str, creds: dict | None = None, days: int = 400,
             excd: str | None = None) -> list[dict]:
    """미국 종목 일봉(오래된→최신) [{date, close, volume, open}]. 100일씩 BYMD로 페이지네이션.

    excd 미지정 시 거래소 자동 탐지. days만큼 모일 때까지(또는 더 안 나올 때까지) 과거로 이동."""
    creds = creds or config.kis_credentials()
    if not creds:
        return []
    excd = excd or detect_exchange(ticker, creds)
    if not excd:
        log.warning("US 거래소 탐지 실패: %s", ticker)
        return []

    by_date: dict[str, dict] = {}
    bymd = ""
    for _ in range((days // 100) + 2):  # 페이지 상한(무한루프 방지)
        rows = _fetch_page(ticker, excd, creds, bymd)
        if not rows:
            break
        for r in rows:
            d = r.get("xymd")
            clos = r.get("clos")
            if not d or not clos:
                continue
            by_date[d] = {"date": f"{d[:4]}-{d[4:6]}-{d[6:]}", "close": float(clos),
                          "open": float(r.get("open") or clos), "volume": float(r.get("tvol") or 0)}
        oldest = min(rows, key=lambda r: r.get("xymd", "99999999")).get("xymd")
        if not oldest or len(by_date) >= days:
            break
        # 다음 페이지: 가장 오래된 날짜 하루 전을 기준일로
        bymd = oldest
        time.sleep(0.12)  # KIS rate limit 여유
    return [by_date[d] for d in sorted(by_date)]
