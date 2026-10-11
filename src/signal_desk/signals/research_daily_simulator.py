"""Research-only next-open cash replay of the shared daily decision contract.

Daily OHLC cannot prove an auction fill. The model reports its full-fill and
capacity assumptions; unknown actions, halted holdings and missing prices stop
return calculation instead of being filled or priced from a later close.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Sequence

from signal_desk import market_clock
from signal_desk.signals.research_decision import (
    CompletedDailyBar, DailyPolicy, DailySnapshot, PortfolioState, Position,
    ResearchClock, ResearchDecision, decide,
)

PROTECTED_START = "2026-08-05"


@dataclass(frozen=True)
class DailyCostModel:
    model_id: str = "research-open-cost-v1"
    buy_fraction: float = 0.00125
    sell_fraction: float = 0.00125

    def __post_init__(self) -> None:
        if (not self.model_id or not all(isinstance(value, (int, float))
                and not isinstance(value, bool) and math.isfinite(value) and 0 <= value < 1
                for value in (self.buy_fraction, self.sell_fraction))):
            raise ValueError("invalid research costs")


def _incomplete(reason: str, snapshot: DailySnapshot, *, ticker: str | None,
                fills: list[dict], decisions: list[ResearchDecision],
                equity_curve: list[dict], source_ids: set[str]) -> dict:
    return {"state": reason, "session": snapshot.session, "ticker": ticker,
            "fills": fills, "decisions": decisions, "equity_curve": equity_curve,
            "source_ids": sorted(source_ids), "net_return_pct": None,
            "strict_pit_eligible": False, "live_order_eligible": False,
            "registered_verdict": False}


def simulate_daily(snapshots: Sequence[DailySnapshot], *, policy: DailyPolicy,
                   costs: DailyCostModel, starting_cash: float = 10_000_000) -> dict:
    """Decision at completed close t, order at next regular open, then mark close.

    Calls :func:`decide` once per snapshot. The observed next open is the fill
    model's price, not a claim that a real order would fill at the auction.
    All snapshots, including exit and marking days, must precede the frozen
    protected interval. A missing calendar session invalidates the path.
    """
    if (not snapshots or policy.cost_model_id != costs.model_id
            or isinstance(starting_cash, bool) or not isinstance(starting_cash, (int, float))
            or not math.isfinite(starting_cash) or starting_cash <= 0):
        raise ValueError("invalid research simulation inputs")
    market = snapshots[0].market
    if any(not isinstance(item, DailySnapshot) or item.market != market
           or item.session >= PROTECTED_START for item in snapshots):
        raise ValueError("mixed market or registered period in development replay")

    cash = float(starting_cash)
    positions: dict[str, Position] = {}
    used_theses: set[str] = set()
    pending: tuple[ResearchDecision, ...] = ()
    fills: list[dict] = []
    decisions: list[ResearchDecision] = []
    equity_curve: list[dict] = []
    source_ids: set[str] = set()
    skips: dict[str, int] = {}
    full_fill_assumptions = 0

    for index, snapshot in enumerate(snapshots):
        if index and not market_clock.consecutive_sessions(market, snapshots[index - 1].session, snapshot.session):
            return _incomplete("incomplete_missing_session", snapshot, ticker=None, fills=fills,
                               decisions=decisions, equity_curve=equity_curve, source_ids=source_ids)
        bars = {bar.ticker: bar for bar in snapshot.bars}
        source_ids.update(bar.source_id for bar in snapshot.bars)

        # The next day's open is read for the previous close's orders only.
        for order in (item for item in pending if item.action == "EXIT"):
            position = positions.get(order.ticker)
            if position is None:
                return _incomplete("incomplete_exit_position", snapshot, ticker=order.ticker,
                                   fills=fills, decisions=decisions, equity_curve=equity_curve,
                                   source_ids=source_ids)
            bar = bars.get(order.ticker)
            if bar is None or bar.open is None or bar.status != "trading":
                return _incomplete("incomplete_exit_open", snapshot, ticker=order.ticker,
                                   fills=fills, decisions=decisions, equity_curve=equity_curve,
                                   source_ids=source_ids)
            if bar.corporate_action or (position.listed_shares is not None
                                        and bar.listed_shares != position.listed_shares):
                return _incomplete("incomplete_corporate_action", snapshot, ticker=order.ticker,
                                   fills=fills, decisions=decisions, equity_curve=equity_curve,
                                   source_ids=source_ids)
            capacity = bar.open_fill_cap_shares
            if capacity is None:
                full_fill_assumptions += 1
            quantity = min(position.shares, capacity) if capacity is not None else position.shares
            if quantity:
                cash += quantity * bar.open * (1 - costs.sell_fraction)
                fills.append({"side": "sell", "ticker": order.ticker, "session": snapshot.session,
                              "price": bar.open, "shares": quantity, "decision_input_id": order.input_id,
                              "partial": quantity < position.shares})
            remaining = position.shares - quantity
            if remaining:
                positions[order.ticker] = replace(position, shares=remaining, exit_requested=True)
            else:
                del positions[order.ticker]

        for order in (item for item in pending if item.action == "ENTER"):
            if order.thesis_id is None or order.thesis_id in used_theses or order.ticker in positions:
                skips["already_held_or_used_thesis"] = skips.get("already_held_or_used_thesis", 0) + 1
                continue
            if len(positions) >= policy.max_positions:
                skips["full_slots"] = skips.get("full_slots", 0) + 1
                continue
            bar = bars.get(order.ticker)
            if bar is None or bar.open is None or bar.status != "trading":
                skips["missing_or_untradable_entry_open"] = skips.get("missing_or_untradable_entry_open", 0) + 1
                continue
            if bar.corporate_action:
                return _incomplete("incomplete_corporate_action", snapshot, ticker=order.ticker,
                                   fills=fills, decisions=decisions, equity_curve=equity_curve,
                                   source_ids=source_ids)
            open_equity = cash
            for held in positions.values():
                held_bar = bars.get(held.ticker)
                if held_bar is None or held_bar.open is None or held_bar.status != "trading":
                    return _incomplete("incomplete_held_open", snapshot, ticker=held.ticker,
                                       fills=fills, decisions=decisions, equity_curve=equity_curve,
                                       source_ids=source_ids)
                open_equity += held.shares * held_bar.open
            allocation = min(cash, open_equity * policy.risk_budget_fraction / policy.max_positions)
            quantity = int(allocation / (bar.open * (1 + costs.buy_fraction)))
            desired_quantity = quantity
            if bar.open_fill_cap_shares is not None:
                quantity = min(quantity, bar.open_fill_cap_shares)
            if quantity < 1:
                skips["no_cash_or_open_capacity"] = skips.get("no_cash_or_open_capacity", 0) + 1
                continue
            if bar.open_fill_cap_shares is None:
                full_fill_assumptions += 1
            cash -= quantity * bar.open * (1 + costs.buy_fraction)
            positions[order.ticker] = Position(order.ticker, quantity, bar.open, index,
                                               order.thesis_id, bar.listed_shares)
            used_theses.add(order.thesis_id)
            fills.append({"side": "buy", "ticker": order.ticker, "session": snapshot.session,
                          "price": bar.open, "shares": quantity, "decision_input_id": order.input_id,
                          "partial": quantity < desired_quantity})

        # Current close is only accessed after every opening fill has been settled.
        equity = cash
        for position in positions.values():
            bar = bars.get(position.ticker)
            if bar is None or bar.close is None or bar.status != "trading":
                return _incomplete("incomplete_held_close", snapshot, ticker=position.ticker,
                                   fills=fills, decisions=decisions, equity_curve=equity_curve,
                                   source_ids=source_ids)
            if bar.corporate_action or (position.listed_shares is not None
                                        and bar.listed_shares != position.listed_shares):
                return _incomplete("incomplete_corporate_action", snapshot, ticker=position.ticker,
                                   fills=fills, decisions=decisions, equity_curve=equity_curve,
                                   source_ids=source_ids)
            equity += position.shares * bar.close
        equity_curve.append({"session": snapshot.session, "equity": round(equity, 6)})
        state = PortfolioState(market, cash, tuple(positions.values()), frozenset(used_theses))
        next_day = market_clock.next_sessions(market, snapshot.session, 1)
        if not next_day:
            return _incomplete("incomplete_calendar_end", snapshot, ticker=None, fills=fills,
                               decisions=decisions, equity_curve=equity_curve, source_ids=source_ids)
        today = decide(snapshot, state, policy,
                       ResearchClock(snapshot.as_of, index, next_day[0]))
        decisions.extend(today)
        pending = tuple(item for item in today if item.action in ("EXIT", "ENTER"))

    if positions or pending:
        return {"state": "unmatured", "last_session": snapshots[-1].session,
                "positions": len(positions), "pending": len(pending), "fills": fills,
                "decisions": decisions, "equity_curve": equity_curve,
                "source_ids": sorted(source_ids), "net_return_pct": None,
                "strict_pit_eligible": False, "live_order_eligible": False,
                "registered_verdict": False}
    peak = float(starting_cash)
    max_drawdown = 0.0
    for mark in equity_curve:
        peak = max(peak, mark["equity"])
        max_drawdown = min(max_drawdown, mark["equity"] / peak - 1)
    return {"state": "complete", "market": market, "policy_id": policy.policy_id,
            "cost_model_id": costs.model_id, "start_session": snapshots[0].session,
            "end_session": snapshots[-1].session,
            "net_return_pct": round((cash / starting_cash - 1) * 100, 4),
            "max_drawdown_pct": round(max_drawdown * 100, 4),
            "cash_end": round(cash, 6), "fills": fills, "decisions": decisions,
            "equity_curve": equity_curve, "skips": dict(sorted(skips.items())),
            "source_ids": sorted(source_ids),
            "fill_model": "next_open_assumed; explicit capacity if provided, otherwise full-fill assumption",
            "full_fill_assumptions": full_fill_assumptions,
            "corporate_action_coverage": "unverified unless all issuer actions and payouts were supplied",
            "strict_pit_eligible": False, "live_order_eligible": False,
            "registered_verdict": False}
