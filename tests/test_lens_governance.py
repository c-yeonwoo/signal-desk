"""전진 근거가 부족하거나 운영 검증이 빠지면 실주문 승격은 항상 차단한다."""

from signal_desk.signals import lens_governance


def _report(n=50, *, excess=.02, policy="policy-v1"):
    return {"market": "kr", "cohorts_seen": n,
            "episodes": [{"signal_policy_id": policy,
                          "combos": {"base": {"net_return": .005, "coverage": 1.0},
                                     "event": {"net_return": .005 + excess, "coverage": .6},
                                     "entry": {"net_return": .005, "coverage": .6},
                                     "event_entry": {"net_return": .005, "coverage": .6}}}
                         for _ in range(n)]}


def test_insufficient_future_evidence_blocks_promotion():
    gate = lens_governance.assess(_report(49))
    assert gate["status"] == "awaiting_prospective_evidence"
    assert gate["next_required_episodes"] == 1
    assert gate["live_eligible"] is False and gate["auto_promote"] is False


def test_positive_holdout_is_only_research_candidate():
    gate = lens_governance.assess(_report())
    assert gate["status"] == "research_candidate_operationally_blocked"
    assert [r["combo"] for r in gate["candidates"]] == ["event"]
    assert gate["candidates"][0]["conservative_lower_bound"] > 0
    assert gate["operational_blocks"] and gate["rollback_conditions"]
    assert gate["live_eligible"] is False


def test_no_advantage_policy_drift_and_missing_coverage_block():
    assert lens_governance.assess(_report(excess=0))["status"] == "no_validated_advantage"
    drifting = _report()
    drifting["episodes"][25]["signal_policy_id"] = "policy-v2"
    assert lens_governance.assess(drifting)["status"] == "blocked_policy_drift"
    incomplete = _report()
    incomplete["cohorts_seen"] = 100
    assert lens_governance.assess(incomplete)["status"] == "blocked_sample_coverage"
