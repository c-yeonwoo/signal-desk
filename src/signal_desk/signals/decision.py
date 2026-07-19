"""Decision 정책 — confirmed + decision_eligible 이벤트만 입력.

점수(`combine`)와 분리. critical→전량 청산·매수 차단, serious→절반 축소·매수 차단.
candidate/레거시 digest 플래그는 여기로 들어오지 않는다(호출자가 필터).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal, Sequence

POLICY_VERSION = "p2"
HoldingAction = Literal["none", "trim", "exit"]

_SEV_RANK = {"critical": 0, "serious": 1, "watch": 2, "info": 3}


@dataclass(frozen=True)
class Decision:
    buy_blocked: bool
    holding_action: HoldingAction
    event_id: int | None
    severity: str | None
    summary: str
    policy_version: str = POLICY_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


def empty_decision() -> Decision:
    return Decision(False, "none", None, None, "", POLICY_VERSION)


def is_actionable_event(event: dict, *, now: int | None = None) -> bool:
    """confirmed + decision_eligible + 미만료(+critical/serious)."""
    if (event.get("status") or "") != "confirmed":
        return False
    if not event.get("decision_eligible"):
        return False
    sev = event.get("severity") or ""
    if sev not in ("critical", "serious"):
        return False
    exp = event.get("expires_at")
    if exp is not None and now is not None and int(exp) < int(now):
        return False
    return True


def decide(events: Sequence[dict], *, now: int | None = None) -> Decision:
    """활성 Decision 이벤트 중 최강 severity 1건으로 정책 산출."""
    actionable = [e for e in events if is_actionable_event(e, now=now)]
    if not actionable:
        return empty_decision()
    best = min(actionable, key=lambda e: (_SEV_RANK.get(e.get("severity") or "", 9),
                                          -(e.get("detected_at") or 0)))
    sev = best.get("severity")
    summary = (best.get("summary") or "")[:200]
    eid = best.get("id")
    if sev == "critical":
        return Decision(True, "exit", eid, "critical", summary, POLICY_VERSION)
    # serious
    return Decision(True, "trim", eid, "serious", summary, POLICY_VERSION)


def decision_from_legacy(*, event_risk: bool = False, event_severity: str = "",
                         event_note: str = "", event_id: int | None = None) -> Decision:
    """테스트·구 SignalResult 조립용 — severity 문자열에서 Decision 복원."""
    if event_severity == "critical":
        return Decision(True, "exit", event_id, "critical", event_note or "", POLICY_VERSION)
    if event_severity == "serious":
        return Decision(True, "trim", event_id, "serious", event_note or "", POLICY_VERSION)
    if event_risk:
        # severity 없이 risk만 있으면 매수 차단만(청산 강도는 severity 필요)
        return Decision(True, "none", event_id, None, event_note or "", POLICY_VERSION)
    return empty_decision()


def decision_reason(d: Decision) -> str:
    if d.holding_action == "exit":
        return f"악재 이벤트 전량 청산 — {d.summary}"
    if d.holding_action == "trim":
        return f"악재 이벤트 부분 청산(절반) — {d.summary}"
    if d.buy_blocked:
        return f"악재 이벤트로 신규 매수 차단 — {d.summary}"
    return ""
