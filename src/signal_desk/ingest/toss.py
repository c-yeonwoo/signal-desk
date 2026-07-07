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


def _access_token() -> str | None:
    if _token["value"] and time.time() < _token["exp"]:
        return _token["value"]
    creds = _creds()
    if not creds:
        return None
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
        log.warning("토스 토큰 발급 실패: HTTP %s %s", e.code, detail)
        return None
    except Exception as e:
        log.warning("토스 토큰 발급 실패: %s", type(e).__name__)
        return None
    tok = body.get("access_token")
    if tok:
        _token["value"] = tok
        _token["exp"] = time.time() + max(60, int(body.get("expires_in", 3600)) - 300)  # 5분 여유
    return tok


def _get(path: str, params: dict | None = None) -> dict | list | None:
    tok = _access_token()
    if not tok:
        return None
    url = _BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"authorization": "Bearer " + tok, "accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        log.warning("토스 요청 실패(%s): HTTP %s", path, e.code)
        return None
    except Exception as e:
        log.warning("토스 요청 실패(%s): %s", path, type(e).__name__)
        return None


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
