"""신규·기존 보유에 하나의 현금 원장/배분/수량 검증을 적용하는 shadow 판단.

수익 최적화 정책이 아니다. 기존 후보 필터와 위험배분을 결합한 보수적 기준 구현이며,
실전 활성화는 별도 OOS·시점/체결 품질 검증을 필요로 한다.
"""

from __future__ import annotations

import hashlib
import json
import math
import datetime
from functools import lru_cache
from pathlib import Path

from signal_desk.signals import portfolio_candidates, portfolio_construction, portfolio_marginal, portfolio_pending, portfolio_trade_plan

POLICY_VERSION = "joint-risk-shadow-v2-pending"


@lru_cache(maxsize=1)
def _source_id() -> str:
    base = Path(__file__).resolve().parent
    files = (base / name for name in ("portfolio_decision.py", "portfolio_candidates.py",
             "portfolio_construction.py", "portfolio_risk.py", "portfolio_trade_plan.py", "portfolio_pending.py"))
    return hashlib.sha256(b"".join(p.read_bytes() for p in files)).hexdigest()[:24]


def _blocked(reason: str, *, policy_id: str | None = None,
             signal_policy_id: str | None = None) -> dict:
    return {key: {"ready": False, "mode": "shadow", "reason": reason, "instructions": []}
            for key in ("allocation", "trade_plan", "entry_candidates")} | {
                "decision": {"policy_version": POLICY_VERSION, "policy_id": policy_id,
                             "signal_policy_id": signal_policy_id,
                             "mode": "shadow", "live_eligible": False, "reason": reason}}


