"""신규 편입 후보를 현재 포트폴리오 제약 안에서 고르는 shadow 레이어.

점수 순위만으로 후보를 내면 이미 보유한 동일 위험을 한 번 더 사게 된다. 이 모듈은 BUY 신호를
후보 자격으로만 쓰고, 현금·종목/섹터 cap·60일 상관을 통과한 후보에만 제한된 편입 비중을 준다.
"""

from __future__ import annotations

import math
from collections import defaultdict

from signal_desk.signals import portfolio_risk


LOOKBACK_DAYS = 60
CORRELATION_LIMIT = 0.75
MAX_CANDIDATES = 5


def _returns(dates: list[str], closes: list[float]) -> dict[str, float]:
    out = {}
    for i in range(1, min(len(dates), len(closes))):
        try:
            prev, cur = float(closes[i - 1]), float(closes[i])
        except (TypeError, ValueError):
            continue
        if math.isfinite(prev) and math.isfinite(cur) and prev > 0 and cur > 0:
            out[str(dates[i])[:10]] = cur / prev - 1.0
    return dict(sorted(out.items())[-LOOKBACK_DAYS:])


def _volatility(returns: dict[str, float]) -> float | None:
    values = list(returns.values())[-LOOKBACK_DAYS:]
    if len(values) < LOOKBACK_DAYS:
        return None
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(variance) if math.isfinite(variance) and variance > 1e-16 else None


def _allocate(candidates: list[dict], *, cash_budget_pct: float, sector_used: dict[str, float],
              max_single_pct: float, max_sector_pct: float) -> list[dict]:
    """신호 강도÷변동성 선호를 cap 아래에서 배분한다. 남는 현금은 억지로 채우지 않는다."""
    target = [0.0] * len(candidates)
    remaining = max(0.0, cash_budget_pct)
    for _ in range(len(candidates) * 4):
        caps = [max(0.0, min(max_single_pct - target[i], max_sector_pct - sector_used[c["sector"]]))
                for i, c in enumerate(candidates)]
        eligible = [i for i, cap in enumerate(caps) if cap > 1e-9]
        if remaining <= 1e-9 or not eligible:
            break
        total_pref = sum(candidates[i]["preference"] for i in eligible)
        requested = {i: remaining * candidates[i]["preference"] / total_pref for i in eligible}
        increment = dict(requested)
        for sector in {candidates[i]["sector"] for i in eligible}:
            idxs = [i for i in eligible if candidates[i]["sector"] == sector]
            total_request = sum(requested[i] for i in idxs)
            sector_left = max(0.0, max_sector_pct - sector_used[sector])
            if total_request > sector_left and total_request > 0:
                for i in idxs:
                    increment[i] *= sector_left / total_request
        added = 0.0
        for i in eligible:
            amount = min(increment[i], caps[i])
            target[i] += amount
            sector_used[candidates[i]["sector"]] += amount
            added += amount
        if added <= 1e-9:
            break
        remaining -= added
    for candidate, weight in zip(candidates, target):
        candidate["proposed_weight_pct"] = math.floor(weight * 10000) / 10000
    return [candidate for candidate in candidates if candidate["proposed_weight_pct"] > 0]


