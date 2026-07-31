"""Desk Report (L4) — 오늘 의사결정 사다리를 한 장으로 조립한다.

템플릿만 쓴다. LLM 호출 없음 · 점수/kind/주문 불변.
입력은 이미 L0~L3가 만든 필드(시그널·선정·편중·익스포저)다.
"""

from __future__ import annotations

from signal_desk.signals.engine import SignalResult, is_buy


def _g(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _vacancy_note(r) -> str | None:
    for reason in _g(r, "reasons") or []:
        if "[선정]" in reason and "공석" in reason:
            return reason.split("—", 1)[-1].strip() if "—" in reason else reason
    if _g(r, "gate_blocked") or _g(r, "event_risk") or _g(r, "decision_buy_blocked"):
        tag = _g(r, "hold_tag")
        return f"{tag}로 자리 공석" if tag else "게이트·악재로 자리 공석"
    return None


def _buy_row(r) -> dict:
    return {
        "ticker": _g(r, "ticker"),
        "name": _g(r, "name") or _g(r, "ticker"),
        "kind": _g(r, "kind"),
        "score": round(float(_g(r, "score") or 0), 2),
        "rank": _g(r, "rank"),
        "sector": _g(r, "sector"),
    }


def build(
    signals,
    *,
    selection: dict | None = None,
    crowding: dict | None = None,
    exposure: float | None = None,
    exposure_reasons: list[str] | None = None,
    market: str = "kospi",
) -> dict:
    """결정론 Desk Report.

    반환 필드는 UI·브리핑·감사 컨텍스트가 같이 쓴다.
    """
    sel = selection or {}
    crowd = crowding or {}
    rows = list(signals or [])
    buys = [r for r in rows if is_buy(_g(r, "kind") or "")]
    slots = sel.get("rank_slots")
    # 원 상위 k자리 안인데 승격 실패 = 공석 (창 밖 채우기 금지 정책의 반대편)
    vacancies = []
    for r in rows:
        rank = _g(r, "rank")
        if not slots or rank is None or rank > slots:
            continue
        if _g(r, "rank_eligible") or is_buy(_g(r, "kind") or ""):
            continue
        vacancies.append({
            "ticker": _g(r, "ticker"),
            "name": _g(r, "name") or _g(r, "ticker"),
            "rank": rank,
            "score": round(float(_g(r, "score") or 0), 2),
            "note": _vacancy_note(r) or "창 안 공석",
            "hold_tag": _g(r, "hold_tag"),
        })
    vacancies.sort(key=lambda x: x["rank"] or 999)

    n_buy = len(buys)
    eligible = sel.get("eligible", n_buy)
    mode = sel.get("mode") or "absolute"

    if n_buy == 0:
        headline = "매수 0 · 정밀도 우선 · 고장 아님"
        stance = "wait"
    elif slots and eligible is not None and eligible < slots:
        headline = f"매수 {n_buy} · 창 {eligible}/{slots} (일부 공석)"
        stance = "partial"
    else:
        headline = f"매수 {n_buy}" + (f" · 선정 {eligible}/{slots}" if slots else "")
        stance = "active"

    layers = [
        {"id": "L0", "name": "Quant Desk",
         "summary": (f"분위 상위 {slots}자리/{sel.get('universe') or '–'}"
                     if mode == "rank"
                     else f"절대 문턱 {sel.get('buy_threshold')}")},
        {"id": "L3", "name": "Risk Clerk",
         "summary": (f"익스포저 {exposure * 100:.0f}%"
                     if exposure is not None else "익스포저 미산출")},
        {"id": "L4", "name": "Desk Report", "summary": "템플릿 조립 · 점수 불변"},
    ]
    if crowd.get("data_quality"):
        layers.insert(1, {"id": "L0dq", "name": "Data Quality",
                          "summary": crowd.get("note") or "섹터맵 공백"})
    elif crowd.get("warn"):
        layers.insert(1, {"id": "L3c", "name": "Crowding",
                          "summary": crowd.get("note") or "섹터 편중"})

    reasons_out = []
    if mode == "rank" and n_buy == 0:
        reasons_out.append("최소점수·게이트·악재로 매수권 자리가 비었거나 창 밖입니다.")
    if vacancies:
        reasons_out.append(f"창 안 공석 {len(vacancies)}자리 — 아래 사유 참고.")
    if exposure_reasons:
        reasons_out.extend(list(exposure_reasons)[:3])
    if crowd.get("data_quality"):
        reasons_out.append("편중 경고가 아니라 섹터 미분류(맵 공백)입니다.")
    elif crowd.get("warn"):
        reasons_out.append(crowd.get("note") or "매수권 섹터 편중")

    return {
        "ready": True,
        "market": market,
        "stance": stance,
        "headline": headline,
        "mode": mode,
        "selection": {
            "eligible": eligible,
            "rank_slots": slots,
            "universe": sel.get("universe"),
            "cutoff_score": sel.get("cutoff_score"),
            "rank_min_score": sel.get("rank_min_score"),
        },
        "exposure": exposure,
        "buys": [_buy_row(r) for r in sorted(
            buys, key=lambda x: float(_g(x, "score") or 0), reverse=True)[:8]],
        "vacancies": vacancies[:8],
        "crowding": {
            "warn": bool(crowd.get("warn")),
            "data_quality": bool(crowd.get("data_quality")),
            "note": crowd.get("note"),
            "top_sector": crowd.get("top_sector"),
            "top_pct": crowd.get("top_pct"),
        },
        "layers": layers,
        "reasons": reasons_out,
        "disclaimer": "증거 보고일 뿐 매수 권유가 아니다. 점수·주문은 이 카드가 바꾸지 않는다.",
    }


def build_from_results(results: list[SignalResult], **kwargs) -> dict:
    return build(results, **kwargs)
