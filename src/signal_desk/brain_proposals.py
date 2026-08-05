"""두뇌 개선 제안 — 실측 IC 기반 보수적 가중/임계 조정 초안 + 관리자 승인 적용.

자동 적용 없음. refresh()가 draft 카드를 만들고, review(approved)만이
signalcfg에 반영한다. 카드 문구는 수식 없이 한국어로 읽히게 쓴다.
"""

from __future__ import annotations

import logging
import time
from datetime import date

from signal_desk import db, signalcfg
from signal_desk.brain import _TIMING_FACTORS

log = logging.getLogger(__name__)

# 랭킹 알파 후보만 가중 nudge (타이밍/게이트 팩터는 IC 낮음이 정상 → 제외)
_FACTOR_KO = {
    "fundamental": "기본적",
    "valuation": "저평가",
    "flow": "수급",
    "quality": "퀄리티",
    "momentum": "모멘텀",
    "short": "공매도",
}
_WEIGHT_KEY = {k: f"weight_{k}" for k in _FACTOR_KO}
_DELTA = 0.05          # 한 번에 바꾸는 최대 폭(보수)
_WEIGHT_FLOOR = 0.05
_WEIGHT_CEIL = 0.50
_IC_MIN_SAMPLES = 20   # 행 단위 — 임계 nudge(정밀도)용. IC에는 쓰지 않는다.
_IC_MIN_DATES = 20     # IC 관측 날짜 최소치(accuracy._MIN_IC_DATES와 같은 규약)
_IC_BOOST_MIN = 0.05   # 이 이상 양수 IC만 비중 ↑ 후보
_THR_DELTA = 0.10
_THR_FLOOR = 0.90
_THR_CEIL = 2.00
_PREC_LOW = 45.0
_PREC_HIGH = 58.0
_METHOD_KEY = "ic_weighting"


def ic_usable(stat: dict | None) -> tuple[bool, str]:
    """이 IC를 가중치 근거로 쓸 수 있는가 — `(가능, 이유)`.

    **크기가 아니라 유의성으로 판단한다.** 예전 게이트는 `matured_primary`(행) ≥ 20이었는데,
    `factor_ic`는 그 시점에 이미 **날짜를 섞은 pooled 상관**이었다. 하루치 200종목이 표본 200으로
    세어져 문턱을 즉시 통과했고, 실제로 `short IC −0.148`이 **단 하루**에서 나온 값이었다.
    지금은 `accuracy.cross_sectional_ic`가 날짜 단위 n·Newey-West SE·t·p를 주므로 그걸 본다.
    """
    if not isinstance(stat, dict):
        return False, "IC 통계 없음 — 횡단면 IC를 계산하지 못했다"
    if stat.get("ic") is None or not stat.get("significant"):
        return False, str(stat.get("blocked_reason") or "IC가 0과 구분 불가")
    return True, ""


def _confidence(stat: dict) -> str:
    """신뢰 등급 — 날짜 수와 p로 정한다(IC 절댓값은 등급의 근거가 아니다)."""
    n = int(stat.get("n_dates") or 0)
    p = stat.get("p")
    if p is None:
        return "low"
    if n >= _IC_MIN_DATES * 3 and p < 0.01:
        return "high"
    if n >= _IC_MIN_DATES and p < 0.05:
        return "medium"
    return "low"


def _ic_evidence(stat: dict) -> dict:
    """제안 evidence에 남길 IC 통계 — 나중에 "무슨 근거로 바꿨나"를 재구성할 수 있어야 한다."""
    return {k: stat.get(k) for k in
            ("n_dates", "independent_dates", "breadth_median", "horizon",
             "ic_ir", "ci95", "t", "p", "significant", "nw_lag", "se_floored")}


def _ic_rationale(label: str, stat: dict) -> str:
    """근거 문구 — IC 하나만 쓰지 않는다. n·CI·p 없이 내보내면 크기가 판별력처럼 읽힌다."""
    ic = stat.get("ic")
    ci = stat.get("ci95")
    bits = [f"{label} 예측력(IC) {ic:+.3f}"]
    if ci is not None:
        bits[0] += f" ±{ci:.3f}"
    bits.append(f"관측 {int(stat.get('n_dates') or 0)}거래일"
                f"(독립 ≈ {int(stat.get('independent_dates') or 0)})")
    if stat.get("p") is not None:
        bits.append(f"p={stat['p']:.4f}")
    bits.append(f"신뢰 {_conf_ko(_confidence(stat))}")
    return " · ".join(bits)


