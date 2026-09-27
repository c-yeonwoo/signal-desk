"""R13b fixed-look OOS alert for the first-observed (not source-timed) cohort."""

from __future__ import annotations

import math
import random

from signal_desk import market_clock
from signal_desk.signals import revision_price_forward as forward, revision_price_freeze as frozen

GATE_ID = "r13-s2-first-observed-oos-v1"
LOOKS = (12, 24, 36, 48)
MIN_COVERAGE = 0.90
ALPHA = 0.05 / (2 * len(LOOKS))  # positive/negative × four fixed looks, KR only
RESAMPLES = 10_000


def _interval(values: list[float]) -> tuple[float, float]:
    rng = random.Random(1313)
    n = len(values)
    draws = sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n
                   for _ in range(RESAMPLES))
    return (draws[max(0, math.ceil(RESAMPLES * ALPHA) - 1)],
            draws[min(RESAMPLES - 1, math.ceil(RESAMPLES * (1 - ALPHA)) - 1)])


def assess(episodes: list[dict], *, completed_session: str | None) -> dict:
    base = {"gate_id": GATE_ID, "market": "kr", "mode": "shadow", "live_eligible": False,
            "auto_promote": False, "source_available_at_verified": False,
            "start_session": frozen.START_SESSION, "looks": list(LOOKS),
            "familywise_alpha": ALPHA, "min_coverage": MIN_COVERAGE,
            "note": "같은 관측 모집단의 비중첩 비용 후 차이. 원천 발표시각·시장/업종·실체결 미검증으로 실전 승격 불가."}
    eligible = sorted((e for e in episodes if e.get("version") == frozen.VERSION
                       and e.get("session", "") >= frozen.START_SESSION
                       and e.get("policies", {}).get("revision_unreacted_price") !=
                       e.get("policies", {}).get("eps_revision_only")), key=lambda e: e["session"])
    selected = []
    last_end = None
    for e in eligible:
        sessions = market_clock.next_sessions("kr", e["session"], forward.HORIZON)
        if len(sessions) != forward.HORIZON or (last_end and e["session"] <= last_end):
            continue
        selected.append((e, sessions[-1]))
        last_end = sessions[-1]
    matured = [e for e, end in selected if completed_session and end <= completed_session]
    look = max((n for n in LOOKS if n <= len(matured)), default=None)
    if look is None:
        return {**base, "status": "awaiting_oos", "divergent_episodes": len(eligible),
                "matured_blocks": len(matured), "next_look": LOOKS[0]}
    panel = matured[:look]
    complete = [e for e in panel if (e.get("forward") or {}).get("ready")
                and (e["forward"]).get("status") == "complete"
                and (e["forward"]).get("delta_net_pp") is not None]
    report = {**base, "look": look, "next_look": next((n for n in LOOKS if n > look), None),
              "divergent_episodes": len(eligible), "matured_blocks": len(matured),
              "effective_blocks": len(complete), "coverage": round(len(complete) / look, 4),
              "blocked_sessions": [e["session"] for e in panel if e not in complete]}
    if len(complete) / look < MIN_COVERAGE or len(complete) < LOOKS[0]:
        return {**report, "status": "blocked_data_quality", "reason": "사전 선택 전진 가격 무결성 부족"}
    try:
        values = [float(e["forward"]["delta_net_pp"]) for e in complete]
    except (KeyError, TypeError, ValueError):
        return {**report, "status": "blocked_data_quality", "reason": "성과 필드 결손"}
    if any(not math.isfinite(v) for v in values):
        return {**report, "status": "blocked_data_quality", "reason": "비유한 성과 값"}
    lower, upper = _interval(values)
    status = "positive_research_signal" if lower > 0 else "negative_research_signal" if upper < 0 else "inconclusive"
    return {**report, "status": status, "net_delta_mean_pp": round(sum(values) / len(values), 4),
            "net_delta_lower_pp": round(lower, 4), "net_delta_upper_pp": round(upper, 4),
            "reason": "최초 관측 전략의 조건부 연구 신호. 원천 발표시각/시장·업종/체결 검증 전 승격 불가"}
