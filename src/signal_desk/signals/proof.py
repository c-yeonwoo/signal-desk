"""시그널 판별력 보드 — A(IC·shadow·harness)를 1열로, B/C를 참고로 모은다.

조각(accuracy · shadow · harness · paper scorecard)은 이미 있다.
없는 것은 **한 계약으로 읽는 진입점**뿐이라 여기서 조립만 한다(판정 로직 복제 금지).
화면 이름은 「시그널 판별력」(구 증명 OS / 북극성 A).
"""

from __future__ import annotations

from typing import Any, Callable

NORTH_STAR = "selection"  # A — docs/north-star-selection.md
PROOF_VERSION = "v1"


def _safe(label: str, fn: Callable[[], Any]) -> dict:
    try:
        return {"ok": True, "label": label, "data": fn()}
    except Exception as e:
        return {"ok": False, "label": label, "error": f"{type(e).__name__}: {e}", "data": None}


def _shadow_slim(v: dict | None) -> dict:
    if not v:
        return {"ready": False, "blocked_reason": "응답 없음"}
    return {
        "verdict_ready": bool(v.get("verdict_ready") or v.get("paired_verdict_ready")),
        "delta_pct": v.get("paired_delta_pct", v.get("delta_pct")),
        "delta_ci95_pp": v.get("paired_delta_ci95_pp", v.get("delta_ci95_pp")),
        "matured": v.get("paired_n", v.get("matured", v.get("matured_smaller_side"))),
        "blocked_reason": (v.get("paired_blocked_reason") or v.get("blocked_reason")
                           or v.get("message")),
        "delta_significant": v.get("delta_significant") or v.get("paired_delta_significant"),
    }


def _accuracy_slim(acc: dict) -> dict:
    """A열용 — 팩터 IC·매수 리프트·커버리지만."""
    if not acc.get("ready", True) and acc.get("reason"):
        return {"ready": False, "blocked_reason": acc.get("reason")}
    ic = acc.get("factor_ic") or {}
    ic_stats = acc.get("factor_ic_stats") or {}
    cov = acc.get("coverage") or {}
    factors = ("score", "technical", "fundamental", "valuation", "reversion",
               "flow", "quality", "momentum", "short", "qualitative")
    return {
        "ready": True,
        "buy_lift_pp": acc.get("buy_lift_pp"),
        "sell_lift_pp": acc.get("sell_lift_pp"),
        "buy_precision_pct": acc.get("buy_precision_pct"),
        "baseline_buy_pct": (acc.get("baseline") or {}).get("up_pct"),
        "factor_ic": {k: ic.get(k) for k in factors},
        # IC를 숫자만 내보내면 크기가 판별력처럼 읽힌다 — n·CI·p·차단이유를 같이 싣는다.
        "factor_ic_stats": {k: {kk: (ic_stats.get(k) or {}).get(kk) for kk in (
            "ic", "ic_mean", "n_dates", "independent_dates", "breadth_median",
            "ci95", "t", "p", "significant", "blocked_reason")} for k in factors},
        "ic_min_dates": acc.get("ic_min_dates"),
        "coverage": {
            "rows": cov.get("rows"),
            "matured_primary": cov.get("matured_primary"),
            "blocked_reason": cov.get("blocked_reason"),
            "stale_prices": cov.get("stale_prices"),
            "price_data_to": cov.get("price_data_to"),
        },
    }


