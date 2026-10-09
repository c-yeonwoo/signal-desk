"""Read-only audit of *recorded* daily signals against later unadjusted price bars.

This is an exploratory diagnostic. It does not reconstruct missing factors or run the
registered harness, and its output is never a trading or promotion verdict.
"""

from __future__ import annotations

import math
from collections import defaultdict
from statistics import mean, median

import pandas as pd

from signal_desk import market_clock

HORIZONS = (1, 5, 20)
FACTOR_COLUMNS = ("technical", "fundamental", "valuation", "reversion",
                  "flow", "quality", "momentum", "short")
MAJOR_KR_TICKERS = ("005930", "000660", "005380", "000270", "035420",
                    "035720", "373220", "207940", "267250", "105560")
MAJOR_US_TICKERS = ("AAPL", "MSFT", "NVDA", "AMZN", "GOOGL",
                    "META", "TSLA", "JPM", "AVGO", "WMT")
EXPORT_SIGNAL_COLUMNS = ("date", "ticker", "score", "kind", *FACTOR_COLUMNS,
                         "qualitative", "rank", "observed_at", "bar_asof")
EXPORT_PRICE_COLUMNS = ("date", "ticker", "open", "close", "volume")


def select_recorded_inputs(signals: pd.DataFrame, prices: pd.DataFrame, *,
                           market: str, sessions: int = 45) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Bounded, read-only export. Never backfill or synthesize absent rows."""
    if market not in {"kr", "us"} or not 1 <= sessions <= 60:
        raise ValueError("invalid market or session count")
    if not {"date", "ticker", "score", "kind"} <= set(signals.columns):
        raise ValueError("signal history lacks required columns")
    if not {"date", "ticker", "open", "close"} <= set(prices.columns):
        raise ValueError("price history lacks required columns")
    frame = signals.copy()
    if "market" in frame:
        frame = frame[frame["market"].fillna("kr").astype(str) == market]
    elif market == "us":
        frame = frame.iloc[0:0]
    frame["date"] = frame["date"].astype(str)
    dates = sorted(frame["date"].unique())[-sessions:]
    frame = frame[frame["date"].isin(dates)]
    if len(frame) > 35000:
        raise ValueError("signal export exceeds row budget")
    frame = frame[[name for name in EXPORT_SIGNAL_COLUMNS if name in frame]].copy()
    bars = prices.copy()
    bars["date"] = bars["date"].astype(str)
    bars["ticker"] = bars["ticker"].astype(str)
    if dates:
        tickers = set(frame["ticker"].astype(str))
        bars = bars[bars["date"].ge(dates[0]) & bars["ticker"].isin(tickers)]
    else:
        bars = bars.iloc[0:0]
    if len(bars) > 45000:
        raise ValueError("price export exceeds row budget")
    bars = bars[[name for name in EXPORT_PRICE_COLUMNS if name in bars]].copy()
    return frame.reset_index(drop=True), bars.reset_index(drop=True)


def _number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _score_results(rows: list[dict], horizon: int) -> dict:
    ready = [row for row in rows if row["outcomes"][str(horizon)]["state"] == "matured"]
    statuses: dict[str, int] = defaultdict(int)
    for row in rows:
        statuses[row["outcomes"][str(horizon)]["state"]] += 1
    if not ready:
        return {"observations": len(rows), "matured": 0, "dates": 0,
                "statuses": dict(sorted(statuses.items()))}
    net = [row["outcomes"][str(horizon)]["net_pct"] for row in ready]
    excess = [row["outcomes"][str(horizon)]["excess_pct"] for row in ready
              if row["outcomes"][str(horizon)]["excess_pct"] is not None]
    return {
        "observations": len(rows), "matured": len(ready),
        "dates": len({row["date"] for row in ready}),
        "mean_net_pct": round(mean(net), 4),
        "median_net_pct": round(median(net), 4),
        "mean_excess_pct": round(mean(excess), 4) if excess else None,
        "loss_count": sum(value < 0 for value in net),
        "loss_10pct_count": sum(value <= -10 for value in net),
        "statuses": dict(sorted(statuses.items())),
    }


def audit_snapshots(signals: pd.DataFrame, prices: pd.DataFrame, *, market: str,
                    major_tickers: tuple[str, ...] = (),
                    score_delta_threshold: float = 0.5,
                    roundtrip_cost_pct: float = 0.25) -> dict:
    """Evaluate every observed ticker-date; report subsets without dropping the denominator.

    Signals are historical observations. The next *scheduled* session's open is entry,
    and its h-th session's close is exit. A missing intermediate bar stays missing.
    All rows and reasons are returned; callers decide where to store research output.
    """
    if market not in {"kr", "us"}:
        raise ValueError("market must be kr or us")
    if score_delta_threshold < 0 or roundtrip_cost_pct < 0:
        raise ValueError("negative threshold or cost")
    if not {"date", "ticker", "score", "kind"} <= set(signals.columns):
        raise ValueError("signal history lacks required columns")
    if not {"date", "ticker", "open", "close"} <= set(prices.columns):
        raise ValueError("price history lacks required columns")
    snapshots = signals.copy()
    bars = prices.copy()
    for frame in (snapshots, bars):
        frame["date"] = frame["date"].astype(str)
        frame["ticker"] = frame["ticker"].astype(str)
    if snapshots.duplicated(["date", "ticker"]).any():
        raise ValueError("duplicate signal ticker-date: select one archived observation first")
    if bars.duplicated(["date", "ticker"]).any():
        raise ValueError("duplicate price ticker-date: price version is ambiguous")
    if snapshots.empty:
        return {"market": market, "source_level": "legacy_snapshot_unverified", "rows": [],
                "summary": {"all": {str(h): _score_results([], h) for h in HORIZONS}}}

    bars_by_ticker = {
        ticker: {str(row["date"]): row for row in group.to_dict("records")}
        for ticker, group in bars.groupby("ticker", sort=False)
    }
    last_price_day = max(bars["date"]) if not bars.empty else ""
    major_set = set(major_tickers)
    expected: dict[str, list[str]] = {}
    rows: list[dict] = []
    prior_by_ticker: dict[str, dict] = {}
    for signal in snapshots.sort_values(["date", "ticker"]).to_dict("records"):
        day, ticker = signal["date"], signal["ticker"]
        prior = prior_by_ticker.get(ticker)
        score = _number(signal.get("score"))
        prior_score = _number(prior.get("score")) if prior else None
        delta = (round(score - prior_score, 4)
                 if score is not None and prior_score is not None else None)
        kind_change = prior is not None and signal.get("kind") != prior.get("kind")
        score_change = delta is not None and abs(delta) >= score_delta_threshold
        gap = bool(prior and not market_clock.consecutive_sessions(market, prior["date"], day))
        prior_by_ticker[ticker] = signal
        factor_changes = {}
        if prior:
            for name in FACTOR_COLUMNS:
                current, previous = _number(signal.get(name)), _number(prior.get(name))
                if current is not None and previous is not None:
                    factor_changes[name] = round(current - previous, 4)

        if day not in expected:
            expected[day] = market_clock.next_sessions(market, day, max(HORIZONS))
        sessions = expected[day]
        outcomes = {}
        ticker_bars = bars_by_ticker.get(ticker, {})
        for h in HORIZONS:
            if not sessions:
                outcomes[str(h)] = {"state": "invalid_signal_session"}
                continue
            entry_day, exit_day = sessions[0], sessions[h - 1]
            if last_price_day < exit_day:
                outcomes[str(h)] = {"state": "not_matured", "exit_date": exit_day}
                continue
            entry = ticker_bars.get(entry_day)
            entry_px = _number(entry.get("open")) if entry else None
            if entry_px is None or entry_px <= 0:
                outcomes[str(h)] = {"state": "missing_entry_open", "entry_date": entry_day}
                continue
            closes = []
            for session in sessions[:h]:
                bar = ticker_bars.get(session)
                px = _number(bar.get("close")) if bar else None
                if px is None or px <= 0:
                    break
                closes.append(px)
            if len(closes) != h:
                outcomes[str(h)] = {"state": "price_gap", "entry_date": entry_day,
                                    "exit_date": exit_day, "first_missing_date": sessions[len(closes)]}
                continue
            net = (closes[-1] / entry_px - 1.0) * 100 - roundtrip_cost_pct
            outcomes[str(h)] = {
                "state": "matured", "entry_date": entry_day, "exit_date": exit_day,
                "entry_open": entry_px, "exit_close": closes[-1],
                "gross_pct": round((closes[-1] / entry_px - 1.0) * 100, 4),
                "net_pct": round(net, 4),
                "close_mae_pct": round((min([entry_px, *closes]) / entry_px - 1.0) * 100, 4),
                "close_mfe_pct": round((max([entry_px, *closes]) / entry_px - 1.0) * 100, 4),
                "excess_pct": None,
            }
        rows.append({
            "market": market, "date": day, "ticker": ticker,
            "kind": str(signal.get("kind")), "score": score,
            "previous_date": prior["date"] if prior else None,
            "previous_kind": str(prior.get("kind")) if prior else None,
            "score_delta": delta, "kind_change": kind_change,
            "score_change": score_change, "snapshot_gap": gap,
            "major": ticker in major_set, "factor_changes": factor_changes,
            "outcomes": outcomes,
        })

    # The comparison universe is all recorded tickers *on the same decision day*.
    # It is an observed-cohort benchmark, not the KOSPI/S&P 500 or a causal control.
    grouped: dict[tuple[str, int], list[float]] = defaultdict(list)
    for row in rows:
        for h in HORIZONS:
            result = row["outcomes"][str(h)]
            if result["state"] == "matured":
                grouped[(row["date"], h)].append(result["net_pct"])
    comparison = {f"{day}:{h}": {"n": len(values), "mean_net_pct": round(mean(values), 4)}
                  for (day, h), values in sorted(grouped.items())}
    for row in rows:
        for h in HORIZONS:
            result = row["outcomes"][str(h)]
            if result["state"] == "matured":
                result["excess_pct"] = round(
                    result["net_pct"] - comparison[f"{row['date']}:{h}"]["mean_net_pct"], 4)

    cohorts = {
        "all": rows,
        "buy": [row for row in rows if row["kind"] in {"BUY", "STRONG_BUY"}],
        "kind_changed": [row for row in rows if row["kind_change"]],
        "score_changed": [row for row in rows if row["score_change"]],
        "major": [row for row in rows if row["major"]],
    }
    summary = {name: {str(h): _score_results(items, h) for h in HORIZONS}
               for name, items in cohorts.items()}
    return {
        "market": market, "source_level": "legacy_snapshot_unverified",
        "method": "next_scheduled_open_to_hth_close_price_only_costed",
        "roundtrip_cost_pct": roundtrip_cost_pct,
        "score_delta_threshold": score_delta_threshold,
        "signal_dates": sorted(set(snapshots["date"])),
        "price_data_to": last_price_day,
        "signal_rows": len(rows), "snapshot_gap_rows": sum(row["snapshot_gap"] for row in rows),
        "cohort_comparison": comparison, "summary": summary, "rows": rows,
        "caveats": ["legacy signal snapshots lack verified original-source availability",
                    "raw open/close returns exclude unverified corporate actions and dividends",
                    "same-day tickers and overlapping horizons are not independent samples",
                    "comparison is only the observed ticker cohort on each signal day"],
    }
