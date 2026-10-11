"""Prioritize original-source checks without fabricating entitlements or returns."""

import copy

import pytest

from scripts.measure import research_action_holding_change_priority as priority


def _inputs():
    rows = [{"window": "pilot", "ticker": "035720", "name": "카카오",
             "listed_share_change_dates": ["2025-11-14"]}]
    episode = {"ticker": "035720", "first_buy_date": "2025-11-11",
               "left_censored": False, "h20_share_status": "listed_shares_changed",
               "h20": {"state": "matured", "entry_date": "2025-11-12",
                       "exit_date": "2025-11-17"}}
    pilots = {"pilot": {"registered_verdict": False,
                        "source_level": "C_offline_price_only_current_engine",
                        "signal_dates": ["2025-11-11"],
                        "buy_episodes": {"count": 2, "episodes": [episode, {
                            **episode, "first_buy_date": "2025-11-13", "left_censored": True}]}}}
    days = {"2025-11-13": [{"ISU_CD": "035720", "LIST_SHRS": "442318138"}],
            "2025-11-14": [{"ISU_CD": "035720", "LIST_SHRS": "442384158"}]}
    return rows, pilots, days


def test_repeated_episodes_become_one_source_event_not_two_actions():
    rows, pilots, days = _inputs()
    result = priority.analyze(rows, pilots, days)
    assert result["price_only_episodes"] == 2
    assert result["overlap_episode_refs"] == 2
    assert result["left_censored_overlap_refs"] == 1
    assert result["distinct_issuer_sessions"] == 1
    assert result["events"][0]["listed_shares_delta"] == 66020
    assert result["events"][0]["event_type"] == "unverified"
    assert result["events"][0]["cash_or_share_entitlement"] == "unverified"
    assert result["verified_total_return"] is False


def test_pilot_and_frozen_krx_share_change_must_agree():
    rows, pilots, days = _inputs()
    changed = copy.deepcopy(pilots)
    changed["pilot"]["buy_episodes"]["episodes"][0]["h20_share_status"] = (
        "listed_shares_unchanged_actions_unverified")
    with pytest.raises(ValueError, match="share flags differ"):
        priority.analyze(rows, changed, days)


def test_missing_previous_krx_session_fails_closed():
    rows, pilots, days = _inputs()
    del days["2025-11-13"]
    with pytest.raises(ValueError, match="no previous KRX session"):
        priority.analyze(rows, pilots, days)
