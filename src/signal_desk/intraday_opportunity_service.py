"""기존 5분 시세 원장에서 연구용 장중 후보를 만든다. 주문/알림 경로와 독립."""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import time
from collections import defaultdict
from zoneinfo import ZoneInfo

from signal_desk import db
from signal_desk.broker import kis
from signal_desk.reference import sectors
from signal_desk.signals import intraday_opportunity as model, macro_release, relation_graph

log = logging.getLogger("signal_desk.intraday_opportunity")
_SESSION_ZONE = {"kr": ZoneInfo("Asia/Seoul"), "us": ZoneInfo("America/New_York")}


def _official_event(ticker: str, at: int) -> dict | None:
    events = db.kb_events_active(ticker, now=at, decision_only=True)
    known = [event for event in events if int(event.get("detected_at") or at + 1) <= at
             and event.get("direction") in ("positive", "negative")]
    if not known:
        return None
    negative = [event for event in known if event["direction"] == "negative"]
    fresh_positive = [event for event in known if event["direction"] == "positive"
                      and at - int(event["detected_at"]) <= 4 * 3600]
    if not negative and not fresh_positive:
        return None
    event = max(negative or fresh_positive, key=lambda row: int(row["detected_at"]))
    return {"direction": event["direction"], "available_at": int(event["detected_at"]),
            "source_id": event["event_key"], "source_verified": True}


def _macro_context(releases: list[dict], at: int) -> dict | None:
    observed = dt.datetime.fromtimestamp(at, dt.timezone.utc)
    for item in releases:
        try:
            known = dt.datetime.fromisoformat(str(item["actual_observed_at"]).replace("Z", "+00:00"))
        except (KeyError, ValueError, TypeError):
            continue
        if (item.get("source_quality") != "operator_attested_official" or known.tzinfo is None
                or not 0 <= (observed - known).total_seconds() <= 86400):
            continue
        try:
            result = macro_release.evaluate(item, as_of=observed)
        except (KeyError, ValueError, TypeError):
            continue
        return {"metric": item["metric"], "release_id": item["id"],
                "actual_observed_at": item["actual_observed_at"],
                "surprise_pp": result["surprise_pp"], "interpretation": "시장 방향 효과는 미검증"}
    return None


def _linked_suppliers(us_ticker: str, at: int) -> list[dict]:
    """승인된 관계도 연구 설명일 뿐. 원천 시각 검증·수익 인과가 없다."""
    result = []
    for edge in relation_graph.active_edges(db.relation_edges_as_of(us_ticker, at), observed_at=at):
        if edge.get("kr_supplier"):
            result.append({"ticker": edge["kr_supplier"], "edge_id": edge["id"],
                           "status": "research_link_not_buy_evidence"})
    return result[:10]


