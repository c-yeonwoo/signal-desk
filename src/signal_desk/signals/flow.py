"""수급 팩터 — 외국인·기관 순매수 강도를 시그널 컴포넌트로. 한국 시장의 핵심 알파.

intensity = (외국인+기관 순매수) / 전체 매수대금 ∈ [-1,1] (store.fetch_flows에서 자기정규화).
데이터 없거나 미미하면 가중치 0으로 제외(그레이스풀). US는 이 소스가 없어 자동 제외된다.
"""

from __future__ import annotations

_MIN_INTENSITY = 0.02  # 이보다 약한 수급은 노이즈 → 중립
_SCALE = 2.5           # 강도(비율)를 [-1,1] 스코어로 확대(0.4면 이미 강한 수급)


def component(flow: dict | None, weight: float) -> tuple[float, float, list[str], float, bool]:
    """flow={intensity,foreign_net,inst_net}|None → (norm[-1,1], weight, reasons, intensity, has_flow)."""
    if not flow or flow.get("intensity") is None:
        return 0.0, 0.0, [], 0.0, False
    inten = float(flow["intensity"])
    if abs(inten) < _MIN_INTENSITY:
        return 0.0, 0.0, [], inten, False
    fn, insn = flow.get("foreign_net", 0) or 0, flow.get("inst_net", 0) or 0
    if inten > 0:
        who = [w for w, v in (("외국인", fn), ("기관", insn)) if v > 0]
        tag = "순매수"
    else:
        who = [w for w, v in (("외국인", fn), ("기관", insn)) if v < 0]
        tag = "순매도"
    reason = f"[수급] {'·'.join(who) or '외국인·기관'} {tag}(강도 {inten:+.2f})"
    norm = max(-1.0, min(1.0, inten * _SCALE))
    return norm, weight, [reason], inten, True
