"""Daily research decisions and next-open cash accounting, without provider I/O."""

import datetime as dt
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pytest

from signal_desk import market_clock
from signal_desk.signals.research_daily_simulator import DailyCostModel, simulate_daily
from signal_desk.signals.research_decision import (
    Candidate, CompletedDailyBar, DailyPolicy, DailySnapshot, PortfolioState,
    Position, ProvisionalDailyPrice, ResearchClock, decide, legacy_candidates,
)
from signal_desk.signals.historical_replay import replay_price_only_asof


def _sessions(count=5):
    return ["2024-04-01", *market_clock.next_sessions("kr", "2024-04-01", count - 1)]


def _snapshot(day, *, opened=100, closed=100, candidate=None, status="trading",
              shares=1_000_000, cap=None, action=None, bars=None):
    as_of = dt.datetime.fromisoformat(f"{day}T16:30:00").replace(tzinfo=ZoneInfo("Asia/Seoul"))
    current = CompletedDailyBar("kr", day, "005930", opened, closed, as_of,
                                f"official-krx:{day}", status=status, listed_shares=shares,
                                corporate_action=action, open_fill_cap_shares=cap)
    return DailySnapshot("kr", day, as_of, f"frozen-input:{day}", tuple(bars or [current]),
                         (candidate,) if candidate else ())


def test_unfilled_order_does_not_require_an_unheld_corporate_action():
    from signal_desk.signals.research_daily_simulator import DailyExecutionScenario
    days = _sessions(3)
    snapshots = [_snapshot(days[0], candidate=Candidate("005930", 1, 2, True, "event")),
                 _snapshot(days[1], shares=900_000, action="unsupported"),
                 _snapshot(days[2], shares=900_000)]
    result = simulate_daily(snapshots, policy=DailyPolicy(), costs=DailyCostModel(), starting_cash=10000,
                            execution=DailyExecutionScenario("none", assumed_fill_fraction=0))
    assert result["state"] == "complete"
    assert result["fills"] == [] and result["cash_end"] == 10000


def test_closed_bar_contract_rejects_future_and_provisional_prices():
    first = _sessions()[0]
    snapshot = _snapshot(first)
    provisional = ProvisionalDailyPrice("kr", first, "005930", 100, snapshot.as_of)
    with pytest.raises(ValueError, match="non-completed"):
        DailySnapshot("kr", first, snapshot.as_of, "input", (provisional,))
    next_day = _sessions()[1]
    with pytest.raises(ValueError, match="not yet completed"):
        CompletedDailyBar("kr", next_day, "005930", 100, 100, snapshot.as_of, "bad")
    with pytest.raises(ValueError, match="non-completed"):
        DailySnapshot("kr", first, snapshot.as_of, "input", (_snapshot(next_day).bars[0],))


def test_position_exit_has_priority_and_rank_gap_is_not_refilled():
    days = _sessions()
    first = _snapshot(days[2])
    gated = Candidate("AAA", 1, 2.0, False, "old:A")
    eligible = Candidate("BBB", 2, 1.8, True, "new:B")
    outside = Candidate("CCC", 3, 1.7, True, "new:C")
    bars = tuple(CompletedDailyBar("kr", days[2], ticker, 100, 100,
                                   first.as_of, f"official-krx:{days[2]}")
                 for ticker in ("AAA", "BBB", "CCC"))
    snapshot = DailySnapshot("kr", days[2], first.as_of, first.input_id,
                             bars, (gated, eligible, outside))
    policy = DailyPolicy(max_positions=2, hold_sessions=2)
    state = PortfolioState("kr", 1000, (Position("ZZZ", 1, 100, 1, "old:Z"),))
    result = decide(snapshot, state, policy, ResearchClock(first.as_of, 2, days[3]))
    assert [(row.ticker, row.action, row.reason) for row in result] == [
        ("ZZZ", "EXIT", "holding_horizon_expired"),
        ("AAA", "WAIT", "legacy_gate_or_score_blocked"),
        ("BBB", "ENTER", "legacy_candidate_requires_execution"),
        ("CCC", "WAIT", "outside_original_rank_window"),
    ]
    assert all(row.p_profit is None and row.valid_until == days[3] for row in result)


