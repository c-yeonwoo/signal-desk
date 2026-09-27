"""Unfilled orders are commitments, not available cash or confirmed holdings.

This is a conservative *shadow* projection. A pending buy consumes its worst-case
limit-price cash and occupies risk capacity, but never creates a real fill. A pending
sell locks its shares without crediting proceeds. Unknown quantities/prices abstain.
"""

from __future__ import annotations

import math

from signal_desk.broker import execution


def project(rows: list[dict], *, cash: float, orders: list[dict], market: str,
            assumptions: dict | None = None) -> dict:
    projected = [dict(row) for row in rows]
    by_ticker = {str(row["ticker"]): row for row in projected}
    if len(by_ticker) != len(projected):
        return {"ready": False, "reason": "중복 보유 종목이 있어 미체결 상태를 계산할 수 없습니다."}
    if not math.isfinite(float(cash)) or cash < 0:
        return {"ready": False, "reason": "현금 잔액이 유효하지 않습니다."}
    available = float(cash)
    commitments = []
    for order in orders:
        ticker = str(order.get("ticker") or "")
        row = by_ticker.get(ticker)
        side, qty, limit = order.get("side"), order.get("qty"), order.get("limit_price")
        if not row or side not in ("buy", "sell") or not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
            return {"ready": False, "reason": "미체결 주문의 종목·방향·정수수량을 확인할 수 없습니다."}
        try:
            limit = float(limit)
            valid = math.isfinite(limit) and limit > 0
        except (TypeError, ValueError):
            valid = False
        if not valid or not row.get("price") or not math.isfinite(float(row["price"])) or float(row["price"]) <= 0:
            return {"ready": False, "reason": "미체결 주문의 상한가 또는 평가가격을 확인할 수 없습니다."}
        row["pending_order"] = True
        if side == "buy":
            # A limit is the worst fill price; commission still applies, while
            # slippage must not make the reservation exceed the order's limit.
            frozen = dict(execution.cost_assumptions(market) if assumptions is None else assumptions)
            frozen["slippage_bps"] = 0.0
            try:
                commitment = -execution.calculate(limit, qty, "buy", market, assumptions=frozen).cash_change
            except (ValueError, TypeError, OverflowError):
                return {"ready": False, "reason": "미체결 주문의 비용 가정을 확인할 수 없습니다."}
            available -= commitment
            if available < -1e-8:
                return {"ready": False, "reason": "미체결 매수의 예약금액이 현금을 초과합니다."}
            row["pending_buy_qty"] = int(row.get("pending_buy_qty") or 0) + qty
            row["pending_risk_value"] = float(row.get("pending_risk_value") or 0) + qty * max(limit, float(row["price"]))
            row["qty"] = float(row.get("qty") or 0) + qty
            row["value"] = float(row["qty"]) * float(row["price"])
            commitments.append({"ticker": ticker, "side": side, "qty": qty,
                                "limit_price": limit, "cash_reserved": round(commitment, 8)})
        else:
            held = float(row.get("qty") or 0)
            locked = float(row.get("pending_sell_qty") or 0) + qty
            if not math.isfinite(held) or locked > held:
                return {"ready": False, "reason": "미체결 매도수량이 실제 보유수량을 초과합니다."}
            row["pending_sell_qty"] = locked
            commitments.append({"ticker": ticker, "side": side, "qty": qty,
                                "limit_price": limit, "cash_reserved": 0.0})
    return {"ready": True, "rows": projected, "cash": max(0.0, available),
            "commitments": commitments,
            "note": "매수는 지정가 상한 체결을 가정해 현금·위험한도를 선점하고, 매도대금은 체결 전 사용하지 않습니다. 실제 체결로 취급하지 않습니다."}
