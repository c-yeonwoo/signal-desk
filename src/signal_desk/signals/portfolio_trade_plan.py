"""목표비중 shadow를 비용·정수수량 제약이 있는 사용자 행동지침으로 변환한다.

여기서 만드는 것은 주문이 아닌 계획이다. 실제 호가·세금 계좌·부분 체결을 모르므로 추정 체결
가정과 잔여 오차를 항상 결과에 같이 내보낸다.
"""

from __future__ import annotations

import math

from signal_desk.broker import execution


def _whole_qty(value: float, price: float) -> int:
    return max(0, math.floor(abs(value) / price)) if price > 0 else 0


def plan(allocation: dict, rows: list[dict], *, cash: float, market: str) -> dict:
    if not allocation.get("ready"):
        return {"ready": False, "reason": allocation.get("reason") or "목표배분이 준비되지 않았습니다."}
    by_ticker = {str(row["ticker"]): row for row in rows}
    if any(abs(float(row.get("qty") or 0) - round(float(row.get("qty") or 0))) > 1e-9 for row in rows):
        return {"ready": False, "reason": "분할주 보유가 있어 정수수량 체결 모델로는 정확한 행동계획을 만들 수 없습니다."}
    sells, buys = [], []
    for item in allocation.get("items") or []:
        row = by_ticker.get(str(item["ticker"]))
        if not row or not row.get("price"):
            continue
        price, held = float(row["price"]), int(round(float(row.get("qty") or 0)))
        delta = float(item.get("delta_value") or 0)
        if item.get("action") == "축소 검토":
            qty = min(held, _whole_qty(delta, price))
            if qty:
                fill = execution.calculate(price, qty, "sell", market).as_dict()
                sells.append({"ticker": item["ticker"], "name": item.get("name"), "side": "sell", "qty": qty,
                              "target_weight_pct": item["target_weight_pct"], "fill": fill,
                              "reason": "목표비중 대비 과다"})
        elif item.get("action") == "확대 검토":
            qty = _whole_qty(delta, price)
            if qty:
                buys.append({"ticker": item["ticker"], "name": item.get("name"), "side": "buy", "qty": qty,
                             "target_weight_pct": item["target_weight_pct"], "price": price,
                             "reason": "목표비중 대비 부족"})
    # 매도부터 가정한다. 매수는 현금(입력 현금+매도 순유입) 한도 안에서 큰 부족분부터 채운다.
    available = max(0.0, float(cash or 0.0)) + sum(float(item["fill"]["cash_change"]) for item in sells)
    executed_buys = []
    for item in sorted(buys, key=lambda x: x["qty"] * x["price"], reverse=True):
        one_cost = -execution.calculate(item["price"], 1, "buy", market).cash_change
        qty = min(item["qty"], math.floor(available / one_cost)) if one_cost > 0 else 0
        if qty <= 0:
            item["unfunded_qty"] = item["qty"]
            continue
        if qty < item["qty"]:
            item["unfunded_qty"] = item["qty"] - qty
        fill = execution.calculate(item["price"], qty, "buy", market).as_dict()
        available += float(fill["cash_change"])
        executed_buys.append({k: v for k, v in item.items() if k != "price"} | {"qty": qty, "fill": fill})
    instructions = sells + executed_buys
    fees = sum(float(item["fill"]["total_fees"]) for item in instructions)
    slippage = sum(float(item["fill"]["slippage_cost"]) for item in instructions)
    unfunded = [{"ticker": item["ticker"], "remaining_qty": item["unfunded_qty"]}
                for item in buys if item.get("unfunded_qty")]
    return {
        "ready": True, "mode": "shadow", "execution_order": "sell_then_buy",
        "instructions": instructions, "unfunded_buys": unfunded,
        "estimated": {"cash_after": round(available, 2), "fees": round(fees, 2), "slippage": round(slippage, 2)},
        "note": "최근 종가와 기본 수수료·슬리피지 가정으로 만든 정수수량 계획입니다. 실제 호가, 계좌별 세금, 부분체결은 반영 전이므로 주문으로 전송되지 않습니다.",
    }
