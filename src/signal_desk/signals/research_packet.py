"""Source-bound input for analyst skills; never a score, order or return model."""
from __future__ import annotations

import datetime as dt
import math

from signal_desk.signals import decision_price_sensitivity, price_validity


def build(bundle: dict, *, as_of: dt.datetime) -> dict:
    """Adapt one immutable public-engine capture without executing the engine.

    as_of is explicit: a quote's receipt is not the decision's timestamp.
    No local caches, live fetches, account state or future outcomes are read.
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of requires a timezone")
    data = decision_price_sensitivity.validate_bundle(bundle)
    saved = data["engine_input"]
    universe = saved["universe"]
    tickers = [u["ticker"] for u in universe]
    rows = data["signal_output"]["rows"]
    if len(set(tickers)) != len(tickers) or sorted(tickers) != sorted(r["ticker"] for r in rows):
        raise ValueError("ambiguous or incomplete captured universe")
    market = bundle["market"]
    from zoneinfo import ZoneInfo
    zone = ZoneInfo("Asia/Seoul" if market == "kr" else "America/New_York")
    if as_of.astimezone(zone).date().isoformat() != saved["today"]:
        raise ValueError("as_of must belong to the captured market session date")
    quotes = data["quote_delta"]["quotes"]
    series = data["price_base"]["series"]
    decision_price_sensitivity._restore(data)  # Check quote/unclassified membership too.
    if data["quote_delta"]["unclassified"] or saved.get("price_structure_status") != "verified":
        raise ValueError("unclassified or unverified price inputs")
    # Non-universe context can affect the engine's market-wide return. Validate it
    # too, rather than certifying only the rows that happen to be displayed.
    for s in series.values():
        days, closes = s.get("dates", []), s.get("closes", [])
        if (len(days) != len(closes) or days != sorted(set(days))
                or any(day >= saved["today"] for day in days)):
            raise ValueError("research packet requires completed, dated price history")
        if any(isinstance(p, bool) or not math.isfinite(float(p)) or float(p) <= 0 for p in closes):
            raise ValueError("invalid completed price")
    for q in quotes.values():
        received = q.get("received_at")
        if received is not None and (not math.isfinite(float(received)) or float(received) > as_of.timestamp()):
            raise ValueError("quote received after research cutoff")
    items = []
    for row in rows:  # Preserve engine order, including every deferred/low-coverage name.
        ticker = row["ticker"]
        s = series.get(ticker, {})
        days, closes = s.get("dates", []), s.get("closes", [])
        q = quotes.get(ticker) or {}
        received = q.get("received_at")
        evidence = {
            "price_basis": "intraday_provisional", "price_session": saved["today"],
            "price_observation_id": q.get("observation_id"),
            "price_received_at": received,
            "price_source_timestamp": q.get("source_timestamp"),
            "price_source_timestamp_field": q.get("source_timestamp_field"),
            "price_source_time_verified": q.get("source_time_verified") is True,
        }
        current = q.get("price")
        validity = price_validity.check(market, current, evidence,
                                       close_date=days[-1] if days else None, now=as_of)
        change = None
        if closes and isinstance(current, (int, float)) and not isinstance(current, bool) and math.isfinite(current) and current > 0:
            change = (current / closes[-1] - 1) * 100
        # Actual contributions can be zero even when the displayed factor is positive.
        contributions = row.get("score_contributions") or {}
        supporting = {k: v for k, v in contributions.items() if isinstance(v, (int, float)) and v > 0}
        opposing = {k: v for k, v in contributions.items() if isinstance(v, (int, float)) and v < 0}
        unknown = ["연속 분봉·거래량 미포함", "bid/ask·호가 수량 미포함", "실제 거래비용 미포함"]
        if not validity["source_time_verified"]:
            unknown.append("공급자 표기 시각의 의미 미인증")
        if not validity["eligible"]:
            unknown.append("가격 확인 필요: " + validity["reason"])
        earnings = saved.get("earnings_dates", {}).get(ticker)
        if earnings:
            unknown.append("실적 일정의 원문·최초 관측 시각 미인증")
        items.append({
            "ticker": ticker, "name": row["name"],
            "recorded_judgment": {k: row.get(k) for k in
                ("score", "kind", "rank", "rank_eligible", "gate_blocked", "gates", "low_coverage")},
            "price": {"current": current, "prior_close": closes[-1] if closes else None,
                      "prior_close_date": days[-1] if days else None,
                      "change_from_prior_close_pct": change, "evidence": evidence,
                      "validity": validity},
            "thesis": {"supporting_score_contributions": supporting,
                       "opposing_score_contributions": opposing,
                       "suppressed_factors": row.get("factor_suppressed") or {},
                       "recorded_reasons": row.get("reasons") or [],
                       "meaning": "기존 점수를 만든 근거이며 수익 확률이나 신규 주문이 아닙니다."},
            "catalysts": {"earnings_date": earnings, "publication_time_verified": False},
            "liquidity": {"status": "unknown", "reason": "현재가만으로 스프레드나 체결 가능한 수량을 알 수 없습니다."},
            "unknown": unknown,
            "concise_brief": {
                "recent_change": (f"전일 종가 대비 {change:+.2f}%" if change is not None else "가격 변화의 근거가 부족합니다."),
                "original": "판단 ID와 관측 ID를 참조합니다. 뉴스 원문은 아직 연결되지 않았습니다.",
                "checked_at": as_of.isoformat(),
                "unknown": unknown,
            },
        })
    return {
        "schema": "signal-research-packet-v1", "market": market, "session": saved["today"],
        "as_of": as_of.isoformat(), "signal_output_id": bundle["signal_output_id"],
        "as_of_provenance": "caller_supplied_not_creation_time_certification",
        "signal_policy_id": saved.get("signal_policy_id"),
        "artifact_ids": {k: v["id"] for k, v in bundle["artifacts"].items()},
        "purpose": "source_bound_research_only", "live_order_eligible": False,
        "profitability_verified": False, "strict_pit_eligible": False,
        "universe_size": len(tickers), "items": items,
        "comparison_readiness": {
            "status": "input_packet_only",
            "missing": ["frozen_candidate_policy", "continuous_intraday_bars_and_volume",
                        "executable_bid_ask_and_sizes", "account_cost_contract",
                        "nonprotected_outcome_cohorts", "source_availability_certification"],
            "meaning": "분석 입력 준비와 수익성 입증은 별도의 완료 조건입니다.",
        },
    }
