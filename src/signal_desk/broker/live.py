"""실전 연결의 조회 전용 경계. 자동매매/주문 활성화 기능은 의도적으로 제공하지 않는다.

잔고/미체결 확인 → 미수 없는 주문 여력 확인까지 실제 어댑터를 사용하며,
사전조회 성공과 투자정책 승인·주문 전송 승인을 서로 다른 상태로 둔다.
"""

from __future__ import annotations

import datetime
import math
import time
from zoneinfo import ZoneInfo

from signal_desk import config
from signal_desk.broker import execution, kis, live_policy

BLOCKERS = ["policy_oos_not_promoted", "durable_order_submission_not_enabled",
            "session_quote_and_risk_limits_not_certified"]
COPY_BLOCKERS = ["reference_event_intent_not_durable", *BLOCKERS]


def status() -> dict:
    try:
        creds = config.kis_credentials()
    except ValueError:
        return {"configured": False, "mode": "read_only", "order_transmission_enabled": False,
                "blockers": ["invalid_environment", *BLOCKERS]}
    return {"configured": bool(creds), "environment": creds["env"] if creds else None,
            "owner_configured": bool(config.kis_account_owner()), "market": "kr", "broker": "kis",
            "mode": "read_only", "order_transmission_enabled": False, "blockers": list(BLOCKERS)}


def snapshot(creds: dict) -> dict:
    start = time.time()
    balance = kis.balance(creds, retries=1)
    day = datetime.datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
    orders = kis.daily_orders(day, creds)
    valid = bool(balance and balance.get("complete") and orders and orders.get("complete"))
    if valid:
        valid = all(math.isfinite(float(balance[k])) and balance[k] >= 0 for k in ("cash", "total_eval"))
        valid = valid and all(math.isfinite(float(h[k])) and h[k] >= 0
                              for h in balance["holdings"] for k in ("qty", "price"))
        valid = valid and all(h["price"] > 0 for h in balance["holdings"] if h["qty"] > 0)
    return {"ready": valid, "mode": "read_only", "order_transmission_enabled": False,
            "observed_at": int(time.time()), "query_started_at": int(start), "environment": creds["env"],
            "balance": balance if valid else None, "order_status": orders if valid else None,
            "reason": None if valid else "잔고·당일 주문의 완전한 조회/수치 검증 실패"}


def _copy_limit_checks(*, snap: dict, ticker: str, side: str, fill: dict, policy: dict,
                       buying_power: dict | None) -> dict:
    """Apply saved limits to a candidate only; a passing result is never an authorization."""
    total = float(snap["balance"]["total_eval"])
    gross = float(fill["gross_notional"])
    checks = []
    def check(key: str, actual: float, limit: float, *, minimum: bool = False, unit: str = "KRW"):
        checks.append({"key": key, "actual": round(actual, 2), "limit": round(limit, 2),
                       "unit": unit, "comparison": "at_least" if minimum else "at_most",
                       "passed": actual + 1e-8 >= limit if minimum else actual <= limit + 1e-8})
    check("order_notional", gross, total * float(policy["max_order_pct"]) / 100)
    current = sum(float(h["qty"]) * float(h["price"]) for h in snap["balance"]["holdings"]
                  if h["ticker"] == ticker)
    position_after = current + gross if side == "buy" else max(0.0, current - gross)
    check("position_notional", position_after, total * float(policy["max_position_pct"]) / 100)
    if side == "buy":
        if buying_power is None:
            checks.append({"key": "minimum_cash", "passed": False, "reason": "주문가능금액 확인 실패"})
        else:
            remaining = float(buying_power["cash_without_margin"]) + float(fill["cash_change"])
            check("minimum_cash", remaining, total * float(policy["min_cash_pct"]) / 100, minimum=True)
    # The broker's daily order feed is reconciliation, not this app's reservation ledger.
    # Do not invent daily use from missing fill prices/status transitions.
    checks.append({"key": "daily_buy_budget", "passed": False,
                   "reason": "주문 intent·예약 원장 전에는 일일 누적 매수 한도를 확정할 수 없습니다."})
    caps_passed = all(item["passed"] for item in checks if item["key"] != "daily_buy_budget")
    return {"passed": False, "caps_passed": caps_passed, "checks": checks,
            "blockers": list(COPY_BLOCKERS),
            "reason": ("설정한 주문·종목·현금 한도는 통과했지만 일일 예약 원장이 없어 복사 주문을 보류합니다."
                       if caps_passed else "설정한 실계좌 한도를 초과해 복사 주문을 보류합니다.")}