def evaluate(*, holdings: list[dict], universe: list[dict], signal_by_ticker: dict,
             prices: dict[str, list[float]], dates_by: dict[str, list[str]], profile: dict) -> dict:
    """신규 후보를 자격→상관 독립성→제약 배분 순으로 평가한다."""
    if any(row.get("value") is None or not row.get("sector") or not row.get("history_ready")
           for row in holdings):
        return {"ready": False, "mode": "shadow", "reason": "보유종목의 평가액·섹터·가격 이력 결손으로 신규 편입을 보류합니다."}
    cash = max(0.0, float(profile.get("cash") or 0.0))
    invested = sum(float(row.get("value") or 0.0) for row in holdings if row.get("value") is not None)
    total = invested + cash
    if total <= 0:
        return {"ready": False, "mode": "shadow", "reason": "후보 편입 한도를 계산할 현금 또는 평가액이 없습니다."}
    cash_pct = cash / total * 100
    budget = max(0.0, cash_pct - float(profile["min_cash_pct"]))
    if budget <= 0:
        return {"ready": False, "mode": "shadow", "reason": "최소 현금 한도를 제외하면 신규 편입에 쓸 현금 여유가 없습니다.",
                "cash_pct": round(cash_pct, 1), "available_cash_pct": 0.0}
    held_tickers = {str(row["ticker"]) for row in holdings}
    holding_returns = {str(row["ticker"]): _returns(dates_by.get(str(row["ticker"])) or [],
                                                       prices.get(str(row["ticker"])) or [])
                       for row in holdings if row.get("history_ready")}
    sector_used: dict[str, float] = defaultdict(float)
    for row in holdings:
        if row.get("sector") and row.get("value") is not None:
            sector_used[str(row["sector"])] += float(row["value"]) / total * 100
    qualified, rejected = [], []
    for asset in universe:
        ticker = str(asset.get("ticker") or "")
        signal = signal_by_ticker.get(ticker)
        if not ticker or ticker in held_tickers:
            continue
        if not signal or getattr(signal, "kind", None) not in ("BUY", "STRONG_BUY"):
            continue
        if getattr(signal, "event_risk", False):
            rejected.append({"ticker": ticker, "name": asset.get("name") or ticker, "reason": "이벤트 위험 감지"})
            continue
        closes, dates = prices.get(ticker) or [], dates_by.get(ticker) or []
        ret = _returns(dates, closes)
        vol = _volatility(ret)
        sector = asset.get("sector")
        if not sector or vol is None:
            rejected.append({"ticker": ticker, "name": asset.get("name") or ticker, "reason": "섹터 또는 60일 가격 이력 부족"})
            continue
        correlations = {held: portfolio_risk.correlation(ret, held_ret, min_observations=LOOKBACK_DAYS)
                        for held, held_ret in holding_returns.items()}
        known = {ticker_: value for ticker_, value in correlations.items() if value is not None}
        if len(known) != len(holding_returns):
            rejected.append({"ticker": ticker, "name": asset.get("name") or ticker, "reason": "보유종목과 공통 가격 이력 부족"})
            continue
        high = [ticker_ for ticker_, value in known.items() if value >= CORRELATION_LIMIT]
        if high:
            rejected.append({"ticker": ticker, "name": asset.get("name") or ticker,
                             "reason": f"보유 {', '.join(high[:3])}와 고상관"})
            continue
        score = float(getattr(signal, "score", 0.0))
        if not math.isfinite(score) or not math.isfinite(float(closes[-1])) or float(closes[-1]) <= 0:
            continue
        qualified.append({"ticker": ticker, "name": asset.get("name") or ticker, "sector": str(sector),
                          "score": round(score, 3), "signal_kind": signal.kind, "price": float(closes[-1]),
                          "max_correlation": round(max(known.values()), 4) if known else None,
                          "volatility": round(vol, 6), "preference": max(score, 0.01) / max(vol, 1e-8)})
    # 후보끼리도 같은 베팅을 중복하지 않게 점수순으로 한 종목씩 통과시킨다.
    independent = []
    for candidate in sorted(qualified, key=lambda x: x["score"], reverse=True):
        candidate_ret = _returns(dates_by.get(candidate["ticker"]) or [], prices.get(candidate["ticker"]) or [])
        overlap = []
        for selected in independent:
            selected_ret = _returns(dates_by.get(selected["ticker"]) or [], prices.get(selected["ticker"]) or [])
            corr = portfolio_risk.correlation(candidate_ret, selected_ret, min_observations=LOOKBACK_DAYS)
            if corr is None or corr >= CORRELATION_LIMIT:
                overlap.append(selected["ticker"])
        if overlap:
            rejected.append({"ticker": candidate["ticker"], "name": candidate["name"],
                             "reason": f"후보 {', '.join(overlap[:3])}와 고상관"})
        else:
            independent.append(candidate)
    selected = _allocate(independent[:MAX_CANDIDATES], cash_budget_pct=budget, sector_used=sector_used,
                         max_single_pct=float(profile["max_single_position_pct"]),
                         max_sector_pct=float(profile["max_sector_pct"]))
    for candidate in selected:
        candidate["proposed_value"] = round(candidate["proposed_weight_pct"] / 100 * total, 2)
        candidate.pop("preference", None)
    return {"ready": bool(selected), "mode": "shadow", "available_cash_pct": round(budget, 1),
            "cash_pct": round(cash_pct, 1), "correlation_limit": CORRELATION_LIMIT,
            "candidates": selected, "rejected": rejected[:20],
            "note": "현재 BUY·이벤트 위험 없음·60일 상관 독립성·현금/종목/섹터 한도를 모두 통과한 후보만 표시합니다. 실제 주문이나 수익 보장이 아닙니다."}
