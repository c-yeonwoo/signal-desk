"""Price-only, C-level historical replay through the current common signal engine.

This is a development diagnostic, not the registered harness or a historical live
decision. Every price is sliced at the decision session before it reaches the engine.
Other historical inputs and the post-engine execution gate are deliberately absent.
"""

from __future__ import annotations

import datetime as dt
import math
from bisect import bisect_right
from dataclasses import asdict

from signal_desk import market_clock
from signal_desk.signals import engine

_PRICE_ONLY_UNAVAILABLE = ("fundamental", "valuation", "flow", "quality", "short")


def replay_price_only_asof(*, market: str, as_of: str, universe: list[dict],
                           closes_by: dict[str, list[float]], dates_by: dict[str, list[str]],
                           config: engine.SignalConfig | None = None) -> dict:
    """Recalculate a frozen cross-section without giving the engine later bars.

    A stale/short/missing ticker remains in the denominator and is not scored.
    Invalid dates or undated price tails fail the entire run instead of being guessed.
    """
    if (market not in {"kr", "us"} or not isinstance(as_of, str)
            or not market_clock.is_session(market, as_of)):
        raise ValueError("decision date is not a verified market session")
    if not isinstance(closes_by, dict) or not isinstance(dates_by, dict):
        raise ValueError("dated price panel is required")
    if not isinstance(universe, list) or not universe:
        raise ValueError("frozen universe is required")
    tickers = [item.get("ticker") for item in universe if isinstance(item, dict)]
    if (len(tickers) != len(universe)
            or any(not isinstance(t, str) or not t for t in tickers)
            or any(not isinstance(item.get("name"), str) for item in universe)):
        raise ValueError("invalid frozen universe")
    if len(tickers) != len(set(tickers)):
        raise ValueError("duplicate ticker in frozen universe")
    if config is not None and not isinstance(config, engine.SignalConfig):
        raise ValueError("explicit signal config must be a SignalConfig")
    cfg = config or engine.SignalConfig()
    minimum_history = max(cfg.ma_long, cfg.momentum_lookback + 1)
    included: dict[str, list[float]] = {}
    excluded: dict[str, str] = {}
    future_bars_removed = 0
    for ticker in tickers:
        days = dates_by.get(ticker)
        closes = closes_by.get(ticker)
        if days is None or closes is None:
            excluded[ticker] = "missing_price_history"
            continue
        if not isinstance(days, list) or not isinstance(closes, list) or len(days) != len(closes):
            raise ValueError(f"{ticker}: undated or mismatched price history")
        if not days:
            excluded[ticker] = "missing_price_history"
            continue
        if (not all(isinstance(day, str) for day in days) or days != sorted(set(days))
                or not all(market_clock.is_session(market, day) for day in days)):
            raise ValueError(f"{ticker}: invalid price sessions")
        if any(isinstance(px, bool) or not isinstance(px, (int, float))
               or not math.isfinite(px) or px <= 0 for px in closes):
            raise ValueError(f"{ticker}: invalid price")
        count = bisect_right(days, as_of)
        future_bars_removed += len(days) - count
        if not count or days[count - 1] != as_of:
            excluded[ticker] = "missing_decision_close"
        elif count < minimum_history:
            excluded[ticker] = "insufficient_history"
        else:
            included[ticker] = list(closes[:count])

    results = engine.evaluate(universe, included, fundamentals={}, sentiment={}, flows={},
                              shorts={}, earnings_dates={}, unavailable=_PRICE_ONLY_UNAVAILABLE,
                              config=cfg, today=dt.date.fromisoformat(as_of))
    return {
        "market": market, "as_of": as_of, "mode": "price_only_current_engine_no_execution_gate",
        "source_level": "C_partial_reconstruction", "strict_pit_eligible": False,
        "live_eligible": False, "registered_verdict": False,
        "frozen_universe_size": len(universe), "priced_tickers": len(included),
        "excluded_tickers": excluded, "future_bars_removed": future_bars_removed,
        "minimum_history_sessions": minimum_history,
        "rows": [asdict(result) for result in results],
        "limitations": ["historical universe and input publication versions unverified",
                        "fundamentals, flows, shorts, sentiment, earnings and events absent",
                        "post-engine execution gate and real fills not replayed"],
    }