def test_missing_completed_candidate_bar_abstains_even_with_buy_label():
    days = _sessions()
    snapshot = _snapshot(days[0], candidate=Candidate("OTHER", 1, 3, True, "event"))
    rows = decide(snapshot, PortfolioState("kr", 1000), DailyPolicy(max_positions=1),
                  ResearchClock(snapshot.as_of, 0, days[1]))
    assert [(row.action, row.reason) for row in rows] == [
        ("WAIT", "completed_candidate_bar_unavailable")]


def test_zero_risk_budget_explicitly_stays_in_cash():
    days = _sessions()
    snapshot = _snapshot(days[0], candidate=Candidate("005930", 1, 2, True, "event"))
    decision = decide(snapshot, PortfolioState("kr", 1000),
                      DailyPolicy(max_positions=1, risk_budget_fraction=0),
                      ResearchClock(snapshot.as_of, 0, days[1]))
    assert decision[0].action == "WAIT" and decision[0].reason == "cash_only_risk_budget"


def test_holding_exit_is_independent_of_continuing_strong_buy():
    days = _sessions()
    snapshot = _snapshot(days[2], candidate=Candidate("005930", 1, 3.0, True, "old"))
    state = PortfolioState("kr", 1000, (Position("005930", 1, 100, 1, "old"),))
    result = decide(snapshot, state, DailyPolicy(hold_sessions=2),
                    ResearchClock(snapshot.as_of, 2, days[3]))
    assert [(item.action, item.reason) for item in result] == [
        ("EXIT", "holding_horizon_expired"), ("WAIT", "same_holding_thesis")]


def test_fresh_buy_episode_can_follow_scheduled_exit_at_next_open():
    days = _sessions()
    snapshot = _snapshot(days[2], candidate=Candidate("005930", 1, 3.0, True, "new"))
    state = PortfolioState("kr", 1000, (Position("005930", 1, 100, 1, "old"),))
    result = decide(snapshot, state, DailyPolicy(max_positions=1, hold_sessions=2),
                    ResearchClock(snapshot.as_of, 2, days[3]))
    assert [(item.action, item.thesis_id) for item in result] == [
        ("EXIT", "old"), ("ENTER", "new")]


def test_legacy_adapter_keeps_frozen_gate_and_rank():
    rows = [
        {"ticker": "AAA", "rank": 1, "score": 2.1, "kind": "HOLD", "rank_eligible": False,
         "gate_blocked": True},
        {"ticker": "BBB", "rank": 2, "score": 1.8, "kind": "STRONG_BUY", "rank_eligible": True},
    ]
    candidates = legacy_candidates(rows, episode_ids={"AAA": "ep:A", "BBB": "ep:B"})
    assert [(item.ticker, item.rank, item.eligible, item.thesis_id) for item in candidates] == [
        ("AAA", 1, False, "ep:A"), ("BBB", 2, True, "ep:B"),
    ]


def test_legacy_adapter_preserves_actual_engine_rank_and_buy_eligibility():
    calendar = xcals.get_calendar("XKRX")
    sessions = [day.date().isoformat() for day in
                calendar.sessions_in_range("2024-01-02", "2025-03-31")][:260]
    tickers = ("AAA", "BBB", "CCC")
    replay = replay_price_only_asof(
        market="kr", as_of=sessions[-1],
        universe=[{"ticker": ticker, "name": ticker} for ticker in tickers],
        closes_by={ticker: [100 + index * scale for index in range(260)]
                   for ticker, scale in (("AAA", 0.2), ("BBB", 0.1), ("CCC", -0.1))},
        dates_by={ticker: sessions for ticker in tickers},
    )
    candidates = legacy_candidates(replay["rows"], episode_ids={ticker: ticker for ticker in tickers})
    assert [(item.ticker, item.rank, item.score, item.eligible) for item in candidates] == [
        (row["ticker"], row["rank"], row["score"],
         row["kind"] in ("BUY", "STRONG_BUY") and row["rank_eligible"]
         and not row["gate_blocked"] and not row["event_risk"]
         and not row["low_coverage"] and not row.get("decision_blocked"))
        for row in replay["rows"]
    ]


