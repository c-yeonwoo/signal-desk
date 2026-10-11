"""Settlement evidence and execution sensitivity remain research-only."""

import datetime as dt
from dataclasses import replace
from zoneinfo import ZoneInfo

import pytest

from scripts.measure.research_daily_foundation import run_development
from scripts.measure.krx_price_replay_pilot import ROOT
from signal_desk import market_clock
from signal_desk.signals.research_daily_simulator import (
    DailyCostModel, DailyExecutionScenario, simulate_daily,
)
from signal_desk.signals.research_decision import (
    Candidate, CompletedDailyBar, DailyPolicy, DailySnapshot,
)
from signal_desk.signals.research_event_evidence import DailyEventEvidence, SettlementEvent


def _days(count=5):
    return ["2024-04-01", *market_clock.next_sessions("kr", "2024-04-01", count - 1)]


def _snapshot(day, *, opened=100, closed=100, shares=1_000_000, candidate=False):
    as_of = dt.datetime.fromisoformat(f"{day}T16:30:00").replace(tzinfo=ZoneInfo("Asia/Seoul"))
    bar = CompletedDailyBar("kr", day, "005930", opened, closed, as_of,
                            f"price:{day}", listed_shares=shares)
    chosen = (Candidate("005930", 1, 1.0, True, "episode"),) if candidate else ()
    return DailySnapshot("kr", day, as_of, f"decision:{day}", (bar,), chosen)


def _proof(day, *, events=()):
    published = dt.datetime.fromisoformat(f"{day}T16:30:00").replace(
        tzinfo=ZoneInfo("Asia/Seoul"))
    return DailyEventEvidence("kr", "005930", day, "verified_complete", tuple(events),
                              f"complete-event-feed:{day}", published,
                              published + dt.timedelta(minutes=1))


def _path(days, *, split=False):
    return [_snapshot(days[0], candidate=True), _snapshot(days[1]),
            _snapshot(days[2], opened=50 if split else 100,
                      closed=60 if split else 100,
                      shares=2_000_000 if split else 1_000_000),
            _snapshot(days[3], opened=60 if split else 120,
                      closed=60 if split else 120,
                      shares=2_000_000 if split else 1_000_000)]


def _replay(series, **kwargs):
    return simulate_daily(series, policy=DailyPolicy(max_positions=1, hold_sessions=2),
                          costs=DailyCostModel(), starting_cash=10_000, **kwargs)


def test_verified_empty_day_needs_explicit_source_and_two_times():
    day = _days()[1]
    with pytest.raises(ValueError, match="source and publication"):
        DailyEventEvidence("kr", "005930", day, "verified_complete")
    with pytest.raises(ValueError, match="unknown coverage"):
        DailyEventEvidence("kr", "005930", day, "unknown",
                           (SettlementEvent("halt"),))
    proof = _proof(day)
    assert proof.coverage == "verified_complete" and proof.events == ()
    with pytest.raises(ValueError, match="share basis"):
        _proof(day, events=(SettlementEvent("split", split_numerator=2,
                                          split_denominator=1),
                            SettlementEvent("cash_dividend", cash_per_share=2,
                                            payment_date=day)))


def test_strict_settlement_abstains_when_event_feed_is_missing_or_unknown():
    days = _days(4)
    series = _path(days)
    missing = _replay(series, require_event_coverage=True)
    assert missing["state"] == "incomplete_action_coverage"
    assert missing["session"] == days[1] and missing["net_return_pct"] is None
    unknown = _replay(series, require_event_coverage=True,
                      event_evidence={(days[1], "005930"): DailyEventEvidence(
                          "kr", "005930", days[1], "unknown")})
    assert unknown["state"] == "incomplete_action_coverage"


def test_verified_no_event_path_is_retroactive_not_pit_or_real_fill():
    days = _days(4)
    ledger = {(day, "005930"): _proof(day) for day in days[1:]}
    result = _replay(_path(days), require_event_coverage=True, event_evidence=ledger)
    assert result["state"] == "complete"
    assert result["corporate_action_coverage"] == "verified_for_traded_paths_only"
    assert result["return_quality"] == "retrospective_event_covered_assumed_fills"
    assert result["strict_pit_eligible"] is False
    assert result["full_fill_assumptions"] == 2