def _conf_ko(c: str) -> str:
    return {"high": "높음", "medium": "보통", "low": "낮음"}.get(c, c)


def composite_ic_estimate(factor_ic: dict, weights: dict) -> float | None:
    """팩터 IC × 가중치 가중평균 — composite score IC의 거친 추정(방향·크기 참고용).

    개별 Spearman IC는 가중치와 무관하므로, 패치 전/후 가중으로만 proxy를 비교한다.
    절댓값은 신뢰하지 말 것.
    """
    total_w = total_wic = 0.0
    for factor, ic in (factor_ic or {}).items():
        if not isinstance(ic, (int, float)):
            continue
        w = float((weights or {}).get(f"weight_{factor}") or 0)
        if w <= 0:
            continue
        total_w += w
        total_wic += w * float(ic)
    if total_w <= 0:
        return None
    return round(total_wic / total_w, 3)


def _attach_shallow_ab(draft: dict, accuracy: dict, weights: dict) -> dict:
    """제안 evidence에 얕은 A/B(추정 composite IC 전후·정밀도 스냅샷)를 붙인다."""
    ev = dict(draft.get("evidence") or {})
    factor_ic = (accuracy or {}).get("factor_ic") or {}
    patch = draft.get("patch") or {}
    before_ic = composite_ic_estimate(factor_ic, weights)
    weight_patch = {k: v for k, v in patch.items() if str(k).startswith("weight_")}
    after_ic = None
    if weight_patch:
        after_ic = composite_ic_estimate(factor_ic, {**(weights or {}), **weight_patch})
    if before_ic is not None:
        ev["before_composite_ic"] = before_ic
    if after_ic is not None:
        ev["after_composite_ic"] = after_ic
        ev["ab_delta"] = round(after_ic - (before_ic or 0), 3)
        ev["ab_kind"] = "composite_ic"
    if draft.get("kind") == "threshold_nudge" and accuracy.get("buy_precision_pct") is not None:
        ev["before_buy_precision_pct"] = accuracy.get("buy_precision_pct")
        ev["ab_kind"] = "threshold_remeasure"
    draft["evidence"] = ev
    return draft


def build_weight_nudge(factor: str, stat: dict, weights: dict) -> dict | None:
    """**유의하게** 음수인 IC 랭킹 팩터 → 비중 ↓ 제안 1장.

    `stat`은 `accuracy.cross_sectional_ic` 산출물이다 — 스칼라 IC만 받던 옛 시그니처는
    n·p를 호출자가 버릴 수 있어서(그리고 실제로 행 단위 n을 넘겼다) 없앴다.
    """
    if factor in _TIMING_FACTORS or factor not in _FACTOR_KO:
        return None
    usable, _ = ic_usable(stat)
    ic = (stat or {}).get("ic")
    if not usable or ic >= 0:
        return None
    wkey = _WEIGHT_KEY[factor]
    cur = float(weights.get(wkey) or 0)
    if cur <= _WEIGHT_FLOOR + 1e-9:
        return None
    new_w = round(max(_WEIGHT_FLOOR, cur - _DELTA), 3)
    if new_w >= cur:
        return None
    label = _FACTOR_KO[factor]
    conf = _confidence(stat)
    return {
        "kind": "weight_nudge",
        "title": f"{label} 비중을 조금 줄이기",
        "body_ko": (
            f"최근 실측에서 ‘{label}’ 신호가 잘 안 맞았습니다. "
            f"이 팩터에 덜 기대도록 비중을 {cur:.2f} → {new_w:.2f}로 살짝 낮춥니다. "
            f"한 번에 크게 바꾸지 않습니다."
        ),
        "rationale_ko": _ic_rationale(label, stat),
        "patch": {wkey: new_w},
        "evidence": {
            "key": f"w_down_{factor}",
            "factor": factor,
            "direction": "down",
            "factor_ic": round(ic, 4),
            **_ic_evidence(stat),
            "from_weight": cur,
            "to_weight": new_w,
        },
        "method_key": _METHOD_KEY,
        "confidence": conf,
    }


