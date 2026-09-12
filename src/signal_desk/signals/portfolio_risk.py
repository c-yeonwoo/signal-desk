"""상관·섹터 집중도 shadow — 여러 매수 신호를 독립 베팅으로 착각하지 않기 위한 관측 레이어.

HRP/리스크 패리티 같은 배분 규칙을 즉시 주문에 적용하지 않는다. 먼저 현재 레퍼런스 장부의
실제 집중도와 상관 데이터 커버리지를 노출하고, 이후 OOS 실행 성과로 제한 규칙을 검증한다.
"""

from __future__ import annotations

import math
from collections import defaultdict


def _returns(dates: list[str], closes: list[float]) -> dict[str, float]:
    out = {}
    for i in range(1, min(len(dates), len(closes))):
        try:
            prev, cur = float(closes[i - 1]), float(closes[i])
        except (TypeError, ValueError):
            continue
        if prev > 0 and cur > 0:
            out[str(dates[i])[:10]] = cur / prev - 1
    return out


def correlation(a: dict[str, float], b: dict[str, float], *, min_observations: int = 20) -> float | None:
    shared = sorted(set(a) & set(b))
    if len(shared) < min_observations:
        return None
    xs, ys = [a[d] for d in shared], [b[d] for d in shared]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    dx = sum((x - mx) ** 2 for x in xs)
    dy = sum((y - my) ** 2 for y in ys)
    if dx <= 0 or dy <= 0:
        return None
    return round(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(dx * dy), 4)


def correlation_matrix(tickers: list[str], dates_by: dict[str, list[str]], closes_by: dict[str, list[float]], *,
                       lookback_days: int = 60, min_observations: int = 20) -> dict[tuple[str, str], float | None]:
    returns = {t: _returns((dates_by.get(t) or [])[-(lookback_days + 1):],
                           (closes_by.get(t) or [])[-(lookback_days + 1):]) for t in tickers}
    return {(a, b): correlation(returns[a], returns[b], min_observations=min_observations)
            for i, a in enumerate(tickers) for b in tickers[i + 1:]}


def _hhi(weights: list[float]) -> float | None:
    total = sum(w for w in weights if w > 0)
    return round(sum((w / total) ** 2 for w in weights if w > 0), 4) if total > 0 else None


def diagnostics(holdings: list[dict], *, dates_by: dict[str, list[str]], closes_by: dict[str, list[float]],
                sector_by: dict[str, str], correlation_threshold: float = 0.75) -> dict:
    """보유 종목의 섹터·고상관 cluster 집중도와 데이터 부족을 함께 반환한다."""
    weights = {}
    for h in holdings:
        try:
            weight = float(h.get("qty") or 0) * float(h.get("price") or h.get("avg_price") or 0)
        except (TypeError, ValueError):
            continue
        if h.get("ticker") and weight > 0:
            weights[str(h["ticker"])] = weight
    tickers = list(weights)
    matrix = correlation_matrix(tickers, dates_by, closes_by)
    links: dict[str, set[str]] = {t: {t} for t in tickers}
    known, high_pairs = 0, []
    for (a, b), corr in matrix.items():
        if corr is None:
            continue
        known += 1
        if corr >= correlation_threshold:
            links[a].add(b); links[b].add(a)
            high_pairs.append({"a": a, "b": b, "correlation": corr})
    # connected component = 단일 고상관 연결 하나를 같은 위험 cluster로 본다.
    components, unseen = [], set(tickers)
    while unseen:
        seed, component, frontier = unseen.pop(), set(), []
        frontier.append(seed)
        while frontier:
            cur = frontier.pop()
            if cur in component:
                continue
            component.add(cur)
            frontier.extend(links[cur] - component)
        unseen -= component
        components.append(sorted(component))
    sector_weights: dict[str, float] = defaultdict(float)
    for t, w in weights.items():
        sector_weights[sector_by.get(t) or "미분류"] += w
    cluster_weights = [sum(weights[t] for t in c) for c in components]
    total = sum(weights.values())
    return {
        "holdings": len(tickers), "total_value": round(total, 2), "correlation_threshold": correlation_threshold,
        "pair_coverage": {"known": known, "total": len(matrix),
                          "pct": round(known / len(matrix) * 100, 1) if matrix else None},
        "high_correlation_pairs": sorted(high_pairs, key=lambda x: x["correlation"], reverse=True),
        "clusters": [{"tickers": c, "weight_pct": round(sum(weights[t] for t in c) / total * 100, 1)}
                     for c in sorted(components, key=lambda c: sum(weights[t] for t in c), reverse=True)] if total else [],
        "sector_weights": [{"sector": s, "weight_pct": round(w / total * 100, 1)}
                           for s, w in sorted(sector_weights.items(), key=lambda x: x[1], reverse=True)] if total else [],
        "sector_hhi": _hhi(list(sector_weights.values())), "cluster_hhi": _hhi(cluster_weights),
        "note": "shadow only — 집중 제한은 OOS 실행 성과를 확인하기 전에는 주문 규칙에 적용하지 않음",
    }