def build(
    *,
    accuracy: dict | None = None,
    advisor_shadow: dict | None = None,
    climate_shadow: dict | None = None,
    kb_coverage_shadow: dict | None = None,
    harness_last: dict | None = None,
    harness_board: dict | None = None,
    paper_scorecard: dict | None = None,
    signal_drift: dict | None = None,
    qualitative_promotion: dict | None = None,
) -> dict:
    """이미 계산된 조각을 받아 Proof OS 페이로드를 만든다(순수)."""
    a_acc = _accuracy_slim(accuracy or {"ready": False, "reason": "accuracy 미제공"})
    shadows = {
        "advisor": _shadow_slim(advisor_shadow),
        "climate": _shadow_slim(climate_shadow),
        "kb_coverage": _shadow_slim(kb_coverage_shadow),
    }
    ready_shadows = [k for k, v in shadows.items() if v.get("verdict_ready")]
    hz = harness_last or {}
    # 사전등록 보드가 있으면 그것이 정본이다 — 판정은 등록된 조합의 요건 충족 1회로만 확정된다.
    # `harness_last`는 그 확정 실행의 사본이므로 보드가 없을 때만 단독으로 쓴다(하위 호환).
    if harness_board and harness_board.get("ready"):
        a_harness = {
            "ready": True,
            "status": harness_board.get("status"),
            "verdict": harness_board.get("verdict"),
            "verdict_why": harness_board.get("verdict_why"),
            # 요건 미충족 동안 백분위를 내지 않는다 — 매일 보는 것이 곧 다중검정이다.
            "percentile": harness_board.get("percentile"),
            "threshold_pct": harness_board.get("threshold_pct"),
            "n_registered": harness_board.get("n_registered"),
            "requirement": harness_board.get("requirement"),
            "looks": harness_board.get("looks"),
            "saved_at": hz.get("saved_at"),
            "market": harness_board.get("market"),
            "blocked_reason": None,
            "note": harness_board.get("note"),
        }
    else:
        reason = ((harness_board or {}).get("reason")
                  or hz.get("reason") or "하네스 미실행 — sigdesk harness")
        a_harness = {
            "ready": bool(hz.get("ready")),
            "status": (harness_board or {}).get("status") or "unregistered",
            "verdict": hz.get("verdict"),
            "verdict_why": hz.get("verdict_why"),
            "percentile": (hz.get("vs_random") or {}).get("percentile"),
            "saved_at": hz.get("saved_at"),
            "market": hz.get("market"),
            "blocked_reason": None if hz.get("ready") else reason,
        }
    # base rate 없는 비율은 내보내지 않는다 — 소비자가 좋은지 나쁜지 알 수 없다.
    b_paper = paper_scorecard or {"resolved": 0, "pending": 0, "win_rate": None,
                                  "baseline": {"up_pct": None}, "lift_pp": None, "ci_pp": None,
                                  "informative": False}
    c_note = (
        "Decision/KB veto는 점수 가산이 아니다. 이 층은 이벤트 청산·매수 차단 PR의 성적이다. "
        "A/B만으로 veto를 롤백하지 말 것."
    )
    return {
        "ready": True,
        "version": PROOF_VERSION,
        "north_star": NORTH_STAR,
        "promotion_rule": (
            "엔진·가중·매수권 변경은 시그널 판별력(A: IC/shadow/harness) 유의 개선이 기본. "
            "B(페이퍼)는 타이밍·체결 정합 참고. C는 Decision PR 전용."
        ),
        "contract": {
            "combine_in_score": [
                "technical", "fundamental", "valuation", "reversion",
                "flow", "quality", "momentum", "short",
            ],
            "qualitative_in_combine": False,
            "macro_cycle_hypo_in_combine": False,
            "kb_events": "decision_veto_only",
            "regime_macro": "exposure_or_threshold_gate",
            "doc": "docs/north-star-selection.md",
        },
        "A": {
            "name": "시그널 판별력",
            "primary": True,
            "accuracy": a_acc,
            "shadows": shadows,
            "shadows_verdict_ready": ready_shadows,
            "harness": a_harness,
            "qualitative_promotion": qualitative_promotion,
            "signal_drift": signal_drift,
        },
        "B": {
            "name": "페이퍼 타이밍(종속)",
            "primary": False,
            "paper_scorecard": b_paper,
            "note": "PnL 극대화가 1순위 아님. 시그널이 사·스킵·게이트와 맞는지 확인용.",
        },
        "C": {
            "name": "Decision 회피",
            "primary": False,
            "note": c_note,
        },
    }


def collect() -> dict:
    """스토어·API 의존을 모아 build(). 실패 조각은 ok=False로 남긴다."""
    from signal_desk import db, store
    from signal_desk.signals import accuracy as acc_mod
    from signal_desk.signals import advisor_shadow, climate, kb_coverage
    from signal_desk import signalcfg

    parts: dict[str, Any] = {}

    def _accuracy():
        df = store.load_signal_history()
        if df.empty:
            return {"ready": False, "reason": "PIT 시그널 이력 없음"}
        return {"ready": True, **acc_mod.realized_accuracy(
            df.to_dict("records"), store.load_all_dated_closes())}

    closes = store.load_all_dated_closes()

    parts["accuracy"] = _safe("accuracy", _accuracy)
    parts["advisor"] = _safe(
        "advisor_shadow", lambda: advisor_shadow.summary(closes))
    # climate API는 summary+verdict를 붙이지만 A열 판정은 verdict 필드가 정본.
    parts["climate"] = _safe("climate_shadow", lambda: climate.shadow_verdict(closes))
    parts["kb_cov"] = _safe(
        "kb_coverage",
        lambda: {**kb_coverage.shadow(closes), "coverage": kb_coverage.coverage_now()})
    parts["harness"] = _safe("harness_last", store.load_harness_last)
    parts["harness_board"] = _safe("harness_board", lambda: store.harness_board("kr"))
    parts["paper"] = _safe("paper", store.decision_scorecard_with_baseline)
    parts["drift"] = _safe("drift", store.signal_drift)

    def _qual():
        df = store.load_signal_history()
        closes = store.load_all_dated_closes()
        metrics = acc_mod.qualitative_promotion_metrics(
            [] if df.empty else df.to_dict("records"), closes)
        return signalcfg.qualitative_promotion_status(metrics)

    parts["qual"] = _safe("qualitative_promotion", _qual)

    payload = build(
        accuracy=parts["accuracy"]["data"] if parts["accuracy"]["ok"] else {
            "ready": False, "reason": parts["accuracy"].get("error")},
        advisor_shadow=parts["advisor"]["data"] if parts["advisor"]["ok"] else None,
        climate_shadow=parts["climate"]["data"] if parts["climate"]["ok"] else None,
        kb_coverage_shadow=parts["kb_cov"]["data"] if parts["kb_cov"]["ok"] else None,
        harness_last=parts["harness"]["data"] if parts["harness"]["ok"] else None,
        harness_board=parts["harness_board"]["data"] if parts["harness_board"]["ok"] else None,
        paper_scorecard=parts["paper"]["data"] if parts["paper"]["ok"] else None,
        signal_drift=parts["drift"]["data"] if parts["drift"]["ok"] else None,
        qualitative_promotion=parts["qual"]["data"] if parts["qual"]["ok"] else None,
    )
    payload["fetch_errors"] = {
        k: v.get("error") for k, v in parts.items() if not v.get("ok")
    }
    return payload