def build_weight_boost(factor: str, stat: dict, weights: dict) -> dict | None:
    """**유의한** 양수 IC 랭킹 팩터 → 비중 ↑ 제안 1장(보수적 상한)."""
    if factor in _TIMING_FACTORS or factor not in _FACTOR_KO:
        return None
    usable, _ = ic_usable(stat)
    ic = (stat or {}).get("ic")
    if not usable or ic < _IC_BOOST_MIN:
        return None
    wkey = _WEIGHT_KEY[factor]
    cur = float(weights.get(wkey) or 0)
    if cur >= _WEIGHT_CEIL - 1e-9:
        return None
    new_w = round(min(_WEIGHT_CEIL, cur + _DELTA), 3)
    if new_w <= cur:
        return None
    label = _FACTOR_KO[factor]
    conf = _confidence(stat)
    return {
        "kind": "weight_nudge",
        "title": f"{label} 비중을 조금 높이기",
        "body_ko": (
            f"최근 실측에서 ‘{label}’ 신호가 비교적 잘 맞았습니다. "
            f"이 팩터에 조금 더 기대도록 비중을 {cur:.2f} → {new_w:.2f}로 살짝 올립니다. "
            f"한 번에 크게 바꾸지 않습니다."
        ),
        "rationale_ko": _ic_rationale(label, stat),
        "patch": {wkey: new_w},
        "evidence": {
            "key": f"w_up_{factor}",
            "factor": factor,
            "direction": "up",
            "factor_ic": round(ic, 4),
            **_ic_evidence(stat),
            "from_weight": cur,
            "to_weight": new_w,
        },
        "method_key": _METHOD_KEY,
        "confidence": conf,
    }


def build_threshold_nudge(accuracy: dict, weights: dict) -> dict | None:
    """매수 정밀도 기반 기본 매수 임계 ±0.1 제안(국면 bump와 별개 — 기본값만 조정)."""
    prec = accuracy.get("buy_precision_pct")
    sample = int(accuracy.get("buy_sample") or 0)
    n = int(((accuracy.get("coverage") or {}).get("matured_primary") or 0))
    if prec is None or sample < _IC_MIN_SAMPLES or n < _IC_MIN_SAMPLES:
        return None
    cur = float(weights.get("buy_threshold") or 1.2)
    strong = float(weights.get("strong_buy_threshold") or 2.0)
    gap = strong - cur  # 강력매수와의 간격 유지

    if prec < _PREC_LOW:
        new_buy = round(min(_THR_CEIL, cur + _THR_DELTA), 3)
        direction = "up"
        title = "매수 문턱을 조금 높이기"
        body = (
            f"최근 매수 시그널의 적중률이 {prec:.0f}%로 낮은 편입니다(표본 {sample}건). "
            f"기본 매수 기준을 {cur:.2f} → {new_buy:.2f}로 살짝 올려, "
            f"확신이 더 높은 경우만 매수로 나가게 합니다. "
            f"(약세 때 자동으로 더 올리는 ‘국면 적응’과는 별개입니다.)"
        )
    elif prec >= _PREC_HIGH:
        new_buy = round(max(_THR_FLOOR, cur - _THR_DELTA), 3)
        direction = "down"
        title = "매수 문턱을 조금 낮추기"
        body = (
            f"최근 매수 시그널의 적중률이 {prec:.0f}%로 괜찮은 편입니다(표본 {sample}건). "
            f"기본 매수 기준을 {cur:.2f} → {new_buy:.2f}로 살짝 내려, "
            f"좋은 기회를 덜 놓치게 합니다. "
            f"(약세·순매도일 때 국면 적응이 여전히 문턱을 올릴 수 있습니다.)"
        )
    else:
        return None

    if abs(new_buy - cur) < 1e-9:
        return None
    new_strong = round(new_buy + max(0.5, gap), 3)
    conf = "high" if sample >= 60 else "medium"
    return {
        "kind": "threshold_nudge",
        "title": title,
        "body_ko": body,
        "rationale_ko": (
            f"매수 정밀도 {prec:.1f}% · 매수 표본 {sample}건 · 신뢰 {_conf_ko(conf)}"
        ),
        "patch": {
            "buy_threshold": new_buy,
            "strong_buy_threshold": new_strong,
        },
        "evidence": {
            "key": f"thr_{direction}",
            "factor": "_buy_threshold",
            "direction": direction,
            "buy_precision_pct": prec,
            "buy_sample": sample,
            "from_weight": cur,
            "to_weight": new_buy,
        },
        "method_key": "threshold_precision",
        "confidence": conf,
    }


