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
    rows = investor_flow_series(code, days=days)
    if not rows:
        return None
    fo = sum(r["foreign_net"] for r in rows)
    ins = sum(r["inst_net"] for r in rows)
    vol = sum(r.get("volume") or 0 for r in rows)
    return {"foreign_net": fo, "inst_net": ins, "total_buy": vol}


def _trend_date(row: dict) -> str | None:
    """네이버 trend 행에서 YYYY-MM-DD 추출."""
    for k in ("bizdate", "businessDate", "localTradedAt", "date", "tradedAt"):
        raw = row.get(k)
        if not raw:
            continue
        s = str(raw).strip().replace(".", "").replace("/", "")[:8]
        if len(s) >= 8 and s[:8].isdigit():
            return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
        if len(str(raw)) >= 10 and str(raw)[4] == "-":
            return str(raw)[:10]
    return None


def investor_flow_series(code: str, days: int = 120) -> list[dict] | None:
    """일별 외국인·기관 순매수 시계열(오래된→최신). 차트 수급 패널용.
    반환: [{date, foreign_net, inst_net, volume}, ...] — 실패/무자료 시 None."""
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
    out: list[dict] = []
    for row in rows[: max(1, days)]:
        dt = _trend_date(row)
        if not dt:
            continue
        out.append({
            "date": dt,
            "foreign_net": _num(row.get("foreignerPureBuyQuant")),
            "inst_net": _num(row.get("organPureBuyQuant")),
            "volume": _num(row.get("accumulatedTradingVolume")),
        })
    if not out:
        return None
    out.reverse()  # API는 보통 최신→과거 — 차트 dates와 맞춰 오래된→최신
    # 날짜 오름차순 보장
    out.sort(key=lambda x: x["date"])
    return out



def _fnum(s) -> float | None:
    """'513,958'/'-1,508' → float. 값 없음('','-',None)이면 None(0과 구분)."""
    if s is None:
        return None
    t = str(s).replace(",", "").strip()
    if t in ("", "-", "N/A"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _get_json(code: str, path: str) -> dict | list | None:
    url = f"https://m.stock.naver.com/api/stock/{code}/{path}"
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        log.warning("네이버 %s 실패(%s): HTTP %s", path, code, e.code)
    except Exception as e:
        log.warning("네이버 %s 실패(%s): %s", path, code, type(e).__name__)
    return None


def consensus(code: str) -> dict | None:
    """종목의 애널리스트 컨센서스 스냅샷. 목표주가·투자의견은 /integration의 consensusInfo,
    선행연도(E) 컨센서스 EPS는 /finance/annual의 isConsensus='Y' 컬럼에서 뽑는다.

    반환: {price_target_mean, recomm_mean, source_date, forwards:[{year,eps}, ...]} — 커버리지 없으면
    각 항목 None/[]. 목표주가·선행EPS가 모두 없으면 None(수집 대상 아님). recomm_mean은 네이버 척도
    (5=적극매수에 가까움 추정, 사용 전 방향 검증 필요) 그대로 보존한다.
    ⚠️ 현재 컨센서스 '수준' 스냅샷일 뿐 리비전(변화)이 아니다 — store가 매일 PIT로 쌓아 시계열화."""
    integ = _get_json(code, "integration")
    ci = (integ or {}).get("consensusInfo") or {}
    ptm = _fnum(ci.get("priceTargetMean"))
    recomm = _fnum(ci.get("recommMean"))
    source_date = ci.get("createDate")

    forwards: list[dict] = []
    fa = _get_json(code, "finance/annual")
    fi = (fa or {}).get("financeInfo") or {}
    cons_keys = [t.get("key") for t in fi.get("trTitleList", []) if t.get("isConsensus") == "Y"]
    eps_row = next((r for r in fi.get("rowList", []) if r.get("title") == "EPS"), None)
    if eps_row and cons_keys:
        for key in cons_keys:
            eps = _fnum((eps_row.get("columns", {}).get(key) or {}).get("value"))
            if key and eps is not None:
                forwards.append({"year": str(key), "eps": eps})

    if ptm is None and not forwards:
        return None  # 애널 커버리지 없음 → 수집 대상 아님
    return {"price_target_mean": ptm, "recomm_mean": recomm,
            "source_date": source_date, "forwards": forwards}
