"""R12b OOS research alert: quality must beat both baseline and cash-matched control."""

from __future__ import annotations

import math
import random

from signal_desk import market_clock
from signal_desk.signals import price_baseline_shadow as price, price_quality_shadow as quality

GATE_ID = "r12-s1-quality-oos-v1"
LOOKS = (12, 24, 36, 48)
MIN_COVERAGE = 0.90
ALPHA = 0.05 / (2 * 2 * 2 * len(LOOKS))  # 2 metrics × both signs × KR/US × 4 looks
RESAMPLES = 10_000


def _interval(values: list[float], seed: int) -> tuple[float, float]:
    rng = random.Random(seed)
    n = len(values)
    draws = sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n
                   for _ in range(RESAMPLES))
    return (draws[max(0, math.ceil(RESAMPLES * ALPHA) - 1)],
            draws[min(RESAMPLES - 1, math.ceil(RESAMPLES * (1 - ALPHA)) - 1)])


def assess(episodes: list[dict], *, market: str, completed_session: str | None) -> dict:
    base = {"gate_id": GATE_ID, "market": market, "mode": "shadow", "live_eligible": False,
            "auto_promote": False, "start_session": quality.START_SESSION,
            "looks": list(LOOKS), "familywise_alpha": ALPHA, "min_coverage": MIN_COVERAGE,
            "note": "사전 고정 비중첩 표본. quality 증분과 같은 현금비중의 가격순위 대조군을 모두 넘어야 연구 신호. 실전 승격 아님."}
    if market not in ("kr", "us"):
        return {**base, "status": "invalid_market"}
    selected = []
    last_end = None
    for episode in sorted((e for e in episodes if e.get("version") == price.VERSION
                           and e.get("session", "") >= quality.START_SESSION), key=lambda e: e["session"]):
        sessions = market_clock.next_sessions(market, episode["session"], price.HORIZON)
        if len(sessions) != price.HORIZON or (last_end and episode["session"] <= last_end):
            continue
        selected.append((episode, sessions[-1]))
        last_end = sessions[-1]
    matured = [e for e, end in selected if completed_session and end <= completed_session]
    look = max((n for n in LOOKS if n <= len(matured)), default=None)
    if look is None:
        return {**base, "status": "awaiting_oos", "matured_blocks": len(matured),
                "next_look": LOOKS[0]}
    panel = matured[:look]
    complete = [e for e in panel if (e.get("quality_forward") or {}).get("ready")
                and (e["quality_forward"]).get("status") == "complete"
                and (e["quality_forward"]).get("delta_net_pp") is not None
                and (e["quality_forward"]).get("selection_delta_pp") is not None]
    report = {**base, "look": look, "next_look": next((n for n in LOOKS if n > look), None),
              "matured_blocks": len(matured), "effective_blocks": len(complete),
              "coverage": round(len(complete) / look, 4),
              "blocked_sessions": [e["session"] for e in panel if e not in complete]}
    if len(complete) / look < MIN_COVERAGE or len(complete) < LOOKS[0]:
        return {**report, "status": "blocked_data_quality", "reason": "quality/PIT/전진 가격 동결 커버리지 부족"}
    try:
        incremental = [float(e["quality_forward"]["delta_net_pp"]) for e in complete]
        selection = [float(e["quality_forward"]["selection_delta_pp"]) for e in complete]
    except (KeyError, TypeError, ValueError):
        return {**report, "status": "blocked_data_quality", "reason": "비교 성과 결손"}
    if any(not math.isfinite(x) for x in incremental + selection):
        return {**report, "status": "blocked_data_quality", "reason": "비유한 성과 값"}
    primary_low, primary_high = _interval(incremental, 1201)
    select_low, select_high = _interval(selection, 1202)
    if primary_low > 0 and select_low > 0:
        status = "positive_research_signal"
    elif primary_high < 0 or select_high < 0:
        status = "negative_research_signal"
    elif primary_low > 0 and select_low <= 0:
        status = "cash_exposure_only"
    else:
        status = "inconclusive"
    return {**report, "status": status,
            "net_delta_mean_pp": round(sum(incremental) / len(incremental), 4),
            "net_delta_lower_pp": round(primary_low, 4), "net_delta_upper_pp": round(primary_high, 4),
            "selection_delta_mean_pp": round(sum(selection) / len(selection), 4),
            "selection_delta_lower_pp": round(select_low, 4), "selection_delta_upper_pp": round(select_high, 4),
            "reason": "가격+quality 점수의 중복·공시 시각·위험/체결 검증 전 정책 변경 불가"}