def test_verified_split_changes_owned_quantity_before_exit_and_checks_issuer_count():
    days = _days(4)
    ledger = {(day, "005930"): _proof(day) for day in days[1:]}
    ledger[(days[2], "005930")] = _proof(days[2], events=(
        SettlementEvent("split", split_numerator=2, split_denominator=1),))
    result = _replay(_path(days, split=True), require_event_coverage=True,
                     event_evidence=ledger)
    assert result["state"] == "complete"
    assert [(fill["side"], fill["shares"]) for fill in result["fills"]] == [
        ("buy", 99), ("sell", 198)]
    marked = _path(days, split=True)
    marked[2] = replace(marked[2], bars=(replace(marked[2].bars[0],
                                                corporate_action="split"),))
    assert _replay(marked, require_event_coverage=True,
                   event_evidence=ledger)["state"] == "complete"
    malformed = dict(ledger)
    malformed[(days[2], "005930")] = _proof(days[2])
    rejected = _replay(_path(days, split=True), require_event_coverage=True,
                       event_evidence=malformed)
    assert rejected["state"] == "incomplete_corporate_action"
    assert rejected["net_return_pct"] is None


def test_verified_dividend_is_receivable_until_payment_not_entry_cash():
    days = _days(4)
    ledger = {(day, "005930"): _proof(day) for day in days[1:]}
    ledger[(days[2], "005930")] = _proof(days[2], events=(
        SettlementEvent("cash_dividend", cash_per_share=2,
                        payment_date=days[3]),))
    result = _replay(_path(days), require_event_coverage=True, event_evidence=ledger)
    assert result["state"] == "complete"
    assert result["dividend_cash_paid"] == 198
    assert result["cash_end"] == 12150.775
    unpaid = dict(ledger)
    unpaid[(days[2], "005930")] = _proof(days[2], events=(
        SettlementEvent("cash_dividend", cash_per_share=2,
                        payment_date="2024-05-01"),))
    result = _replay(_path(days), require_event_coverage=True, event_evidence=unpaid)
    assert result["state"] == "unmatured" and result["net_return_pct"] is None
    assert result["unpaid_dividend_receivables"] == 1


def test_unresolved_halt_or_delisting_never_receives_a_fictional_recovery():
    days = _days(4)
    for kind in ("halt", "delisting"):
        ledger = {(day, "005930"): _proof(day) for day in days[1:]}
        ledger[(days[2], "005930")] = _proof(days[2], events=(SettlementEvent(kind),))
        result = _replay(_path(days), require_event_coverage=True, event_evidence=ledger)
        assert result["state"] == "incomplete_untradable_event"
        assert result["net_return_pct"] is None


def test_execution_stress_reduces_claimed_price_only_return_or_leaves_it_unmatured():
    days = _days(4)
    series = _path(days)
    baseline = _replay(series)
    adverse = _replay(series, execution=DailyExecutionScenario("slip-25", 25, 25))
    assert adverse["state"] == "complete"
    assert adverse["net_return_pct"] < baseline["net_return_pct"]
    assert adverse["fills"][0]["price"] > adverse["fills"][0]["observed_open"]
    partial = _replay(series, execution=DailyExecutionScenario("half", 0, 0, 0.5))
    assert partial["state"] == "unmatured" and partial["net_return_pct"] is None
    assert partial["fills"][0]["partial"] is True
    no_fill = _replay(series, execution=DailyExecutionScenario("none", 0, 0, 0))
    assert no_fill["state"] == "complete" and no_fill["net_return_pct"] == 0
    assert no_fill["fills"] == []
    assert no_fill["return_quality"] == "cash_only_no_fills"
    no_fill_strict = _replay(series, execution=DailyExecutionScenario("none", 0, 0, 0),
                             require_event_coverage=True)
    assert no_fill_strict["state"] == "complete" and no_fill_strict["fills"] == []


def test_frozen_krx_price_archive_cannot_claim_verified_total_return():
    # data/research is intentionally gitignored; CI verifies the same guard
    # with synthetic rows above, while a local archive enables this extra audit.
    if not (ROOT / "data/research/krx-price-only-development-2026q2early-v5.json").exists():
        pytest.skip("optional local KRX archive is not part of the repository")
    report = run_development(ROOT / "data/research", window="2026q2early-v5", horizon=5,
                             sensitivity=True)
    assert report["legacy_replay_parity"] is True
    assert report["verified_total_return_state"] == "incomplete_action_coverage"
    assert report["verified_total_return_pct"] is None
    assert report["execution_sensitivity"][0]["return_quality"] == "price_only_unverified_actions"