def preflight(data: dict, creds: dict, *, copy_policy: dict | None = None) -> dict:
    """소유자가 입력한 지정가 주문의 사전조회. 요청 본문으로 자격증명/승격 여부를 받지 않는다."""
    ticker, side = str(data.get("ticker", "")), data.get("side")
    qty, price = data.get("qty"), data.get("limit_price")
    if (len(ticker) != 6 or not ticker.isascii() or not ticker.isdigit() or side not in {"buy", "sell"}
            or isinstance(qty, bool) or not isinstance(qty, int) or not 0 < qty <= 1_000_000
            or isinstance(price, bool) or not isinstance(price, int) or not 0 < price <= 100_000_000):
        raise ValueError("국내 종목코드·매수/매도·양의 정수 수량·지정가가 필요합니다.")
    snap = snapshot(creds)
    result = {"ready": False, "broker_checks_passed": False, "mode": "read_only",
              "order_transmission_enabled": False, "blockers": list(BLOCKERS),
              "order": {"ticker": ticker, "side": side, "qty": qty, "limit_price": price},
              "snapshot_observed_at": snap["observed_at"]}
    if not snap["ready"]:
        return {**result, "reason": snap["reason"]}
    # 미체결의 예약 자금/수량을 완전히 대조하기 전에는 추가 주문 가능 판정을 하지 않는다.
    if any(o["status"] in {"open", "partial", "unknown"} for o in snap["order_status"]["orders"]):
        return {**result, "reason": "미체결/부분체결/상태 불명 주문이 있어 사전검증을 보류합니다."}
    power = None
    if side == "buy":
        power = kis.buying_power(ticker, price, creds)
        if power is None:
            return {**result, "reason": "미수 없는 주문가능금액 조회 실패"}
        estimated_cost = -execution.calculate(price, qty, "buy", "kr").cash_change
        passed = qty <= power["qty_without_margin"] and estimated_cost <= power["cash_without_margin"]
        result["buying_power"] = power
        result["estimated_cash_required"] = estimated_cost
    else:
        positions = [h for h in snap["balance"]["holdings"] if h["ticker"] == ticker]
        if not positions or any(h.get("sellable_qty") is None for h in positions):
            return {**result, "reason": "증권사의 매도가능수량을 확인할 수 없습니다."}
        passed = qty <= sum(h["sellable_qty"] for h in positions)
    if time.time() - snap["query_started_at"] > 30:
        return {**result, "reason": "조회가 지연되어 계좌 상태를 다시 확인해야 합니다."}
    if not passed:
        return {**result, "broker_checks_passed": False, "reason": "계좌 여력 부족"}
    if copy_policy is not None:
        fill = execution.calculate(price, qty, side, "kr").as_dict()
        checks = _copy_limit_checks(snap=snap, ticker=ticker, side=side, fill=fill,
                                    policy=copy_policy, buying_power=power)
        return {**result, "broker_checks_passed": True, "copy_policy": checks,
                "reason": checks["reason"]}
    return {**result, "broker_checks_passed": True,
            "reason": "계좌 여력 조회 통과. 투자판단 승인 또는 주문 승인이 아닙니다."}


def copy_preview(source: dict, *, limit_price: int, policy: dict, creds: dict) -> dict:
    """Scale a stored reference event under saved caps; no broker order is ever built."""
    if source.get("side") not in {"buy", "sell"}:
        raise ValueError("참조 봇의 매매 방향이 유효하지 않습니다.")
    scaled = live_policy.scaled_quantity(source, follow_pct=float(policy["follow_pct"]), limit_price=limit_price)
    base = {"ready": False, "mode": "copy_preview_only", "order_transmission_enabled": False,
            "source": {k: source.get(k) for k in ("id", "ticker", "name", "side", "qty", "price", "ts", "reason")},
            "copy": scaled, "policy": policy, "blockers": list(COPY_BLOCKERS)}
    if scaled["qty"] <= 0:
        return {**base, "reason": "추종 비율을 적용한 금액이 지정가 기준 1주에 못 미쳐 복사를 보류합니다."}
    candidate = preflight({"ticker": source["ticker"], "side": source["side"], "qty": scaled["qty"],
                           "limit_price": limit_price}, creds, copy_policy=policy)
    return {**base, "preflight": candidate, "reason": candidate["reason"]}
