"""Research-only cash/slot replay of C-level price signals; no live state writes.

Both arms see the same decision-day signal stream. Entry and exit are at the
following observed session's open, never at the signal close. This is not the
production eight-factor engine, its exit policy, or an order recommendation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path

from scripts.measure.krx_price_replay_pilot import ROOT, _number, load_krx_archives, run_pilot
from scripts.measure.price_challenger_lab import HOLDOUT_ARCHIVES, HOLDOUT_WINDOWS, PricePanel

START_CASH = 10_000_000.0
MAX_POSITIONS = 6
SIDE_COST = 0.00125
HORIZONS = (5, 20)


def _open_price(panel: PricePanel, day: str, ticker: str) -> float | None:
    """Execution can depend on the observed open, not the same day's future close."""
    opened = panel.bars.get((day, ticker), (None, None))[0]
    return opened if opened is not None and opened > 0 else None


def first_buy_decisions(pilot: dict, panel: PricePanel) -> dict[str, list[dict]]:
    """Only observed non-buy -> buy transitions inside the daily top-six rank."""
    dates = pilot["signal_dates"]
    if not dates or max(dates) >= "2026-08-05":
        raise ValueError("registered period cannot be used for portfolio selection")
    if pilot.get("source_level") != "C_offline_price_only_current_engine" or pilot.get("registered_verdict") is not False:
        raise ValueError("not a C-level replay")
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    by_day: dict[str, list[dict]] = defaultdict(list)
    for row in pilot["rows"]:
        by_ticker[row["ticker"]].append(row)
        by_day[row["date"]].append(row)
    top_six = {day: {row["ticker"] for row in sorted(
        rows, key=lambda value: (-value["score"], value["ticker"]))[:MAX_POSITIONS]}
        for day, rows in by_day.items()}
    selected: dict[str, list[dict]] = defaultdict(list)
    for ticker, rows in by_ticker.items():
        previous = None
        for row in sorted(rows, key=lambda value: value["date"]):
            is_buy = row["kind"] in {"BUY", "STRONG_BUY"}
            if (is_buy and ticker in top_six[row["date"]] and previous is not None
                    and panel.index.get(row["date"], -2) == panel.index.get(previous["date"], -4) + 1
                    and previous["kind"] not in {"BUY", "STRONG_BUY"}):
                selected[row["date"]].append({"ticker": ticker, "score": row["score"]})
            previous = row
    for candidates in selected.values():
        candidates.sort(key=lambda item: (-item["score"], item["ticker"]))
    return selected


