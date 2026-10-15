"""Same-seed paper policy replay, isolated from signals and live orders.

The price-only result is a sensitivity, never booked account performance. An
action-certified *modeled* return requires an unadjusted price basis, a complete
action certificate and matching daily event evidence for every traded security;
it still cannot verify historical fills or costs.
"""

from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from zoneinfo import ZoneInfo

from signal_desk import market_clock
from signal_desk.broker import execution
from signal_desk.signals.research_action_coverage import CoverageCertificate
from signal_desk.signals.research_daily_simulator import SETTLEMENT_REQUIRED_KINDS
from signal_desk.signals.research_event_evidence import DailyEventEvidence

VERSION = "paper-same-seed-cash-replay-v1"
DEVELOPMENT_END = "2026-08-04"  # Registered evaluation starts the next session.
ARMS = ("actual_schedule", "no_add", "hold5", "hold20", "cash")
_ZONES = {"kr": "Asia/Seoul", "us": "America/New_York"}
_ASSUMPTIONS = {
    "kr": {"version": 1, "market": "kr", "slippage_bps": 0.0,
           "commission_bps": 1.5, "sell_tax_bps": 20.0, "sec_sell_bps": 0.0,
           "finra_per_share": 0.0, "finra_cap": 0.0},
    "us": {"version": 1, "market": "us", "slippage_bps": 0.0,
           "commission_bps": 0.0, "sell_tax_bps": 0.0, "sec_sell_bps": 0.206,
           "finra_per_share": 0.000195, "finra_cap": 9.79},
}


