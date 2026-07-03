"""KIS Developers API — 모의투자 자동매매(BACKLOG #7). 인증/잔고조회/주문(현금).

⚠️ `config.kis_credentials()["env"]`는 반드시 'demo'(모의투자)로 둘 것 — 'prod'면 실계좌 주문
API(TTTC...)를 호출하게 된다. base URL도 완전히 다른 도메인이라 실수로 섞어 쓸 위험은 낮지만,
env 값 자체를 실수로 바꾸는 건 사람이 저지를 수 있는 실수라 명시적으로 확인할 것.

실키로 검증됨(2026-07-02): 인증 성공, 계좌번호+상품코드("01") 조합으로 잔고조회 정상 응답 확인.

⚠️ 토큰 발급(oauth2/tokenP)에 엄격한 rate limit이 있음을 실제로 확인함(짧은 간격 재요청 시
HTTP 403). 그래서 토큰은 반드시 파일 캐시로 재사용해야 한다(1일 유효) — `get_token()`을 거치지
않고 직접 발급 API를 호출하지 말 것.
"""

from __future__ import annotations

import datetime
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

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
_TIMEOUT = 20
_TOKEN_FILE = Path("data/cache/kis_token.json")


def _load_cached_token() -> str | None:
    if not _TOKEN_FILE.exists():
        return None
    try:
        data = json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
        if data.get("expires_at", 0) > time.time() + 60:  # 60초 여유
            return data["token"]
    except Exception:
        pass
    return None


def _save_token(token: str, expires_at: float) -> None:
    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_FILE.write_text(json.dumps({"token": token, "expires_at": expires_at}), encoding="utf-8")


def get_token(creds: dict | None = None) -> str | None:
    """캐시된 토큰을 우선 재사용, 없거나 만료 임박이면 새로 발급."""
    creds = creds or config.kis_credentials()
    if not creds:
        return None
    cached = _load_cached_token()
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
        log.error("KIS 토큰 발급 실패: %s", e)
        return None

    token = data.get("access_token")
    if not token:
        log.error("KIS 토큰 응답에 access_token 없음: %s", data)
        return None
    try:
        expires_at = datetime.datetime.strptime(
            data["access_token_token_expired"], "%Y-%m-%d %H:%M:%S"
        ).timestamp()
    except Exception:
        expires_at = time.time() + 23 * 3600  # 파싱 실패 시 보수적 기본값(23시간)
    _save_token(token, expires_at)
    return token


def _request(path: str, tr_id: str, creds: dict, params: dict, method: str = "GET") -> dict | None:
    token = get_token(creds)
    if not token:
        return None
    base = _BASE[creds["env"]]
    headers = {
        "authorization": f"Bearer {token}", "appkey": creds["app_key"], "appsecret": creds["app_secret"],
        "tr_id": tr_id, "custtype": "P", "Content-Type": "application/json; charset=utf-8",
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
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        log.error("KIS API HTTP 오류(%s): %s", path, e)
        return None
    except Exception as e:
        log.error("KIS API 요청 실패(%s): %s", path, e)
        return None


def balance(creds: dict | None = None) -> dict | None:
    """예수금(현금)·총평가금액·보유종목. 실패 시 None."""
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
    for attempt in range(3):  # KIS 간헐 500 대비 재시도
        body = _request("/uapi/domestic-stock/v1/trading/inquire-balance", tr_id, creds, params)
        if body and body.get("rt_cd") == "0":
            break
        time.sleep(0.5)
    if not body or body.get("rt_cd") != "0":
        log.error("KIS 잔고조회 실패: %s", body.get("msg1") if body else "응답 없음")
        return None

    holdings = [
        {
            "ticker": h["pdno"], "name": h["prdt_name"],
            "qty": int(h["hldg_qty"]), "avg_price": float(h["pchs_avg_pric"]),
        }
        for h in body.get("output1", []) if int(h.get("hldg_qty", 0)) > 0
    ]
    summary = (body.get("output2") or [{}])[0]

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
    return {
        "cash": max(0.0, free_cash),                 # 가용현금(순자산−유가증권평가)
        "deposit_raw": _f("dnca_tot_amt"),           # KIS 예수금총금액(참고)
        "total_eval": net_asset,                     # 총평가금액(순자산)
        "stock_eval": stock_eval,                    # 유가증권 평가금액
        "invested": invested,                        # 매입금액합계
        "pnl": pnl,                                  # 평가손익합계
        "pnl_pct": round(pnl / invested * 100, 2) if invested else None,  # 실제 총손익률
        "holdings": holdings,
    }


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
