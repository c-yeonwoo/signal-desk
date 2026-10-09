"""장중 기회 탐색의 연구 전용 판단. 등록 엔진·봇 주문과 격리한다.

모든 판단은 현재까지 실제 수집된 관측만 사용한다. 가격 급등은 매수 신호가
아니고 검토 시작점이다. 거래량·공시·수급의 부재를 긍정 근거로 바꾸지 않는다.
"""

from __future__ import annotations

import math
from collections import defaultdict
import datetime as dt
from zoneinfo import ZoneInfo

VERSION = "intraday-opportunity-v1"
PRICE_MOVE_PCT = 1.5  # 탐색 후보의 운영 한도. 등록 헤드라인/실주문 게이트가 아니다.
MAX_QUOTE_AGE_SEC = 120
MIN_REPLAY_DAYS = 30
MIN_REPLAY_EVENTS = 30
_ZONES = {"kr": ZoneInfo("Asia/Seoul"), "us": ZoneInfo("America/New_York")}


def _number(value) -> float | None:
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def detect_move(market: str, ticker: str, previous: dict, current: dict) -> dict | None:
    """4~7분 가격 변화를 후보로 표시한다. 누락된 10분 구간을 5분이라고 부르지 않는다."""
    if market not in ("kr", "us") or not ticker or not previous or not current:
        return None
    prev_px, px = _number(previous.get("price")), _number(current.get("price"))
    try:
        age = int(current["ts"]) - int(previous["ts"])
    except (KeyError, TypeError, ValueError):
        return None
    if not prev_px or not px or prev_px <= 0 or px <= 0 or not 240 <= age <= 420:
        return None
    move = 100 * (px / prev_px - 1)
    if abs(move) < PRICE_MOVE_PCT:
        return None
    return {"version": VERSION, "market": market, "ticker": ticker,
            "detected_at": int(current["ts"]), "previous_at": int(previous["ts"]),
            "previous_price": prev_px, "price": px, "move_pct": round(move, 4),
            "direction": "surge" if move > 0 else "drop",
            "quote_observation_id": current.get("observation_id"),
            "price_provider": current.get("provider"), "coverage": "price_only"}


def volume_evidence(previous: dict | None, current: dict | None,
                    *, detected_at: int, reference_price: float) -> dict:
    """누적 거래량의 증가만 확인한다. 상대거래량/전일대비 거래량이라 부르지 않는다."""
    if not current:
        return {"state": "unavailable"}
    try:
        ts, cumulative = int(current["ts"]), int(current["cumulative_volume"])
        price = float(current["price"])
    except (KeyError, ValueError, TypeError, OverflowError):
        return {"state": "invalid"}
    if (ts > detected_at + 30 or detected_at - ts > MAX_QUOTE_AGE_SEC or cumulative < 0
            or not math.isfinite(price) or price <= 0 or abs(price / reference_price - 1) > 0.02):
        return {"state": "stale_or_mismatched"}
    evidence = {"state": "observed", "cumulative_volume": cumulative,
                "observed_at": ts, "provider": current.get("provider")}
    if previous:
        try:
            prev_ts, prev_volume = int(previous["ts"]), int(previous["cumulative_volume"])
        except (KeyError, ValueError, TypeError, OverflowError):
            return evidence
        if 240 <= ts - prev_ts <= 900 and 0 <= prev_volume <= cumulative:
            evidence["interval_volume"] = cumulative - prev_volume
            evidence["interval_seconds"] = ts - prev_ts
    return evidence


def context_evidence(*, at: int, official_event: dict | None = None,
                     flow: dict | None = None, sector: dict | None = None,
                     relation: dict | None = None) -> dict:
    """출처·가용 시각이 검증된 증거만 승격한다. 다른 정보는 설명에만 쓴다."""
    result = {"official_event": None, "flow": None, "sector": None, "relation": None,
              "unknown": []}
    for name, item in (("official_event", official_event), ("flow", flow),
                       ("sector", sector), ("relation", relation)):
        if not item:
            result["unknown"].append(name)
            continue
        try:
            known_at = int(item["available_at"])
        except (KeyError, ValueError, TypeError):
            result["unknown"].append(name)
            continue
        if (known_at > at or item.get("source_verified") is not True
                or (name == "relation" and item.get("approved") is not True)):
            result["unknown"].append(name)
            continue
        result[name] = {"direction": item.get("direction"), "available_at": known_at,
                        "source_id": item.get("source_id")}
    return result


