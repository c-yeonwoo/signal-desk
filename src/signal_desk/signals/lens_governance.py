"""렌즈 연구결과의 승격 차단 계약. 이 모듈은 주문 상태를 변경하지 않는다."""

from __future__ import annotations

import math
import statistics

from signal_desk.signals import lens_forward

VERSION = "lens-promotion-gate-v1"
DISCOVERY_EPISODES = 20
HOLDOUT_EPISODES = 30
MAX_EXCLUSION_RATE = 0.10
MIN_INVESTED_FRACTION = 0.50
# 한 번만 보는 3개 사전등록 조합의 편도 다중비교 한계보다 보수적인 근사.
LOWER_BOUND_Z = 2.5


def _lower_bound(values: list[float]) -> float:
    return statistics.mean(values) - LOWER_BOUND_Z * statistics.stdev(values) / math.sqrt(len(values))


def assess(report: dict) -> dict:
    """처음 20회는 탐색, 다음 30회만 별도 검증. 이후 재조회로 유리한 구간을 고르지 않는다."""
    episodes = report.get("episodes") or []
    market = report.get("market")
    base = {"version": VERSION, "market": market, "mode": "research_only",
            "live_eligible": False, "auto_promote": False,
            "discovery_required": DISCOVERY_EPISODES, "holdout_required": HOLDOUT_EPISODES,
            "matured_episodes": len(episodes), "candidates": [],
            "operational_blocks": [
                "주문과 무관한 사용자 조회 시각에 표본 수집이 의존함",
                "배당·분할을 포함한 수정주가 검증이 없음",
                "실제 호가·체결·세금·슬리피지 비용 검증이 없음",
                "시장·섹터 노출과 현금 효과를 분리하지 못함",
                "승격 승인자·정책 버전·실시간 중지 조건 기록이 없음",
            ],
            "rollback_conditions": [
                "원천 가격 또는 최초 관측 시점의 무결성 오류",
                "실거래 비용이 연구 가정을 초과하거나 주문 실패·중복 발생",
                "검증 구간의 비용 후 상대 성과가 음수로 전환",
                "신호 분포 또는 데이터 커버리지가 검증 구간 밖으로 이탈",
            ]}
    if market not in ("kr", "us"):
        return {**base, "status": "invalid_market"}
    required = DISCOVERY_EPISODES + HOLDOUT_EPISODES
    if len(episodes) < required:
        return {**base, "status": "awaiting_prospective_evidence",
                "next_required_episodes": required - len(episodes),
                "reason": "사전 동결한 독립 구간과 미래 검증 구간이 아직 부족합니다."}
    panel = episodes[:required]
    policy_ids = {e.get("signal_policy_id") for e in panel}
    if len(policy_ids) != 1 or None in policy_ids:
        return {**base, "status": "blocked_policy_drift",
                "reason": "비교 도중 기본 시그널 정책 버전이 바뀌거나 확인되지 않았습니다."}
    observed = max(int(report.get("cohorts_seen") or 0), len(episodes))
    exclusion_rate = 1 - len(episodes) / observed
    if exclusion_rate > MAX_EXCLUSION_RATE:
        return {**base, "status": "blocked_sample_coverage", "exclusion_rate": exclusion_rate,
                "reason": "조회 후 제외된 구간이 많아 결과 대표성을 판단할 수 없습니다."}
    holdout = panel[DISCOVERY_EPISODES:]
    for combo in ("event", "entry", "event_entry"):
        try:
            paired = [float(e["combos"][combo]["net_return"]
                            - e["combos"]["base"]["net_return"]) for e in holdout]
            coverage = statistics.mean(float(e["combos"][combo]["coverage"]) for e in holdout)
            drawdown = lens_forward._drawdown([float(e["combos"][combo]["net_return"]) for e in holdout])
            base_drawdown = lens_forward._drawdown([float(e["combos"]["base"]["net_return"]) for e in holdout])
        except (KeyError, ValueError, TypeError, statistics.StatisticsError):
            return {**base, "status": "blocked_data_quality", "reason": "검증 구간 수익·비중이 누락됐습니다."}
        if not all(math.isfinite(v) for v in paired) or not math.isfinite(coverage):
            return {**base, "status": "blocked_data_quality", "reason": "검증 구간 수익·비중이 유효하지 않습니다."}
        lower = _lower_bound(paired)
        if lower > 0 and coverage >= MIN_INVESTED_FRACTION and drawdown <= base_drawdown:
            base["candidates"].append({"combo": combo, "mean_excess": statistics.mean(paired),
                                       "conservative_lower_bound": lower,
                                       "mean_invested_fraction": coverage,
                                       "holdout_drawdown": drawdown,
                                       "base_drawdown": base_drawdown})
    return {**base, "status": "research_candidate_operationally_blocked" if base["candidates"]
            else "no_validated_advantage", "holdout_evaluated": HOLDOUT_EPISODES,
            "exclusion_rate": exclusion_rate,
            "reason": "연구상 우위가 보여도 운영·체결·가격 품질 게이트를 통과하기 전에는 주문에 연결하지 않습니다."}
