"""Predeclared, non-overlapping R15 event-cluster research looks."""

from __future__ import annotations

import math
import random

from signal_desk import market_clock
from signal_desk.signals import relation_event_study as study

GATE_ID = "r15-us-kr-event-oos-v1"
LOOKS = (12, 24, 36, 48)
MIN_COVERAGE = 0.90
ALPHA = 0.05 / (2 * len(LOOKS))
RESAMPLES = 10_000


def _interval(values: list[float]) -> tuple[float, float]:
    rng = random.Random(1515)
    n = len(values)
    draws = sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n
                   for _ in range(RESAMPLES))
    return (draws[max(0, math.ceil(RESAMPLES * ALPHA) - 1)],
            draws[min(RESAMPLES - 1, math.ceil(RESAMPLES * (1 - ALPHA)) - 1)])


def assess(events: list[dict], *, completed_session: str | None) -> dict:
    base = {"gate_id": GATE_ID, "mode": "research_only", "live_eligible": False,
            "auto_promote": False, "source_available_at_verified": False,
            "start_session": study.START_DATE, "looks": list(LOOKS),
            "familywise_alpha": ALPHA, "min_coverage": MIN_COVERAGE,
            "note": "동일 20거래일 사건을 한 클러스터로 묶은 방향 정규화 상대성과. 원천 공개시각·기업행동·호가·실체결 미검증; 실전 승격 불가."}
    population = sorted((e for e in events if (e.get("review") or {}).get("verdict") == "approved"
                         and (e["review"].get("capture_session") or "") >= study.START_DATE),
                        key=lambda e: (e["review"]["capture_session"], e["id"]))
    clusters: list[dict] = []
    for event in population:
        day = event["review"]["capture_session"]
        sessions = market_clock.next_sessions("kr", day, study.HORIZON)
        end = sessions[-1] if len(sessions) == study.HORIZON else day
        if clusters and day <= clusters[-1]["end"]:
            clusters[-1]["events"].append(event)
            clusters[-1]["end"] = max(clusters[-1]["end"], end)
            continue
        clusters.append({"start": day, "end": end, "events": [event]})
    matured = [c for c in clusters if completed_session and c["end"] <= completed_session]
    look = max((n for n in LOOKS if n <= len(matured)), default=None)
    if look is None:
        return {**base, "status": "awaiting_oos", "approved_events": len(population),
                "matured_clusters": len(matured), "next_look": LOOKS[0]}
    panel = matured[:look]
    complete = []
    blocked = []
    for cluster in panel:
        forwards = [e.get("forward") or {} for e in cluster["events"]]
        values = [f.get("directional_delta_net_pp") for f in forwards]
        if not all(f.get("ready") and f.get("status") == "complete"
                   and type(v) in (int, float) and math.isfinite(v)
                   for f, v in zip(forwards, values)):
            blocked.append(cluster["start"])
            continue
        complete.append(sum(values) / len(values))
    report = {**base, "look": look, "next_look": next((n for n in LOOKS if n > look), None),
              "approved_events": len(population), "matured_clusters": len(matured),
              "effective_clusters": len(complete), "coverage": round(len(complete) / look, 4),
              "blocked_cluster_starts": blocked}
    if len(complete) / look < MIN_COVERAGE or len(complete) < LOOKS[0]:
        return {**report, "status": "blocked_data_quality",
                "reason": "사전 선택 사건 클러스터 입력/전진가격 무결성 부족"}
    lower, upper = _interval(complete)
    status = ("positive_research_signal" if lower > 0 else
              "negative_research_signal" if upper < 0 else "inconclusive")
    return {**report, "status": status,
            "directional_delta_mean_pp": round(sum(complete) / len(complete), 4),
            "directional_delta_lower_pp": round(lower, 4),
            "directional_delta_upper_pp": round(upper, 4),
            "reason": "조건부 연구 신호만; 신호·페이퍼·실주문 정책 자동 변경 없음"}