def scan_market(market: str, *, now: int | None = None, max_kis_requests: int = 2) -> list[dict]:
    """작은 상한으로 급변 후보만 KIS 누적 거래량을 조회한다. 실패는 가격 전용 후보로 남긴다."""
    if market not in _SESSION_ZONE:
        raise ValueError("unsupported market")
    at = int(time.time()) if now is None else int(now)
    rows = db.intraday_opportunity_quote_window(market, after_ts=at - 900, before_ts=at)
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_ticker[row["ticker"]].append(row)
    audit = {"quote_tickers": len(by_ticker), "fresh_tickers": 0, "paired_tickers": 0,
             "price_candidates": 0, "evaluated_candidates": 0, "saved_candidates": 0,
             "kis_snapshot_requests": 0, "kis_snapshot_success": 0,
             "kis_minute_requests": 0, "kis_minute_success": 0,
             "kis_flow_requests": 0, "kis_flow_success": 0,
             "volume_supported": 0, "status": "observed"}
    candidates = []
    observed_moves: dict[str, float] = {}
    for ticker, history in by_ticker.items():
        current = history[-1]
        if at - int(current["ts"]) > model.MAX_QUOTE_AGE_SEC:
            continue
        audit["fresh_tickers"] += 1
        older = [row for row in history[:-1] if 240 <= int(current["ts"]) - int(row["ts"]) <= 900]
        if not older:
            continue
        previous = min(older, key=lambda row: abs(int(current["ts"]) - int(row["ts"]) - 300))
        if int(current["ts"]) - int(previous["ts"]) <= 420:
            audit["paired_tickers"] += 1
        if previous["price"] and previous["price"] > 0:
            observed_moves[ticker] = 100 * (current["price"] / previous["price"] - 1)
        candidate = model.detect_move(market, ticker, previous, current)
        if candidate:
            candidates.append(candidate)
    candidates.sort(key=lambda item: abs(item["move_pct"]), reverse=True)
    audit["price_candidates"] = len(candidates)
    audit["evaluated_candidates"] = min(len(candidates), 20)
    try:
        recent_macro = db.macro_release_recent(5) if candidates else []
    except Exception as exc:
        log.warning("장중 거시 발표 근거 조회 실패: %s", type(exc).__name__)
        recent_macro = []
    up_fraction = (sum(move > 0 for move in observed_moves.values()) / len(observed_moves)
                   if observed_moves else None)
    tape_regime = ("broad_up" if up_fraction is not None and len(observed_moves) >= 30
                   and up_fraction >= 0.6 else "broad_down" if up_fraction is not None
                   and len(observed_moves) >= 30 and up_fraction <= 0.4 else "mixed"
                   if len(observed_moves) >= 30 else "unknown")
    saved = []
    for index, candidate in enumerate(candidates[:20]):
        ticker = candidate["ticker"]
        current_volume = None
        minute_volumes = None
        flow_estimate = None
        if market == "kr" and index < max(0, min(max_kis_requests, 3)):
            audit["kis_snapshot_requests"] += 1
            try:
                observation = kis.domestic_market_snapshot(ticker)
                if observation:
                    audit["kis_snapshot_success"] += 1
                    db.intraday_opportunity_volume_record(market, ticker, observation)
                    current_volume = {**observation, "ts": observation["received_at"]}
                    audit["kis_minute_requests"] += 1
                    minute_volumes = kis.domestic_completed_minute_volumes(ticker)
                    if minute_volumes:
                        audit["kis_minute_success"] += 1
            except Exception as exc:
                log.warning("장중 거래량 조회 실패(%s): %s", ticker, type(exc).__name__)
        if market == "kr" and index == 0 and current_volume:
            audit["kis_flow_requests"] += 1
            try:
                flow_estimate = kis.domestic_investor_estimate(ticker)
                if flow_estimate:
                    audit["kis_flow_success"] += 1
            except Exception as exc:
                log.warning("장중 수급 가집계 조회 실패(%s): %s", ticker, type(exc).__name__)
        if current_volume:
            candidate["detected_at"] = max(candidate["detected_at"], current_volume["ts"])
        if minute_volumes:
            candidate["detected_at"] = max(candidate["detected_at"], minute_volumes["received_at"])
        if flow_estimate:
            candidate["detected_at"] = max(candidate["detected_at"], flow_estimate["received_at"])
        candidate["session"] = dt.datetime.fromtimestamp(candidate["detected_at"],
                                                           _SESSION_ZONE[market]).date().isoformat()
        volumes = db.intraday_opportunity_volumes(
            market, ticker, after_ts=candidate["detected_at"] - 900,
            before_ts=candidate["detected_at"])
        if current_volume is None and volumes:
            current_volume = volumes[-1]
        previous_volume = volumes[-2] if len(volumes) >= 2 else None
        volume = model.volume_evidence(previous_volume, current_volume,
                                       detected_at=candidate["detected_at"],
                                       reference_price=candidate["price"])
        if minute_volumes and minute_volumes["ratio"] is not None and volume["state"] == "observed":
            volume["recent_5m_volume"] = minute_volumes["recent_5m_volume"]
            volume["previous_5m_volume"] = minute_volumes["previous_5m_volume"]
            volume["minute_volume_ratio"] = minute_volumes["ratio"]
            volume["minute_last_complete"] = minute_volumes["last_complete_minute"]
            volume["complete_bars"] = minute_volumes["complete_bars"]
        try:
            official_event = _official_event(ticker, candidate["detected_at"])
        except Exception as exc:
            log.warning("장중 공시 근거 조회 실패(%s): %s", ticker, type(exc).__name__)
            official_event = None
        context = model.context_evidence(at=candidate["detected_at"], official_event=official_event)
        sector_name = sectors.sector_of(ticker) if market == "kr" else None
        peers = sorted(((peer, observed_moves[peer]) for peer in sectors.by_sector(sector_name)
                        if peer in observed_moves), key=lambda pair: pair[1], reverse=True) if sector_name else []
        context["sector_sample"] = ({"sector": sector_name, "observed_peers": len(peers),
                                     "sample_leader": peers[0][0] if len(peers) >= 2 else None,
                                     "status": "curated_sample_not_market_wide"}
                                    if sector_name else None)
        context["flow_estimate"] = flow_estimate
        try:
            context["linked_suppliers"] = (_linked_suppliers(ticker, candidate["detected_at"])
                                           if market == "us" else [])
        except Exception as exc:
            log.warning("장중 관계 근거 조회 실패(%s): %s", ticker, type(exc).__name__)
            context["linked_suppliers"] = []
        context["relation_status"] = "research_link_not_buy_evidence"
        context["macro_release"] = _macro_context(recent_macro, candidate["detected_at"])
        day_high = current_volume.get("day_high") if current_volume else None
        pullback_pct = (100 * (day_high / current_volume["price"] - 1)
                        if day_high and current_volume["price"] > 0
                        and volume.get("state") == "observed" else None)
        decision = model.plan(candidate, volume, context, pullback_pct=pullback_pct)
        if model.has_volume_support(volume):
            audit["volume_supported"] += 1
        payload = {**candidate, "volume": volume, "context": context, "decision": decision,
                   "intraday_high_pullback_pct": (round(pullback_pct, 4) if pullback_pct is not None else None),
                   "regime": tape_regime, "regime_basis": "5min_observed_breadth",
                   "regime_sample_size": len(observed_moves), "research_only": True}
        identity = f"{market}:{ticker}:{candidate['quote_observation_id']}:{candidate['detected_at']}"
        event_id = hashlib.sha256(identity.encode()).hexdigest()
        if db.intraday_opportunity_record(event_id, payload):
            saved.append({"id": event_id, **payload})
    audit["saved_candidates"] = len(saved)
    if not by_ticker:
        audit["status"] = "no_quote_rows"
    elif not audit["paired_tickers"]:
        audit["status"] = "no_comparable_prices"
    elif not candidates:
        audit["status"] = "no_price_candidates"
    db.intraday_opportunity_scan_record(market, ts=at, payload=audit)
    return saved


def recent_with_replay(market: str, *, after_ts: int, limit: int = 50,
                       sampled: bool = False) -> list[dict] | tuple[list[dict], bool]:
    """종료 가격이 아직 없으면 미성숙으로 둔다. 연구 API만 사용한다."""
    if sampled:
        rows, truncated = db.intraday_opportunities_sampled(market, after_ts=after_ts)
    else:
        rows = db.intraday_opportunities_recent(market, after_ts=after_ts, limit=limit)
    for row in rows:
        quotes = db.intraday_quotes_list(market, row["ticker"],
                                         after_ts=row["detected_at"],
                                         before_ts=row["detected_at"] + 3 * 3600,
                                         include_metadata=True)
        row["replay"] = model.replay(row, quotes)
    return (rows, truncated) if sampled else rows
