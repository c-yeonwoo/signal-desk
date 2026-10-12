"""The frozen denominator survives selected event review unchanged."""

import json
from pathlib import Path

import pytest

from scripts.measure import research_action_scope_audit_v2 as scope


def test_seven_window_report_is_replayed_from_frozen_inputs():
    report = scope.audit()
    saved = json.loads((Path(__file__).resolve().parents[1]
                        / "docs/research-action-scope-audit-v2-2026-10-12.json").read_text())
    assert report == saved
    assert len(report["windows"]) == 7
    assert report["ticker_window_denominator"] == 1488
    assert report["coverage_partial_rows"] == 1488
    assert report["coverage_complete_rows"] == 0
    assert report["selected_issuer_events"] == 7
    assert report["selected_event_episode_refs"] == 13
    assert report["verified_economic_return_pct"] is None
    assert report["policy_comparison_ready"] is False
    assert sum(row["ticker_window_rows"] for row in report["windows"]) == 1488
    assert all(row["action_coverage_complete"] == 0 for row in report["windows"])


def test_source_mutation_cannot_silently_drop_a_losing_row(tmp_path):
    source = scope.LEDGER.read_bytes().splitlines()
    damaged = tmp_path / "ledger.jsonl"
    damaged.write_bytes(b"\n".join(source[:-1]) + b"\n")
    with pytest.raises(ValueError, match="frozen source digest"):
        scope.audit(ledger_path=damaged)
