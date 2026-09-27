"""R11 S0 회전 shadow 사전등록 OOS 판정. 승격 후보만 알리고 주문은 절대 바꾸지 않는다."""

from __future__ import annotations

import math
import random

from signal_desk import market_clock
from signal_desk.signals import rotation_shadow

GATE_ID = "r11-s0-rotation-oos-v1"
START_SESSION = "2026-09-28"  # v2 전진 채점 코드 배포 뒤 최초 세션; 과거 v1 소급 금지
HORIZON = 20
LOOKS = (12, 24, 36, 48)  # 매일 결과를 보더라도 통계 판정은 이 유효블록 수에서만 갱신
MIN_COVERAGE = 0.90
MAX_MEAN_MDD_WORSE_PP = 1.0
MAX_SINGLE_MDD_WORSE_PP = 3.0
FAMILYWISE_ALPHA = 0.05 / (2 * 6 * len(LOOKS))  # 양방향 × KR/US × 3성향 × 4회 시점
RESAMPLES = 10_000


def _divergent(episode: dict) -> bool:
    decisions = episode.get("decisions") or {}
    a, b = (decisions.get(name) or {} for name in ("champion_rotation_proxy", "s0_rank_buffer"))
    # 청산/매수 수량까지 동결된 *실제 비교 계획*이 달라야 정보가 있다.
    return a.get("fixed_orders") != b.get("fixed_orders")


def _bootstrap_interval(values: list[float]) -> tuple[float, float]:
    """비중첩 에피소드 평균 차이의 고정 시드 percentile 구간(독립성은 근사)."""
    rng = random.Random(1107)
    n = len(values)
    draws = sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n
                   for _ in range(RESAMPLES))
    low = draws[max(0, math.ceil(RESAMPLES * FAMILYWISE_ALPHA) - 1)]
    high = draws[min(RESAMPLES - 1, math.ceil(RESAMPLES * (1 - FAMILYWISE_ALPHA)) - 1)]
    return low, high


def select_nonoverlap(episodes: list[dict], *, market: str) -> tuple[list[dict], list[tuple[dict, str]]]:
    """결과를 읽기 전에 날짜·동결 주문 차이만으로 분석 블록을 고정한다."""
    eligible = sorted((e for e in episodes if e.get("version") == rotation_shadow.VERSION
                       and e.get("session", "") >= START_SESSION and _divergent(e)),
                      key=lambda e: e["session"])
    selected = []
    last_end = None
    for episode in eligible:
        day = episode["session"]
        if last_end and day <= last_end:
            continue
        sessions = market_clock.next_sessions(market, day, HORIZON)
        if len(sessions) != HORIZON:
            continue
        selected.append((episode, sessions[-1]))
        last_end = sessions[-1]
    return eligible, selected