def _upsert_draft(draft: dict, baseline: dict, today: str) -> dict:
    key = (draft.get("evidence") or {}).get("key") or draft["title"]
    factor = (draft.get("evidence") or {}).get("factor")
    existing = db.brain_proposal_draft_for_factor(factor) if factor else None
    # 같은 factor라도 up/down 키가 다르면 별도 카드 — key로 재검색
    if existing and (existing.get("evidence") or {}).get("key") != key:
        existing = None
        for row in db.brain_proposal_list(status="draft", limit=80):
            if (row.get("evidence") or {}).get("key") == key:
                existing = row
                break
    pid = existing["id"] if existing else f"bp_{today}_{key}"
    item = {
        **draft,
        "id": pid,
        "baseline": baseline,
        "status": "draft",
        "created": existing["created"] if existing else int(time.time()),
        "reviewed": None,
        "note": None,
    }
    db.brain_proposal_upsert(item)
    return item


def _verdict_gate(*, automated: bool) -> tuple[bool, str]:
    """판별력 판정이 파라미터 변경을 허용하는지. 실패해도 예외를 던지지 않는다(이유를 화면에 낸다)."""
    try:
        from signal_desk import prereg, store
        ok, why, _ = prereg.change_allowed(store.harness_board("kr"), automated=automated)
        return ok, why
    except Exception as e:                      # noqa: BLE001 — 게이트가 죽으면 **막는 쪽**이 안전하다
        return False, f"판별력 보드를 읽을 수 없어 제안을 보류합니다: {type(e).__name__}"


def refresh(accuracy: dict, weights: dict | None = None) -> dict:
    """실측 accuracy → draft 제안 upsert. 트래커 미성숙이면 생성 스킵.

    반환: {ok, created, reason?, items[]}
    """
    weights = weights or signalcfg.get_dict()
    # 판정 게이트(N2) — 정본 판정이 확정되기 전에는 **제안을 만들지 않는다**.
    # 생성을 허용하고 승인만 막으면 큐가 쌓이고, 쌓인 큐는 관리자 화면 배지로 떠서 승인을
    # 유도하는 압력이 된다. 그리고 지금 측정된 상태는 IC 0과 구분 불가 · 6팩터 백분위 53.5다 —
    # 그 위에서 LLM이 가중치를 제안하면 그건 곡선 맞추기다.
    gate_ok, gate_why = _verdict_gate(automated=True)
    if not gate_ok:
        return {"ok": True, "created": 0, "reason": gate_why, "items": [], "gated": True}
    if not (accuracy or {}).get("ready"):
        return {"ok": True, "created": 0,
                "reason": (
                    "실측 트래커가 아직 성숙하지 않아 제안을 만들지 않습니다. "
                    "매일 장이 끝난 뒤 그날 시그널을 저장하고, 약 20거래일 뒤부터 성적표를 냅니다. "
                    "시그널이 적거나 봇이 안 사는 것과는 별개입니다."
                ),
                "items": []}
    n = int(((accuracy.get("coverage") or {}).get("matured_primary") or 0))
    if n < _IC_MIN_SAMPLES:
        return {"ok": True, "created": 0,
                "reason": (
                    f"성숙 표본 {n}/{_IC_MIN_SAMPLES}건 — 제안 보류. "
                    f"시그널을 낸 뒤 20거래일이 지나야 채점됩니다(봇 매매 여부와 무관)."
                ),
                "items": []}
    # IC 통계(날짜 단위 n·CI·t·p). 없으면 스칼라 IC 하나로 제안하지 않는다 —
    # 그건 예전 pooled 상관이고, 크기만으로 가중치를 바꾸는 게 정확히 막으려는 것이다.
    ic_stats = accuracy.get("factor_ic_stats") or {}
    today = date.today().isoformat().replace("-", "")
    created, items = 0, []
    baseline = dict(weights)
    skipped: list[str] = []

    # 1) **유의하게** 음수인 IC → 비중 ↓ (해당 팩터마다)
    for factor, stat in ic_stats.items():
        if factor in _TIMING_FACTORS or factor not in _FACTOR_KO:
            continue
        usable, why = ic_usable(stat)
        if not usable:
            skipped.append(f"{_FACTOR_KO[factor]}: {why}")
            continue
        draft = build_weight_nudge(factor, stat, weights)
        if not draft:
            continue
        _attach_shallow_ab(draft, accuracy, weights)
        items.append(_upsert_draft(draft, baseline, today))
        created += 1

    # 2) 가장 강한 양수 IC 1개만 비중 ↑ (카드 폭증 방지)
    best_up = None
    best_ic = _IC_BOOST_MIN - 1
    for factor, stat in ic_stats.items():
        if not ic_usable(stat)[0]:
            continue
        ic = float(stat["ic"])
        if ic > best_ic:
            cand = build_weight_boost(factor, stat, weights)
            if cand:
                best_up, best_ic = cand, ic
    if best_up:
        _attach_shallow_ab(best_up, accuracy, weights)
        items.append(_upsert_draft(best_up, baseline, today))
        created += 1

    # 3) 매수 정밀도 기반 임계 nudge (있을 때만 1장)
    thr = build_threshold_nudge(accuracy, weights)
    if thr:
        _attach_shallow_ab(thr, accuracy, weights)
        items.append(_upsert_draft(thr, baseline, today))
        created += 1

    if not items:
        # 「안정권이라 제안 없음」은 변호였다. 0의 이유는 점검 결과여야 한다 —
        # 팩터별로 왜 못 썼는지(날짜 부족인지 무유의인지)를 이름과 함께 낸다.
        reason = "지금은 바꿀 만한 제안이 없습니다."
        if skipped:
            reason += " IC를 근거로 쓸 수 없는 팩터 — " + " / ".join(skipped)
        return {"ok": True, "created": 0, "items": [], "reason": reason,
                "ic_skipped": skipped}
    return {"ok": True, "created": created, "items": items, "ic_skipped": skipped}


