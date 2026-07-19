"""P2 Decision 정책 — confirmed+eligible만, 점수와 독립."""

from signal_desk.signals.decision import (
    POLICY_VERSION, decide, decision_from_legacy, empty_decision, is_actionable_event,
)


def _ev(**kw):
    base = {
        "id": 1, "status": "confirmed", "decision_eligible": True,
        "severity": "serious", "summary": "유상증자", "detected_at": 100,
        "expires_at": None,
    }
    base.update(kw)
    return base


def test_decide_empty():
    d = decide([])
    assert d == empty_decision()
    assert d.buy_blocked is False and d.holding_action == "none"


def test_ignores_candidate_and_non_eligible():
    assert decide([_ev(status="candidate")]) == empty_decision()
    assert decide([_ev(decision_eligible=False)]) == empty_decision()
    assert decide([_ev(severity="info")]) == empty_decision()
    assert not is_actionable_event(_ev(status="candidate"))


def test_critical_beats_serious():
    d = decide([
        _ev(id=1, severity="serious", summary="증자", detected_at=200),
        _ev(id=2, severity="critical", summary="감자", detected_at=100),
    ])
    assert d.buy_blocked and d.holding_action == "exit"
    assert d.event_id == 2 and d.severity == "critical"
    assert d.policy_version == POLICY_VERSION


def test_serious_trim():
    d = decide([_ev(severity="serious")])
    assert d.buy_blocked and d.holding_action == "trim"


def test_expired_ignored():
    d = decide([_ev(expires_at=10)], now=20)
    assert d == empty_decision()


def test_legacy_helper():
    d = decision_from_legacy(event_risk=True, event_severity="critical", event_note="x")
    assert d.holding_action == "exit" and d.summary == "x"
