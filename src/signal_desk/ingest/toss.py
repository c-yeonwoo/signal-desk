"""토스증권 Open API — 시장데이터(시세·종목마스터·캔들·투자경고). KR+US 단일 API(읽기전용).

OAuth2 client credentials(TOSS_CLIENT_ID/SECRET, .env) → Bearer 토큰(~1일). 시장데이터는 계정 헤더 불필요.
symbol은 우리 티커와 동일(KRX 6자리 '005930', US 'AAPL') — 매핑 불필요. 자격증명 없으면 조용히 폴백.
표준 HTTPS(443)라 Railway 등 클라우드에서 안정적(KIS :29443 차단 이슈 없음).
※ 주문/잔고(실계좌) 엔드포인트는 의도적으로 미구현 — 봇은 유저별 paper 격리 유지, 여긴 데이터만.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("signal_desk.ingest.toss")

_BASE = "https://openapi.tossinvest.com"
_TIMEOUT = 20
_token: dict = {"value": None, "exp": 0.0}  # 프로세스 내 토큰 캐시(만료 재발급)


def _creds() -> tuple[str, str] | None:
    cid, csec = os.environ.get("TOSS_CLIENT_ID"), os.environ.get("TOSS_CLIENT_SECRET")
    return (cid, csec) if (cid and csec) else None


def available() -> bool:
    return _creds() is not None


def _clear_token() -> None:
    """캐시 토큰 폐기 — 데이터 API 401·키 교체 후 강제 재발급용."""
    _token["value"] = None
    _token["exp"] = 0.0


def _access_token(*, force: bool = False) -> str | None:
    if not force and _token["value"] and time.time() < _token["exp"]:
        return _token["value"]
    creds = _creds()
    if not creds:
        return None
    _clear_token()  # 재발급 직전 폐기(force/만료 공통)
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials", "client_id": creds[0], "client_secret": creds[1],
    }).encode()
    req = urllib.request.Request(_BASE + "/oauth2/token", data=data,
                                 headers={"content-type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        log.warning("토스 토큰 발급 실패: HTTP %s %s — TOSS_CLIENT_ID/SECRET 재발급·Railway env 확인",
                    e.code, detail)
        return None
    except Exception as e:
        log.warning("토스 토큰 발급 실패: %s", type(e).__name__)
        return None
    tok = body.get("access_token")
    if tok:
        _token["value"] = tok
        _token["exp"] = time.time() + max(60, int(body.get("expires_in", 3600)) - 300)  # 5분 여유
    return tok


def _http_detail(err: urllib.error.HTTPError, limit: int = 240) -> str:
    try:
        return err.read().decode("utf-8", "replace")[:limit]
    except Exception:
        return ""


def _authorized_get(url: str, *, headers: dict[str, str]) -> dict | list | None:
    """Bearer GET. 401이면 토큰 폐기→재발급→1회만 재시도(죽은 캐시 토큰 고착 방지)."""
    tok = _access_token()
    if not tok:
        return None

    def _once(bearer: str):
        req = urllib.request.Request(url, headers={**headers, "authorization": "Bearer " + bearer,
                                                   "accept": "application/json"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        return _once(tok)
    except urllib.error.HTTPError as e:
        if e.code != 401:
            log.warning("토스 요청 실패(%s): HTTP %s %s", url.split("?")[0].replace(_BASE, ""),
                        e.code, _http_detail(e))
            return None
        detail = _http_detail(e)
        _clear_token()
        tok2 = _access_token(force=True)
        if not tok2:
            log.warning("토스 401 후 토큰 재발급 실패(%s) %s — 키 폐기/오타 가능",
                        url.split("?")[0].replace(_BASE, ""), detail)
            return None
        try:
            return _once(tok2)
        except urllib.error.HTTPError as e2:
            log.warning("토스 401 재시도도 실패(%s): HTTP %s %s — TOSS_CLIENT_ID/SECRET 재발급 검토",
                        url.split("?")[0].replace(_BASE, ""), e2.code, _http_detail(e2))
            return None
        except Exception as e2:
            log.warning("토스 401 재시도 실패(%s): %s", url.split("?")[0].replace(_BASE, ""), type(e2).__name__)
            return None
    except Exception as e:
        log.warning("토스 요청 실패(%s): %s", url.split("?")[0].replace(_BASE, ""), type(e).__name__)
        return None


def _get(path: str, params: dict | None = None) -> dict | list | None:
    url = _BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    return _authorized_get(url, headers={})


def holdings(account: str = "1") -> dict | None:
    """앱에 연동된 계좌의 실보유 자산(자산 API /api/v1/holdings). 반환: result 객체
    {totalPurchaseAmount, marketValue, profitLoss, items:[...]} 또는 None(자격증명·조회 실패).
    ⚠️ 개인 계좌 데이터 — 호출·노출은 반드시 owner-gated(api 계층)에서만. 여긴 요청만 담당."""
    body = _authorized_get(
        _BASE + "/api/v1/holdings",
        headers={"X-Tossinvest-Account": str(account)},
    )
    return body.get("result") if isinstance(body, dict) else None


def _rows(body) -> list[dict]:
    """응답 리스트 추출 — 토스는 {result:[...]} 형태. 방어적으로 몇 가지 키도 대응."""
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for k in ("result", "data", "items"):
            v = body.get(k)
            if isinstance(v, list):
                return v
    return []


def _batched(symbols: list[str], n: int = 200):
    for i in range(0, len(symbols), n):
        yield symbols[i:i + n]


def stocks(symbols: list[str]) -> dict[str, dict]:
    """종목 마스터 — symbol -> {name, market, shares_outstanding(float|None), currency, status}."""
    out: dict[str, dict] = {}
    for chunk in _batched(symbols):
        body = _get("/api/v1/stocks", {"symbols": ",".join(chunk)})
        for r in _rows(body):
            sym = r.get("symbol")
            if not sym:
                continue
            so = r.get("sharesOutstanding")
            try:
                so = float(so) if so not in (None, "") else None
            except (TypeError, ValueError):
                so = None
            out[sym] = {"name": r.get("name"), "market": r.get("market"),
                        "shares_outstanding": so, "currency": r.get("currency"), "status": r.get("status")}
    return out


def prices(symbols: list[str]) -> dict[str, float]:
    """현재가 — symbol -> lastPrice(float). 조회 실패분은 생략."""
    out: dict[str, float] = {}
    for chunk in _batched(symbols):
        body = _get("/api/v1/prices", {"symbols": ",".join(chunk)})
        for r in _rows(body):
            sym, lp = r.get("symbol"), r.get("lastPrice")
            if sym and lp not in (None, ""):
                try:
                    out[sym] = float(lp)
                except (TypeError, ValueError):
                    pass
    return out


def daily_closes(symbol: str, count: int = 200) -> list[float]:
    """일봉 종가(수정주가) 오래된->최신 순. count<=200(1콜). 실패 시 빈 리스트.
    토스 candles 응답은 result.candles(최신순) → 종가만 뽑아 오래된→최신으로 뒤집는다."""
    body = _get("/api/v1/candles", {"symbol": symbol, "interval": "1d",
                                    "count": min(count, 200), "adjusted": "true"})
    candles = ((body or {}).get("result") or {}).get("candles") if isinstance(body, dict) else None
    if not isinstance(candles, list):
        return []
    closes = []
    for r in candles:
        c = r.get("closePrice")
        if c not in (None, ""):
            try:
                closes.append(float(c))
            except (TypeError, ValueError):
                pass
    closes.reverse()  # 응답 최신순 → 오래된→최신
    return closes


def daily_ohlcv(symbol: str, count: int = 200) -> list[dict]:
    """일봉 [{date, open, close, volume}] 오래된->최신 순. 실패 시 빈 리스트."""
    body = _get("/api/v1/candles", {"symbol": symbol, "interval": "1d",
                                    "count": min(count, 200), "adjusted": "true"})
    candles = ((body or {}).get("result") or {}).get("candles") if isinstance(body, dict) else None
    if not isinstance(candles, list):
        return []
    out = []
    for r in candles:
        try:
            out.append({"date": (r.get("timestamp") or "")[:10],
                        "open": float(r.get("openPrice")), "close": float(r.get("closePrice")),
                        "volume": float(r.get("volume") or 0)})
        except (TypeError, ValueError):
            continue
    out.reverse()  # 응답 최신순 → 오래된→최신
    return out


def warnings(symbol: str) -> list[str]:
    """활성 투자경고/거래정지/과열/VI 유형 목록. 없으면 빈 리스트(=정상)."""
    body = _get(f"/api/v1/stocks/{symbol}/warnings")
    return [w for r in _rows(body) if (w := r.get("warningType"))]


def _amt(obj: dict | None, key: str) -> float:
    """{buyAmount,sellAmount} 문자열 금액 → float(원). 파싱 실패 0."""
    try:
        return float(str((obj or {}).get(key, "") or 0).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def market_investor_trading(market: str = "KOSPI", interval: str = "1d", count: int = 20) -> list[dict]:
    """시장 전체(KOSPI/KOSDAQ) 투자자별 매수·매도대금 → 순매수(net). pykrx 종목별 수급이 KRX
    스키마 변경으로 죽어(2026-07) 그 대체로 '시장 국면'용 시장전체 수급만 토스에서 받는다.
    ⚠️ 종목별(per-ticker)은 토스 Open API에 없음 — 시장 지수(KOSPI/KOSDAQ)만 지원.
    반환(최신→과거): [{date, foreigner_net, institution_net, individual_net, total_buy}(원, float)]."""
    body = _get(f"/api/v1/market-indicators/{market}/investor-trading",
                {"interval": interval, "count": max(1, min(100, count))})
    # 응답: {"result": {"records": [{date, foreigner:{buyAmount,sellAmount}, institution:{...}, ...}]}}
    result = body.get("result") if isinstance(body, dict) else None
    records = (result or {}).get("records") if isinstance(result, dict) else None
    if not isinstance(records, list):
        records = _rows(body)  # 방어적 폴백(래핑 형태가 다를 때)
    out: list[dict] = []
    for r in records:
        if not isinstance(r, dict) or not r.get("date"):
            continue
        fo, ins, ind = r.get("foreigner"), r.get("institution"), r.get("individual")
        total_buy = sum(_amt(g, "buyAmount") for g in (fo, ins, ind, r.get("otherCorporation")))
        out.append({
            "date": r["date"],
            "foreigner_net": _amt(fo, "buyAmount") - _amt(fo, "sellAmount"),
            "institution_net": _amt(ins, "buyAmount") - _amt(ins, "sellAmount"),
            "individual_net": _amt(ind, "buyAmount") - _amt(ind, "sellAmount"),
            "total_buy": total_buy,
        })
    out.sort(key=lambda x: x["date"], reverse=True)  # 최신 우선(응답 정렬 무관하게 보장)
    return out
