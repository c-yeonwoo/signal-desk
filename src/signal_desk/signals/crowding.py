"""매수권 섹터 편중 경고 — crowded trade lite.

전체 Barra 없이, 오늘 매수권(BUY/STRONG_BUY)의 섹터 집중도만 본다.
섹터맵에 없는 종목은 '미분류'로 모이는데, 그건 crowded trade가 아니라 **데이터 공백**이다.
"""

from __future__ import annotations

from collections import Counter

from signal_desk.reference import sectors
from signal_desk.signals.engine import is_buy

_UNMAPPED = "미분류"


def assess(signals, *, warn_pct: float = 40.0) -> dict:
    """signals: SignalResult 또는 {ticker,kind} dict.
    반환: {n_buy, top_sector, top_pct, warn, data_quality, note, distribution}
    """
    buys = []
    for s in signals or []:
        kind = getattr(s, "kind", None) or (s.get("kind") if isinstance(s, dict) else None)
        ticker = getattr(s, "ticker", None) or (s.get("ticker") if isinstance(s, dict) else None)
        if ticker and kind and is_buy(kind):
            buys.append(ticker)
    n = len(buys)
    if n == 0:
        return {"n_buy": 0, "top_sector": None, "top_pct": None, "warn": False,
                "data_quality": False, "note": "매수권 없음", "distribution": {}}
    # dict 행에 sector가 있으면(미국 유니버스 등) 그걸 쓰고, 없으면 KR 큐레이션 맵
    def _sec(s, t):
        if isinstance(s, dict) and s.get("sector"):
            return s["sector"]
        return sectors.sector_of(t) or _UNMAPPED

    by_ticker = {}
    for s in signals or []:
        t = getattr(s, "ticker", None) or (s.get("ticker") if isinstance(s, dict) else None)
        if t:
            by_ticker[t] = s
    cnt = Counter(_sec(by_ticker.get(t), t) for t in buys)
    top_sec, top_n = cnt.most_common(1)[0]
    top_pct = round(top_n / n * 100.0, 1)
    # 미분류 쏠림 = 맵 공백(가짜 crowded). 진짜 업종 집중만 warn.
    data_quality = top_sec == _UNMAPPED and top_pct >= warn_pct and n >= 3
    warn = (not data_quality) and top_pct >= warn_pct and n >= 3
    if data_quality:
        note = f"매수권 {n}종목 중 {_UNMAPPED} {top_pct}% — 섹터맵 공백(편중 아님)"
    elif warn:
        note = f"매수권 {n}종목 중 {top_sec} {top_pct}% — crowded"
    else:
        note = f"매수권 {n}종목 · 최대 {top_sec} {top_pct}%"
    return {
        "n_buy": n,
        "top_sector": top_sec,
        "top_pct": top_pct,
        "warn": warn,
        "data_quality": data_quality,
        "distribution": dict(cnt.most_common(5)),
        "note": note,
    }
