"""R12 price-control paired OOS alert, never a live-policy promotion gate."""

from __future__ import annotations

import math
import random

from signal_desk import market_clock
from signal_desk.signals import price_baseline_shadow

GATE_ID = "r12-s1-price-oos-v1"
START_SESSION = "2026-09-28"
LOOKS = (12, 24, 36, 48)
MIN_COVERAGE = 0.90
ALPHA = 0.05 / (2 * 2 * len(LOOKS))  # both signs × KR/US × four fixed looks
RESAMPLES = 10_000


def _interval(values: list[float]) -> tuple[float, float]:
    rng = random.Random(1212)
    n = len(values)
    draws = sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n
                   for _ in range(RESAMPLES))
    return (draws[max(0, math.ceil(RESAMPLES * ALPHA) - 1)],
            draws[min(RESAMPLES - 1, math.ceil(RESAMPLES * (1 - ALPHA)) - 1)])


def assess(episodes: list[dict], *, market: str, completed_session: str | None) -> dict:
    base = {"gate_id": GATE_ID, "market": market, "mode": "shadow", "live_eligible": False,
            "auto_promote": False, "start_session": START_SESSION, "looks": list(LOOKS),
            "familywise_alpha": ALPHA, "min_coverage": MIN_COVERAGE,
            "note": "비중첩 단일 에피소드의 근사 bootstrap 신호. 베타/섹터/현금·호가·기업행동 통제 전 실전 승격 금지."}
    if market not in ("kr", "us"):
        return {**base, "status": "invalid_market"}
    selected = sorted((e for e in episodes if e.get("version") == price_baseline_shadow.VERSION
                       and e.get("session", "") >= START_SESSION
                       and e.get("policies", {}).get("price_3factor") !=
                       e.get("policies", {}).get("sector_momentum")), key=lambda e: e["session"])
    matured = []
    previous_end = None
    for episode in selected:
        sessions = market_clock.next_sessions(market, episode["session"], price_baseline_shadow.HORIZON)
        if len(sessions) != price_baseline_shadow.HORIZON or (previous_end and episode["session"] <= previous_end):
            continue
        previous_end = sessions[-1]
        if completed_session and sessions[-1] <= completed_session:
            matured.append(episode)
    look = max((n for n in LOOKS if n <= len(matured)), default=None)
    if look is None:
        return {**base, "status": "awaiting_oos", "divergent_episodes": len(selected),
                "matured_blocks": len(matured), "next_look": LOOKS[0]}
    panel = matured[:look]
    complete = [e for e in panel if (e.get("forward") or {}).get("ready")
                and (e["forward"]).get("status") == "complete"
                and (e["forward"]).get("delta_net_pp") is not None]
    coverage = len(complete) / look
    report = {**base, "look": look, "next_look": next((n for n in LOOKS if n > look), None),
              "divergent_episodes": len(selected), "matured_blocks": len(matured),
              "effective_blocks": len(complete), "coverage": round(coverage, 4),
              "blocked_sessions": [e["session"] for e in panel if e not in complete]}
    if coverage < MIN_COVERAGE or len(complete) < LOOKS[0]:
        return {**report, "status": "blocked_data_quality", "reason": "사전 선택 에피소드 가격·무결성 부족"}
    try:
        values = [float(e["forward"]["delta_net_pp"]) for e in complete]
    except (KeyError, TypeError, ValueError):
        return {**report, "status": "blocked_data_quality", "reason": "수익 차이 결손"}
    if any(not math.isfinite(v) for v in values):
        return {**report, "status": "blocked_data_quality", "reason": "비유한 수익 차이"}
    lower, upper = _interval(values)
    status = "positive_research_signal" if lower > 0 else "negative_research_signal" if upper < 0 else "inconclusive"
    return {**report, "status": status, "net_delta_mean_pp": round(sum(values) / len(values), 4),
            "net_delta_lower_pp": round(lower, 4), "net_delta_upper_pp": round(upper, 4),
            "reason": "통계 신호일 뿐 섹터/베타/현금 민감도·체결 검증 전 정책 변경 불가"}
