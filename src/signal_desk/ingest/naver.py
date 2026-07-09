"""네이버 금융 — 종목별 투자자 수급(외국인·기관 순매수). pykrx 투자자 엔드포인트가 KRX 스키마
변경으로 죽어(2026-07) 그 대체. m.stock.naver.com JSON(비공식이지만 안정적, 수량 기반이라
스케일된 시세와 무관). 표준 라이브러리(urllib)만 사용, 실패 시 None(그레이스풀).
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger("signal_desk.ingest.naver")

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15) AppleWebKit/537.36"
_TIMEOUT = 10


def _num(s) -> float:
    """'+971,031' / '-3,015,093' → float. 파싱 실패 0."""
    try:
        return float(str(s).replace(",", "").replace("+", "").strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def investor_flow(code: str, days: int = 20) -> dict | None:
    """종목의 최근 days 거래일 외국인·기관 순매수(주수) 누적 + 거래량 합. 실패/무자료 시 None.
    반환: {foreign_net, inst_net, total_buy}(주수) — store가 intensity로 정규화."""
    url = f"https://m.stock.naver.com/api/stock/{code}/trend"
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            rows = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        log.warning("네이버 수급 실패(%s): HTTP %s", code, e.code)
        return None
    except Exception as e:
        log.warning("네이버 수급 실패(%s): %s", code, type(e).__name__)
        return None
    if not isinstance(rows, list) or not rows:
        return None
    fo = ins = vol = 0.0
    for row in rows[:days]:
        fo += _num(row.get("foreignerPureBuyQuant"))
        ins += _num(row.get("organPureBuyQuant"))
        vol += _num(row.get("accumulatedTradingVolume"))
    return {"foreign_net": fo, "inst_net": ins, "total_buy": vol}
