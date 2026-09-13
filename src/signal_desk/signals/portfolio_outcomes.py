"""포트폴리오 행동계획의 사후 비용·방향성 결과 재현.

이는 사용자가 실제로 주문했다는 기록이 아니다. 분석 시점의 제안이 이후 가격 경로에서 어떤
반사실적 결과를 냈는지 보는 shadow 측정이며, 매수와 매도는 같은 수익률로 섞지 않는다.
"""

from __future__ import annotations

from signal_desk.broker import execution


HORIZONS = (1, 5, 20)


def evaluate(item: dict, *, dates: list[str], closes: list[float], market: str) -> list[dict]:
    reference_date = str(item.get("reference_date") or "")[:10]
    normalized_dates = [str(day)[:10] for day in dates]
    try:
        start = normalized_dates.index(reference_date)
    except ValueError:
        return []
    try:
        reference = float(item["reference_price"])
        qty = int(item["qty"])
    except (TypeError, ValueError):
        return []
    if reference <= 0 or qty <= 0:
        return []
    out = []
    for horizon in HORIZONS:
        end = start + horizon
        if end >= len(closes):
            continue
        try:
            exit_price = float(closes[end])
        except (TypeError, ValueError):
            continue
        if exit_price <= 0:
            continue
        raw = (exit_price / reference - 1) * 100
        side = item.get("side")
        directional = raw if side == "buy" else -raw
        cost_adjusted = None
        if side == "buy" and item.get("entry_cash"):
            exit_fill = execution.calculate(exit_price, qty, "sell", market)
            cost_adjusted = (exit_fill.cash_change - float(item["entry_cash"])) / float(item["entry_cash"]) * 100
        out.append({"horizon_days": horizon, "evaluated_price": round(exit_price, 8),
                    "evaluated_date": normalized_dates[end], "raw_return_pct": round(raw, 4),
                    "directional_return_pct": round(directional, 4),
                    "cost_adjusted_return_pct": round(cost_adjusted, 4) if cost_adjusted is not None else None})
    return out
