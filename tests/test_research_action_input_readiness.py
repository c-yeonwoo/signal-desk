"""Price-input readiness is not a corporate-action or return certificate."""

import json

import pytest

from scripts.measure import research_action_input_readiness as readiness


def _fixture():
    sessions = [f"2024-04-0{day}" for day in range(1, 6)]
    days = {}
    for index, day in enumerate(sessions):
        days[day] = [
            {"ISU_CD": f"{ticker:05d}0", "ISU_NM": str(ticker),
             "MKTCAP": str(1000 - ticker),
             "TDD_CLSPRC": "100" if ticker != 0 or index >= 2 else "0"}
            for ticker in range(200)
        ]
    window = {"window": "test", "decision_start": sessions[2],
              "decision_end": sessions[3], "decision_sessions": 2,
              "candidate_slots": 400, "unique_tickers": 200}
    rows = [{"window": "test", "ticker": f"{ticker:05d}0",
             "name": str(ticker), "candidate_sessions": 2,
             "first_candidate": sessions[2], "last_candidate": sessions[3],
             "required_scope_end": sessions[4], "missing_raw_price_sessions": 0,
             "missing_before_first_price": 0,
             "missing_between_first_and_last_price": 0,
             "missing_after_last_price": 0,
             "listed_share_change_dates": [sessions[3]] if ticker == 1 else []}
            for ticker in range(200)]
    return window, rows, days


def test_ticker_streak_reproduces_pilot_exclusion_and_keeps_all_candidates():
    window, rows, days = _fixture()
    report = readiness.audit_window(window, rows, days, warmup=3)
    assert report["candidate_slots"] == 400
    assert report["excluded_candidate_sessions"] == 2
    assert report["admitted_candidate_sessions"] == 398
    assert report["tickers_with_excluded_sessions"] == 1
    flagged = {row["ticker"]: row for row in report["flagged_ticker_rows"]}
    assert flagged["000000"]["first_candidate_close_streak"] == 1
    assert flagged["000000"]["excluded_candidate_sessions"] == 2
    assert "000010" not in flagged  # The old gap ledger already inventories share changes.
    assert all(row["action_coverage"] == "partial" for row in flagged.values())


def test_changed_candidate_panel_is_not_silently_accepted():
    window, rows, days = _fixture()
    rows[0]["candidate_sessions"] = 1
    with pytest.raises(ValueError, match="candidate dates differ"):
        readiness.audit_window(window, rows, days, warmup=3)


def test_frozen_ledger_hash_rejects_promoted_verdict(tmp_path):
    path = tmp_path / "ledger.jsonl"
    path.write_text(json.dumps({"type": "manifest", "schema": "research-action-gap-ledger-v1",
                                "registered_verdict": True}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="frozen gap ledger SHA differs"):
        readiness.load_ledger(path)
