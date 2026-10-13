"""A selected exchange must not relabel seven partial windows as returns."""

import pytest

from scripts.measure import research_share_exchange_window_readiness as readiness


def test_fixed_seven_window_denominator_remains_partial():
    result = readiness.build()
    assert result["window_count"] == 7
    assert result["candidate_ticker_rows"] == 1488
    assert result["certified_total_return_pct"] is None
    assert sum(row["candidate_ticker_rows"] for row in result["windows"]) == 1488
    assert all(row["rows_with_partial_action_coverage"] == row["candidate_ticker_rows"]
               and row["certified_total_return_pct"] is None for row in result["windows"])
    possible = [(row["window"], case["old_ticker"])
                for row in result["windows"] for case in row["selected_possible_exchange_cases"]]
    assert possible == [("2025q4-v5", "010620"), ("2025q4-v5", "042670"),
                        ("2026q1-v5", "042670")]
    assert result["live_order_eligible"] is False


def test_changed_frozen_ledger_is_rejected(tmp_path, monkeypatch):
    changed = tmp_path / "ledger.jsonl"
    changed.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(readiness, "LEDGER", changed)
    with pytest.raises(ValueError, match="frozen seven-window"):
        readiness.build()
