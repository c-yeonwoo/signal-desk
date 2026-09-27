"""Read-only three-profile comparison using one alpha snapshot and paper ledgers.

No trade path imports this module. The diagnostic sector/cluster caps come from the
existing portfolio UI defaults; they are *not* promoted reference-bot rules.
"""

from __future__ import annotations

import math

from signal_desk import strategy
from signal_desk.signals import portfolio_audit


def profile(style: str, cash: float) -> dict:
    if style not in strategy.STYLES:
        raise ValueError("unknown reference style")
    preset = strategy.preset(style)
    # The active bot caps each position and number of positions. This is their
    # implied maximum deployment, not a new calibration of expected returns.
    max_invested = min(1.0, preset["max_positions"] * preset["position_pct"])
    return {"cash": float(cash), "max_positions": preset["max_positions"],
            "min_cash_pct": round((1 - max_invested) * 100, 8),
            "max_single_position_pct": round(preset["position_pct"] * 100, 8),
            "max_sector_pct": 35.0, "max_cluster_pct": 45.0}


def reservation_orders(reservations: list[dict], *, balance: dict, style: str) -> dict:
    """Freeze the *estimated* quantity the existing bot would request at open.

    Reservations have no persisted quantity. A missing/invalid estimate blocks the
    comparison instead of treating that unknown liability as spendable cash.
    """
    orders = []
    for item in reservations:
        try:
            target = float(item["target_price"])
            chase = float(item["max_chase_pct"])
            qty = math.floor(min(float(balance["total_eval"]) * strategy.preset(style)["position_pct"],
                                 float(balance["cash"])) / target)
        except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError):
            return {"ready": False, "reason": "예약 주문의 수량·가격 추정 불가"}
        if item.get("side") != "buy" or not math.isfinite(target) or target <= 0 or not math.isfinite(chase) or chase < 0 or chase >= 1 or qty < 1:
            return {"ready": False, "reason": "예약 주문의 방향·상한·예상수량 확인 불가"}
        orders.append({"ticker": str(item["ticker"]), "side": "buy", "qty": qty,
                       "limit_price": target * (1 + chase)})
    return {"ready": True, "orders": orders,
            "quantity_status": "estimated_from_current_balance_not_persisted_order_quantity"}


def compare(*, market: str, universe: list[dict], prices: dict, dates_by: dict,
            signal_by_ticker: dict, signal_policy_id: str,
            balances: dict[str, dict], reservations: dict[str, list[dict]]) -> dict:
    """Three distinct risk profiles over exactly the same signal/close arrays."""
    assets = {str(a["ticker"]): a for a in universe if a.get("ticker")}
    # Non-BUY assets cannot affect this already-computed alpha decision. Keep the
    # common eligible pool plus all held/pending names; avoid serializing hundreds
    # of irrelevant price histories three times in an admin-only comparison.
    relevant = {t for t, s in signal_by_ticker.items() if getattr(s, "kind", None) in ("BUY", "STRONG_BUY")}
    relevant.update(str(h["ticker"]) for bal in balances.values() for h in bal["holdings"])
    relevant.update(str(r.get("ticker")) for orders in reservations.values() for r in orders)
    active_universe = [a for a in universe if str(a.get("ticker")) in relevant]
    active_signals = {t: s for t, s in signal_by_ticker.items() if t in relevant}
    results = {}
    for style in strategy.STYLES:
        bal = balances[style]
        planned = reservation_orders(reservations.get(style, []), balance=bal, style=style)
        if not planned["ready"]:
            results[style] = {"ready": False, "reason": planned["reason"]}
            continue
        rows = []
        for holding in bal["holdings"]:
            ticker = str(holding["ticker"])
            ps, ds = prices.get(ticker) or [], dates_by.get(ticker) or []
            try:
                price = float(ps[-1]) if ps else None
                if price is not None and (not math.isfinite(price) or price <= 0):
                    price = None
            except (TypeError, ValueError):
                price = None
            signal = signal_by_ticker.get(ticker)
            event_risk = bool(getattr(signal, "event_risk", False)) if signal else False
            rows.append({"ticker": ticker, "name": holding.get("name") or ticker,
                         "qty": holding["qty"], "price": price,
                         "value": price * holding["qty"] if price is not None else None,
                         "sector": assets.get(ticker, {}).get("sector"),
                         "history_ready": len(ps) >= 61 and len(ds) >= 61,
                         "price_as_of": ds[-1] if ds else None,
                         "entry_allowed": bool(signal and signal.kind in ("BUY", "STRONG_BUY") and not event_risk)})
        result, artifact = portfolio_audit.capture(
            rows=rows, universe=active_universe, signal_by_ticker=active_signals,
            prices=prices, dates_by=dates_by, profile=profile(style, bal["cash"]), market=market,
            signal_policy_id=signal_policy_id, pending_orders=planned["orders"])
        results[style] = {"ready": bool(result["trade_plan"].get("ready") and artifact["timing"].get("aligned")),
                          "style": style, "profile": profile(style, bal["cash"]),
                          "pending_quantity_status": planned["quantity_status"],
                          "timing": artifact["timing"], "result": result,
                          "audit_digest": portfolio_audit.digest(artifact)}
    return {"mode": "shadow", "market": market, "live_eligible": False,
            "same_alpha_snapshot": True, "profiles": results,
            "note": "세 성향은 동일한 신호·종가 입력을 공유합니다. 섹터/상관 한도는 진단용 기존 포트폴리오 기본값이며 봇 주문 규칙이 아닙니다. 예약수량은 현재 잔고로 추정한 값이고, 계좌·가격 시점과 실제 미체결/체결 대사는 아직 검증되지 않았습니다."}
