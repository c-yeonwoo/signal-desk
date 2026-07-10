"""KRX 종목별 공매도 거래 — data.krx.co.kr의 외부용(_OUT) 엔드포인트(iframe 임베드용).

일반 KRX 포털(getJsonData)·pykrx 공매도는 2026 스키마 변경/OTP 게이트로 죽었고, short.krx.co.kr은
로그인 필요. 반면 네이버 '공매도현황'이 임베드하는 iframe이 쓰는 `MDCSTAT30001_OUT` bld는 로그인/
OTP 없이 열려 있어 이걸 쓴다(표준 라이브러리 urllib만). 공매도 거래량은 주수라 스케일 시세와 무관.

반환은 {날짜: 공매도거래량(주)} — store가 우리 일별 총거래량과 조인해 '공매도 거래비중'으로 정규화.
"""

from __future__ import annotations

import datetime
import http.cookiejar
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("signal_desk.ingest.krx_short")

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"
_BASE = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
_REF = "https://data.krx.co.kr/comm/srt/srtLoader/index.cmd?screenId=MDCSTAT300&isuCd=005930"
_BLD = "dbms/MDC_OUT/STAT/srt/MDCSTAT30001_OUT"
_TIMEOUT = 15
_opener: urllib.request.OpenerDirector | None = None


def _isin(code: str) -> str:
    """6자리 종목코드 → 표준 ISIN(KR7+코드+00+체크숫자). 체크숫자는 ISIN 알고리즘(문자→숫자 확장 후
    Luhn mod10). 삼성전자 005930→KR7005930003 등 실검증됨."""
    base = "KR7" + code + "00"
    expanded = "".join(str(ord(c) - 55) if c.isalpha() else c for c in base)
    total = 0
    for i, ch in enumerate(reversed(expanded)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return base + str((10 - total % 10) % 10)


def _session() -> urllib.request.OpenerDirector:
    """쿠키(JSESSIONID) 세션을 1회 확보해 재사용. getJsonData는 srtLoader 세션 쿠키를 요구한다."""
    global _opener
    if _opener is not None:
        return _opener
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", _UA)]
    try:
        op.open(_REF, timeout=_TIMEOUT).read()
    except Exception as e:
        log.warning("KRX 공매도 세션 확보 실패: %s", type(e).__name__)
    _opener = op
    return op


def _num(s) -> float:
    try:
        return float(str(s).replace(",", "").strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def short_volume(code: str, days: int = 20) -> dict[str, float] | None:
    """종목의 최근 days 거래일 일별 공매도 거래량(주). {'YYYY-MM-DD': vol}. 실패/무자료 시 None.

    한도 여유를 위해 달력일 기준 넉넉히 조회창을 잡고 최신 days개만 반환한다(거래일 근사)."""
    op = _session()
    today = datetime.date.today()
    start = today - datetime.timedelta(days=days * 2 + 15)  # 거래일 days개 확보용 여유창(주말·휴장)
    params = {
        "bld": _BLD, "locale": "ko_KR", "isuCd": _isin(code),
        "strtDd": start.strftime("%Y%m%d"), "endDd": today.strftime("%Y%m%d"),
        "share": "1", "money": "1", "csvxls_isNo": "false",
    }
    req = urllib.request.Request(
        _BASE, data=urllib.parse.urlencode(params).encode(),
        headers={"User-Agent": _UA, "Referer": _REF, "X-Requested-With": "XMLHttpRequest",
                 "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                 "Accept": "application/json"})
    try:
        with op.open(req, timeout=_TIMEOUT) as r:
            body = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        log.warning("KRX 공매도 실패(%s): HTTP %s", code, e.code)
        return None
    except Exception as e:
        log.warning("KRX 공매도 실패(%s): %s", code, type(e).__name__)
        return None
    if not body.strip().startswith("{"):
        return None
    try:
        rows = json.loads(body).get("OutBlock_1") or []
    except json.JSONDecodeError:
        return None
    if not rows:
        return None
    out: dict[str, float] = {}
    for row in rows:
        dd = str(row.get("TRD_DD", "")).replace("/", "-")
        if len(dd) == 10:
            out[dd] = _num(row.get("CVSRTSELL_TRDVOL"))
    if not out:
        return None
    # 최신 days개만(날짜 내림차순 상위)
    latest = dict(sorted(out.items(), reverse=True)[:days])
    return latest
