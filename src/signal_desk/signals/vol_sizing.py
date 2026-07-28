"""변동성 조정 사이징 — alloc × (med_vol / vol), clamp.

동일가중은 고변동 종목에 리스크를 몰아준다. 봇 매수 alloc에만 부착.
"""

from __future__ import annotations

import math
import statistics


def realized_vol(closes: list[float], window: int = 20) -> float | None:
    """최근 window일 일간 수익률 표준편차(소수)."""
    if not closes or len(closes) < window + 1:
        return None
    rets = []
    for i in range(len(closes) - window, len(closes)):
        a, b = closes[i - 1], closes[i]
        if a and a > 0 and b and b > 0:
            rets.append(b / a - 1.0)
    if len(rets) < max(5, window // 2):
        return None
    try:
        return statistics.pstdev(rets)
    except statistics.StatisticsError:
        return None


def scale(vol: float | None, ref_vol: float | None,
          *, lo: float = 0.5, hi: float = 1.5) -> float:
    """ref/vol 비율을 [lo,hi]로 clamp. 데이터 없으면 1.0."""
    if vol is None or ref_vol is None or vol <= 0 or ref_vol <= 0:
        return 1.0
    return max(lo, min(hi, ref_vol / vol))


def median_vol(closes_by: dict[str, list[float]], tickers: list[str],
               window: int = 20) -> float | None:
    vols = []
    for t in tickers:
        v = realized_vol(closes_by.get(t) or [], window)
        if v is not None and v > 0:
            vols.append(v)
    if not vols:
        return None
    return float(statistics.median(vols))
