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
_TIMEOUT = 8  # KIS 미도달 시 오래 매달리지 않도록(대시보드 응답성). 실주문 경로는 재시도로 보완.
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
        body = _request("/uapi/domestic-stock/v1/trading/inquire-balance", tr_id, creds, params)
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