def review(pid: str, status: str, note: str = "", accuracy: dict | None = None) -> dict:
    """승인|반려. 승인 시 patch를 현재 설정에 병합·이력 기록·캐시는 호출측에서 clear.

    status 전환은 draft CAS(claim)로 이중 승인을 막는다 — 선점 성공 후에만 설정 반영.
    accuracy가 있으면 승인 시점 정밀도·추정 IC를 history에 남겨 얕은 A/B로 쓴다.
    """
    if status not in ("approved", "rejected"):
        return {"ok": False, "error": "status는 approved|rejected"}
    item = db.brain_proposal_get(pid)
    if not item:
        return {"ok": False, "error": "제안을 찾을 수 없습니다."}
    if item["status"] != "draft":
        return {"ok": False, "error": f"이미 처리된 제안입니다({item['status']})."}

    if not db.brain_proposal_claim(pid, status, note):
        return {"ok": False, "error": "이미 처리된 제안입니다."}

    applied = None
    if status == "approved":
        gate_ok, gate_why = _verdict_gate(automated=True)
        if not gate_ok:
            return {"ok": False, "error": gate_why, "id": pid, "status": status, "gated": True}
        before = signalcfg.get_dict()
        patch = {k: v for k, v in (item.get("patch") or {}).items() if k in signalcfg.FIELDS}
        if not patch:
            return {"ok": False, "error": "적용할 변경값이 없습니다.", "id": pid, "status": status}
        merged = {**before, **patch}
        after = signalcfg.set_dict(merged)
        ev = item.get("evidence") or {}
        acc_snap = None
        if accuracy:
            cov = (accuracy.get("coverage") or {})
            acc_snap = {
                "buy_precision_pct": accuracy.get("buy_precision_pct"),
                "factor_ic": accuracy.get("factor_ic") or {},
                "matured_primary": cov.get("matured_primary"),
                "composite_ic": composite_ic_estimate(accuracy.get("factor_ic") or {}, before),
                "projected_composite_ic": ev.get("after_composite_ic"),
                "ab_kind": ev.get("ab_kind"),
            }
        signalcfg.append_history({
            "ts": int(time.time()),
            "source": "brain_proposal",
            "proposal_id": pid,
            "title": item.get("title"),
            "before": before,
            "after": after,
            "patch": patch,
            "note": note or None,
            "accuracy_at_approve": acc_snap,
        })
        applied = {"patch": patch, "config": after, "accuracy_at_approve": acc_snap}
        log.info("brain proposal approved id=%s patch=%s", pid, patch)

    return {"ok": True, "id": pid, "status": status, "applied": applied}


def list_proposals(status: str | None = "draft", limit: int = 50) -> list[dict]:
    return db.brain_proposal_list(status=status, limit=limit)
