"""실보유만 대상으로 한 제약형 리스크-패리티 목표배분 shadow.

기대수익률을 추정해 최적화하면 작은 표본에서 오차가 배분을 지배한다. 이 단계는 검증 가능한
과거 변동성·상관과 사용자가 입력한 집중도 한도만 사용한다. 결과는 매매 제안이며 주문이 아니다.
"""

from __future__ import annotations

from collections import defaultdict

import numpy as np


MIN_OBSERVATIONS = 60
SHRINKAGE = 0.35  # 표본 공분산을 대각행렬 쪽으로 축소해 작은 포트폴리오의 불안정을 줄인다.
TRADE_BAND_PCT = 1.0


def _returns(dates: list[str], closes: list[float]) -> dict[str, float]:
    out = {}
    for i in range(1, min(len(dates), len(closes))):
        try:
            prev, cur = float(closes[i - 1]), float(closes[i])
        except (TypeError, ValueError):
            continue
        if prev > 0 and cur > 0:
            out[str(dates[i])[:10]] = cur / prev - 1.0
    return out


def _aligned_returns(tickers: list[str], dates_by: dict[str, list[str]], closes_by: dict[str, list[float]]) -> np.ndarray:
    series = {ticker: _returns(dates_by.get(ticker) or [], closes_by.get(ticker) or []) for ticker in tickers}
    shared = set.intersection(*(set(values) for values in series.values())) if series else set()
    if len(shared) < MIN_OBSERVATIONS:
        return np.empty((0, len(tickers)))
    return np.asarray([[series[ticker][day] for ticker in tickers] for day in sorted(shared)], dtype=float)


def _shrunk_covariance(returns: np.ndarray) -> np.ndarray:
    cov = np.atleast_2d(np.cov(returns, rowvar=False, ddof=1))
    diagonal = np.diag(np.diag(cov))
    out = (1 - SHRINKAGE) * cov + SHRINKAGE * diagonal
    # 수치 오차로 0/음수 고유값이 생기는 것을 막는다. 목표비중을 과신하지 않기 위한 바닥값이다.
    return out + np.eye(out.shape[0]) * 1e-12


def _risk_parity(covariance: np.ndarray) -> np.ndarray:
    """long-only equal-risk-contribution 해. 수렴 실패 시에도 inverse-vol 선행값으로 안전 폴백한다."""
    n = covariance.shape[0]
    vol = np.sqrt(np.maximum(np.diag(covariance), 1e-16))
    weights = 1 / vol
    weights /= weights.sum()
    for _ in range(2_000):
        marginal = covariance @ weights
        contribution = weights * marginal
        total = float(contribution.sum())
        if total <= 0 or np.any(contribution <= 0):
            break
        target = total / n
        updated = weights * np.clip(target / contribution, 0.2, 5.0)
        updated /= updated.sum()
        if np.max(np.abs(updated - weights)) < 1e-10:
            weights = updated
            break
        weights = updated
    return weights


def _apply_caps(preference: np.ndarray, sectors: list[str], *, equity_budget: float,
                max_single: float, max_sector: float) -> tuple[np.ndarray, float]:
    """선호 가중치에 종목·섹터 cap을 적용해 물을 채우듯 재배분한다.

    남는 비중은 억지로 매수하지 않고 구조적 현금으로 반환한다. 이것이 cap이 서로 충돌하는
    경우에 숫자를 100%로 맞추며 한도를 깨는 것보다 정직하다.
    """
    n = len(preference)
    target = np.zeros(n)
    sector_used: dict[str, float] = defaultdict(float)
    remaining = max(0.0, equity_budget)
    for _ in range(n * 4):
        capacities = np.asarray([max(0.0, min(max_single - target[i], max_sector - sector_used[sectors[i]]))
                                 for i in range(n)])
        eligible = capacities > 1e-10
        if remaining <= 1e-10 or not eligible.any():
            break
        wanted = preference * eligible
        wanted /= wanted.sum()
        requested = remaining * wanted
        # 같은 섹터의 여러 종목이 동시에 배정되면 각 종목의 cap만으로는 섹터 cap을 넘길 수 있다.
        # 섹터별 요청 합계에 먼저 비례 축소를 적용한다.
        increment = requested.copy()
        for sector in set(sectors):
            idxs = [i for i, value in enumerate(sectors) if value == sector]
            requested_sector = float(requested[idxs].sum())
            sector_left = max(0.0, max_sector - sector_used[sector])
            if requested_sector > sector_left and requested_sector > 0:
                increment[idxs] *= sector_left / requested_sector
        increment = np.minimum(increment, capacities)
        if float(increment.sum()) <= 1e-10:
            break
        target += increment
        remaining -= float(increment.sum())
        for i, amount in enumerate(increment):
            sector_used[sectors[i]] += float(amount)
    return target, remaining


