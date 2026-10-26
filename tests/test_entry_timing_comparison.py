import copy

import pytest

from scripts.measure import entry_timing_comparison as study
from tests.test_earnings_announcement_execution import event, fixture_sources


def test_legacy_ledger_is_unchanged():
    inputs = {"events": [event()]}
    picks = study.ledger.choices(inputs, "immediate", "eps")
    original = study.ledger.simulate(inputs, fixture_sources(), "2023", "immediate", "eps")
    shared = study.ledger.execute_selected(inputs, fixture_sources(), picks, year="2023",
        path="immediate", policy="eps", fee_bp=10, fill=1, delay=0, size_scale=1)
    assert original == shared
    with pytest.raises(ValueError, match="frozen"):
        study.ledger.simulate(inputs, {}, "2025", "immediate", "eps")


def test_same_six_events_and_clocks_are_fixed():
    inputs = study.inputs()
    early = study.selected(inputs, "immediate")
    opening = study.selected(inputs, "opening")
    assert len(early) == len(opening) == 6
    assert {e["id"] for e in early} == {e["id"] for e in opening}
    assert all(e["decision_ns"] == e["ready_ns"] for e in early)
    assert all(e["decision_ns"] >= e["ready_ns"] for e in opening)
    assert all(e["decision_ns"] == study.orb.session_clocks(e["opening_session"])[0] + 5 * study.orb.MINUTE for e in opening)
    delayed = study.selected(inputs, "immediate", 60)
    assert all(b["decision_ns"] - a["decision_ns"] == 60 * study.ledger.SECOND for a, b in zip(early, delayed))


def test_missing_quotes_not_zero_and_reports_are_read_only():
    result = study.assess(study.inputs(), {})
    assert set(result["verdicts"].values()) == {"blocked_sources"}
    report = study.public_report(result)
    assert not report["live_order_eligible"] and report["research_only"]
    assert all(p["conditional_account_return_pct"] is None for p in report["paths"].values())
    assert "source_hashes" not in report


def test_opening_cannot_precede_information():
    inputs = copy.deepcopy(study.inputs())
    eligible = next(e for e in inputs["events"] if e["eligible"])
    eligible["ready_ns"] = study.orb.session_clocks(eligible["opening_session"])[0] + 6 * study.orb.MINUTE
    with pytest.raises(ValueError, match="precedes"):
        study.selected(inputs, "opening")
