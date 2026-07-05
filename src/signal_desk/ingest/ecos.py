"""한국은행 ECOS 거시 지표 — 한국 기준금리·국고채10년·CPI(YoY). 국내 거시 축.

FRED(미국)와 짝을 이루는 한국 측 거시 지표다. 원/달러 환율은 FRED(DEXKOUS)에 이미 있어 제외.
ECOS_API_KEY가 없으면 조용히 빈 값(그레이스풀). 각 지표는 KOSPI 관점의 favor(+1 우호/-1 비우호)와
사유를 함께 담아 반환해, 상위(macro.read extra)에서 시황 점수·전광판 칩으로 바로 쓰인다.

통계코드(실검증): 기준금리 722Y001/M/0101000 · 국고채10년 817Y002/D/010210000 · CPI 901Y009/M/0.
"""

from __future__ import annotations

import datetime
import json
import logging
import urllib.request

from signal_desk import config

log = logging.getLogger("signal_desk.ingest.ecos")

_BASE = "https://ecos.bok.or.kr/api/StatisticSearch"
_TIMEOUT = 20
CPI_TARGET = 2.0  # 한은 물가안정목표 2% — 초과 + 상승이면 금리 부담(비우호)


def _series(code: str, cycle: str, item: str, count: int) -> list[tuple[str, float]]:
    """최신 count개 관측을 (기간, 값) 최신→과거 순으로. 키 없음·실패 시 빈 리스트."""
    key = config.ecos_key()
    if not key:
        return []
    today = datetime.date.today()
    if cycle == "D":
        start = (today - datetime.timedelta(days=count * 2 + 10)).strftime("%Y%m%d")
        end = today.strftime("%Y%m%d")
    else:  # 월간(M) — YoY 위해 넉넉히
        start = (today.replace(day=1) - datetime.timedelta(days=32 * (count + 1))).strftime("%Y%m")
        end = today.strftime("%Y%m")
    url = f"{_BASE}/{key}/json/kr/1/{count + 20}/{code}/{cycle}/{start}/{end}/{item}"
    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log.warning("ECOS 요청 실패(%s): %s", code, type(e).__name__)
        return []
    rows = (body.get("StatisticSearch") or {}).get("row") or []
    out = [(r["TIME"], float(r["DATA_VALUE"])) for r in rows if r.get("DATA_VALUE") not in (None, "", ".")]
    return list(reversed(out))  # ECOS는 과거→최신 → 최신→과거로 뒤집음


def macro_indicators() -> list[dict]:
    """한국 거시 지표 [{key,label,unit,value,change,dir,asof,favor,reason}]. 데이터 없으면 생략."""
    out = []

    def _rate(code, item, label, key):
        obs = _series(code, "M" if code == "722Y001" else "D", item, 3)
        if not obs:
            return
        asof, latest = obs[0]
        change = round(latest - obs[1][1], 2) if len(obs) > 1 else None
        favor = 0 if not change else (-1 if change > 0 else 1)  # 금리 상승=비우호
        r = None
        if change:
            r = f"[거시] 한국 {label} {latest:.2f}% {'상승' if change > 0 else '하락'} — {'유동성 부담' if change > 0 else '우호'}"
        out.append({"key": key, "label": f"한국 {label}", "unit": "%", "value": round(latest, 2),
                    "change": change, "dir": 0 if not change else (1 if change > 0 else -1),
                    "asof": asof, "favor": favor, "reason": r})

    _rate("722Y001", "0101000", "기준금리", "KR_BASE")
    _rate("817Y002", "010210000", "국고채10년", "KR_TB10")

    # CPI YoY (901Y009 월간 레벨 → 전년동월비)
    cpi = _series("901Y009", "M", "0", 16)
    if len(cpi) > 12:
        asof, latest = cpi[0]
        yoy = round((latest / cpi[12][1] - 1) * 100, 2)
        prev_yoy = round((cpi[1][1] / cpi[13][1] - 1) * 100, 2) if len(cpi) > 13 else None
        change = round(yoy - prev_yoy, 2) if prev_yoy is not None else None
        favor = 0
        reason = None
        if yoy > CPI_TARGET and (change or 0) > 0:
            favor, reason = -1, f"[거시] 한국 CPI {yoy:.1f}% — 목표 상회·반등, 물가 부담"
        elif (change or 0) < 0:
            favor, reason = 1, f"[거시] 한국 CPI {yoy:.1f}% — 둔화 흐름, 우호"
        out.append({"key": "KR_CPI", "label": "한국 CPI", "unit": "% YoY", "value": yoy,
                    "change": change, "dir": 0 if not change else (1 if change > 0 else -1),
                    "asof": asof, "favor": favor, "reason": reason})
    return out
