"""Risk-only, cash-funded marginal candidate check on identical observed dates.

No expected-return estimate enters this calculation. It is deliberately narrower
than the combined trade plan: one candidate is added to today's holdings using
cash, with no sales, rebalancing, dividends, FX or future prices.
"""

from __future__ import annotations

import math
from statistics import stdev

WINDOW = 60
ANNUAL_SESSIONS = 252
VERSION = "cash-funded-marginal-risk-v1"


def _returns(ticker: str, dates_by: dict, closes_by: dict) -> dict[str, float] | None:
    dates, closes = dates_by.get(ticker) or [], closes_by.get(ticker) or []
    if len(dates) != len(closes) or dates != sorted(set(dates)) or len(dates) <= WINDOW:
        return None
    try:
        prices = [float(p) for p in closes]
    except (TypeError, ValueError, OverflowError):
        return None
    if any(not math.isfinite(p) or p <= 0 for p in prices):
        return None
    return {str(dates[i])[:10]: prices[i] / prices[i - 1] - 1
            for i in range(1, len(dates))}


def assess(*, holdings: list[dict], candidate: dict, cash: float,
           dates_by: dict, closes_by: dict) -> dict:
    """Annualized volatility delta for a single candidate versus holding cash."""
    base = {"version": VERSION, "mode": "shadow", "live_eligible": False,
            "annualization_sessions": ANNUAL_SESSIONS}

    def blocked(reason: str):
        return {**base, "ready": False, "reason": reason,
                "before_volatility_pct": None, "after_volatility_pct": None,
                "delta_volatility_pp": None}

    try:
        cash = float(cash)
        values = {str(row["ticker"]): float(row["value"]) for row in holdings}
        amount = float(candidate["proposed_value"])
        ticker = str(candidate["ticker"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return blocked("보유·현금·후보 금액의 정합성 확인 실패")
    if (len(values) != len(holdings) or ticker in values or amount <= 0 or not math.isfinite(amount) or
            not math.isfinite(cash) or cash < 0 or
            any(not math.isfinite(v) or v < 0 for v in values.values())):
        return blocked("후보 또는 보유 금액이 유효하지 않습니다.")
    total = sum(values.values()) + cash
    if total <= 0 or amount > cash + 1e-8:
        return blocked("현재 현금만으로 후보를 편입할 수 없어 단독 위험 비교를 보류합니다.")
    tickers = list(values) + [ticker]
    ends = {str((dates_by.get(t) or [None])[-1])[:10] for t in tickers}
    if len(ends) != 1 or None in ends or "None" in ends:
        return blocked("보유와 후보의 최종 종가 기준일이 다릅니다.")
    series = {t: _returns(t, dates_by, closes_by) for t in tickers}
    if any(s is None for s in series.values()):
        return blocked("가격 이력의 날짜·종가 정합성이 부족합니다.")
    common = sorted(set.intersection(*(set(s) for s in series.values())))
    if len(common) < WINDOW:
        return blocked("보유와 후보의 공통 수익률 60거래일이 부족합니다.")
    days = common[-WINDOW:]
    before = [sum(value / total * series[t][day] for t, value in values.items()) for day in days]
    after = [before[i] + amount / total * series[ticker][day] for i, day in enumerate(days)]
    scale = math.sqrt(ANNUAL_SESSIONS) * 100
    before_vol = stdev(before) * scale if values else 0.0
    after_vol = stdev(after) * scale
    return {**base, "ready": True, "observations": WINDOW, "price_session": days[-1],
            "before_volatility_pct": round(before_vol, 3),
            "after_volatility_pct": round(after_vol, 3),
            "delta_volatility_pp": round(after_vol - before_vol, 3),
            "candidate_weight_pct": round(amount / total * 100, 3),
            "note": "후보 한 종목을 현금으로 추가한 과거 변동성 비교이며 예상 수익률·전체 행동계획 결과가 아닙니다."}
