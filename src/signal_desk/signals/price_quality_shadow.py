"""R12b quality veto increment on the *same* frozen R12a price basket and marks.

Only first-observed PIT quality metadata is added. No replacement pick is introduced;
cash-only improvement is controlled by a same-count top-price basket.
"""

from __future__ import annotations

import datetime as dt
import math
import statistics

from signal_desk import db, market_clock, store
from signal_desk.signals import price_baseline_shadow as price

VERSION = "r12-s1-quality-v1"
START_SESSION = "2026-09-28"
MIN_EVALUABLE = {"kr": 3, "us": 2}
PASS_RATIO = 0.5  # semantic majority/nonnegative; frozen before outcomes, not optimized
MAX_CASH_MATCH_GAP_PP = 1.0  # integer-share residual cash must not masquerade as selection alpha


def _integer(value) -> int | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if math.isfinite(number) and number.is_integer() else None


def decide(base_order: list[str], quality: dict[str, dict]) -> dict:
    """Filter first; never back-fill vacated slots with lower-ranked names."""
    filtered = [t for t in base_order if quality[t]["points"] / quality[t]["evaluable"] >= PASS_RATIO]
    return {"price_3factor": list(base_order), "quality_veto": filtered,
            "same_count_top_price": list(base_order[:len(filtered)])}


def capture(market: str, now: dt.datetime) -> dict:
    if market not in MIN_EVALUABLE or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("market and timezone-aware now required")
    session = market_clock.latest_completed_session(market, now)
    if not session or session < START_SESSION or market_clock.is_open(market, now):
        return {"saved": 0, "reason": "해당 세션 없음"}
    close = market_clock._calendar(market).schedule.loc[session]["close"].to_pydatetime()
    age = now.astimezone(dt.timezone.utc) - close
    if not dt.timedelta(hours=1) <= age <= dt.timedelta(hours=20 if market == "us" else 12):
        return {"saved": 0, "reason": "최초 관측 시간창 밖"}
    # R12a must have frozen the common universe and first-observed reference closes *today*.
    base = next((e for e in db.price_baseline_recent(market, 1) if e["session"] == session), None)
    if base is None or base.get("version") != price.VERSION:
        return {"saved": 0, "reason": "동일 세션 가격 대조군 동결 전"}
    if db.price_quality_get(market, session) is not None:
        return {"saved": 0, "reason": "이미 동결됨"}
    history = store.load_signal_history(market)
    pit = (history[history["date"].astype(str) == session]
           if not history.empty and "date" in history.columns else None)
    rows = ({} if pit is None or pit.empty or pit["ticker"].duplicated().any()
            else {str(r["ticker"]): r for r in pit.to_dict("records")})
    order = list(base["policies"]["price_3factor"])
    quality = {}
    invalid = []
    for ticker in order:
        row = rows.get(ticker) or {}
        points, evaluable = _integer(row.get("quality")), _integer(row.get("quality_evaluable"))
        if (row.get("session_valid") is not True or str(row.get("bar_asof")) != session
                or points is None or evaluable is None
                or evaluable < MIN_EVALUABLE[market] or evaluable > 5
                or points < 0 or points > evaluable):
            invalid.append(ticker)
            continue
        quality[ticker] = {"points": points, "evaluable": evaluable,
                           "ratio": round(points / evaluable, 6)}
    eligible = not invalid and bool(order)
    ratios = [quality[t]["ratio"] for t in order if t in quality]
    snapshot = {"version": VERSION, "market": market, "session": session,
                "base_version": price.VERSION, "base_observed_at": base["observed_at"],
                "observed_at": now.isoformat(), "mode": "shadow", "live_eligible": False,
                "eligible": eligible, "invalid_tickers": invalid,
                "quality": quality, "min_evaluable": MIN_EVALUABLE[market], "pass_ratio": PASS_RATIO,
                "ratio_distribution": ({"min": min(ratios), "median": round(statistics.median(ratios), 6),
                                        "max": max(ratios), "available": len(ratios),
                                        "passed": sum(r >= PASS_RATIO for r in ratios)} if ratios else None),
                "policies": decide(order, quality) if eligible else None,
                "reason": None if eligible else "가격 상위 후보의 평가 가능 재무 항목/PIT 세션 결손",
                "limits": "축약 5검사, US는 주로 2검사. 원천 공시 공개시각 미검증, 첫 관측 이후만 연구. 실주문 미연결."}
    return {"saved": int(db.price_quality_add_once(market, session, snapshot)),
            "session": session, "eligible": eligible, "invalid": len(invalid)}


def evaluate(snapshot: dict | None, base: dict, marks: dict[str, dict[str, float]], *,
             completed_session: str, revision_halt: str | None = None) -> dict:
    shell = {"version": VERSION, "mode": "shadow", "live_eligible": False}
    if snapshot is None:
        return {**shell, "ready": False, "status": "blocked", "reason": "동일 세션 quality 입력 동결 누락"}
    if (snapshot.get("version") != VERSION or snapshot.get("session") != base.get("session")
            or snapshot.get("base_version") != base.get("version")
            or snapshot.get("base_observed_at") != base.get("observed_at")):
        return {**shell, "ready": False, "status": "blocked", "reason": "가격·quality 동결 버전 불일치"}
    if not snapshot.get("eligible") or not snapshot.get("policies"):
        return {**shell, "ready": False, "status": "blocked", "reason": snapshot.get("reason") or "quality 입력 불완전"}
    common = {**base, "policies": {"price_3factor": snapshot["policies"]["price_3factor"],
                                    "sector_momentum": snapshot["policies"]["quality_veto"]}}
    primary = price.evaluate(common, marks, completed_session=completed_session, revision_halt=revision_halt)
    if not primary.get("ready"):
        return {**shell, "ready": False, "status": primary["status"], "reason": primary["reason"]}
    cash_control = price.evaluate(
        {**common, "policies": {"price_3factor": snapshot["policies"]["price_3factor"],
                                "sector_momentum": snapshot["policies"]["same_count_top_price"]}},
        marks, completed_session=completed_session, revision_halt=revision_halt)
    if not cash_control.get("ready"):
        return {**shell, "ready": False, "status": cash_control["status"], "reason": cash_control["reason"]}
    baseline = primary["outcomes"]["price_3factor"]
    filtered = primary["outcomes"]["sector_momentum"]
    count_control = cash_control["outcomes"]["sector_momentum"]
    exposure_gap = abs(filtered["entry_notional_pct"] - count_control["entry_notional_pct"])
    if exposure_gap > MAX_CASH_MATCH_GAP_PP:
        return {**shell, "ready": False, "status": "blocked_exposure_match",
                "reason": "정수주 잔여현금으로 실제 진입 투자비중 차이 1%p 초과",
                "entry_exposure_gap_pp": round(exposure_gap, 6)}
    return {**shell, "ready": True, "status": "complete", "entry_session": primary["entry_session"],
            "exit_session": primary["exit_session"],
            "outcomes": {"price_3factor": baseline, "quality_veto": filtered,
                         "same_count_top_price": count_control},
            "delta_net_pp": round(filtered["net_return_pct"] - baseline["net_return_pct"], 6),
            "selection_delta_pp": round(filtered["net_return_pct"] - count_control["net_return_pct"], 6),
            "entry_exposure_gap_pp": round(exposure_gap, 6),
            "note": "quality 제외와 같은 현금비중의 가격순위 대조군을 별도로 비교; 자동 승격 없음."}