def _price(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _blocked(reason: str, **extra) -> dict:
    return {"version": VERSION, "state": "blocked", "reason": reason,
            "mode": "research_only", "live_eligible": False,
            "registered_verdict": False, "strict_pit_eligible": False,
            "verified_total_return": None, "arms": {}, **extra}


def _certificate_gaps(tickers: set[str], dates: list[str], market: str,
                      certificates: dict[str, CoverageCertificate],
                      evidence: dict[tuple[str, str], DailyEventEvidence],
                      security_ids: dict[str, str], now: dt.datetime) -> list[str]:
    gaps = []
    for ticker in sorted(tickers):
        cert = certificates.get(ticker)
        if (cert is None or cert.market != market
                or cert.security_id != security_ids.get(ticker)
                or cert.state != "complete_for_scope"
                or any(window.observed_at > now for window in cert.windows)
                or cert.start > dates[0] or cert.end < dates[-1]
                or not SETTLEMENT_REQUIRED_KINDS <= cert.required_kinds):
            gaps.append(f"{ticker}:action_scope")
            continue
        for day in dates:
            proof = evidence.get((day, ticker))
            if (proof is None or proof.market != market or proof.ticker != ticker
                    or proof.session != day or proof.coverage != "verified_complete"
                    or proof.source_observed_at > now
                    or proof.scope_reconciliation_sha256 != cert.reconciliation_sha256):
                gaps.append(f"{ticker}:{day}:event_day")
    return gaps


def replay(trades: list[dict], closes: dict[str, tuple[list[str], list[float | None]]],
           *, market: str, seed_cash: float, as_of: str, now: dt.datetime | None = None,
           price_basis: str = "unknown",
           certificates: dict[str, CoverageCertificate] | None = None,
           event_evidence: dict[tuple[str, str], DailyEventEvidence] | None = None,
           security_ids: dict[str, str] | None = None) -> dict:
    """Replay fixed order paths with identical seed and explicit evidence gates.

    `actual_schedule` uses recorded fill prices and quantities, but a fixed
    *assumed* historical fee schedule. `no_add` removes every buy while the
    actual ticker is already held, then sells integer shares in the observed
    sell fraction, flushing the remainder on the final actual sale. Hold arms
    ignore later observed orders and sell after 5/20 completed sessions at the
    raw close with an additional adverse 5bp sell slippage assumption.
    """
    if market not in _ZONES or _price(seed_cash) is None or not trades:
        return _blocked("invalid_market_seed_or_empty_journal")
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("timezone-aware now required")
    latest = market_clock.latest_completed_session(market, now)
    if (not market_clock.is_session(market, as_of) or latest is None or as_of > latest):
        return _blocked("as_of_not_completed", latest_completed_session=latest)
    if as_of > DEVELOPMENT_END:
        return _blocked("protected_research_period", study_window_end=DEVELOPMENT_END)
    zone = ZoneInfo(_ZONES[market])
    ordered: list[dict] = []
    last_id, last_ts = -1, -1.0
    for trade in trades:
        try:
            row_id, timestamp = int(trade["id"]), float(trade["ts"])
            ticker = str(trade["ticker"])
            side = str(trade["side"]).lower()
            qty, price = int(trade["qty"]), _price(trade["price"])
            day = dt.datetime.fromtimestamp(timestamp, zone).date().isoformat()
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            return _blocked("invalid_journal_row")
        if (row_id <= last_id or timestamp < last_ts or ticker == ""
                or side not in {"buy", "sell"} or qty < 1
                or isinstance(trade["qty"], bool) or float(trade["qty"]) != qty
                or price is None or not market_clock.is_session(market, day)):
            return _blocked("invalid_journal_order_or_fill")
        last_id, last_ts = row_id, timestamp
        if day > DEVELOPMENT_END:
            return _blocked("protected_research_period", study_window_end=DEVELOPMENT_END)
        if day <= as_of:
            ordered.append({"id": row_id, "day": day, "ticker": ticker,
                            "side": side, "qty": qty, "price": price})
    if not ordered:
        return _blocked("no_completed_journal_rows", as_of=as_of)
    first = ordered[0]["day"]
    first_date = dt.date.fromisoformat(first)
    date_count = (dt.date.fromisoformat(as_of) - first_date).days + 1
    dates = [(first_date + dt.timedelta(days=offset)).isoformat()
             for offset in range(date_count)
             if market_clock.is_session(market, first_date + dt.timedelta(days=offset))]
    tickers = {row["ticker"] for row in ordered}
    panels: dict[str, dict[str, float]] = {}
    for ticker in tickers:
        series = closes.get(ticker)
        if (series is None or len(series) != 2 or len(series[0]) != len(series[1])
                or series[0] != sorted(set(series[0]))):
            return _blocked("missing_or_invalid_dated_prices", ticker=ticker)
        panels[ticker] = dict(zip(series[0], series[1]))
    certificates = certificates or {}
    event_evidence = event_evidence or {}
    action_gaps = _certificate_gaps(tickers, dates, market, certificates,
                                    event_evidence, security_ids or {}, now)
    action_certified = price_basis == "unadjusted_verified" and not action_gaps
    if price_basis != "unadjusted_verified":
        action_gaps.insert(0, "unverified_price_basis")
    events_by_day = defaultdict(list)
    if action_certified:
        seen_cash_events: set[tuple[str, str]] = set()
        for ticker in tickers:
            for day in dates:
                for event in event_evidence[(day, ticker)].events:
                    if event.kind not in {"split", "cash_dividend"}:
                        return _blocked("unsupported_certified_action", ticker=ticker,
                                        session=day, action_kind=event.kind)
                    if event.kind == "cash_dividend" and (
                            event.cash_amount_status != "final"
                            or event.payment_status != "confirmed"
                            or event.payment_date is None or not event.event_id):
                        return _blocked("unsettled_certified_dividend", ticker=ticker,
                                        session=day)
                    if event.kind == "cash_dividend":
                        key = (ticker, event.event_id)
                        if key in seen_cash_events:
                            return _blocked("duplicate_cash_entitlement", ticker=ticker,
                                            session=day)
                        seen_cash_events.add(key)
                    events_by_day[day].append((ticker, event))
    rows_by_day = defaultdict(list)
    for row in ordered:
        rows_by_day[row["day"]].append(row)
    states = {arm: {"cash": float(seed_cash), "holdings": {}, "scheduled": {},
                    "receivable": 0.0, "due": defaultdict(float), "fees": 0.0,
                    "skipped_entries": 0, "fills": 0} for arm in ARMS}
    actual_qty = defaultdict(int)
    paths = []
    assumptions = _ASSUMPTIONS[market]

    def fill(arm: str, ticker: str, side: str, qty: int, price: float,
             *, close_exit: bool = False, strict: bool = False) -> bool:
        state = states[arm]
        before = state["holdings"].get(ticker, 0)
        if qty < 1 or (side == "sell" and qty > before):
            raise ValueError("unmatched sell")
        model = {**assumptions, "slippage_bps": 5.0 if close_exit else 0.0}
        result = execution.calculate(price, qty, side, market, assumptions=model)
        if side == "buy" and state["cash"] + result.cash_change < -0.000001:
            if strict:
                raise ValueError("actual order exceeds assumed cash")
            state["skipped_entries"] += 1
            return False
        state["cash"] += result.cash_change
        state["fees"] += result.total_fees + result.slippage_cost
        state["fills"] += 1
        remaining = before + (qty if side == "buy" else -qty)
        if remaining:
            state["holdings"][ticker] = remaining
        else:
            state["holdings"].pop(ticker, None)
        return True

    try:
        for day in dates:
            if action_certified:
                for ticker, event in events_by_day[day]:
                    for arm in ARMS[:-1]:
                        state = states[arm]
                        qty = state["holdings"].get(ticker, 0)
                        if not qty:
                            continue
                        if event.kind == "split":
                            shares = qty * event.split_numerator
                            if shares % event.split_denominator:
                                raise ValueError("unresolved fractional split right")
                            state["holdings"][ticker] = shares // event.split_denominator
                        else:
                            amount = qty * event.cash_per_share
                            state["receivable"] += amount
                            state["due"][event.payment_date] += amount
                    if event.kind == "split":
                        qty = actual_qty.get(ticker, 0)
                        if qty:
                            shares = qty * event.split_numerator
                            if shares % event.split_denominator:
                                raise ValueError("unresolved actual fractional split")
                            actual_qty[ticker] = shares // event.split_denominator
            for row in rows_by_day[day]:
                ticker, side, qty, price = (row[k] for k in ("ticker", "side", "qty", "price"))
                before = actual_qty[ticker]
                if side == "buy":
                    fill("actual_schedule", ticker, side, qty, price, strict=True)
                    if before == 0:
                        fill("no_add", ticker, side, qty, price)
                        for horizon in (5, 20):
                            arm = f"hold{horizon}"
                            if ticker in states[arm]["holdings"]:
                                states[arm]["skipped_entries"] += 1
                                continue
                            if fill(arm, ticker, side, qty, price):
                                target = market_clock.next_sessions(market, day, horizon)
                                if len(target) != horizon:
                                    raise ValueError("horizon outside verified calendar")
                                states[arm]["scheduled"][ticker] = target[-1]
                    actual_qty[ticker] += qty
                else:
                    if before < qty:
                        raise ValueError("journal sell exceeds actual holding")
                    fill("actual_schedule", ticker, side, qty, price, strict=True)
                    no_add_qty = states["no_add"]["holdings"].get(ticker, 0)
                    if no_add_qty:
                        sold = no_add_qty if qty == before else int(no_add_qty * qty / before)
                        if sold:
                            fill("no_add", ticker, side, sold, price)
                    actual_qty[ticker] -= qty
            for horizon in (5, 20):
                arm = f"hold{horizon}"
                state = states[arm]
                for ticker, target in list(state["scheduled"].items()):
                    if target != day:
                        continue
                    price = _price(panels[ticker].get(day))
                    if price is None:
                        raise ValueError(f"missing scheduled close:{ticker}:{day}")
                    fill(arm, ticker, "sell", state["holdings"][ticker], price,
                         close_exit=True)
                    del state["scheduled"][ticker]
            point = {"session": day}
            for arm, state in states.items():
                for due_day, amount in list(state["due"].items()):
                    if due_day <= day:
                        state["cash"] += amount
                        state["receivable"] -= amount
                        del state["due"][due_day]
                equity = state["cash"] + state["receivable"]
                for ticker, qty in state["holdings"].items():
                    close = _price(panels[ticker].get(day))
                    if close is None:
                        raise ValueError(f"missing held close:{ticker}:{day}")
                    equity += qty * close
                point[arm] = round(equity, 6)
            paths.append(point)
    except ValueError as exc:
        return _blocked(str(exc), as_of=as_of, certificate_gaps=action_gaps[:20])

    results = {}
    for arm, state in states.items():
        nav = paths[-1][arm]
        results[arm] = {"nav": nav, "return_pct": round((nav / seed_cash - 1) * 100, 4),
                        "cash": round(state["cash"], 6),
                        "receivable": round(state["receivable"], 6),
                        "holdings": dict(sorted(state["holdings"].items())),
                        "assumed_costs": round(state["fees"], 6),
                        "fills": state["fills"],
                        "pending_scheduled_exits": len(state["scheduled"]),
                        "skipped_entries": state["skipped_entries"]}
    return {"version": VERSION, "state": "complete", "mode": "research_only",
            "live_eligible": False, "registered_verdict": False,
            "strict_pit_eligible": False, "as_of": as_of, "first_session": first,
            "journal_rows_used": len(ordered),
            "journal_rows_after_as_of": len(trades) - len(ordered),
            "seed_cash": seed_cash, "market": market,
            "price_basis": price_basis, "action_certified": action_certified,
            "certificate_gaps_count": len(action_gaps),
            "certificate_gaps_sample": action_gaps[:20],
            "cost_model": {"observed_fills": assumptions,
                           "daily_close_sell_slippage_bps": 5.0},
            "arms": results, "path": paths,
            "action_certified_model_return_pct": (
                {arm: row["return_pct"] for arm, row in results.items()}
                if action_certified else None),
            "verified_total_return": None,
            "note": ("같은 초기현금의 고정 주문경로 재생입니다. 현재 원천이 미인증이면 NAV는 "
                     "가격 전용 민감도일 뿐 실제 계좌/총수익이 아닙니다. 종가 청산·비용·체결은 "
                     "가정이며, 추가매수 제거가 이후 판단을 바꿀 수 있습니다.")}
