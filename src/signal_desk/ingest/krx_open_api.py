"""KRX 공식 Open API(data-dbg.krx.co.kr, AUTH_KEY 인증) 클라이언트.

서비스 이용신청 승인 완료(2026-07-02) 후 실응답으로 필드명 검증 완료 —
`sto/stk_bydd_trd` 응답은 `{"OutBlock_1": [{"ISU_CD","ISU_NM","MKT_NM","MKTCAP","LIST_SHRS",...}]}`
형태(추정 필드명 ISU_CD/ISU_NM/MKTCAP가 그대로 맞았음). 우선주(예: "005935 삼성전자우")가
섞여 나오는 것도 확인해 `_is_common_share()`로 걸러낸다(코스피200은 보통주만 편입).

공식 Open API에는 "지수 구성종목"(코스피200 편입종목 리스트) 서비스 자체가 없다(카탈로그
확인됨) — 그래서 시가총액 상위 200종목(보통주만)으로 근사한다. 진짜 편입종목과는 리밸런싱
시점 차이 등으로 다를 수 있음 — 정확한 편입종목이 필요하면 data.krx.co.kr 수동 다운로드가
유일한 경로.
"""

from __future__ import annotations

import datetime
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from signal_desk import config

log = logging.getLogger("signal_desk.ingest.krx_open_api")

BASE = "https://data-dbg.krx.co.kr/svc/apis"
_TIMEOUT = 30

# 실응답 검증 전까지 후보 필드명 목록 — 하나라도 있으면 사용, 전부 없으면 명확히 실패시킴
_CODE_FIELDS = ("ISU_SRT_CD", "ISU_CD", "SRTN_CD")
_NAME_FIELDS = ("ISU_ABBRV", "ISU_NM", "ISU_NM_KOR")
_MKTCAP_FIELDS = ("MKTCAP", "MKT_CAP", "MKTCAP_AMT", "TDD_MKTCAP")


def _request(path: str, params: dict) -> list[dict] | None:
    key = config.krx_key()
    if not key:
        return None
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"{BASE}/{path}?{qs}", headers={"AUTH_KEY": key}
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        log.error("KRX Open API HTTP 오류(%s): %s", path, e)
        return None
    except Exception as e:
        log.error("KRX Open API 요청 실패(%s): %s", path, e)
        return None

    if "respCode" in body:  # {"respCode":"401","respMsg":"..."} 형태의 오류 응답
        log.warning("KRX Open API 오류(%s): %s %s", path, body.get("respCode"), body.get("respMsg"))
        return None

    for key_name in ("OutBlock_1", "output", "block1"):
        if key_name in body:
            return body[key_name]
    log.error("KRX Open API 응답 구조 예상과 다름(%s): 키=%s", path, list(body.keys()))
    return None


def _pick(row: dict, candidates: tuple[str, ...]) -> str | None:
    for c in candidates:
        if c in row:
            return row[c]
    return None


def _is_common_share(code: str) -> bool:
    """KRX 종목코드 마지막 자리 관례: 0=보통주, 5~9(+영문)=우선주 시리즈.
    실응답으로 확인됨(예: 005930 삼성전자 vs 005935 삼성전자우, 001460 BYC vs 001465 BYC우).
    코스피200은 보통주만 편입하므로 우선주는 유니버스에서 제외한다."""
    return len(code) == 6 and code.endswith("0")


def stock_base_info(bas_dd: str) -> list[dict]:
    """유가증권(KOSPI) 종목 기본정보. 서비스 미승인/키 없으면 빈 리스트."""
    rows = _request("sto/stk_isu_base_info", {"basDd": bas_dd})
    return rows or []


def daily_trading(bas_dd: str) -> list[dict]:
    """유가증권(KOSPI) 일별매매정보(시가총액 포함 기대). 서비스 미승인/키 없으면 빈 리스트."""
    rows = _request("sto/stk_bydd_trd", {"basDd": bas_dd})
    return rows or []


def universe_by_marketcap(bas_dd: str, limit: int = 200) -> list[dict]:
    """시가총액 상위 `limit`종목을 코스피200 근사치로 반환. 실패 시 빈 리스트(폴백은 상위 레이어 책임)."""
    trading = daily_trading(bas_dd)
    if not trading:
        return []

    ranked = []
    for row in trading:
        code = _pick(row, _CODE_FIELDS)
        name = _pick(row, _NAME_FIELDS)
        mktcap_raw = _pick(row, _MKTCAP_FIELDS)
        if not code or mktcap_raw is None:
            continue
        if not _is_common_share(code):  # 우선주 제외 — 코스피200은 보통주만 편입
            continue
        try:
            mktcap = float(str(mktcap_raw).replace(",", ""))
        except ValueError:
            continue
        ranked.append({"ticker": code, "name": name or code, "mktcap": mktcap})

    if not ranked:
        log.error(
            "KRX 일별매매정보 응답에서 시가총액 필드를 못 찾음(후보: %s) — 실응답 구조 확인 필요",
            _MKTCAP_FIELDS,
        )
        return []

    ranked.sort(key=lambda r: r["mktcap"], reverse=True)
    return [{"ticker": r["ticker"], "name": r["name"]} for r in ranked[:limit]]


def market_caps(max_lookback_days: int = 5) -> dict[str, float]:
    """최근 영업일 기준 ticker -> 시가총액. PER/PBR 계산용(MKTCAP/순이익, MKTCAP/자본총계).
    키 없음/서비스 미승인/휴장일 연속 등으로 못 구하면 빈 dict(호출부가 그레이스풀하게 처리)."""
    today = datetime.date.today()
    for delta in range(max_lookback_days):
        bas_dd = (today - datetime.timedelta(days=delta)).strftime("%Y%m%d")
        rows = daily_trading(bas_dd)
        if not rows:
            continue
        out = {}
        for row in rows:
            code = _pick(row, _CODE_FIELDS)
            mktcap_raw = _pick(row, _MKTCAP_FIELDS)
            if not code or mktcap_raw is None:
                continue
            try:
                out[code] = float(str(mktcap_raw).replace(",", ""))
            except ValueError:
                continue
        if out:
            return out
    return {}