def test_next_open_execution_cash_horizon_and_no_same_close_fill():
    days = _sessions()
    candidate = Candidate("005930", 1, 2.0, True, "episode-1")
    series = [_snapshot(days[0], candidate=candidate),
              _snapshot(days[1], opened=100, closed=110),
              _snapshot(days[2], opened=120, closed=120),
              _snapshot(days[3], opened=120, closed=120)]
    result = simulate_daily(series, policy=DailyPolicy(max_positions=1, hold_sessions=2),
                            costs=DailyCostModel(), starting_cash=10_000)
    assert result["state"] == "complete" and result["registered_verdict"] is False
    assert [(fill["side"], fill["session"], fill["price"]) for fill in result["fills"]] == [
        ("buy", days[1], 100), ("sell", days[3], 120)]
    assert result["cash_end"] == 11952.775
    assert result["full_fill_assumptions"] == 2
    assert all(item.action != "ENTER" or item.session == days[0] for item in result["decisions"])


def test_future_close_cannot_change_prior_entry_quantity():
    days = _sessions(4)
    candidate = Candidate("005930", 1, 2.0, True, "episode-1")
    common = [_snapshot(days[0], candidate=candidate)]
    first = simulate_daily([*common, _snapshot(days[1], opened=100, closed=110),
                            _snapshot(days[2], opened=120, closed=120),
                            _snapshot(days[3], opened=120, closed=120)],
                           policy=DailyPolicy(max_positions=1, hold_sessions=2),
                           costs=DailyCostModel(), starting_cash=10_000)
    changed = simulate_daily([*common, _snapshot(days[1], opened=100, closed=500),
                              _snapshot(days[2], opened=120, closed=120),
                              _snapshot(days[3], opened=120, closed=120)],
                             policy=DailyPolicy(max_positions=1, hold_sessions=2),
                             costs=DailyCostModel(), starting_cash=10_000)
    assert first["fills"] == changed["fills"]


@pytest.mark.parametrize(("change", "expected"), [
    ({"opened": None}, "incomplete_exit_open"),
    ({"status": "halted"}, "incomplete_exit_open"),
    ({"shares": 2_000_000}, "incomplete_corporate_action"),
    ({"action": "unverified_dividend"}, "incomplete_corporate_action"),
])
def test_unknown_exit_path_has_no_claimed_return(change, expected):
    days = _sessions(4)
    candidate = Candidate("005930", 1, 2.0, True, "episode-1")
    series = [_snapshot(days[0], candidate=candidate), _snapshot(days[1]),
              _snapshot(days[2]), _snapshot(days[3], **change)]
    result = simulate_daily(series, policy=DailyPolicy(max_positions=1, hold_sessions=2),
                            costs=DailyCostModel(), starting_cash=10_000)
    assert result["state"] == expected and result["net_return_pct"] is None


def test_partial_exit_retries_without_inventing_fill():
    days = _sessions(5)
    candidate = Candidate("005930", 1, 2.0, True, "episode-1")
    series = [_snapshot(days[0], candidate=candidate), _snapshot(days[1]),
              _snapshot(days[2]), _snapshot(days[3], cap=30), _snapshot(days[4])]
    result = simulate_daily(series, policy=DailyPolicy(max_positions=1, hold_sessions=2),
                            costs=DailyCostModel(), starting_cash=10_000)
    assert result["state"] == "complete"
    assert [(fill["side"], fill["shares"]) for fill in result["fills"]] == [
        ("buy", 99), ("sell", 30), ("sell", 69)]
    assert any(row.reason == "exit_partially_filled" for row in result["decisions"])


def test_protected_or_gapped_calendar_cannot_be_called_complete():
    days = _sessions(4)
    snapshots = [_snapshot(days[0]), _snapshot(days[2])]
    result = simulate_daily(snapshots, policy=DailyPolicy(), costs=DailyCostModel())
    assert result["state"] == "incomplete_missing_session" and result["net_return_pct"] is None
    with pytest.raises(ValueError, match="registered period"):
        simulate_daily([_snapshot("2026-08-05")], policy=DailyPolicy(), costs=DailyCostModel())
