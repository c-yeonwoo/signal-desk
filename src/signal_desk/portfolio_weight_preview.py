"""Read-only arithmetic for a user-entered addition to analysis holdings.

This is deliberately not a trade plan: no share rounding, order pricing, or
broker cash is inferred from a manual portfolio input.
"""

from __future__ import annotations

import math


def _positive(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def preview(*, ticker: str, amount: object, cash: object, rows: list[dict],
            expected_session: str | None, profile: dict, market: str) -> dict:
    base = {"ready": False, "market": market, "scope": "manual_analysis",
            "not_order_advice": True, "order_created": False}
    spend = _positive(amount)
    if spend is None:
        return {**base, "reason": "추가할 금액은 0보다 큰 숫자로 입력하세요."}
    try:
        available = float(cash)
    except (TypeError, ValueError, OverflowError):
        available = math.nan
    if not math.isfinite(available) or available < 0:
        return {**base, "reason": "분석용 현금 입력을 확인할 수 없습니다."}
    if not expected_session:
        return {**base, "reason": "마지막으로 마감한 거래일을 확인할 수 없습니다."}
    if not rows:
        return {**base, "reason": "먼저 분석용 보유종목을 입력하세요."}

    valued = {}
    sectors = {}
    for row in rows:
        symbol = str(row.get("ticker") or "")
        quantity, close = _positive(row.get("qty")), _positive(row.get("close"))
        if (not symbol or symbol in valued or quantity is None or close is None
                or row.get("close_date") != expected_session):
            return {**base, "reason": "보유종목의 확정 종가가 모두 같은 최신 거래일에 있어야 합니다."}
        valued[symbol] = quantity * close
        sectors[symbol] = row.get("sector") or None
    if ticker not in valued:
        return {**base, "reason": "선택한 종목이 이 시장의 분석용 보유 입력에 없습니다."}
    if spend > available:
        return {**base, "reason": "입력 금액이 분석용 현금보다 큽니다. 차입을 가정하지 않습니다."}
    total = available + sum(valued.values())
    if not math.isfinite(total) or total <= 0:
        return {**base, "reason": "전체 자산 값을 계산할 수 없습니다."}
    before = valued[ticker] / total * 100
    after = (valued[ticker] + spend) / total * 100
    cash_before = available / total * 100
    cash_after = (available - spend) / total * 100
    sector = sectors[ticker]
    # Unknown peers may belong to this sector; a partial map understates concentration.
    sector_before = (sum(value for symbol, value in valued.items() if sectors[symbol] == sector) / total * 100
                     if sector and all(sectors.values()) else None)
    sector_after = sector_before + spend / total * 100 if sector_before is not None else None
    single_limit = _positive(profile.get("max_single_position_pct"))
    sector_limit = _positive(profile.get("max_sector_pct"))
    min_cash = profile.get("min_cash_pct")
    try:
        min_cash = float(min_cash)
    except (TypeError, ValueError, OverflowError):
        min_cash = math.nan
    limits = {"single_over": single_limit is not None and after > single_limit,
              "sector_over": sector_after is not None and sector_limit is not None and sector_after > sector_limit,
              "cash_below_min": math.isfinite(min_cash) and cash_after < min_cash}
    return {**base, "ready": True, "ticker": ticker, "currency": "USD" if market == "us" else "KRW",
            "price_session": expected_session, "entered_amount": spend, "total_value": round(total, 2),
            "cash_before": round(available, 2), "cash_after": round(available - spend, 2),
            "weight_before_pct": round(before, 2), "weight_after_pct": round(after, 2),
            "cash_weight_before_pct": round(cash_before, 2), "cash_weight_after_pct": round(cash_after, 2),
            "sector": sector, "sector_weight_before_pct": round(sector_before, 2) if sector_before is not None else None,
            "sector_weight_after_pct": round(sector_after, 2) if sector_after is not None else None,
            "limits": limits,
            "note": "분석용 입력과 확정 종가의 산술 비교입니다. 실계좌 잔고·현재가·체결·비용이 아니며 주문을 만들지 않습니다."}
