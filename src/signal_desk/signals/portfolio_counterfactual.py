"""One frozen plan versus hold and cash, on the same forward close-price panel.

This is a paired shadow episode, not an OOS strategy track record. No cash flows,
dividend credits, automatic rebalancing, or actual broker fills are inferred.
"""

from __future__ import annotations

import math

import pandas as pd

from signal_desk.broker import execution
from signal_desk.signals import portfolio_audit as audit

VERSION = "paired-close-episode-v1"


def evaluate(body: dict, *, prices: dict, dates_by: dict, now=None) -> dict:
    base = {"version": VERSION, "mode": "shadow", "live_eligible": False, "oos_verified": False,
            "note": "동일 초기자산의 단일 고정 계획 비교입니다. 거래비용 반영 평가자산이며, 배당·기업행동·실제 체결·통계적 우위는 검증 전입니다."}

    def blocked(reason, panel=None):
        return {**base, "ready": False, "reason": reason, "observed_panel": panel or {}, "path": []}

    timing, inputs = body["timing"], body["inputs"]
    plan = body["result"]["trade_plan"]
    if not timing.get("aligned") or not plan.get("ready"):
        return blocked("판단 시점/행동계획 검증 미충족")
    now = now or audit.utc_now()
    sessions = [s for s in timing["evaluation_sessions"] if pd.Timestamp(s["close"]) < pd.Timestamp(now)]
    if not sessions:
        return blocked("판단 이후 첫 평가 거래일 종가를 기다립니다.")
    try:
        held = {r["ticker"]: float(r["qty"]) for r in inputs["rows"]}
        if any(not math.isfinite(q) or q < 0 or not q.is_integer() for q in held.values()):
            return blocked("정수 보유수량 검증 실패")
        tickers = sorted(set(held) | {i["ticker"] for i in plan.get("instructions", [])})
        panel = {}
        for ticker in tickers:
            ds, ps = dates_by.get(ticker, []), prices.get(ticker, [])
            if len(ds) != len(ps) or ds != sorted(set(ds)):
                return blocked("평가 가격·날짜 정합성 실패: " + ticker)
            mapping = dict(zip(ds, ps))
            reference_day = inputs["dates_by"][ticker][-1]
            reference_price = inputs["prices"][ticker][-1]
            # A split/restatement may change the historical adjustment basis. Never splice it silently.
            if mapping.get(reference_day) != reference_price:
                return blocked("저장 당시 기준 가격이 변경/누락되었습니다. 기업행동·가격 수정 검토 필요: " + ticker)
            panel[ticker] = {}
            for session in sessions:
                day = session["date"]
                price = mapping.get(day)
                if isinstance(price, bool) or price is None or not math.isfinite(float(price)) or float(price) <= 0:
                    return blocked("완료 거래일 가격 결손 — 날짜 건너뛰기/전일가 대체 없음: " + ticker + " " + day)
                panel[ticker][day] = float(price)
        if not tickers:
            return blocked("비교할 자산 없음")
        initial = float(inputs["profile"]["cash"]) + sum(float(r["value"]) for r in inputs["rows"])
        if not math.isfinite(initial) or initial <= 0:
            return blocked("초기 자산 검증 실패")
        cash = {k: float(inputs["profile"]["cash"]) for k in ("policy", "hold", "cash")}
        positions = {k: dict(held) for k in cash}
        fills = {"policy": [], "hold": [], "cash": []}
        day = sessions[0]["date"]
        assumptions, market = inputs["assumptions"], inputs["market"]

        def fill(portfolio, ticker, side, qty):
            if isinstance(qty, bool) or qty <= 0 or int(qty) != qty:
                raise ValueError("invalid order quantity")
            qty = int(qty)
            current = positions[portfolio].get(ticker, 0)
            result = execution.calculate(panel[ticker][day], qty, side, market, assumptions=assumptions)
            if (side == "sell" and qty > current) or cash[portfolio] + result.cash_change < -1e-7:
                raise ValueError("fixed plan no longer funded")
            cash[portfolio] += result.cash_change
            positions[portfolio][ticker] = current + qty * (1 if side == "buy" else -1)
            fills[portfolio].append({"ticker": ticker, **result.as_dict()})

        # Fixed quantities; no hindsight resizing when the next-session price gaps.
        for item in sorted(plan.get("instructions", []), key=lambda i: i["side"] != "sell"):
            if item["side"] not in ("buy", "sell"):
                raise ValueError("invalid side")
            fill("policy", item["ticker"], item["side"], item["qty"])
        for ticker, qty in held.items():
            if qty:
                fill("cash", ticker, "sell", qty)
        path = []
        peaks = {k: initial for k in cash}
        worst = {k: 0.0 for k in cash}
        for session in sessions:
            day = session["date"]
            point = {"date": day}
            for k in cash:
                nav = cash[k] + sum(q * panel[t][day] for t, q in positions[k].items())
                if not math.isfinite(nav) or nav < 0:
                    raise ValueError("invalid NAV")
                peaks[k] = max(peaks[k], nav)
                worst[k] = min(worst[k], (nav / peaks[k] - 1) * 100)
                point[k] = nav
            path.append(point)
        metrics = {k: {"nav": path[-1][k], "return_pct": (path[-1][k] / initial - 1) * 100,
                       "max_drawdown_pct": worst[k], "fees": sum(f["total_fees"] for f in fills[k]),
                       "slippage": sum(f["slippage_cost"] for f in fills[k])} for k in cash}
        return {**base, "ready": True, "complete": len(sessions) == len(timing["evaluation_sessions"]),
                "initial_value": initial, "entry_date": sessions[0]["date"], "as_of": path[-1]["date"],
                "completed_sessions": len(sessions), "target_sessions": len(timing["evaluation_sessions"]),
                "metrics": metrics, "delta_vs_hold_pp": metrics["policy"]["return_pct"] - metrics["hold"]["return_pct"],
                "path": path, "fills": fills, "observed_panel": panel}
    except (TypeError, ValueError, KeyError, OverflowError):
        return blocked("동결 계획을 다음 거래일 가격에 적용할 수 없습니다. 자금/수량/가격을 확인하세요.")
