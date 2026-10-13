"""Research-only next-open cash replay of the shared daily decision contract.

Daily OHLC cannot prove an auction fill. The model reports its full-fill and
capacity assumptions; unknown actions, halted holdings and missing prices stop
return calculation instead of being filled or priced from a later close.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Literal, Mapping, Sequence

from signal_desk import market_clock
from signal_desk.signals.research_decision import (
    CompletedDailyBar, DailyPolicy, DailySnapshot, PortfolioState, Position,
    ResearchClock, ResearchDecision, decide,
)
from signal_desk.signals.research_event_evidence import DailyEventEvidence
from signal_desk.signals.research_share_exchange_settlement import (
    ShareExchangeTerms, value_exchange,
)
from signal_desk.signals.research_action_coverage import (
    ACTION_KINDS, CoverageCertificate, ReviewedShareCountChange,
)

PROTECTED_START = "2026-08-05"
SETTLEMENT_REQUIRED_KINDS = ACTION_KINDS - {"unsupported"}


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
                   require_event_coverage: bool = False,
                   reviewed_share_changes: Mapping[tuple[str, str], ReviewedShareCountChange] | None = None,
                   share_exchange_terms: Mapping[tuple[str, str], ShareExchangeTerms] | None = None,
                   coverage_certificates: Mapping[str, CoverageCertificate] | None = None,
                   return_contract: Literal["legacy", "settlement_v2"] = "legacy") -> dict:
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
    if return_contract not in ("legacy", "settlement_v2"):
        raise ValueError("invalid research return contract")
    execution = execution or DailyExecutionScenario()
    if not isinstance(execution, DailyExecutionScenario):
        raise ValueError("invalid execution scenario")
    market = snapshots[0].market
    if any(not isinstance(item, DailySnapshot) or item.market != market
           or item.session >= PROTECTED_START for item in snapshots):
        raise ValueError("mixed market or registered period in development replay")
    for key, fact in (reviewed_share_changes or {}).items():
        if (not isinstance(key, tuple) or len(key) != 2
                or not isinstance(fact, ReviewedShareCountChange)
                or market != "kr" or key != (fact.listing_session, fact.ticker)):
            raise ValueError("reviewed share-change key or market mismatch")
    for ticker, certificate in (coverage_certificates or {}).items():
        if (not isinstance(ticker, str) or not ticker
                or not isinstance(certificate, CoverageCertificate)
                or certificate.market != market):
            raise ValueError("coverage certificate key or market mismatch")
    if share_exchange_terms and (market != "kr" or return_contract != "settlement_v2"):
        raise ValueError("share exchange needs Korean settlement_v2 replay")
    for key, terms in (share_exchange_terms or {}).items():
        if (not isinstance(key, tuple) or len(key) != 2
                or not isinstance(terms, ShareExchangeTerms)
                or key != (terms.effective_session, terms.old_ticker)
                or terms.effective_session < snapshots[0].session
                or terms.first_tradable_session > snapshots[-1].session):
            raise ValueError("share exchange key or scope mismatch")
    term_rows = tuple((share_exchange_terms or {}).values())
    if (len({row.event_id for row in term_rows}) != len(term_rows)
            or len({row.successor_ticker for row in term_rows}) != len(term_rows)):
        raise ValueError("duplicate share exchange event or successor")

    def certified_traded_scope(filled_tickers: set[str]) -> bool:
        # The old-security coverage certificate does not prove the successor's
        # entire holding path or fractional-payment chain. Keep the new path
        # uncertified until a joined two-security certificate is implemented.
        if share_exchanged_tickers:
            return False
        if not require_event_coverage or not filled_tickers:
            return False
        for ticker in filled_tickers:
            certificate = (coverage_certificates or {}).get(ticker)
            if (certificate is None or certificate.state != "complete_for_scope"
                    or not SETTLEMENT_REQUIRED_KINDS <= certificate.required_kinds
                    or certificate.start > snapshots[0].session
                    or certificate.end < snapshots[-1].session):
                return False
            path_bars = [bar for snapshot in snapshots for bar in snapshot.bars
                         if bar.ticker == ticker]
            if (not path_bars or any(bar.security_id != certificate.security_id
                                     for bar in path_bars)):
                return False
            bought = [fill["session"] for fill in fills
                      if fill["ticker"] == ticker and fill["side"] == "buy"]
            sold = [fill["session"] for fill in fills
                    if fill["ticker"] == ticker and fill["side"] == "sell"]
            last_held = (snapshots[-1].session if ticker in positions
                         else max(sold, default=exchanged_tickers.get(
                             ticker, snapshots[-1].session)))
            if not bought:
                return False
            for snapshot in snapshots:
                if not min(bought) <= snapshot.session <= last_held:
                    continue
                proof = (event_evidence or {}).get((snapshot.session, ticker))
                if (proof is None or proof.coverage != "verified_complete"
                        or proof.scope_reconciliation_sha256
                        != certificate.reconciliation_sha256):
                    return False
        return True

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
    # payment_date, amount, amount_status, payment_status, event_id, kind, paid source.
    receivables: list[tuple[str | None, float, str | None, str | None,
                            str | None, str, str | None]] = []
    dividend_cash_paid = 0.0
    exchange_cash_paid = 0.0
    seen_cash_right_ids: set[tuple[str, str]] = set()
    exchanged_tickers: dict[str, str] = {}
    share_exchanged_tickers: dict[str, str] = {}
    pending_share_rights: dict[str, tuple[Position, ShareExchangeTerms]] = {}
    unresolved_fractional_rights: list[dict] = []
    simulated_share_deliveries: list[dict] = []
    reported_share_scenarios: list[dict] = []
    applied_issuer_facts: set[str] = set()

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
        fact = (reviewed_share_changes or {}).get((snapshot.session, bar.ticker))
        if (fact is not None and bar.corporate_action == fact.kind
                and bar.listed_shares == fact.listed_shares_after):
            return False
        return (proof is None or proof.coverage != "verified_complete"
                or not any(event.kind == bar.corporate_action for event in proof.events))

    for index, snapshot in enumerate(snapshots):
        if index and not market_clock.consecutive_sessions(market, snapshots[index - 1].session, snapshot.session):
            return _incomplete("incomplete_missing_session", snapshot, ticker=None, fills=fills,
                               decisions=decisions, equity_curve=equity_curve, source_ids=source_ids)
        bars = {bar.ticker: bar for bar in snapshot.bars}
        source_ids.update(bar.source_id for bar in snapshot.bars)
        if return_contract == "settlement_v2":
            day_tickers = set(bars) | {ticker for day, ticker in (event_evidence or {})
                                       if day == snapshot.session}
            for ticker in day_tickers:
                day_proof = evidence_for(snapshot, ticker)
                if day_proof is None or day_proof.coverage != "verified_complete":
                    continue
                for day_event in day_proof.events:
                    if (day_event.kind not in ("cash_dividend", "cash_exchange")
                            or day_event.event_id is None):
                        continue
                    event_key = ticker, day_event.event_id
                    if event_key in seen_cash_right_ids:
                        return _incomplete("incomplete_duplicate_entitlement", snapshot,
                                           ticker=ticker, fills=fills, decisions=decisions,
                                           equity_curve=equity_curve, source_ids=source_ids)
                    seen_cash_right_ids.add(event_key)
        day_changes = {ticker: fact for (day, ticker), fact in
                       (reviewed_share_changes or {}).items() if day == snapshot.session}
        for ticker, fact in day_changes.items():
            prior_bars = ({bar.ticker: bar for bar in snapshots[index - 1].bars}
                          if index else {})
            prior, current = prior_bars.get(ticker), bars.get(ticker)
            if (prior is None or current is None
                    or snapshots[index - 1].session != fact.previous_session
                    or prior.listed_shares != fact.listed_shares_before
                    or current.listed_shares != fact.listed_shares_after):
                return _incomplete("incomplete_issuer_share_change", snapshot, ticker=ticker,
                                   fills=fills, decisions=decisions, equity_curve=equity_curve,
                                   source_ids=source_ids)
            proof = evidence_for(snapshot, ticker)
            if (proof is not None and proof.coverage == "verified_complete"
                    and (sum(event.kind == fact.kind for event in proof.events) != 1
                         or any(event.kind == "split" for event in proof.events))):
                return _incomplete("incomplete_issuer_share_change", snapshot, ticker=ticker,
                                   fills=fills, decisions=decisions, equity_curve=equity_curve,
                                   source_ids=source_ids)

        # Ex-date actions apply to positions held before this open. An empty
        # verified record is a source assertion; absent coverage is not one.
        for ticker, position in list(positions.items()):
            proof = evidence_for(snapshot, ticker)
            fact = day_changes.get(ticker)
            if require_event_coverage and (proof is None or proof.coverage != "verified_complete"):
                return _incomplete("incomplete_action_coverage", snapshot, ticker=ticker,
                                   fills=fills, decisions=decisions, equity_curve=equity_curve,
                                   source_ids=source_ids)
            if fact is not None:
                if position.listed_shares != fact.listed_shares_before:
                    return _incomplete("incomplete_issuer_share_change", snapshot, ticker=ticker,
                                       fills=fills, decisions=decisions, equity_curve=equity_curve,
                                       source_ids=source_ids)
                positions[ticker] = replace(position, listed_shares=fact.listed_shares_after)
                applied_issuer_facts.add(f"{ticker}:{snapshot.session}:{fact.source_sha256}")
            if proof is None or proof.coverage != "verified_complete":
                continue
            event_source_ids.add(proof.source_id)
            bar = bars.get(ticker)
            for event in proof.events:
                if event.kind in ("halt", "delisting"):
                    return _incomplete("incomplete_untradable_event", snapshot, ticker=ticker,
                                       fills=fills, decisions=decisions, equity_curve=equity_curve,
                                       source_ids=source_ids)
                if event.kind == "cash_exchange":
                    if (return_contract != "settlement_v2"
                            or any(order.ticker == ticker and order.action == "EXIT"
                                   for order in pending)
                            or (bar is not None and
                                (bar.status == "trading" or bar.open is not None))
                            or ticker in exchanged_tickers):
                        return _incomplete("incomplete_cash_exchange_path", snapshot,
                                           ticker=ticker, fills=fills, decisions=decisions,
                                           equity_curve=equity_curve, source_ids=source_ids)
                    receivables.append((event.payment_date,
                                        position.shares * event.cash_per_share,
                                        event.cash_amount_status, event.payment_status,
                                        event.event_id, "cash_exchange",
                                        event.payment_source_id))
                    event_source_ids.add(event.effective_source_id)
                    exchanged_tickers[ticker] = snapshot.session
                    del positions[ticker]
                    break
                if event.kind == "share_exchange":
                    terms = (share_exchange_terms or {}).get((snapshot.session, ticker))
                    if (return_contract != "settlement_v2" or terms is None
                            or terms.old_security_id != (bar.security_id if bar else None)
                            or any(order.ticker == ticker and order.action == "EXIT"
                                   for order in pending)
                            or (bar is not None and
                                (bar.status == "trading" or bar.open is not None))
                            or ticker in share_exchanged_tickers
                            or terms.successor_ticker in positions):
                        return _incomplete("incomplete_share_exchange_path", snapshot,
                                           ticker=ticker, fills=fills, decisions=decisions,
                                           equity_curve=equity_curve, source_ids=source_ids)
                    pending_share_rights[ticker] = position, terms
                    share_exchanged_tickers[ticker] = snapshot.session
                    event_source_ids.add(terms.source_id)
                    del positions[ticker]
                    break
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
                    if (return_contract == "settlement_v2"
                            and (event.event_id is None or event.cash_amount_status is None
                                 or event.payment_status is None)):
                        return _incomplete("incomplete_dividend_terms", snapshot, ticker=ticker,
                                           fills=fills, decisions=decisions, equity_curve=equity_curve,
                                           source_ids=source_ids)
                    receivables.append((event.payment_date,
                                        position.shares * event.cash_per_share,
                                        event.cash_amount_status, event.payment_status,
                                        event.event_id, "cash_dividend",
                                        event.payment_source_id))

        # Share delivery is not an opening auction fill. The first tradable
        # successor bar must exist on its documented session, with its own ID.
        # No future successor close is used during the earlier rights gap.
        for old_ticker, (old_position, terms) in list(pending_share_rights.items()):
            if snapshot.session < terms.first_tradable_session:
                continue
            if snapshot.session > terms.first_tradable_session:
                continue  # Unknown precision stays an unpriced, untradeable right.
            successor = bars.get(terms.successor_ticker)
            if (successor is None or successor.security_id != terms.successor_security_id
                    or successor.status != "trading" or successor.open is None
                    or successor.close is None or terms.successor_ticker in positions
                    or unresolved_bar_action(snapshot, successor)):
                return _incomplete("incomplete_successor_listing", snapshot,
                                   ticker=terms.successor_ticker, fills=fills,
                                   decisions=decisions, equity_curve=equity_curve,
                                   source_ids=source_ids)
            successor_proof = evidence_for(snapshot, terms.successor_ticker)
            if require_event_coverage and (successor_proof is None
                                            or successor_proof.coverage != "verified_complete"):
                return _incomplete("incomplete_action_coverage", snapshot,
                                   ticker=terms.successor_ticker, fills=fills,
                                   decisions=decisions, equity_curve=equity_curve,
                                   source_ids=source_ids)
            if successor_proof is not None and successor_proof.coverage == "verified_complete":
                event_source_ids.add(successor_proof.source_id)
            valuation = value_exchange(
                old_shares=old_position.shares, ratio=terms.ratio,
                fractional_listing_price_krw=str(successor.close),
                successor_mark_price_krw=str(successor.close))
            reported_share_scenarios.append({"event_id": terms.event_id,
                "session": snapshot.session, "old_ticker": old_ticker,
                "successor_ticker": terms.successor_ticker, "valuation": valuation})
            if valuation["exact_value"] is None:
                continue
            quantity = int(valuation["exact_value"]["whole_new_shares"])
            fraction = Decimal(valuation["exact_value"]["fractional_new_share"])
            if quantity:
                positions[terms.successor_ticker] = replace(old_position,
                    ticker=terms.successor_ticker, shares=quantity,
                    entry_price=old_position.entry_price * old_position.shares / quantity,
                    listed_shares=successor.listed_shares)
                simulated_share_deliveries.append({"event_id": terms.event_id,
                    "session": snapshot.session, "old_ticker": old_ticker,
                    "successor_ticker": terms.successor_ticker,
                    "whole_new_shares": quantity, "source_id": terms.source_id,
                    "account_delivery_verified": False})
            if fraction:
                unresolved_fractional_rights.append({"event_id": terms.event_id,
                    "old_ticker": old_ticker,
                    "unrounded_cash_krw": str(fraction * Decimal(str(successor.close))),
                    "state": "payment_amount_and_date_unconfirmed"})
            del pending_share_rights[old_ticker]

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
            if order.ticker in exchanged_tickers or order.ticker in share_exchanged_tickers:
                return _incomplete("incomplete_untradable_entry_after_exchange", snapshot,
                                   ticker=order.ticker, fills=fills, decisions=decisions,
                                   equity_curve=equity_curve, source_ids=source_ids)
            if order.thesis_id is None or order.thesis_id in used_theses or order.ticker in positions:
                skips["already_held_or_used_thesis"] = skips.get("already_held_or_used_thesis", 0) + 1
                continue
            if len(positions) + len(pending_share_rights) >= policy.max_positions:
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
                if any(event.kind in ("halt", "delisting", "cash_exchange", "share_exchange")
                       for event in proof.events):
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
        due = [item for item in receivables if item[0] is not None
               and item[0] <= snapshot.session
               and (return_contract == "legacy" or item[3] == "confirmed")]
        if due:
            dividend_cash_paid += sum(item[1] for item in due if item[5] == "cash_dividend")
            exchange_cash_paid += sum(item[1] for item in due if item[5] == "cash_exchange")
            event_source_ids.update(item[6] for item in due if item[6] is not None)
            cash += sum(item[1] for item in due)
            receivables = [item for item in receivables if item not in due]

        # Current close is only accessed after every opening fill has been settled.
        unknown_rights = (return_contract == "settlement_v2"
                          and (any(item[2] != "final" for item in receivables)
                               or bool(pending_share_rights) or bool(unresolved_fractional_rights)))
        equity = cash + sum(item[1] for item in receivables
                            if return_contract == "legacy" or item[2] == "final")
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
        equity_curve.append({"session": snapshot.session,
                             "equity": None if unknown_rights else round(equity, 6)})
        state = PortfolioState(market, cash, tuple(positions.values()), frozenset(used_theses))
        next_day = market_clock.next_sessions(market, snapshot.session, 1)
        if not next_day:
            return _incomplete("incomplete_calendar_end", snapshot, ticker=None, fills=fills,
                               decisions=decisions, equity_curve=equity_curve, source_ids=source_ids)
        today = decide(snapshot, state, policy,
                       ResearchClock(snapshot.as_of, index, next_day[0]))
        decisions.extend(today)
        pending = tuple(item for item in today if item.action in ("EXIT", "ENTER"))

    if positions or pending or receivables or pending_share_rights or unresolved_fractional_rights:
        result = {"state": "unmatured", "last_session": snapshots[-1].session,
                "positions": len(positions), "pending": len(pending), "fills": fills,
                "decisions": decisions, "equity_curve": equity_curve,
                "source_ids": sorted(source_ids), "net_return_pct": None,
                "unpaid_dividend_receivables": sum(item[5] == "cash_dividend"
                                                   for item in receivables),
                "strict_pit_eligible": False, "live_order_eligible": False,
                "registered_verdict": False}
        if reviewed_share_changes is not None:
            result["applied_issuer_facts"] = sorted(applied_issuer_facts)
        if return_contract == "settlement_v2":
            final_mark = equity_curve[-1]["equity"]
            certified = certified_traded_scope({fill["ticker"] for fill in fills})
            result.update({"economic_mark_return_pct": (
                round((final_mark / starting_cash - 1) * 100, 4)
                if certified and final_mark is not None else None),
                "simulated_liquidated_return_pct": None,
                "account_realized_return_pct": None,
                "cash_available_end": round(cash, 6),
                "known_receivable_end": round(sum(item[1] for item in receivables
                                                   if item[2] == "final"), 6),
                "unknown_receivable_count": sum(item[2] != "final" for item in receivables),
                "dividend_cash_paid": round(dividend_cash_paid, 6),
                "exchange_cash_paid": round(exchange_cash_paid, 6),
                "unpaid_cash_exchange_receivables": sum(
                    item[5] == "cash_exchange" for item in receivables),
                "event_source_ids": sorted(event_source_ids),
                "cash_exchange_profile": "general_holder_no_election_gross_pre_tax"
                if exchanged_tickers else None,
                "cash_exchange_effective_sessions": dict(sorted(exchanged_tickers.items())),
                "share_exchange_effective_sessions": dict(sorted(share_exchanged_tickers.items())),
                "pending_share_rights": sorted(pending_share_rights),
                "simulated_share_deliveries": simulated_share_deliveries,
                "unresolved_fractional_rights": unresolved_fractional_rights,
                "reported_share_scenarios": reported_share_scenarios,
                "cash_settlement_state": (
                    "unconfirmed_or_future_receivable" if receivables else "no_receivable"),
                "valuation_state": "unknown_share_exchange_right" if pending_share_rights
                else "unknown_fractional_cash" if unresolved_fractional_rights
                else "unknown_right_amount" if final_mark is None else
                "ex_post_known_rights_mark" if certified else "uncertified_action_scope",
                "decision_basis": "daily_snapshot_replay",
                "execution_basis": execution.scenario_id,
                "coverage_state": "certified_for_traded_paths_only" if certified
                else "certificate_missing_or_partial"})
        return result
    peak = float(starting_cash)
    max_drawdown = 0.0
    mark_gap = False
    for mark in equity_curve:
        if mark["equity"] is None:
            mark_gap = True
            continue
        peak = max(peak, mark["equity"])
        max_drawdown = min(max_drawdown, mark["equity"] / peak - 1)
    result = {"state": "complete", "market": market, "policy_id": policy.policy_id,
            "cost_model_id": costs.model_id, "start_session": snapshots[0].session,
            "end_session": snapshots[-1].session,
            "net_return_pct": round((cash / starting_cash - 1) * 100, 4),
            "max_drawdown_pct": None if mark_gap else round(max_drawdown * 100, 4),
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
    if reviewed_share_changes is not None:
        result["applied_issuer_facts"] = sorted(applied_issuer_facts)
    if return_contract == "settlement_v2":
        certified = certified_traded_scope({fill["ticker"] for fill in fills})
        result.update({"economic_mark_return_pct": (
            round((equity_curve[-1]["equity"] / starting_cash - 1) * 100, 4)
            if certified else None),
            "simulated_liquidated_return_pct": result["net_return_pct"]
            if certified else None,
            "account_realized_return_pct": None,
            "cash_available_end": round(cash, 6),
            "known_receivable_end": 0.0,
            "unknown_receivable_count": 0,
            "exchange_cash_paid": round(exchange_cash_paid, 6),
            "unpaid_cash_exchange_receivables": 0,
            "cash_exchange_profile": "general_holder_no_election_gross_pre_tax"
            if exchanged_tickers else None,
            "cash_exchange_effective_sessions": dict(sorted(exchanged_tickers.items())),
            "share_exchange_effective_sessions": dict(sorted(share_exchanged_tickers.items())),
            "pending_share_rights": [],
            "simulated_share_deliveries": simulated_share_deliveries,
            "unresolved_fractional_rights": [],
            "reported_share_scenarios": reported_share_scenarios,
            "cash_settlement_state": "confirmed_or_no_receivable",
            "valuation_state": "ex_post_known_rights_mark" if certified
            else "uncertified_action_scope",
            "decision_basis": "daily_snapshot_replay",
            "execution_basis": execution.scenario_id,
            "coverage_state": "certified_for_traded_paths_only" if certified
            else "certificate_missing_or_partial"})
        if not certified:
            result["net_return_pct"] = None
            result["max_drawdown_pct"] = None
            result["corporate_action_coverage"] = "certificate_missing_or_partial"
            result["return_quality"] = "uncertified_action_scope"
    return result