def has_volume_support(volume: dict) -> bool:
    """누적 거래량 증가 또는 완료된 10분봉의 최근 5분 증가만 확인한다."""
    interval_confirmed = (_number(volume.get("interval_volume")) or 0) > 0
    minute_confirmed = (volume.get("complete_bars") == 10
                        and (_number(volume.get("previous_5m_volume")) or 0) > 0
                        and (_number(volume.get("recent_5m_volume")) or 0) > 0
                        and (_number(volume.get("minute_volume_ratio")) or 0) >= 1.5)
    return volume.get("state") == "observed" and (interval_confirmed or minute_confirmed)


def plan(candidate: dict, volume: dict, context: dict, *, pullback_pct: float | None = None,
         spread_bps: float | None = None) -> dict:
    """두 전략의 *관찰 계획*. 여기서 주문 승인이나 목표 수익률을 반환하지 않는다."""
    event = context.get("official_event")
    if event and event.get("direction") == "negative":
        return {"playbook": "event_risk", "status": "review_exit" if candidate["direction"] == "drop" else "avoid",
                "reason": "확인된 악재가 있습니다. 보유 중이라면 매수 근거가 남아 있는지 다시 확인하세요.",
                "order_eligible": False}
    if candidate["direction"] == "drop":
        return {"playbook": "holder_thesis_review", "status": "review_loss",
                "reason": "급락 원인과 보유 논리를 다시 확인해야 합니다.", "order_eligible": False}
    if spread_bps is not None and spread_bps > 50:
        return {"playbook": "liquidity_check", "status": "avoid",
                "reason": "매수·매도 호가 차이가 큽니다.", "order_eligible": False}
    if not has_volume_support(volume):
        return {"playbook": "volume_check", "status": "watch",
                "reason": "가격 변화는 확인했지만 같은 구간의 거래량 증가 근거는 아직 부족합니다.",
                "order_eligible": False}
    if pullback_pct is not None and 0.3 <= pullback_pct <= 2.0:
        playbook, reason = "breakout_pullback", "급등 뒤 되돌림이 이어지는지 관찰합니다."
    elif event and event.get("direction") == "positive":
        playbook, reason = "event_continuation", "확인된 호재 뒤 가격과 거래량의 지속성을 관찰합니다."
    else:
        playbook, reason = "unexplained_surge", "가격과 거래량이 움직였지만 원인은 확인되지 않았습니다."
    return {"playbook": playbook, "status": "shadow", "reason": reason,
            "order_eligible": False}


def replay(candidate: dict, quotes: list[dict], *, hold_seconds: int = 3600,
           roundtrip_cost_bps: float = 25, side_slippage_bps: float = 10) -> dict:
    """실제 다음 관측에만 가상 체결. 즉시/한 틱 대기/기권을 같은 가격 경로로 비교한다."""
    at = int(candidate["detected_at"])
    market = candidate.get("market")
    session = candidate.get("session")
    zone = _ZONES.get(market)
    future = sorted((q for q in quotes if int(q.get("ts", 0)) > at and
                     _number(q.get("price")) is not None and float(q["price"]) > 0 and
                     (not zone or not session or
                      dt.datetime.fromtimestamp(int(q["ts"]), zone).date().isoformat() == session)),
                    key=lambda q: int(q["ts"]))
    result = {"status": "immature", "no_trade_pct": 0.0, "immediate": None, "wait_one": None}
    if not future or int(future[-1]["ts"]) < at + hold_seconds:
        return result
    for name, offset in (("immediate", 0), ("wait_one", 1)):
        if len(future) <= offset:
            continue
        entry = future[offset]
        if int(entry["ts"]) - at > 900:
            continue
        exit_at = int(entry["ts"]) + hold_seconds
        exit_quote = next((q for q in future[offset + 1:]
                           if exit_at <= int(q["ts"]) <= exit_at + 900), None)
        if exit_quote is None:
            continue
        gross = (float(exit_quote["price"]) / float(entry["price"]) - 1) * 100
        net = gross - roundtrip_cost_bps / 100 - 2 * side_slippage_bps / 100
        result[name] = {"entry_ts": int(entry["ts"]), "exit_ts": int(exit_quote["ts"]),
                        "entry_observation_id": entry.get("observation_id"),
                        "exit_observation_id": exit_quote.get("observation_id"),
                        "gross_pct": round(gross, 4), "net_pct": round(net, 4)}
    if result["immediate"] and result["wait_one"]:
        result["status"] = "complete"
    return result


