"""매수권 섹터 편중 경고 — crowded trade lite.

전체 Barra 없이, 오늘 매수권(BUY/STRONG_BUY)의 섹터 집중도만 본다.
"""

from __future__ import annotations

from collections import Counter

from signal_desk.reference import sectors
from signal_desk.signals.engine import is_buy


def assess(signals, *, warn_pct: float = 40.0) -> dict:
    """signals: SignalResult 또는 {ticker,kind} dict.
    반환: {n_buy, top_sector, top_pct, warn, note}
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
                "note": "매수권 없음"}
    cnt = Counter(sectors.sector_of(t) or "미분류" for t in buys)
    top_sec, top_n = cnt.most_common(1)[0]
    top_pct = round(top_n / n * 100.0, 1)
    warn = top_pct >= warn_pct and n >= 3
    note = (f"매수권 {n}종목 중 {top_sec} {top_pct}% — crowded"
            if warn else f"매수권 {n}종목 · 최대 {top_sec} {top_pct}%")
    return {
        "n_buy": n,
        "top_sector": top_sec,
        "top_pct": top_pct,
        "warn": warn,
        "distribution": dict(cnt.most_common(5)),
        "note": note,
    }
