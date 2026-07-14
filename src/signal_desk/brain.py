"""두뇌 레이어 — 엔진 헬스 스냅샷(자가 진단의 score→diagnose 골격, 읽기 전용).

코어 파이프라인(소스→팩터→게이트→엔진→실측 트래커→진단→제안 루프)을 노드 그래프로 모델링하고,
각 노드의 헬스 상태와 규칙 기반 findings를 낸다. **읽기 전용** — 개선을 자동 적용하지 않는다
(제안까지만; 적용은 어드민 승인 후 사람). LLM 진단(opus)은 트래커가 성숙한 뒤 붙일 훅으로 남겨둔다.

status: ok(정상) · warn(주의) · stale(오래됨) · idle(대기/누적중) · candidate(후보/미가동).
build(freshness, accuracy, weights, is_ready) → 순수 함수(입력 주입, 테스트 분리).
"""

from __future__ import annotations

# 팩터 → 입력 소스(freshness key) · 가중치 키 매핑
_FACTORS = [
    ("technical", "기술", ["prices"], "weight_technical"),
    ("fundamental", "기본", ["fundamentals"], "weight_fundamental"),
    ("valuation", "밸류", ["fundamentals"], "weight_valuation"),
    ("reversion", "낙폭", ["prices"], "weight_reversion"),
    ("flow", "수급", ["flows"], "weight_flow"),
    ("quality", "퀄리티", ["fundamentals"], "weight_quality"),
    ("momentum", "모멘텀", ["prices"], "weight_momentum"),
    ("short", "공매도", ["short"], "weight_short"),
]
_GATES = [("regime", "국면", ["macro"]), ("trend", "추세", ["prices"]),
          ("earnings", "어닝", ["us_fund"]), ("kb_veto", "KB veto", ["kb"])]
_SOURCES = [  # (freshness key, 표시명)
    ("prices", "국내 시세"), ("us_prices", "미국 시세"), ("fundamentals", "재무"),
    ("flows", "수급"), ("short", "공매도"), ("consensus", "컨센서스"), ("macro", "거시"),
]
_IC_MIN_WARN = 20   # IC 신뢰 최소 표본(미만이면 판정 보류)
# 타이밍/게이트 역할 팩터 — 5.5년 실측상 횡단면 IC≈0(랭킹 알파 아님). 진입 타이밍·추세게이트로
# 기능하므로 낮은/음수 IC를 경고하지 않는다(오탐 방지). 랭킹 알파는 모멘텀 등이 담당.
_TIMING_FACTORS = {"technical", "reversion"}