def calibrate(rows: list[dict], *, min_days: int = MIN_REPLAY_DAYS,
              min_events: int = MIN_REPLAY_EVENTS) -> dict:
    """독립 날짜·완료 가상체결이 부족하면 확률을 표시하지 않는다.

    이는 보수적 요약이지 미래 수익 보장이나 라이브 모델 승격이 아니다.
    """
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        if row.get("replay", {}).get("status") != "complete":
            continue
        groups[(str(row.get("playbook")), str(row.get("regime") or "unknown"))].append(row)
    result = {}
    for key, group in groups.items():
        usable = [row for row in group if _number(row["replay"]["immediate"].get("net_pct")) is not None]
        by_day: dict[str, list[float]] = defaultdict(list)
        for row in usable:
            if row.get("session"):
                by_day[str(row["session"])].append(float(row["replay"]["immediate"]["net_pct"]))
        day_returns = [sum(values) / len(values) for values in by_day.values()]
        count = len(day_returns)
        if len(usable) < min_events or count < min_days:
            result[key] = {"status": "insufficient", "events": len(usable), "days": count,
                           "positive_rate": None}
            continue
        wins = sum(value > 0 for value in day_returns)
        p = wins / count
        z = 1.96
        lower = (p + z*z/(2*count) - z*math.sqrt((p*(1-p)+z*z/(4*count))/count))/(1+z*z/count)
        result[key] = {"status": "measured_shadow", "events": len(usable), "days": count,
                       "positive_rate": round(p, 4), "wilson_lower": round(lower, 4),
                       "mean_net_pct": round(sum(day_returns) / count, 4)}
    return result


def research_choice(history: list[dict], *, asof_session: str, regime: str) -> dict:
    """미래 세션을 배제한 그림자 전략 선택. 라이브 주문 자격을 반환하지 않는다."""
    past = [row for row in history if row.get("session") and str(row["session"]) < asof_session]
    measured = calibrate(past)
    candidates = [(playbook, stats) for (playbook, bucket), stats in measured.items()
                  if bucket == regime and stats["status"] == "measured_shadow"
                  and stats["mean_net_pct"] > 0 and stats["wilson_lower"] > 0.5]
    if not candidates:
        return {"status": "abstain", "playbook": None, "order_eligible": False,
                "reason": "독립 날짜의 비용 후 우위 근거가 부족합니다."}
    playbook, stats = max(candidates, key=lambda pair: (pair[1]["wilson_lower"],
                                                       pair[1]["mean_net_pct"]))
    return {"status": "shadow_candidate", "playbook": playbook, "order_eligible": False,
            "historical_days": stats["days"], "historical_events": stats["events"],
            "reason": "과거 관측 기준 연구 후보이며 미검증 라이브 정책입니다."}


def walk_forward(rows: list[dict], *, min_oos_days: int = 10) -> dict:
    """날짜별로 이전 세션만 학습한 선택을 다음 세션에 재생한다.

    하루의 여러 종목은 독립 표본으로 세지 않는다. 비교군도 같은 날짜·장세의
    가상 체결만 사용한다. 이 값은 라이브 주문이나 수익 보장이 아니다.
    """
    complete = [row for row in rows if row.get("session") and
                row.get("replay", {}).get("status") == "complete" and
                _number(row["replay"]["immediate"].get("net_pct")) is not None]
    sessions = sorted({str(row["session"]) for row in complete})
    selected: dict[str, list[float]] = defaultdict(list)
    baseline: dict[str, list[float]] = defaultdict(list)
    for session in sessions:
        today = [row for row in complete if row["session"] == session]
        past = [row for row in complete if row["session"] < session]
        for regime in {str(row.get("regime") or "unknown") for row in today}:
            choice = research_choice(past, asof_session=session, regime=regime)
            if choice["status"] != "shadow_candidate":
                continue
            matched = [row for row in today if str(row.get("regime") or "unknown") == regime
                       and row.get("playbook") == choice["playbook"]]
            if not matched:
                continue
            selected[session].extend(float(row["replay"]["immediate"]["net_pct"])
                                      for row in matched)
            baseline[session].extend(float(row["replay"]["immediate"]["net_pct"])
                                      for row in today if str(row.get("regime") or "unknown") == regime)
    days = sorted(selected)
    if len(days) < min_oos_days:
        return {"status": "insufficient", "oos_days": len(days), "required_days": min_oos_days,
                "selected_events": sum(map(len, selected.values())), "mean_net_pct": None,
                "baseline_mean_net_pct": None, "order_eligible": False}
    daily = [sum(selected[day]) / len(selected[day]) for day in days]
    controls = [sum(baseline[day]) / len(baseline[day]) for day in days]
    return {"status": "measured_shadow", "oos_days": len(days),
            "selected_events": sum(map(len, selected.values())),
            "mean_net_pct": round(sum(daily) / len(days), 4),
            "baseline_mean_net_pct": round(sum(controls) / len(days), 4),
            "excess_vs_same_day_pct_points": round(sum(a - b for a, b in zip(daily, controls)) / len(days), 4),
            "order_eligible": False}
