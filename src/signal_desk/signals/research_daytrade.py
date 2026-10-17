"""Fixed, research-only daily selections and same-session cash execution.

Selection sees completed decision-day features only. Orders fix integer shares
before the next open; only execution receives next-session bars. No broker,
store, network, wall clock, or production policy is used here.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass

from signal_desk import market_clock

POLICIES = ("legacy_price", "trend_leader", "leader_pullback", "volume_breakout",
            "cap_control", "cash")
CHALLENGERS = POLICIES[1:4]
SLOTS = 6


@dataclass(frozen=True)
class Features:
    ticker: str
    close: float
    volume: float
    market_cap: float
    ma60: float
    ret5: float
    ret20: float
    ret60: float
    relative_volume: float
    close_location: float
    day_return: float
    legacy_score: float = 0.0
    legacy_buy: bool = False

    def __post_init__(self):
        values = (self.close, self.volume, self.market_cap, self.ma60, self.ret5,
                  self.ret20, self.ret60, self.relative_volume, self.close_location,
                  self.day_return, self.legacy_score)
        if (not self.ticker or any(isinstance(v, bool) or not math.isfinite(v) for v in values)
                or min(self.close, self.volume, self.market_cap, self.ma60) <= 0
                or self.relative_volume < 0 or not 0 <= self.close_location <= 1):
            raise ValueError("invalid daytrade features")


def select(features: tuple[Features, ...], policy: str) -> tuple[Features, ...]:
    if policy not in POLICIES or len({row.ticker for row in features}) != len(features):
        raise ValueError("invalid research policy or duplicate features")
    if len(features) < 160 or policy == "cash":
        return ()
    if policy == "legacy_price":
        ranked = sorted(features, key=lambda row: (-row.legacy_score, row.ticker))[:SLOTS]
        return tuple(row for row in ranked if row.legacy_buy)
    if policy == "cap_control":
        return tuple(sorted(features, key=lambda row: (-row.market_cap, row.ticker))[:SLOTS])
    if policy == "trend_leader":
        median = statistics.median(row.ret20 for row in features)
        pool = [row for row in features if row.close > row.ma60 and row.ret20 > max(0, median)]
        key = lambda row: (-row.ret20, row.ticker)
    elif policy == "leader_pullback":
        pool = [row for row in features if row.close > row.ma60 and row.ret60 > 0
                and -0.05 <= row.ret5 < 0]
        key = lambda row: (-row.ret60, row.ticker)
    else:
        pool = [row for row in features if row.relative_volume >= 2 and row.day_return > 0
                and row.close_location >= 0.75 and row.close > row.ma60]
        key = lambda row: (-row.relative_volume, -row.ret20, row.ticker)
    return tuple(sorted(pool, key=key)[:SLOTS])


@dataclass(frozen=True)
class Order:
    ticker: str
    shares: int
    budget: float
    reference_close: float


def plan_orders(selected: tuple[Features, ...], cash: float, side_cost: float) -> tuple[Order, ...]:
    """No next-session price can affect requested shares or the fixed slot budget."""
    if (len(selected) > SLOTS or len({row.ticker for row in selected}) != len(selected)
            or not math.isfinite(cash) or cash <= 0 or not 0 <= side_cost < 1):
        raise ValueError("invalid order planning inputs")
    budget = cash / SLOTS
    return tuple(Order(row.ticker,
                       min(int(budget / (row.close * 1.30 * (1 + side_cost))),
                           int(row.volume * 0.01)), budget, row.close)
                 for row in selected)


def _positive(value) -> bool:
    return (isinstance(value, (float, int)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def execute_day(orders: tuple[Order, ...], bars: dict[str, dict], *,
                cash: float, side_cost: float, fill_fraction: float = 1.0) -> dict:
    """Opening/closing prices model auction fills, not verified executable quotes."""
    if (not _positive(cash) or not 0 <= side_cost < 1 or not 0 <= fill_fraction <= 1
            or len({order.ticker for order in orders}) != len(orders)):
        raise ValueError("invalid day execution inputs")
    fills, skips = [], Counter()
    for order in orders:
        qty = int(order.shares * fill_fraction)
        if qty < 1:
            skips["quantity_or_fill_assumption_zero"] += 1
            continue
        bar = bars.get(order.ticker) or {}
        opened, closed, volume = bar.get("open"), bar.get("close"), bar.get("volume")
        if not _positive(opened) or not _positive(volume):
            skips["no_open_or_trading_volume"] += 1
            continue
        high, low = bar.get("high"), bar.get("low")
        if _positive(high) and _positive(low) and high == low == opened == closed:
            skips["single_price_bar_fill_unverified"] += 1
            continue
        debit = qty * opened * (1 + side_cost)
        if debit > order.budget + 1e-8 or debit > cash + 1e-8:
            skips["precommitted_budget_exceeded"] += 1
            continue
        if (not _positive(closed) or not _positive(high) or not _positive(low)
                or not low <= min(opened, closed) <= max(opened, closed) <= high):
            return {"state": "incomplete_exit", "ticker": order.ticker,
                    "cash_end": None, "fills": fills, "skips": dict(skips)}
        credit = qty * closed * (1 - side_cost)
        pnl = credit - debit
        cash += pnl
        fills.append({"ticker": order.ticker, "requested_shares": order.shares,
                      "shares": qty, "reference_close": order.reference_close,
                      "open": opened, "close": closed, "buy_notional": qty * opened,
                      "net_pnl": pnl, "net_trade_pct": (credit / debit - 1) * 100})
    return {"state": "complete", "cash_end": cash, "fills": fills, "skips": dict(skips)}


def replay(days: list[dict], policy: str, *, cost_bps: int, fill_fraction: float) -> dict:
    if not days or policy not in POLICIES or cost_bps not in (45, 75, 120):
        raise ValueError("invalid frozen replay scope")
    decisions = [day["decision_session"] for day in days]
    if (decisions != sorted(set(decisions))
            or any(day["entry_session"] >= "2026-08-05"
                   or not market_clock.consecutive_sessions("kr", day["decision_session"], day["entry_session"])
                   for day in days)
            or any(not market_clock.consecutive_sessions("kr", before, after)
                   for before, after in zip(decisions, decisions[1:]))):
        raise ValueError("protected period or missing replay sessions")
    cash = peak = 10_000_000.0
    side_cost = cost_bps / 20_000
    curve, fills, skipped = [], [], Counter()
    pnl_by_ticker = defaultdict(float)
    max_drawdown = 0.0
    missed_winners = seen_winners = 0
    for day in days:
        selected = day["selected"][policy]
        orders = plan_orders(selected, cash, side_cost)
        result = execute_day(orders, day["next_bars"], cash=cash,
                             side_cost=side_cost, fill_fraction=fill_fraction)
        if result["state"] != "complete":
            return {"state": result["state"], "session": day["entry_session"],
                    "ticker": result["ticker"], "net_return_pct": None,
                    "trade_count": len(fills), "daily": curve}
        previous_cash, cash = cash, result["cash_end"]
        peak = max(peak, cash)
        max_drawdown = min(max_drawdown, cash / peak - 1)
        bought = {fill["ticker"] for fill in result["fills"]}
        for ticker in day["feature_tickers"]:
            bar = day["next_bars"].get(ticker) or {}
            if (_positive(bar.get("open")) and _positive(bar.get("close"))
                    and bar["close"] / bar["open"] - 1 >= 0.03):
                seen_winners += 1
                missed_winners += ticker not in bought
        for fill in result["fills"]:
            pnl_by_ticker[fill["ticker"]] += fill["net_pnl"]
            fills.append({"session": day["entry_session"], **fill})
        skipped.update(result["skips"])
        curve.append({"decision_session": day["decision_session"],
                      "session": day["entry_session"], "cash": cash,
                      "return_pct": (cash / previous_cash - 1) * 100,
                      "selected": len(selected), "fills": len(result["fills"]),
                      "buy_exposure": sum(fill["buy_notional"] for fill in result["fills"]) / previous_cash})
    impact = sum(abs(pnl) for pnl in pnl_by_ticker.values())
    return {"state": "complete", "net_return_pct": (cash / 10_000_000 - 1) * 100,
            "max_drawdown_pct": max_drawdown * 100, "sessions": len(days),
            "active_sessions": sum(point["fills"] > 0 for point in curve),
            "trade_count": len(fills), "distinct_tickers": len(pnl_by_ticker),
            "positive_trade_count": sum(fill["net_pnl"] > 0 for fill in fills),
            "average_daily_exposure": statistics.mean(point["buy_exposure"] for point in curve),
            "worst_trade_pct": min((fill["net_trade_pct"] for fill in fills), default=None),
            "largest_ticker_absolute_pnl_share": max(
                (abs(pnl) / impact for pnl in pnl_by_ticker.values()), default=None) if impact else None,
            "observed_ge3pct_open_close_names": seen_winners,
            "missed_ge3pct_open_close_names": missed_winners,
            "skips": dict(skipped), "daily": curve, "fills": fills}


def grade_validation(windows: dict[str, dict], policy: str) -> dict:
    """One fixed descriptive screen; not a significance or live-promotion test."""
    if policy not in CHALLENGERS or len(windows) != 4:
        raise ValueError("expected a fixed challenger and four validation blocks")
    primary = [window["75bp_full"][policy] for window in windows.values()]
    if any(row["state"] != "complete" for row in primary):
        return {"decision": "inconclusive", "reason": "incomplete_price_path"}
    if (sum(row["trade_count"] for row in primary) < 60
            or sum(row["active_sessions"] for row in primary) < 30):
        return {"decision": "inconclusive", "reason": "insufficient_activity"}
    stress = [window["120bp_full"][policy] for window in windows.values()]
    if any(row["state"] != "complete" for row in stress):
        return {"decision": "inconclusive", "reason": "incomplete_stress_path"}
    checks = {
        "at_least_three_positive_blocks": sum(row["net_return_pct"] > 0 for row in primary) >= 3,
        "mean_block_net_positive": statistics.mean(row["net_return_pct"] for row in primary) > 0,
        "worst_block_at_least_minus5": min(row["net_return_pct"] for row in primary) >= -5,
        "mean_120bp_net_positive": statistics.mean(row["net_return_pct"] for row in stress) > 0,
    }
    for control in ("legacy_price", "cap_control"):
        controls = [window["75bp_full"][control] for window in windows.values()]
        if any(row["state"] != "complete" for row in controls):
            return {"decision": "inconclusive", "reason": "incomplete_control_path"}
        checks[f"mean_excess_over_{control}_positive"] = statistics.mean(
            candidate["net_return_pct"] - baseline["net_return_pct"]
            for candidate, baseline in zip(primary, controls)) > 0
    return {"decision": "shadow_candidate" if all(checks.values()) else "rejected",
            "checks": checks, "live_order_eligible": False}
