"""User-owned limits for a future reference-bot copy path.

This is a validation policy only. A saved policy never enables brokerage transport,
creates an order, or substitutes for a durable intent ledger.
"""

from __future__ import annotations

import math

from signal_desk import strategy

FIELDS = ("follow_pct", "max_order_pct", "max_daily_buy_pct", "max_position_pct", "min_cash_pct")

# These are deliberately small *defaults*, not claims that a style is safe or profitable.
DEFAULTS = {
    "conservative": {"follow_pct": 25.0, "max_order_pct": 5.0, "max_daily_buy_pct": 10.0,
                     "max_position_pct": 10.0, "min_cash_pct": 40.0},
    "balanced": {"follow_pct": 50.0, "max_order_pct": 10.0, "max_daily_buy_pct": 20.0,
                 "max_position_pct": 15.0, "min_cash_pct": 25.0},
    "aggressive": {"follow_pct": 75.0, "max_order_pct": 15.0, "max_daily_buy_pct": 30.0,
                   "max_position_pct": 20.0, "min_cash_pct": 15.0},
}


def defaults(style: str = "balanced") -> dict:
    style = strategy.normalize(style)
    return {"source_style": style, **DEFAULTS[style], "configured": False,
            "mode": "copy_preview_only", "order_transmission_enabled": False}


def validate(data: dict, *, existing: dict | None = None) -> dict:
    """Merge a partial user update, reject coercion/NaN and contradictory constraints."""
    style = strategy.normalize(data.get("source_style") or (existing or {}).get("source_style") or "balanced")
    base = defaults(style) if not existing else {**defaults(style), **existing, "source_style": style}
    out = {"source_style": style}
    for key in FIELDS:
        value = data[key] if key in data else base[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError("유한한 숫자 비율이 필요합니다.")
        value = float(value)
        # 100% above cannot mean a percentage; do not silently turn it into leverage.
        if ((key == "min_cash_pct" and not 0 <= value < 100)
                or (key != "min_cash_pct" and not 0 < value <= 100)):
            raise ValueError("추종·주문·일일·종목 비중은 0 초과 100 이하, 최소 현금은 0 이상 100 미만으로 설정하세요.")
        out[key] = round(value, 4)
    if out["max_order_pct"] > out["max_daily_buy_pct"]:
        raise ValueError("주문당 한도는 일일 매수 한도보다 클 수 없습니다.")
    if out["max_order_pct"] > out["max_position_pct"]:
        raise ValueError("주문당 한도는 종목 최대비중보다 클 수 없습니다.")
    return {**out, "configured": True, "mode": "copy_preview_only", "order_transmission_enabled": False}


def scaled_quantity(source: dict, *, follow_pct: float, limit_price: int) -> dict:
    """Scale notional, not share count, so a different price cannot magnify exposure."""
    try:
        qty, source_price = int(source["qty"]), float(source["price"])
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ValueError("참조 봇 체결 수량·가격이 유효하지 않습니다.") from None
    if (isinstance(source.get("qty"), bool) or qty <= 0 or source_price <= 0 or not math.isfinite(source_price)
            or limit_price <= 0):
        raise ValueError("참조 체결 또는 지정가가 유효하지 않습니다.")
    reference_notional = qty * source_price
    requested_notional = reference_notional * follow_pct / 100
    copied_qty = math.floor(requested_notional / limit_price)
    return {"source_notional": round(reference_notional, 2), "follow_pct": follow_pct,
            "requested_notional": round(requested_notional, 2), "qty": copied_qty,
            "unallocated_notional": round(requested_notional - copied_qty * limit_price, 2)}