def propose(rows: list[dict], *, dates_by: dict[str, list[str]], closes_by: dict[str, list[float]],
            profile: dict) -> dict:
    """전체자산(현금 포함) 비중 기준 목표배분과 조정 방향.

    가격·섹터·60일 공통 이력이 모두 있는 보유만 대상으로 한다. 하나라도 빠지면 리스크 추정이
    완전하지 않으므로 목표비중을 만들지 않고 입력 보완을 요구한다.
    """
    eligible = [row for row in rows if row.get("value") is not None and row.get("sector") and row.get("history_ready")]
    if len(eligible) != len(rows):
        return {"ready": False, "mode": "shadow", "reason": "가격·섹터·공통 과거시세가 확인된 보유종목에서만 목표배분을 계산합니다."}
    if not eligible:
        return {"ready": False, "mode": "shadow", "reason": "목표배분을 계산할 보유종목이 없습니다."}
    tickers = [str(row["ticker"]) for row in eligible]
    returns = _aligned_returns(tickers, dates_by, closes_by)
    if len(returns) < MIN_OBSERVATIONS:
        return {"ready": False, "mode": "shadow", "reason": f"공통 거래일 {MIN_OBSERVATIONS}일이 부족해 상관 기반 목표배분을 보류합니다.",
                "observations": int(len(returns))}
    covariance = _shrunk_covariance(returns)
    preference = _risk_parity(covariance)
    budget = max(0.0, 100.0 - float(profile["min_cash_pct"]))
    target, structural_cash = _apply_caps(
        preference, [str(row["sector"]) for row in eligible], equity_budget=budget,
        max_single=float(profile["max_single_position_pct"]), max_sector=float(profile["max_sector_pct"]),
    )
    total = sum(float(row["value"]) for row in eligible) + max(0.0, float(profile.get("cash") or 0))
    current = np.asarray([float(row["value"]) / total * 100 if total > 0 else 0.0 for row in eligible])
    marginal = covariance @ target
    rc = target * marginal
    rc_total = float(rc.sum())
    items = []
    for i, row in enumerate(eligible):
        delta = float(target[i] - current[i])
        if delta < -TRADE_BAND_PCT:
            action = "축소 검토"
        elif delta > TRADE_BAND_PCT:
            action = "확대 검토"
        else:
            action = "유지"
        items.append({"ticker": row["ticker"], "name": row.get("name") or row["ticker"], "sector": row["sector"],
                      "current_weight_pct": round(float(current[i]), 1), "target_weight_pct": round(float(target[i]), 1),
                      "delta_weight_pct": round(delta, 1), "delta_value": round(delta / 100 * total, 2),
                      "risk_contribution_pct": round(float(rc[i]) / rc_total * 100, 1) if rc_total > 0 else None,
                      "action": action})
    items.sort(key=lambda x: abs(x["delta_weight_pct"]), reverse=True)
    return {
        "ready": True, "mode": "shadow", "method": "shrunk_covariance_risk_parity_with_caps",
        "observations": int(len(returns)), "shrinkage": SHRINKAGE, "trade_band_pct": TRADE_BAND_PCT,
        "equity_budget_pct": round(budget, 1), "structural_cash_pct": round(structural_cash, 1),
        "items": items,
        "note": "예상수익률을 사용하지 않는 위험기여 균형안입니다. 제약 충돌로 남는 비중은 현금으로 남기며 주문을 내지 않습니다.",
    }
