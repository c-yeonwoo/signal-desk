"""Research-only, clock-explicit daily decisions shared by replay and shadow.

The legacy rank is an input, not a calibrated probability or an order. No store,
broker, network, or wall clock is read here. The production engine is untouched.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Literal, Sequence

from signal_desk import market_clock

Action = Literal["ENTER", "WAIT", "HOLD", "EXIT"]


def _aware(value: dt.datetime) -> bool:
    return isinstance(value, dt.datetime) and value.tzinfo is not None and value.utcoffset() is not None


def _positive(value: float | None) -> bool:
    return value is not None and not isinstance(value, bool) and math.isfinite(value) and value > 0


@dataclass(frozen=True)
class CompletedDailyBar:
    market: Literal["kr", "us"]
    session: str
    ticker: str
    open: float | None
    close: float | None
    available_at: dt.datetime
    source_id: str
    availability_kind: Literal["observed", "simulated"] = "simulated"
    status: Literal["trading", "halted", "delisted", "unknown"] = "trading"
    listed_shares: int | None = None
    # A source-verified action is still an explicit stop in the first daily kernel.
    corporate_action: str | None = None
    open_fill_cap_shares: int | None = None

    def __post_init__(self) -> None:
        if (self.market not in ("kr", "us") or not market_clock.is_session(self.market, self.session)
                or not self.ticker or not self.source_id or not _aware(self.available_at)
                or self.availability_kind not in ("observed", "simulated")
                or self.status not in ("trading", "halted", "delisted", "unknown")
                or (self.open is not None and not _positive(self.open))
                or (self.close is not None and not _positive(self.close))):
            raise ValueError("invalid completed daily bar")
        if market_clock.latest_completed_session(self.market, self.available_at) != self.session:
            raise ValueError("daily bar was not yet completed at its availability time")
        if self.listed_shares is not None and (
                not isinstance(self.listed_shares, int) or isinstance(self.listed_shares, bool)
                or self.listed_shares <= 0):
            raise ValueError("invalid listed shares")
        if self.open_fill_cap_shares is not None and (
                isinstance(self.open_fill_cap_shares, bool) or self.open_fill_cap_shares < 0):
            raise ValueError("invalid opening fill capacity")


@dataclass(frozen=True)
class ProvisionalDailyPrice:
    """Not accepted by a completed-bar snapshot or the daily simulator."""

    market: Literal["kr", "us"]
    session: str
    ticker: str
    last: float
    observed_at: dt.datetime

    def __post_init__(self) -> None:
        if (self.market not in ("kr", "us") or not market_clock.is_session(self.market, self.session)
                or not self.ticker or not _positive(self.last) or not _aware(self.observed_at)):
            raise ValueError("invalid provisional daily price")


@dataclass(frozen=True)
class QuoteObservation:
    """Quote prices have their own clock and cannot be cast to a daily close."""

    market: Literal["kr", "us"]
    ticker: str
    bid: float | None
    ask: float | None
    observed_at: dt.datetime

    def __post_init__(self) -> None:
        if (self.market not in ("kr", "us") or not self.ticker or not _aware(self.observed_at)
                or (self.bid is not None and not _positive(self.bid))
                or (self.ask is not None and not _positive(self.ask))
                or (self.bid is not None and self.ask is not None and self.bid > self.ask)):
            raise ValueError("invalid quote observation")


@dataclass(frozen=True)
class Candidate:
    ticker: str
    rank: int
    score: float
    eligible: bool
    thesis_id: str
    reason: str = ""

    def __post_init__(self) -> None:
        if (not self.ticker or not self.thesis_id or not isinstance(self.rank, int)
                or isinstance(self.rank, bool) or self.rank < 1
                or not isinstance(self.score, (int, float)) or isinstance(self.score, bool)
                or not math.isfinite(self.score)):
            raise ValueError("invalid research candidate")


@dataclass(frozen=True)
class DailySnapshot:
    market: Literal["kr", "us"]
    session: str
    as_of: dt.datetime
    input_id: str
    bars: tuple[CompletedDailyBar, ...]
    candidates: tuple[Candidate, ...] = ()
    invalidated_theses: tuple[str, ...] = ()
    source_level: str = "C_reconstructed"

    def __post_init__(self) -> None:
        if (self.market not in ("kr", "us") or not market_clock.is_session(self.market, self.session)
                or not _aware(self.as_of) or not self.input_id
                or market_clock.latest_completed_session(self.market, self.as_of) != self.session):
            raise ValueError("snapshot requires an explicit completed market session")
        if any(not isinstance(bar, CompletedDailyBar) or bar.market != self.market
               or bar.session != self.session or bar.available_at > self.as_of for bar in self.bars):
            raise ValueError("snapshot contains a non-completed, future, or foreign bar")
        if (any(not isinstance(item, Candidate) for item in self.candidates)
                or len({bar.ticker for bar in self.bars}) != len(self.bars)
                or len({item.ticker for item in self.candidates}) != len(self.candidates)
                or len({item.rank for item in self.candidates}) != len(self.candidates)):
            raise ValueError("duplicate or invalid research rows")


@dataclass(frozen=True)
class Position:
    ticker: str
    shares: int
    entry_price: float
    entry_index: int
    thesis_id: str
    listed_shares: int | None = None
    exit_requested: bool = False

    def __post_init__(self) -> None:
        if (not self.ticker or not isinstance(self.shares, int) or isinstance(self.shares, bool)
                or self.shares < 1 or not _positive(self.entry_price)
                or not isinstance(self.entry_index, int) or self.entry_index < 0
                or not self.thesis_id
                or (self.listed_shares is not None and (
                    not isinstance(self.listed_shares, int) or self.listed_shares <= 0))):
            raise ValueError("invalid research position")


@dataclass(frozen=True)
class PortfolioState:
    market: Literal["kr", "us"]
    cash: float
    positions: tuple[Position, ...] = ()
    used_theses: frozenset[str] = frozenset()


@dataclass(frozen=True)
class DailyPolicy:
    policy_id: str = "research-daily-v1"
    cost_model_id: str = "research-open-cost-v1"
    max_positions: int = 6
    hold_sessions: int = 5
    risk_budget_fraction: float = 1.0

    def __post_init__(self) -> None:
        if (not self.policy_id or not self.cost_model_id
                or not isinstance(self.max_positions, int) or isinstance(self.max_positions, bool)
                or self.max_positions < 1
                or not isinstance(self.hold_sessions, int) or isinstance(self.hold_sessions, bool)
                or self.hold_sessions < 1
                or not isinstance(self.risk_budget_fraction, (int, float))
                or not math.isfinite(self.risk_budget_fraction)
                or not 0 <= self.risk_budget_fraction <= 1):
            raise ValueError("invalid daily research policy")


@dataclass(frozen=True)
class ResearchClock:
    now: dt.datetime
    session_index: int
    next_session: str


@dataclass(frozen=True)
class ResearchDecision:
    policy_id: str
    input_id: str
    market: str
    session: str
    ticker: str
    action: Action
    valid_until: str
    horizon_sessions: int
    candidate_rank: int | None
    thesis_id: str | None
    reason: str
    cost_model_id: str
    risk_budget_fraction: float
    p_profit: None = None


def decide(snapshot: DailySnapshot, portfolio: PortfolioState,
           policy: DailyPolicy, clock: ResearchClock) -> tuple[ResearchDecision, ...]:
    """At the confirmed close, emit actions valid only at the next session open."""
    if (snapshot.market != portfolio.market or not _aware(clock.now)
            or clock.now != snapshot.as_of or clock.session_index < 0
            or not market_clock.consecutive_sessions(snapshot.market, snapshot.session, clock.next_session)):
        raise ValueError("decision clock or market mismatch")
    held = {position.ticker: position for position in portfolio.positions}
    if (len(held) != len(portfolio.positions) or len(held) > policy.max_positions
            or any(position.shares < 1 or position.entry_index > clock.session_index
                   or not _positive(position.entry_price) or not position.thesis_id
                   for position in portfolio.positions)):
        raise ValueError("invalid portfolio positions")
    if not math.isfinite(portfolio.cash) or portfolio.cash < 0:
        raise ValueError("invalid portfolio cash")

    def result(ticker: str, action: Action, rank: int | None,
               thesis_id: str | None, reason: str) -> ResearchDecision:
        return ResearchDecision(policy.policy_id, snapshot.input_id, snapshot.market,
                                snapshot.session, ticker, action, clock.next_session,
                                policy.hold_sessions, rank, thesis_id, reason,
                                policy.cost_model_id, policy.risk_budget_fraction)

    actions = []
    completed_bars = {bar.ticker: bar for bar in snapshot.bars}
    continuing = 0
    exiting: set[str] = set()
    for position in sorted(portfolio.positions, key=lambda item: item.ticker):
        age = clock.session_index - position.entry_index + 1
        if position.exit_requested:
            action, reason = "EXIT", "exit_partially_filled"
        elif position.thesis_id in snapshot.invalidated_theses:
            action, reason = "EXIT", "thesis_invalidated"
        elif age >= policy.hold_sessions:
            action, reason = "EXIT", "holding_horizon_expired"
        else:
            action, reason = "HOLD", "holding_horizon_active"
            continuing += 1
        if action == "EXIT":
            exiting.add(position.ticker)
        actions.append(result(position.ticker, action, None, position.thesis_id, reason))

    free_slots = max(0, policy.max_positions - continuing)
    for item in sorted(snapshot.candidates, key=lambda value: value.rank):
        if item.ticker in held and item.ticker not in exiting:
            continue
        if item.rank > policy.max_positions:
            action, reason = "WAIT", "outside_original_rank_window"
        elif not item.eligible:
            action, reason = "WAIT", "legacy_gate_or_score_blocked"
        elif (item.ticker not in completed_bars
              or completed_bars[item.ticker].close is None
              or completed_bars[item.ticker].status != "trading"):
            action, reason = "WAIT", "completed_candidate_bar_unavailable"
        elif policy.risk_budget_fraction == 0:
            action, reason = "WAIT", "cash_only_risk_budget"
        elif item.ticker in held and item.thesis_id == held[item.ticker].thesis_id:
            action, reason = "WAIT", "same_holding_thesis"
        elif item.thesis_id in portfolio.used_theses:
            action, reason = "WAIT", "thesis_already_used"
        elif free_slots <= 0:
            action, reason = "WAIT", "portfolio_slots_full"
        else:
            action, reason = "ENTER", "legacy_candidate_requires_execution"
            free_slots -= 1
        actions.append(result(item.ticker, action, item.rank, item.thesis_id, reason))
    return tuple(actions)


def legacy_candidates(results: Sequence[dict], *, episode_ids: dict[str, str]) -> tuple[Candidate, ...]:
    """Freeze legacy output labels; do not rescore or refill a vacated rank."""
    candidates = []
    for row in results:
        ticker = row.get("ticker")
        rank = row.get("rank")
        score = row.get("score")
        if not isinstance(rank, int) or not ticker or not isinstance(score, (int, float)):
            raise ValueError("legacy result lacks a frozen rank or score")
        eligible = (row.get("kind") in {"BUY", "STRONG_BUY"}
                    and row.get("rank_eligible") is True
                    and not row.get("gate_blocked") and not row.get("event_risk")
                    and not row.get("low_coverage") and not row.get("decision_blocked"))
        if eligible and not episode_ids.get(ticker):
            raise ValueError("eligible legacy candidate needs an explicit entry episode")
        candidates.append(Candidate(
            ticker=ticker, rank=rank, score=float(score),
            eligible=eligible,
            thesis_id=episode_ids.get(ticker, f"legacy-unidentified:{ticker}"),
            reason="legacy_rank_eligible" if row.get("rank_eligible") else "legacy_not_eligible",
        ))
    return tuple(candidates)
