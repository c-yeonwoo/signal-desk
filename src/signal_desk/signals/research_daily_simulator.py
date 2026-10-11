"""Research-only next-open cash replay of the shared daily decision contract.

Daily OHLC cannot prove an auction fill. The model reports its full-fill and
capacity assumptions; unknown actions, halted holdings and missing prices stop
return calculation instead of being filled or priced from a later close.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Mapping, Sequence

from signal_desk import market_clock
from signal_desk.signals.research_decision import (
    CompletedDailyBar, DailyPolicy, DailySnapshot, PortfolioState, Position,
    ResearchClock, ResearchDecision, decide,
)
from signal_desk.signals.research_event_evidence import DailyEventEvidence

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


@dataclass(frozen=True)
class DailyExecutionScenario:
    """A declared stress assumption, not an observed auction or fill forecast."""

    scenario_id: str = "open-full-fill-v1"
    buy_adverse_slippage_bps: float = 0.0
    sell_adverse_slippage_bps: float = 0.0
    assumed_fill_fraction: float = 1.0

    def __post_init__(self) -> None:
        if (not self.scenario_id or any(
                not isinstance(value, (int, float)) or isinstance(value, bool)
                or not math.isfinite(value) or not 0 <= value < 10_000
                for value in (self.buy_adverse_slippage_bps,
                              self.sell_adverse_slippage_bps))
                or not isinstance(self.assumed_fill_fraction, (int, float))
                or isinstance(self.assumed_fill_fraction, bool)
                or not math.isfinite(self.assumed_fill_fraction)
                or not 0 <= self.assumed_fill_fraction <= 1):
            raise ValueError("invalid research execution scenario")


def _fillable_quantity(requested: int, *, capacity: int | None,
                       scenario: DailyExecutionScenario) -> int:
    if scenario.assumed_fill_fraction == 0:
        return 0
    assumed = max(1, int(requested * scenario.assumed_fill_fraction))
    return min(requested, assumed, capacity if capacity is not None else requested)


def _incomplete(reason: str, snapshot: DailySnapshot, *, ticker: str | None,
                fills: list[dict], decisions: list[ResearchDecision],
                equity_curve: list[dict], source_ids: set[str]) -> dict:
    return {"state": reason, "session": snapshot.session, "ticker": ticker,
            "fills": fills, "decisions": decisions, "equity_curve": equity_curve,
            "source_ids": sorted(source_ids), "net_return_pct": None,
            "strict_pit_eligible": False, "live_order_eligible": False,
            "registered_verdict": False}


def simulate_daily(snapshots: Sequence[DailySnapshot], *, policy: DailyPolicy,
                   costs: DailyCostModel, starting_cash: float = 10_000_000,
                   execution: DailyExecutionScenario | None = None,
                   event_evidence: Mapping[tuple[str, str], DailyEventEvidence] | None = None,
                   require_event_coverage: bool = False) -> dict:
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
    execution = execution or DailyExecutionScenario()
    if not isinstance(execution, DailyExecutionScenario):
        raise ValueError("invalid execution scenario")
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
    event_source_ids: set[str] = set()
    skips: dict[str, int] = {}
    full_fill_assumptions = 0
    receivables: list[tuple[str, float]] = []
    dividend_cash_paid = 0.0

    def evidence_for(snapshot: DailySnapshot, ticker: str) -> DailyEventEvidence | None:
        row = (event_evidence or {}).get((snapshot.session, ticker))
        if row is not None and (not isinstance(row, DailyEventEvidence)
                                or row.market != market or row.session != snapshot.session
                                or row.ticker != ticker):
            raise ValueError("corporate-action evidence key or market mismatch")
        return row

    def unresolved_bar_action(snapshot: DailySnapshot, bar: CompletedDailyBar) -> bool:
        if not bar.corporate_action:
            return False
        proof = evidence_for(snapshot, bar.ticker)
        return (proof is None or proof.coverage != "verified_complete"
                or not any(event.kind == bar.corporate_action for event in proof.events))

    for index, snapshot in enumerate(snapshots):
        if index and not market_clock.consecutive_sessions(market, snapshots[index - 1].session, snapshot.session):
            return _incomplete("incomplete_missing_session", snapshot, ticker=None, fills=fills,
                               decisions=decisions, equity_curve=equity_curve, source_ids=source_ids)
        bars = {bar.ticker: bar for bar in snapshot.bars}
        source_ids.update(bar.source_id for bar in snapshot.bars)

        # Ex-date actions apply to positions held before this open. An empty
        # verified record is a source assertion; absent coverage is not one.
        for ticker, position in list(positions.items()):
            proof = evidence_for(snapshot, ticker)
            if require_event_coverage and (proof is None or proof.coverage != "verified_complete"):
                return _incomplete("incomplete_action_coverage", snapshot, ticker=ticker,
                                   fills=fills, decisions=decisions, equity_curve=equity_curve,
                                   source_ids=source_ids)
            if proof is None or proof.coverage != "verified_complete":
                continue
            event_source_ids.add(proof.source_id)
            bar = bars.get(ticker)
            for event in proof.events:
                if event.kind in ("halt", "delisting"):
                    return _incomplete("incomplete_untradable_event", snapshot, ticker=ticker,
                                       fills=fills, decisions=decisions, equity_curve=equity_curve,
                                       source_ids=source_ids)
                if event.kind == "split":
                    num, den = event.split_numerator, event.split_denominator
                    if (bar is None or position.listed_shares is None or bar.listed_shares is None
                            or position.shares * num % den
                            or position.listed_shares * num % den
                            or bar.listed_shares != position.listed_shares * num // den):
                        return _incomplete("incomplete_split_terms", snapshot, ticker=ticker,
                                           fills=fills, decisions=decisions, equity_curve=equity_curve,
                                           source_ids=source_ids)
                    position = replace(position, shares=position.shares * num // den,
                                       entry_price=position.entry_price * den / num,
                                       listed_shares=bar.listed_shares)
                    positions[ticker] = position
                elif event.kind == "cash_dividend":
                    receivables.append((event.payment_date,
                                        position.shares * event.cash_per_share))

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
            if unresolved_bar_action(snapshot, bar) or (position.listed_shares is not None
                                        and bar.listed_shares != position.listed_shares):
                return _incomplete("incomplete_corporate_action", snapshot, ticker=order.ticker,
                                   fills=fills, decisions=decisions, equity_curve=equity_curve,
                                   source_ids=source_ids)
            capacity = bar.open_fill_cap_shares
            if capacity is None and execution.assumed_fill_fraction == 1:
                full_fill_assumptions += 1
            quantity = _fillable_quantity(position.shares, capacity=capacity, scenario=execution)
            fill_price = bar.open * (1 - execution.sell_adverse_slippage_bps / 10_000)
            if quantity:
                cash += quantity * fill_price * (1 - costs.sell_fraction)
                fills.append({"side": "sell", "ticker": order.ticker, "session": snapshot.session,
                              "price": fill_price, "observed_open": bar.open,
                              "shares": quantity, "decision_input_id": order.input_id,
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
            if unresolved_bar_action(snapshot, bar):
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
            fill_price = bar.open * (1 + execution.buy_adverse_slippage_bps / 10_000)
            quantity = int(allocation / (fill_price * (1 + costs.buy_fraction)))
            desired_quantity = quantity
            quantity = _fillable_quantity(quantity, capacity=bar.open_fill_cap_shares,
                                          scenario=execution)
            if quantity < 1:
                skips["no_cash_or_open_capacity"] = skips.get("no_cash_or_open_capacity", 0) + 1
                continue
            proof = evidence_for(snapshot, order.ticker)
            if require_event_coverage and (proof is None or proof.coverage != "verified_complete"):
                return _incomplete("incomplete_action_coverage", snapshot, ticker=order.ticker,
                                   fills=fills, decisions=decisions, equity_curve=equity_curve,
                                   source_ids=source_ids)
            if proof is not None and proof.coverage == "verified_complete":
                event_source_ids.add(proof.source_id)
                if any(event.kind in ("halt", "delisting") for event in proof.events):
                    return _incomplete("incomplete_untradable_event", snapshot, ticker=order.ticker,
                                       fills=fills, decisions=decisions, equity_curve=equity_curve,
                                       source_ids=source_ids)
            if bar.open_fill_cap_shares is None and execution.assumed_fill_fraction == 1:
                full_fill_assumptions += 1
            cash -= quantity * fill_price * (1 + costs.buy_fraction)
            positions[order.ticker] = Position(order.ticker, quantity, fill_price, index,
                                               order.thesis_id, bar.listed_shares)
            used_theses.add(order.thesis_id)
            fills.append({"side": "buy", "ticker": order.ticker, "session": snapshot.session,
                          "price": fill_price, "observed_open": bar.open,
                          "shares": quantity, "decision_input_id": order.input_id,
                          "partial": quantity < desired_quantity})

        # Payouts are not purchasing power at the open without an intraday time.
        due = [item for item in receivables if item[0] <= snapshot.session]
        if due:
            dividend_cash_paid += sum(amount for _, amount in due)
            cash += sum(amount for _, amount in due)
            receivables = [item for item in receivables if item[0] > snapshot.session]

        # Current close is only accessed after every opening fill has been settled.
        equity = cash + sum(amount for _, amount in receivables)
        for position in positions.values():
            bar = bars.get(position.ticker)
            if bar is None or bar.close is None or bar.status != "trading":
                return _incomplete("incomplete_held_close", snapshot, ticker=position.ticker,
                                   fills=fills, decisions=decisions, equity_curve=equity_curve,
                                   source_ids=source_ids)
            if unresolved_bar_action(snapshot, bar) or (position.listed_shares is not None
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

    if positions or pending or receivables:
        return {"state": "unmatured", "last_session": snapshots[-1].session,
                "positions": len(positions), "pending": len(pending), "fills": fills,
                "decisions": decisions, "equity_curve": equity_curve,
                "source_ids": sorted(source_ids), "net_return_pct": None,
                "unpaid_dividend_receivables": len(receivables),
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
            "event_source_ids": sorted(event_source_ids),
            "execution_scenario_id": execution.scenario_id,
            "execution_assumptions": {
                "buy_adverse_slippage_bps": execution.buy_adverse_slippage_bps,
                "sell_adverse_slippage_bps": execution.sell_adverse_slippage_bps,
                "assumed_fill_fraction": execution.assumed_fill_fraction,
            },
            "fill_model": "next_open_assumed; explicit capacity if provided, otherwise scenario assumption",
            "full_fill_assumptions": full_fill_assumptions,
            "dividend_cash_paid": round(dividend_cash_paid, 6),
            "corporate_action_coverage": (
                "no_traded_paths" if not fills else
                "verified_for_traded_paths_only" if require_event_coverage else
                "unverified_price_only"),
            "return_quality": (
                "cash_only_no_fills" if not fills else
                "retrospective_event_covered_assumed_fills" if require_event_coverage else
                "price_only_unverified_actions"),
            "strict_pit_eligible": False, "live_order_eligible": False,
            "registered_verdict": False}
