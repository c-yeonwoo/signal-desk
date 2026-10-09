"""KIS Developers API — 모의투자 자동매매(BACKLOG #7). 인증/잔고조회/주문(현금).

환경은 demo/real만 허용한다. real은 조회 전용이며 이 빌드에서는 주문 전송을 차단한다.
환경변수나 기존 페이퍼 봇 활성화로 실주문을 열 수 없다.

실키로 검증됨(2026-07-02): 인증 성공, 계좌번호+상품코드("01") 조합으로 잔고조회 정상 응답 확인.

⚠️ 토큰 발급(oauth2/tokenP)에 엄격한 rate limit이 있음을 실제로 확인함(짧은 간격 재요청 시
HTTP 403). 그래서 토큰은 반드시 파일 캐시로 재사용해야 한다(1일 유효) — `get_token()`을 거치지
않고 직접 발급 API를 호출하지 말 것.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

from signal_desk import config

log = logging.getLogger("signal_desk.broker.kis")

_BASE = {
    "demo": "https://openapivts.koreainvestment.com:29443",
    "real": "https://openapi.koreainvestment.com:9443",
}
_TR_ID = {
    ("demo", "buy"): "VTTC0012U", ("demo", "sell"): "VTTC0011U", ("demo", "balance"): "VTTC8434R",
    ("real", "buy"): "TTTC0012U", ("real", "sell"): "TTTC0011U", ("real", "balance"): "TTTC8434R",
}
_TIMEOUT = 8  # KIS 미도달 시 오래 매달리지 않도록(대시보드 응답성). 실주문 경로는 재시도로 보완.
_TOKEN_FILE = Path("data/cache/kis_token.json")
_TOKEN_LOCK = threading.Lock()


def _validate_credentials(creds: dict) -> None:
    if creds.get("env") not in _BASE:
        raise ValueError("KIS_ENV must be demo or real")
    if any(not creds.get(key) for key in ("app_key", "app_secret", "account_no", "product_cd")):
        raise ValueError("incomplete KIS credentials")


def _token_path(creds: dict) -> Path:
    _validate_credentials(creds)
    identity = json.dumps([creds[k] for k in ("env", "app_key", "app_secret", "account_no", "product_cd")])
    fingerprint = hashlib.sha256(identity.encode()).hexdigest()
    return _TOKEN_FILE.with_name(f"kis_token_{fingerprint}.json")


def _load_cached_token(creds: dict | None = None) -> str | None:
    creds = creds or config.kis_credentials()
    if not creds:
        return None
    path = _token_path(creds)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("expires_at", 0) > time.time() + 60:  # 60초 여유
            return data["token"]
    except Exception:
        pass
    return None


def _save_token(token: str, expires_at: float, creds: dict) -> None:
    path = _token_path(creds)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 키/계좌별 토큰 격리, 소유자만 읽는 원자적 파일 교체. 기존 무구분 토큰은 재사용하지 않는다.
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".kis-token-", delete=False) as f:
        json.dump({"token": token, "expires_at": expires_at}, f)
        tmp = f.name
    os.replace(tmp, path)


def get_token(creds: dict | None = None) -> str | None:
    with _TOKEN_LOCK:
        return _get_token(creds)


def _get_token(creds: dict | None = None) -> str | None:
    """캐시된 토큰을 우선 재사용, 없거나 만료 임박이면 새로 발급."""
    creds = creds or config.kis_credentials()
    if not creds:
        return None
    _validate_credentials(creds)
    cached = _load_cached_token(creds)
    if cached:
        return cached

    base = _BASE[creds["env"]]
    body = json.dumps({
        "grant_type": "client_credentials", "appkey": creds["app_key"], "appsecret": creds["app_secret"],
    }).encode()
    req = urllib.request.Request(f"{base}/oauth2/tokenP", data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        log.error("KIS 토큰 발급 실패: %s", type(e).__name__)
        return None

    token = data.get("access_token")
    if not token:
        log.error("KIS 토큰 응답에 access_token 없음")
        return None
    try:
        expires_at = datetime.datetime.strptime(
            data["access_token_token_expired"], "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=ZoneInfo("Asia/Seoul")).timestamp()
    except Exception:
        expires_at = time.time() + 23 * 3600  # 파싱 실패 시 보수적 기본값(23시간)
    _save_token(token, expires_at, creds)
    return token


def _request(path: str, tr_id: str, creds: dict, params: dict, method: str = "GET", *, tr_cont: str = "") -> dict | None:
    _validate_credentials(creds)
    if creds["env"] == "real" and method != "GET":
        raise PermissionError("실계좌는 조회 전용입니다. 실주문 전송 경로는 잠겨 있습니다.")
    token = get_token(creds)
    if not token:
        return None
    base = _BASE[creds["env"]]
    headers = {
        "authorization": f"Bearer {token}", "appkey": creds["app_key"], "appsecret": creds["app_secret"],
        "tr_id": tr_id, "custtype": "P", "Content-Type": "application/json; charset=utf-8",
        "tr_cont": tr_cont,
    }
    try:
        if method == "GET":
            qs = urllib.parse.urlencode(params)
            req = urllib.request.Request(f"{base}{path}?{qs}", headers=headers)
        else:
            req = urllib.request.Request(
                f"{base}{path}", data=json.dumps(params).encode(), headers=headers, method="POST"
            )
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            body = json.loads(resp.read().decode())
            body["_tr_cont"] = resp.headers.get("tr_cont", "")
            return body
    except urllib.error.HTTPError as e:
        log.error("KIS API HTTP 오류(%s): %s", path, e.code)
        return None
    except Exception as e:
        log.error("KIS API 요청 실패(%s): %s", path, type(e).__name__)
        return None


def _read_all(path: str, tr_id: str, creds: dict, params: dict) -> dict | None:
    """누락된 뒷 페이지를 빈 보유/미체결로 오인하지 않도록 전체 조회 또는 실패."""
    rows, seen, continuation = [], set(), ""
    params = dict(params)
    for _ in range(20):
        body = (_request(path, tr_id, creds, params, tr_cont=continuation) if continuation
                else _request(path, tr_id, creds, params))
        if not body or body.get("rt_cd") != "0" or not isinstance(body.get("output1"), list):
            return None
        rows.extend(body["output1"])
        if body.get("_tr_cont") not in ("M", "F"):
            return {**body, "output1": rows, "complete": True}
        cursor = (body.get("ctx_area_fk100"), body.get("ctx_area_nk100"))
        if not all(isinstance(v, str) for v in cursor) or not any(v.strip() for v in cursor) or cursor in seen:
            return None
        seen.add(cursor)
        params.update(CTX_AREA_FK100=cursor[0], CTX_AREA_NK100=cursor[1])
        continuation = "N"
    return None


def domestic_market_snapshot(ticker: str, creds: dict | None = None) -> dict | None:
    """조회 전용 국내 현재가·누적 거래량. 체결시각/호가로 오인하지 않는다.

    KIS 공식 주식현재가 시세 FHKST01010100. 공급자 응답 시각의 의미가
    확인되지 않았으므로 서버 수신시각만 기록하고 주문 근거로 사용하지 않는다.
    """
    if not isinstance(ticker, str) or not re.fullmatch(r"[0-9][0-9A-Z]{5}", ticker):
        return None
    creds = creds or config.kis_credentials()
    if not creds or creds.get("env") != "real":
        return None
    body = _request("/uapi/domestic-stock/v1/quotations/inquire-price", "FHKST01010100", creds,
                    {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker})
    if not body or body.get("rt_cd") != "0" or not isinstance(body.get("output"), dict):
        return None
    row = body["output"]
    try:
        price, volume = float(row["stck_prpr"]), int(row["acml_vol"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(price) or price <= 0 or volume < 0:
        return None
    try:
        day_high = float(row.get("stck_hgpr"))
        if not math.isfinite(day_high) or day_high < price:
            day_high = None
    except (TypeError, ValueError, OverflowError):
        day_high = None
    return {"price": price, "cumulative_volume": volume, "received_at": int(time.time()),
            "day_high": day_high, "provider": "kis", "source_time_verified": False}


def domestic_investor_estimate(ticker: str, creds: dict | None = None) -> dict | None:
    """KIS 외인·기관 *가집계*. 발표 슬롯이 드물고 확정 수급이 아니므로 설명 전용."""
    if not isinstance(ticker, str) or not re.fullmatch(r"[0-9][0-9A-Z]{5}", ticker):
        return None
    creds = creds or config.kis_credentials()
    if not creds or creds.get("env") != "real":
        return None
    body = _request("/uapi/domestic-stock/v1/quotations/investor-trend-estimate",
                    "HHPTJ04160200", creds, {"MKSC_SHRN_ISCD": ticker})
    if not body or body.get("rt_cd") != "0" or not isinstance(body.get("output2"), list):
        return None
    for row in body["output2"]:
        if not isinstance(row, dict):
            continue
        try:
            foreign = int(row["frgn_fake_ntby_qty"])
            institution = int(row["orgn_fake_ntby_qty"])
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
        return {"foreign_estimate_qty": foreign, "institution_estimate_qty": institution,
                "provider_slot": row.get("bsop_hour_gb"), "received_at": int(time.time()),
                "source_verified": False, "estimate_only": True, "provider": "kis"}
    return None


def domestic_completed_minute_volumes(ticker: str, creds: dict | None = None,
                                      *, now: datetime.datetime | None = None) -> dict | None:
    """오늘 완료된 연속 10개 분봉의 앞/뒤 5분 거래량. 누락 분봉은 0으로 채우지 않는다."""
    if not isinstance(ticker, str) or not re.fullmatch(r"[0-9][0-9A-Z]{5}", ticker):
        return None
    creds = creds or config.kis_credentials()
    if not creds or creds.get("env") != "real":
        return None
    if now is not None and (now.tzinfo is None or now.utcoffset() is None):
        return None
    observed = (now or datetime.datetime.now(ZoneInfo("Asia/Seoul"))).astimezone(ZoneInfo("Asia/Seoul"))
    body = _request("/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
                    "FHKST03010200", creds,
                    {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker,
                     "FID_INPUT_HOUR_1": observed.strftime("%H%M%S"),
                     "FID_PW_DATA_INCU_YN": "N", "FID_ETC_CLS_CODE": ""})
    if not body or body.get("rt_cd") != "0" or not isinstance(body.get("output2"), list):
        return None
    day = observed.strftime("%Y%m%d")
    bars: dict[datetime.datetime, int] = {}
    for row in body["output2"]:
        if not isinstance(row, dict):
            continue
        try:
            stamp = datetime.datetime.strptime(day + str(row["stck_cntg_hour"]),
                                               "%Y%m%d%H%M%S").replace(tzinfo=ZoneInfo("Asia/Seoul"))
            volume = int(row["cntg_vol"])
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
        if stamp.second == 0 and volume >= 0 and stamp < observed.replace(second=0, microsecond=0):
            bars[stamp] = volume
    ordered = sorted(bars.items())[-10:]
    if len(ordered) != 10 or (observed - ordered[-1][0]).total_seconds() > 150:
        return None
    if any(int((ordered[i][0] - ordered[i-1][0]).total_seconds()) != 60 for i in range(1, 10)):
        return None
    prior = sum(volume for _, volume in ordered[:5])
    recent = sum(volume for _, volume in ordered[5:])
    return {"previous_5m_volume": prior, "recent_5m_volume": recent,
            "ratio": round(recent / prior, 4) if prior > 0 else None,
            "last_complete_minute": ordered[-1][0].isoformat(),
            "received_at": int(time.time()) if now is None else int(observed.timestamp()), "provider": "kis",
            "source_time_verified": False, "complete_bars": 10}


def domestic_rank_watchlist(creds: dict | None = None) -> dict:
    """공식 KIS 순위 첫 페이지만 조회한다. 거래 시각·전수 시장·주문 근거가 아니다.

    등락률/거래량 증가 순위가 반환한 종목코드만 다음 시세 주기에 관찰한다.
    응답 원문 시각을 확인할 수 없으므로 서버 수신 시각을 별도로 남긴다.
    """
    creds = creds or config.kis_credentials()
    if not creds or creds.get("env") != "real":
        return {"status": "unavailable", "candidates": [], "sources": {}}
    queries = (
        ("price_rank", "/uapi/domestic-stock/v1/ranking/fluctuation", "FHPST01700000", {
            "FID_COND_MRKT_DIV_CODE": "J", "FID_COND_SCR_DIV_CODE": "20170",
            "FID_INPUT_ISCD": "0000", "FID_RANK_SORT_CLS_CODE": "0000", "FID_INPUT_CNT_1": "30",
            "FID_PRC_CLS_CODE": "0", "FID_INPUT_PRICE_1": "0", "FID_INPUT_PRICE_2": "1000000",
            "FID_VOL_CNT": "0", "FID_TRGT_CLS_CODE": "0", "FID_TRGT_EXLS_CLS_CODE": "0",
            "FID_DIV_CLS_CODE": "1", "FID_RSFL_RATE1": "0", "FID_RSFL_RATE2": "30"}),
        ("volume_rank", "/uapi/domestic-stock/v1/quotations/volume-rank", "FHPST01710000", {
            "FID_COND_MRKT_DIV_CODE": "J", "FID_COND_SCR_DIV_CODE": "20171",
            "FID_INPUT_ISCD": "0000", "FID_DIV_CLS_CODE": "1", "FID_BLNG_CLS_CODE": "1",
            "FID_TRGT_CLS_CODE": "111111111", "FID_TRGT_EXLS_CLS_CODE": "0000000000",
            "FID_INPUT_PRICE_1": "0", "FID_INPUT_PRICE_2": "1000000",
            "FID_VOL_CNT": "0", "FID_INPUT_DATE_1": ""}),
    )
    candidates: list[str] = []
    sources: dict[str, dict] = {}
    for name, path, tr_id, params in queries:
        body = _request(path, tr_id, creds, params)
        if not body or body.get("rt_cd") != "0" or not isinstance(body.get("output"), list):
            sources[name] = {"status": "failed", "rows": 0}
            continue
        rows = body["output"]
        count = 0
        selected = 0
        for row in rows[:30]:
            if not isinstance(row, dict):
                continue
            ticker = row.get("stck_shrn_iscd") or row.get("mksc_shrn_iscd")
            if not isinstance(ticker, str) or not re.fullmatch(r"[0-9]{6}", ticker):
                continue
            count += 1
            if ticker not in candidates and selected < 10:
                candidates.append(ticker)
                selected += 1
        sources[name] = {"status": "first_page_only" if body.get("_tr_cont") in ("M", "F") else "ok",
                         "rows": len(rows), "valid_codes": count, "selected": selected}
    successes = sum(value["status"] != "failed" for value in sources.values())
    return {"status": "observed" if successes == 2 else "partial" if successes else "failed",
            "candidates": candidates, "sources": sources, "received_at": int(time.time()),
            "source_time_verified": False, "research_only": True}


def domestic_historical_minute_probe(ticker: str, session: str,
                                     creds: dict | None = None) -> dict:
    """과거 1분봉 첫 120행만 읽는다. 보호 구간 수익 계산·원장 역기입은 하지 않는다."""
    if not isinstance(ticker, str) or not re.fullmatch(r"[0-9]{6}", ticker):
        return {"status": "invalid_ticker", "bars": []}
    try:
        day = datetime.datetime.strptime(session, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return {"status": "invalid_session", "bars": []}
    today = datetime.datetime.now(ZoneInfo("Asia/Seoul")).date()
    if day > datetime.date(2026, 8, 4) or not 0 <= (today - day).days <= 365:
        return {"status": "outside_development_window", "bars": []}
    creds = creds or config.kis_credentials()
    if not creds or creds.get("env") != "real":
        return {"status": "unavailable", "bars": []}
    body = _request("/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice",
                    "FHKST03010230", creds,
                    {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker,
                     "FID_INPUT_HOUR_1": "153000", "FID_INPUT_DATE_1": day.strftime("%Y%m%d"),
                     "FID_PW_DATA_INCU_YN": "Y", "FID_FAKE_TICK_INCU_YN": ""})
    if not body or body.get("rt_cd") != "0" or not isinstance(body.get("output2"), list):
        return {"status": "provider_error", "bars": []}
    bars = []
    invalid = 0
    source_dates = set()
    for row in body["output2"][:120]:
        if not isinstance(row, dict):
            invalid += 1
            continue
        try:
            hour = str(row["stck_cntg_hour"])
            stamp = datetime.datetime.strptime(day.strftime("%Y%m%d") + hour, "%Y%m%d%H%M%S")
            price = float(row["stck_prpr"])
            volume = int(row["cntg_vol"])
        except (KeyError, TypeError, ValueError, OverflowError):
            invalid += 1
            continue
        reported_date = row.get("stck_bsop_date")
        if reported_date is not None:
            source_dates.add(str(reported_date))
            if str(reported_date) != day.strftime("%Y%m%d"):
                invalid += 1
                continue
        if not (9 <= stamp.hour <= 15) or (stamp.hour == 15 and stamp.minute > 30):
            invalid += 1
            continue
        if not math.isfinite(price) or price <= 0 or volume < 0:
            invalid += 1
            continue
        bars.append({"time": stamp.strftime("%H:%M:%S"), "price": price, "volume": volume,
                     "date_reported": reported_date is not None})
    bars = sorted({bar["time"]: bar for bar in bars}.values(), key=lambda bar: bar["time"])
    return {"status": "observed" if bars else "empty_or_invalid", "ticker": ticker,
            "session": session, "bars": bars, "raw_rows": len(body["output2"]),
            "invalid_rows": invalid, "date_attested_by_rows": bool(bars) and all(
                bar["date_reported"] for bar in bars) and source_dates == {day.strftime("%Y%m%d")},
            "bars_sha256": hashlib.sha256(json.dumps(bars, sort_keys=True,
                                                    separators=(",", ":")).encode()).hexdigest(),
            "first_time": bars[0]["time"] if bars else None,
            "last_time": bars[-1]["time"] if bars else None,
            "first_page_only": True, "source_time_verified": False,
            "source": "kis:inquire-time-dailychartprice", "research_only": True}


def balance(creds: dict | None = None, retries: int = 3) -> dict | None:
    """예수금(현금)·총평가금액·보유종목. 실패 시 None. retries=1이면 fail-fast(표시용 — 매매는 3회)."""
    creds = creds or config.kis_credentials()
    if not creds:
        return None
    tr_id = _TR_ID[(creds["env"], "balance")]
    params = {
        "CANO": creds["account_no"], "ACNT_PRDT_CD": creds["product_cd"],
        "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01",
        "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "01",
        "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
    }
    body = None
    for attempt in range(max(1, retries)):  # KIS 간헐 500 대비 재시도(표시용은 1회)
        body = _read_all("/uapi/domestic-stock/v1/trading/inquire-balance", tr_id, creds, params)
        if body and body.get("rt_cd") == "0":
            break
        if attempt < retries - 1:
            time.sleep(0.5)
    if not body or body.get("rt_cd") != "0":
        log.error("KIS 잔고조회 실패: %s", body.get("msg1") if body else "응답 없음")
        return None

    holdings = [
        {
            "ticker": h["pdno"], "name": h["prdt_name"],
            "qty": int(h["hldg_qty"]), "avg_price": float(h["pchs_avg_pric"]),
            "price": float(h.get("prpr") or 0),               # 현재가
            "pnl_pct": float(h.get("evlu_pfls_rt") or 0),     # 평가손익률(%)
            "sellable_qty": int(h["ord_psbl_qty"]) if h.get("ord_psbl_qty") not in (None, "") else None,
        }
        for h in body.get("output1", []) if int(h.get("hldg_qty", 0)) > 0
    ]
    summaries = body.get("output2")
    if not isinstance(summaries, list) or not summaries or not isinstance(summaries[0], dict):
        return None
    summary = summaries[0]
    required = ("dnca_tot_amt", "evlu_amt_smtl_amt")
    if any(summary.get(k) in (None, "") for k in required) or not any(
        summary.get(k) not in (None, "") for k in ("nass_amt", "tot_evlu_amt")
    ):
        return None

    def _f(key: str) -> float:
        return float(summary.get(key, 0) or 0)

    # 손익·현금은 KIS 자체 집계로 계산(클라이언트 산술 착오 방지):
    #  - total_eval = 순자산(nass_amt) = 가용현금 + 유가증권평가
    #  - 총손익률 = 평가손익합계 / 매입금액합계
    #  - 가용현금 = 순자산 − 유가증권평가 (모의계좌 dnca_tot_amt가 매수 후에도 안 줄어드는
    #    quirk가 있어 순자산에서 역산하는 게 정합적 — 봇 매수여력도 이 값을 써야 과대추정 방지)
    net_asset = _f("nass_amt") or _f("tot_evlu_amt")
    stock_eval = _f("evlu_amt_smtl_amt")
    invested = _f("pchs_amt_smtl_amt")   # 매입금액합계
    pnl = _f("evlu_pfls_smtl_amt")       # 평가손익합계
    free_cash = round(net_asset - stock_eval) if net_asset else _f("dnca_tot_amt")
    if not all(math.isfinite(v) for v in (net_asset, stock_eval, invested, pnl, free_cash)):
        return None
    return {
        "cash": max(0.0, free_cash),                 # 가용현금(순자산−유가증권평가)
        "deposit_raw": _f("dnca_tot_amt"),           # KIS 예수금총금액(참고)
        "total_eval": net_asset,                     # 총평가금액(순자산)
        "stock_eval": stock_eval,                    # 유가증권 평가금액
        "invested": invested,                        # 매입금액합계
        "pnl": pnl,                                  # 평가손익합계
        "pnl_pct": round(pnl / invested * 100, 2) if invested else None,  # 실제 총손익률
        "holdings": holdings,
        "complete": True, "cash_is_buying_power": False,
    }


_US_EXCHANGES = ("NASD", "NYSE", "AMEX")  # 해외 잔고조회 거래소코드(미국)


def _pick(d: dict, *keys, default=0.0) -> float:
    """후보 필드명 중 먼저 잡히는 값을 float로. KIS 해외 응답 필드명이 문서·버전마다 달라 방어적."""
    for k in keys:
        if k in d and str(d[k]).strip() not in ("", "0"):
            try:
                return float(str(d[k]).replace(",", ""))
            except ValueError:
                continue
    return default


def overseas_balance(creds: dict | None = None) -> dict | None:
    """미국 주식 잔고(USD) — 예수금·평가·손익·보유종목. 거래소별(NASD/NYSE/AMEX) 조회 후 병합.
    KIS 미도달/실패 시 None(호출부가 빈 상태로 처리). 필드명은 방어적으로 후보 매칭.

    ⚠️ US 실주문·잔고 필드는 미국장 개장 중 실응답으로 최종 검증 예정(현 환경 KIS 도메인 차단)."""
    creds = creds or config.kis_credentials()
    if not creds:
        return None
    tr = "VTTS3012R" if creds["env"] == "demo" else "TTTS3012R"
    holdings, cash, invested, pnl = [], 0.0, 0.0, 0.0
    reached = False
    for excd in _US_EXCHANGES:
        params = {"CANO": creds["account_no"], "ACNT_PRDT_CD": creds["product_cd"],
                  "OVRS_EXCG_CD": excd, "TR_CRCY_CD": "USD", "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""}
        body = _request("/uapi/overseas-stock/v1/trading/inquire-balance", tr, creds, params)
        if not body or body.get("rt_cd") != "0":
            continue
        reached = True
        for h in body.get("output1", []):
            qty = int(_pick(h, "ovrs_cblc_qty", "cblc_qty"))
            if qty <= 0:
                continue
            holdings.append({"ticker": (h.get("ovrs_pdno") or h.get("pdno") or "").strip(),
                             "name": (h.get("ovrs_item_name") or "").strip(),
                             "qty": qty, "avg_price": _pick(h, "pchs_avg_pric"),
                             "price": _pick(h, "now_pric2", "ovrs_now_pric1")})
        o2 = body.get("output2")
        summ = (o2[0] if isinstance(o2, list) and o2 else o2) or {}
        if isinstance(summ, dict):
            cash += _pick(summ, "frcr_dncl_amt_2", "frcr_dncl_amt1", "frcr_dncl_amt")
            invested += _pick(summ, "frcr_pchs_amt1", "frcr_buy_amt_smtl1")
            pnl += _pick(summ, "ovrs_tot_pfls", "tot_evlu_pfls_amt")
    if not reached:
        return None  # KIS 미도달
    stock_eval = sum(h["qty"] * h["price"] for h in holdings)
    return {"cash": cash, "stock_eval": round(stock_eval, 2), "invested": invested, "pnl": pnl,
            "total_eval": round(cash + stock_eval, 2),
            "pnl_pct": round(pnl / invested * 100, 2) if invested else None, "holdings": holdings}


def current_price(ticker: str, creds: dict | None = None) -> float | None:
    """국내 종목 실시간 현재가. 봇이 장중 청산·진입 판단 시점에 조회(캐시 종가와의 갭 대응).
    실패 시 None(호출부가 캐시 종가로 폴백). 간헐 500 대비 재시도."""
    creds = creds or config.kis_credentials()
    if not creds:
        return None
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker}
    for attempt in range(3):
        body = _request("/uapi/domestic-stock/v1/quotations/inquire-price", "FHKST01010100", creds, params)
        if body and body.get("rt_cd") == "0":
            try:
                return float(body["output"]["stck_prpr"])
            except (KeyError, TypeError, ValueError):
                return None
        time.sleep(0.3)
    return None


def place_order(
    ticker: str, side: str, qty: int, price: float | None = None, creds: dict | None = None
) -> dict | None:
    """side: 'buy'|'sell'. price=None이면 시장가(ORD_DVSN=01), 지정하면 지정가(00). 실패 시 None."""
    if side not in ("buy", "sell"):
        raise ValueError("side must be 'buy' or 'sell'")
    creds = creds or config.kis_credentials()
    if not creds:
        return None

    _validate_credentials(creds)
    if creds["env"] == "real":
        raise PermissionError("실계좌는 조회 전용입니다. 실주문 전송 경로는 잠겨 있습니다.")
    if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
        raise ValueError("qty must be a positive integer")
    if price is not None and (not math.isfinite(price) or price <= 0 or price != int(price)):
        raise ValueError("price must be a positive integer KRW price")
    tr_id = _TR_ID[(creds["env"], side)]
    params = {
        "CANO": creds["account_no"], "ACNT_PRDT_CD": creds["product_cd"],
        "PDNO": ticker, "ORD_DVSN": "01" if price is None else "00",
        "ORD_QTY": str(qty), "ORD_UNPR": str(int(price)) if price is not None else "0",
        "EXCG_ID_DVSN_CD": "KRX", "SLL_TYPE": "01" if side == "sell" else "", "CNDT_PRIC": "",
    }
    body = _request("/uapi/domestic-stock/v1/trading/order-cash", tr_id, creds, params, method="POST")
    if not body or body.get("rt_cd") != "0":
        log.error("KIS 주문 실패(%s %s x%d): %s", side, ticker, qty, body.get("msg1") if body else "응답 없음")
        return None

    out = body.get("output", {})
    return {"order_no": out.get("ODNO"), "order_time": out.get("ORD_TMD")}


def buying_power(ticker: str, price: int, creds: dict) -> dict | None:
    """미수 없는 매수가능 금액/수량 조회. 예수금 역산값을 주문 여력으로 사용하지 않는다.

    공식 inquire_psbl_order 규약: 01로 조회해야 종목 증거금률이 반영된 nrcvb 수량을 얻는다.
    이는 조회 조건일 뿐 실제 시장가 주문을 생성하지 않는다.
    """
    _validate_credentials(creds)
    if len(ticker) != 6 or not ticker.isascii() or not ticker.isdigit() or price <= 0:
        raise ValueError("domestic ticker and positive limit price required")
    body = _request("/uapi/domestic-stock/v1/trading/inquire-psbl-order",
                    "TTTC8908R" if creds["env"] == "real" else "VTTC8908R", creds,
                    {"CANO": creds["account_no"], "ACNT_PRDT_CD": creds["product_cd"],
                     "PDNO": ticker, "ORD_UNPR": str(price), "ORD_DVSN": "01",
                     "CMA_EVLU_AMT_ICLD_YN": "N", "OVRS_ICLD_YN": "N"})
    if not body or body.get("rt_cd") != "0":
        return None
    try:
        out = body["output"]
        amount, qty = float(out["nrcvb_buy_amt"]), int(out["nrcvb_buy_qty"])
        if not math.isfinite(amount) or amount < 0 or qty < 0:
            return None
        return {"cash_without_margin": amount, "qty_without_margin": qty}
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def daily_orders(day: str, creds: dict) -> dict | None:
    """KRX 당일 주문·체결의 전체 페이지 조회. 조회 실패/불완전은 빈 주문으로 바꾸지 않는다."""
    _validate_credentials(creds)
    parsed = datetime.date.fromisoformat(day)
    today = datetime.datetime.now(ZoneInfo("Asia/Seoul")).date()
    if parsed != today:
        raise ValueError("only today's KRX order reconciliation is supported")
    compact = parsed.strftime("%Y%m%d")
    body = _read_all("/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
                     "TTTC0081R" if creds["env"] == "real" else "VTTC0081R", creds,
                     {"CANO": creds["account_no"], "ACNT_PRDT_CD": creds["product_cd"],
                      "INQR_STRT_DT": compact, "INQR_END_DT": compact, "SLL_BUY_DVSN_CD": "00",
                      "CCLD_DVSN": "00", "INQR_DVSN": "00", "INQR_DVSN_3": "01", "PDNO": "",
                      "ORD_GNO_BRNO": "", "ODNO": "", "INQR_DVSN_1": "",
                      "CTX_AREA_FK100": "", "CTX_AREA_NK100": "", "EXCG_ID_DVSN_CD": "KRX"})
    if body is None:
        return None
    orders = []
    try:
        for row in body["output1"]:
            qty, filled = int(row["ord_qty"]), int(row["tot_ccld_qty"])
            remaining = int(row["rmn_qty"])
            if min(qty, filled, remaining) < 0 or filled + remaining > qty or not row.get("odno"):
                return None
            state = ("filled" if qty > 0 and filled == qty else "cancelled" if row.get("cncl_yn") == "Y"
                     else "partial" if filled > 0 and remaining > 0 else "open" if remaining > 0 else "unknown")
            orders.append({"order_no": row["odno"], "ticker": row["pdno"], "qty": qty,
                           "filled_qty": filled, "remaining_qty": remaining, "status": state})
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return {"date": day, "complete": True, "orders": orders}
