"""Read-only corporate-action source queue for *filled* C-level research paths.

This reports separate filled holding episodes, not an action certificate,
verified total return, historical eight-factor replay, or OOS.
The frozen candidate denominator remains in the separate 1,488-row ledger.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from scripts.measure.holding_horizon_portfolio_lab import SIDE_COST
from scripts.measure.krx_price_replay_pilot import ROOT
from scripts.measure.price_challenger_lab import HOLDOUT_WINDOWS, PILOTS
from scripts.measure.research_daily_foundation import _frozen_inputs, build_snapshots
from signal_desk.signals.research_daily_simulator import DailyCostModel, simulate_daily
from signal_desk.signals.research_decision import DailyPolicy


def audit(data_dir: Path) -> dict:
    reviewed_path = ROOT / "docs/research-reviewed-share-count-changes-2026-10-12.json"
    reviewed_bytes = reviewed_path.read_bytes()
    reviewed = json.loads(reviewed_bytes)
    if (reviewed.get("schema") != "research-reviewed-share-count-change-v1"
            or len(reviewed.get("facts", [])) != 7):
        raise ValueError("selected reviewed-action source changed")
    episodes = []
    for window in (*PILOTS, *(name for name, _, _ in HOLDOUT_WINDOWS)):
        pilot, days, sources, pilot_sha256, start, end = _frozen_inputs(data_dir, window)
        for horizon in (5, 20):
            snapshots = build_snapshots(pilot, days, sources, pilot_hash=pilot_sha256,
                                        start=start, end=end, horizon=horizon)
            result = simulate_daily(snapshots, policy=DailyPolicy(
                max_positions=6, hold_sessions=horizon), costs=DailyCostModel(
                    buy_fraction=SIDE_COST, sell_fraction=SIDE_COST))
            if result["state"] != "complete":
                raise ValueError(f"price-only path incomplete: {window} h{horizon}")
            open_buys: dict[str, dict] = {}
            for fill in result["fills"]:
                ticker = fill["ticker"]
                if fill["side"] == "buy":
                    if ticker in open_buys:
                        raise ValueError(f"overlapping filled path: {window} h{horizon} {ticker}")
                    open_buys[ticker] = fill
                elif fill["side"] == "sell":
                    buy = open_buys.pop(ticker, None)
                    if (buy is None or buy["session"] > fill["session"]
                            or buy["shares"] != fill["shares"]):
                        raise ValueError(f"invalid filled path: {window} h{horizon} {ticker}")
                    episodes.append({
                        "window": window, "horizon": horizon, "ticker": ticker,
                        "buy_session": buy["session"], "sell_session": fill["session"],
                        "selected_reviewed_actions": [fact["event_ref"]
                            for fact in reviewed["facts"]
                            if fact["ticker"] == ticker
                            and buy["session"] <= fact["effective_on"] <= fill["session"]],
                        "pilot_sha256": pilot_sha256, "coverage": "partial",
                    })
                else:
                    raise ValueError(f"unknown fill side: {fill['side']}")
            if open_buys:
                raise ValueError(f"unclosed filled path: {window} h{horizon}")
    return {
        "schema": "research-traded-action-scope-v1", "market": "kr",
        "source_level": "C_price_only_rank_proxy", "registered_verdict": False,
        "live_order_eligible": False, "verified_total_return": False,
        "windows": len(PILOTS) + len(HOLDOUT_WINDOWS), "episodes": episodes,
        "tickers": sorted({row["ticker"] for row in episodes}),
        "selected_reviewed_source_sha256": hashlib.sha256(reviewed_bytes).hexdigest(),
        "selected_reviewed_action_refs_on_filled_paths": sorted({
            ref for row in episodes for ref in row["selected_reviewed_actions"]}),
        "limitations": [
            "Filled holding episodes only; unfilled candidates still need historical input and rank checks.",
            "Seven selected share-count facts are not an exhaustive action inventory.",
            "Source completeness, corrections, ex-dates and later cash settlement are not certified.",
            "The inspected windows are development data, not independent OOS evidence.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data/research")
    args = parser.parse_args()
    print(json.dumps(audit(args.data_dir), ensure_ascii=False, sort_keys=True,
                     allow_nan=False))


if __name__ == "__main__":
    main()