def decide(*, rows: list[dict], universe: list[dict], signal_by_ticker: dict,
           prices: dict, dates_by: dict, profile: dict, market: str,
           assumptions: dict | None = None, signal_policy_id: str | None = None,
           pending_orders: list[dict] | None = None) -> dict:
    policy_material = {"version": POLICY_VERSION, "source": _source_id(), "market": market,
                       "profile": {k: v for k, v in profile.items()
                                   if k not in ("updated", "configured", "cash")},
                       "assumptions": assumptions, "signal_policy_id": signal_policy_id}
    policy_id = hashlib.sha256(json.dumps(policy_material, sort_keys=True,
                                          allow_nan=False).encode()).hexdigest()[:24]
    if any(r.get("value") is None or not math.isfinite(float(r["value"])) or float(r["value"]) < 0 for r in rows):
        return _blocked("전체 보유의 평가액을 확정할 수 없어 통합 계획을 보류합니다.",
                        policy_id=policy_id, signal_policy_id=signal_policy_id)
    pending_orders = pending_orders or []
    # A pending new buy must be included in exposure/correlation even though it is
    # not a confirmed holding. Its evaluated shares are a conditional projection.
    prepared = list(rows)
    held = {str(row["ticker"]) for row in rows}
    assets = {str(asset["ticker"]): asset for asset in universe if asset.get("ticker")}
    for order in pending_orders:
        ticker = str(order.get("ticker") or "")
        if ticker in held or ticker not in assets:
            continue
        ds, ps = dates_by.get(ticker) or [], prices.get(ticker) or []
        if not ds or not ps:
            return _blocked("미체결 종목의 기준 가격·거래일 확인 불가: " + ticker,
                            policy_id=policy_id, signal_policy_id=signal_policy_id)
        asset = assets[ticker]
        prepared.append({"ticker": ticker, "name": asset.get("name") or ticker,
                         "sector": asset.get("sector"), "qty": 0, "value": 0,
                         "price": ps[-1], "history_ready": len(ds) >= 61 and len(ps) >= 61,
                         "price_as_of": ds[-1], "entry_allowed": False})
        held.add(ticker)
    pending = portfolio_pending.project(prepared, cash=profile["cash"], orders=pending_orders,
                                        market=market, assumptions=assumptions)
    if not pending["ready"]:
        return _blocked(pending["reason"], policy_id=policy_id, signal_policy_id=signal_policy_id)
    rows = pending["rows"]
    projected_profile = {**profile, "cash": pending["cash"]}
    candidates = portfolio_candidates.evaluate(
        holdings=rows, universe=universe, signal_by_ticker=signal_by_ticker,
        prices=prices, dates_by=dates_by, profile=projected_profile)
    combined = list(rows)
    for item in candidates.get("candidates", []):
        ticker = item["ticker"]
        combined.append({"ticker": ticker, "name": item["name"], "sector": item["sector"],
                         "qty": 0, "value": 0, "price": item["price"], "history_ready": True,
                         "price_as_of": dates_by[ticker][-1], "entry_allowed": True})
    end_dates = set()
    for row in combined:
        ticker = row["ticker"]
        ds, ps = dates_by.get(ticker, []), prices.get(ticker, [])
        try:
            days = [datetime.date.fromisoformat(str(d)[:10]).isoformat() for d in ds]
            valid = bool(days) and len(ds) == len(ps) and days == sorted(set(days))
            valid = valid and all(math.isfinite(float(p)) and float(p) > 0 for p in ps)
        except (TypeError, ValueError):
            valid = False
        if not valid:
            return _blocked("가격·날짜 정합성 검사 실패: " + str(ticker),
                            policy_id=policy_id, signal_policy_id=signal_policy_id)
        end_dates.add(days[-1])
    if len(end_dates) > 1:
        return _blocked("종목별 최종 가격 기준일이 달라 통합 계획을 보류합니다.",
                        policy_id=policy_id, signal_policy_id=signal_policy_id)
    allocation = portfolio_construction.propose(combined, dates_by=dates_by, closes_by=prices, profile=projected_profile)
    trade = portfolio_trade_plan.plan(allocation, combined, cash=pending["cash"], market=market,
                                      profile=projected_profile, assumptions=assumptions)
    trade["pending_commitments"] = pending["commitments"]
    trade["pending_note"] = pending["note"]
    # 후보 비중은 독립적인 두 번째 지출 계획이 아니라 통합 계획의 실제 정수 수량을 표시한다.
    buys = {i["ticker"]: i for i in trade.get("instructions", []) if i["side"] == "buy"}
    total_after = (trade.get("post_trade") or {}).get("total_value", 0)
    for item in candidates.get("candidates", []):
        instruction = buys.get(item["ticker"])
        qty = instruction["qty"] if instruction else 0
        item.update({"proposed_qty": qty, "proposed_value": qty * item["price"],
                     "proposed_weight_pct": qty * item["price"] / total_after * 100 if total_after else 0,
                     "plan_status": "included" if qty else "not_funded_or_blocked"})
        if qty:
            item["marginal_risk"] = portfolio_marginal.assess(
                holdings=rows, candidate=item, cash=pending["cash"],
                dates_by=dates_by, closes_by=prices)
    candidates["ready"] = any(i.get("proposed_qty", 0) for i in candidates.get("candidates", []))
    candidates["note"] = "통합 행동계획에 포함된 금액입니다. 기존 보유 매매와 별도로 추가 집행하지 마세요."
    material = {"policy_version": POLICY_VERSION, "policy_id": policy_id,
                "market": market, "profile": profile,
                "rows": combined, "pending_orders": pending_orders,
                "allocation": allocation, "trade_plan": trade}
    digest = hashlib.sha256(json.dumps(material, sort_keys=True, allow_nan=False).encode()).hexdigest()
    return {"allocation": allocation, "trade_plan": trade, "entry_candidates": candidates,
            "decision": {"id": digest, "policy_version": POLICY_VERSION,
                         "policy_id": policy_id, "signal_policy_id": signal_policy_id,
                         "mode": "shadow",
                         "live_eligible": False, "reason": "실전 정책 승격 및 시점·체결 검증 전"}}