def assess(episodes: list[dict], *, market: str, completed_session: str | None) -> dict:
    """겹치지 않는 20일 에피소드를 결과를 보기 *전* 날짜만으로 뽑는다.

    누락/가격수정 에피소드를 다음 겹치는 좋은 에피소드로 대체하면 생존편향이므로,
    우선 선택된 블록은 실패해도 커버리지 분모에 남긴다.
    """
    base = {"gate_id": GATE_ID, "market": market, "mode": "shadow",
            "live_eligible": False, "auto_promote": False,
            "start_session": START_SESSION, "primary_horizon_sessions": HORIZON,
            "looks": list(LOOKS), "familywise_alpha": FAMILYWISE_ALPHA,
            "min_coverage": MIN_COVERAGE,
            "note": "비중첩 h20 paired 차이의 근사 bootstrap 구간. 양방향×6시장/성향×4회 판정 보정. 후보라도 실제 봇/실주문 자동 변경 없음."}
    if market not in ("kr", "us"):
        return {**base, "status": "invalid_market", "reason": "지원하지 않는 시장"}
    eligible, selected = select_nonoverlap(episodes, market=market)
    matured = [(e, end) for e, end in selected if completed_session and end <= completed_session]
    look = max((n for n in LOOKS if n <= len(matured)), default=None)
    if look is None:
        return {**base, "status": "awaiting_oos", "reason": "첫 비중첩 12개 h20 완료 대기",
                "divergent_episodes": len(eligible), "nonoverlap_blocks": len(selected),
                "matured_blocks": len(matured), "effective_blocks": 0, "next_look": LOOKS[0]}
    panel = matured[:look]
    complete = [(e, e["forward"]) for e, _ in panel
                if (e.get("forward") or {}).get("ready") and (e["forward"]).get("complete")
                and (e["forward"].get("delta_net_pp") or {}).get("h20") is not None]
    coverage = len(complete) / look
    report = {**base, "look": look, "divergent_episodes": len(eligible),
              "nonoverlap_blocks": len(selected), "matured_blocks": len(matured),
              "effective_blocks": len(complete), "coverage": round(coverage, 4),
              "next_look": next((n for n in LOOKS if n > look), None),
              "selected_sessions": [e["session"] for e, _ in panel],
              "blocked_sessions": [e["session"] for e, _ in panel if not any(e is x for x, _ in complete)]}
    if coverage < MIN_COVERAGE or len(complete) < LOOKS[0]:
        return {**report, "status": "blocked_data_quality",
                "reason": "사전 선택한 비중첩 블록의 완료·무결성 부족 — 겹치는 대체 표본 사용 금지"}
    try:
        net = [float(f["delta_net_pp"]["h20"]) for _, f in complete]
        turnover = [float(f["metrics"]["s0_rank_buffer"]["traded_notional_pct"]) -
                    float(f["metrics"]["champion_rotation_proxy"]["traded_notional_pct"]) for _, f in complete]
        cost = [float(f["metrics"]["s0_rank_buffer"]["cost_drag_pct"]) -
                float(f["metrics"]["champion_rotation_proxy"]["cost_drag_pct"]) for _, f in complete]
        mdd = [float(f["metrics"]["s0_rank_buffer"]["h20"]["max_drawdown_pct"]) -
               float(f["metrics"]["champion_rotation_proxy"]["h20"]["max_drawdown_pct"]) for _, f in complete]
    except (KeyError, TypeError, ValueError):
        return {**report, "status": "blocked_data_quality", "reason": "성과 필드 결손·형식 오류"}
    if any(not math.isfinite(v) for group in (net, turnover, cost, mdd) for v in group):
        return {**report, "status": "blocked_data_quality", "reason": "비유한 성과 값"}
    lower, upper = _bootstrap_interval(net)
    mean = lambda vals: sum(vals) / len(vals)
    stats = {"net_delta_mean_pp": round(mean(net), 4), "net_delta_lower_pp": round(lower, 4),
             "net_delta_upper_pp": round(upper, 4),
             "traded_notional_delta_mean_pp": round(mean(turnover), 4),
             "cost_drag_delta_mean_pp": round(mean(cost), 4),
             "mdd_delta_mean_pp": round(mean(mdd), 4),
             "worst_mdd_delta_pp": round(min(mdd), 4)}
    risk_ok = mean(mdd) >= -MAX_MEAN_MDD_WORSE_PP and min(mdd) >= -MAX_SINGLE_MDD_WORSE_PP
    cost_ok = mean(turnover) < 0 and mean(cost) < 0
    if upper < 0:
        status = "negative"
    elif lower <= 0:
        status = "inconclusive"
    elif not risk_ok or not cost_ok:
        status = "risk_or_cost_failed"
    else:
        status = "manual_review_candidate"
    return {**report, **stats, "status": status,
            "risk_gate_pass": risk_ok, "turnover_cost_gate_pass": cost_ok,
            "reason": ("수동 검토 후보일 뿐 주문/챔피언 변경 권한 없음" if status == "manual_review_candidate"
                       else "사전등록 순성과·회전/비용·낙폭 조건 미충족")}