def replay_portfolio(panel: PricePanel, candidates: dict[str, list[dict]], *,
                     start: str, end: str, horizon: int, side_cost: float = SIDE_COST,
                     share_counts: dict[tuple[str, str], int | None] | None = None) -> dict:
    """Six equal equity slots, integer shares, fixed open-to-open holding period.

    If an owned bar is missing, the path is incomplete: never invent valuation
    or claim a portfolio return. A missing next open for a candidate is a
    skipped buy, while a missing scheduled sell open invalidates the path.
    """
    if horizon < 1 or start > end or end >= "2026-08-05":
        raise ValueError("invalid or protected portfolio window")
    if not (0 <= side_cost < 1) or start not in panel.index or end not in panel.index:
        raise ValueError("invalid cost or uncovered window")
    cash = START_CASH
    positions: dict[str, dict] = {}
    pending: list[dict] = []
    fills: list[dict] = []
    skips: dict[str, int] = defaultdict(int)
    equity_curve: list[dict] = []
    final_decision_index = panel.index[end]
    for index in range(panel.index[start], len(panel.sessions)):
        day = panel.sessions[index]
        # Close-based decisions from the previous session are executed only now.
        for ticker, position in list(positions.items()):
            if (share_counts is not None
                    and (position["entry_shares_outstanding"] is None
                         or share_counts.get((day, ticker)) != position["entry_shares_outstanding"])):
                return {"state": "incomplete_share_history", "day": day, "ticker": ticker,
                        "horizon": horizon, "fills": len(fills), "skips": dict(skips)}
            if index < position["exit_index"]:
                continue
            opened = _open_price(panel, day, ticker)
            if opened is None:
                return {"state": "incomplete_exit_open", "day": day, "ticker": ticker,
                        "horizon": horizon, "fills": len(fills), "skips": dict(skips)}
            cash += position["shares"] * opened * (1 - side_cost)
            fills.append({"side": "sell", "day": day, "ticker": ticker,
                          "price": opened, "shares": position["shares"]})
            del positions[ticker]
        for order in pending:
            ticker = order["ticker"]
            if ticker in positions:
                skips["already_held"] += 1
                continue
            if len(positions) >= MAX_POSITIONS:
                skips["full_slots"] += 1
                continue
            opened = _open_price(panel, day, ticker)
            if opened is None:
                skips["missing_entry_open"] += 1
                continue
            # At the open, value existing positions at their observed opens.
            open_equity = cash
            for held_ticker, held in positions.items():
                held_open = _open_price(panel, day, held_ticker)
                if held_open is None:
                    return {"state": "incomplete_held_open", "day": day, "ticker": held_ticker,
                            "horizon": horizon, "fills": len(fills), "skips": dict(skips)}
                open_equity += held["shares"] * held_open
            allocation = min(cash, open_equity / MAX_POSITIONS)
            shares = int(allocation / (opened * (1 + side_cost)))
            if shares < 1:
                skips["insufficient_cash"] += 1
                continue
            cash -= shares * opened * (1 + side_cost)
            positions[ticker] = {"shares": shares, "entry_index": index,
                                 "exit_index": index + horizon,
                                 "entry_shares_outstanding": (
                                     share_counts.get((day, ticker)) if share_counts is not None else None)}
            fills.append({"side": "buy", "day": day, "ticker": ticker,
                          "price": opened, "shares": shares})
        pending = candidates.get(day, []) if start <= day <= end else []
        if index == len(panel.sessions) - 1 and pending:
            return {"state": "incomplete_next_open", "day": day, "horizon": horizon,
                    "fills": len(fills), "skips": dict(skips)}
        equity = cash
        for ticker, position in positions.items():
            if (share_counts is not None
                    and (position["entry_shares_outstanding"] is None
                         or share_counts.get((day, ticker)) != position["entry_shares_outstanding"])):
                return {"state": "incomplete_share_history", "day": day, "ticker": ticker,
                        "horizon": horizon, "fills": len(fills), "skips": dict(skips)}
            bar = panel.bar(day, ticker)
            if bar is None:
                return {"state": "incomplete_held_close", "day": day, "ticker": ticker,
                        "horizon": horizon, "fills": len(fills), "skips": dict(skips)}
            equity += position["shares"] * bar[1]
        equity_curve.append({"day": day, "equity": equity})
        if index > final_decision_index and not positions and not pending:
            break
    if positions or pending:
        return {"state": "unmatured", "horizon": horizon, "positions": len(positions),
                "pending": len(pending), "skips": dict(skips)}
    peak = START_CASH
    max_drawdown = 0.0
    for point in equity_curve:
        peak = max(peak, point["equity"])
        max_drawdown = min(max_drawdown, point["equity"] / peak - 1)
    buys = [fill for fill in fills if fill["side"] == "buy"]
    return {"state": "complete", "horizon": horizon, "first_day": start,
            "last_day": equity_curve[-1]["day"], "decision_days": final_decision_index - panel.index[start] + 1,
            "entry_candidates": sum(len(rows) for day, rows in candidates.items() if start <= day <= end),
            "cash_end": round(cash, 2), "net_return_pct": round((cash / START_CASH - 1) * 100, 4),
            "max_drawdown_pct": round(max_drawdown * 100, 4),
            "buy_fills": len(buys), "sell_fills": len(fills) - len(buys),
            "distinct_tickers_bought": len({fill["ticker"] for fill in buys}),
            "gross_buy_notional": round(sum(fill["price"] * fill["shares"] for fill in buys), 2),
            "skips": dict(sorted(skips.items())), "fills": fills}


def run_holdout(data_dir: Path) -> dict:
    days, sources = load_krx_archives([data_dir / name for name in HOLDOUT_ARCHIVES])
    panel = PricePanel(days)
    share_counts = {}
    for day, rows in days.items():
        for row in rows:
            value = _number(row.get("LIST_SHRS"))
            share_counts[day, str(row.get("ISU_CD") or "")] = (
                int(value) if value is not None and value > 0 and value.is_integer() else None)
    windows = {}
    for name, start, end in HOLDOUT_WINDOWS:
        pilot = run_pilot(days, start=start, end=end)
        decisions = first_buy_decisions(pilot, panel)
        windows[name] = {f"hold{h}": replay_portfolio(
            panel, decisions, start=start, end=end, horizon=h, share_counts=share_counts)
                         for h in HORIZONS}
    return {"schema": "holding-horizon-portfolio-development-v1", "market": "kr",
            "source_level": "C_price_only", "registered_verdict": False,
            "live_order_eligible": False, "starting_cash_krw": START_CASH,
            "max_positions": MAX_POSITIONS, "side_cost_pct": SIDE_COST * 100,
            "horizons_open_to_open_sessions": list(HORIZONS), "sources": sources,
            "code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "windows": windows,
            "limitations": [
                "This is an already-inspected historical development cohort, not independent OOS.",
                "Price-only signals and retrospective top-200 universe omit the production eight-factor and order gates.",
                "No verified split/dividend adjustment, slippage, spread, depth, tax, or partial fills.",
                "Observed listed-share changes/missingness invalidate a held path; unchanged shares do not verify dividends.",
                "The 5/20 open-to-open exit is a research policy, not the paper bot's actual exit rule.",
            ]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data/research")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output exists; research result is never overwritten")
    result = run_holdout(args.data_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({"output": str(args.output),
                      "states": {name: {arm: item["state"] for arm, item in arms.items()}
                                 for name, arms in result["windows"].items()},
                      "registered_verdict": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