def build(freshness: list[dict], accuracy: dict, weights: dict, is_ready: bool) -> dict:
    """엔진 헬스 스냅샷: {score, level, nodes[], edges[], findings[], summary}."""
    fresh = {f["key"]: f for f in (freshness or [])}
    factor_ic = (accuracy or {}).get("factor_ic") or {}
    ic_samples = ((accuracy or {}).get("coverage") or {}).get("matured_primary", 0)
    weights = weights or {}

    nodes: list[dict] = []
    edges: list[dict] = []
    findings: list[dict] = []

    def src_status(keys: list[str]) -> str:
        rs = [fresh.get(k) for k in keys if fresh.get(k)]
        if not rs:
            return "idle"  # 신선도 미추적(KB 등)
        if any(r["stale"] for r in rs):
            return "stale"
        return "ok"

    # 소스 노드
    for key, label in _SOURCES:
        f = fresh.get(key)
        stale = bool(f and f["stale"])
        idle = key == "consensus" and (f is None or (f.get("rows") or 0) == 0)
        st = "stale" if stale else ("idle" if idle else "ok")
        metric = "누적 중" if idle else (f"{f['age_hours']:.0f}h 전" if f and f.get("age_hours") is not None else "없음")
        nodes.append({"id": f"src:{key}", "label": label, "group": "source", "status": st, "metric": metric})
        if stale:
            findings.append({"level": "warn", "text": f"{label} 데이터가 오래됨({metric}) — 갱신 필요"})

    # 팩터 노드 (+ 소스→팩터 엣지)
    active_factors = 0
    for key, label, srcs, wkey in _FACTORS:
        w = weights.get(wkey)
        ss = src_status(srcs)
        ic = factor_ic.get(key)
        st = "ok"
        metric = f"w={w:.2f}" if isinstance(w, (int, float)) else "—"
        if ss == "stale":
            st = "stale"
        elif ic is not None and key in _TIMING_FACTORS:
            metric += f" · IC{ic:+.2f}"  # 타이밍/게이트 역할 — 낮은/음수 IC 정상(경고 안 함)
            if ic_samples >= _IC_MIN_WARN and ic < 0:
                findings.append({"level": "info", "text": f"{label} 팩터 IC {ic:+.2f} — 타이밍/게이트 역할이라 횡단면 IC 낮음이 정상"})
        elif ic is not None and ic_samples >= _IC_MIN_WARN and ic < 0:
            st = "warn"; metric += f" · IC{ic:+.2f}"
            findings.append({"level": "warn", "text": f"{label} 팩터 IC 음수({ic:+.2f}, N={ic_samples}) — 가중 재검토 후보"})
        elif ic is not None:
            metric += f" · IC{ic:+.2f}"
        if ss != "stale":
            active_factors += 1
        nodes.append({"id": f"fac:{key}", "label": label, "group": "factor", "status": st, "metric": metric})
        for s in srcs:
            if any(sk == s for sk, _ in _SOURCES):
                edges.append({"source": f"src:{s}", "target": f"fac:{key}"})
        edges.append({"source": f"fac:{key}", "target": "engine"})

    # 게이트 노드 (→ 엔진)
    for key, label, srcs in _GATES:
        st = "stale" if src_status(srcs) == "stale" else "ok"
        nodes.append({"id": f"gate:{key}", "label": label, "group": "gate", "status": st, "metric": "게이트"})
        edges.append({"source": f"gate:{key}", "target": "engine"})

    # 컨센서스 → 엔진(목표가 v2, 점선)
    edges.append({"source": "src:consensus", "target": "engine", "kind": "aux"})

    # 엔진 노드
    nodes.append({"id": "engine", "label": "종합 엔진", "group": "engine",
                  "status": "ok" if is_ready else "idle",
                  "metric": "가동" if is_ready else "데이터 대기"})
    edges.append({"source": "engine", "target": "tracker"})

    # 실측 트래커 노드
    cov = (accuracy or {}).get("coverage") or {}
    tracker_ready = bool((accuracy or {}).get("ready"))
    days = cov.get("dates") or 0
    if tracker_ready:
        tstat, tmetric = "ok", f"성숙 {ic_samples}건"
    else:
        tstat, tmetric = "idle", f"누적 {days}일 · 성숙 0"
        findings.append({"level": "info", "text": f"실측 트래커 누적 {days}일차 — 20거래일 성숙 후 팩터 IC·진단 가동"})
    nodes.append({"id": "tracker", "label": "실측 트래커", "group": "tracker", "status": tstat, "metric": tmetric})

    # 두뇌: 진단 → 제안 (트래커 성숙 전까진 idle)
    brain_stat = "ok" if tracker_ready else "idle"
    nodes.append({"id": "diagnose", "label": "자가 진단", "group": "brain", "status": brain_stat,
                  "metric": "규칙+LLM" if tracker_ready else "대기"})
    nodes.append({"id": "propose", "label": "개선 제안", "group": "brain", "status": brain_stat,
                  "metric": "어드민 승인" })
    edges.append({"source": "tracker", "target": "diagnose"})
    edges.append({"source": "diagnose", "target": "propose"})
    edges.append({"source": "propose", "target": "engine", "kind": "loop"})  # 승인 후 반영 루프

    # 헬스 스코어(소스 신선도·팩터 가동·트래커)
    src_total = len(_SOURCES)
    src_fresh = sum(1 for k, _ in _SOURCES if fresh.get(k) and not fresh[k]["stale"])
    factor_frac = active_factors / len(_FACTORS)
    src_frac = src_fresh / src_total if src_total else 0
    score = round(100 * (0.5 * src_frac + 0.3 * factor_frac + 0.2 * (1 if tracker_ready else 0.5 if is_ready else 0)))
    stale_n = sum(1 for f in findings if f["level"] == "warn")
    level = "warn" if (score < 70 or stale_n) else "ok"

    summary = (f"엔진 헬스 {score}/100 · 팩터 {active_factors}/{len(_FACTORS)} 가동 · "
               f"소스 {src_fresh}/{src_total} 신선 · 트래커 {'성숙' if tracker_ready else '누적중'}")
    return {"score": score, "level": level, "nodes": nodes, "edges": edges,
            "findings": findings[:12], "summary": summary}
