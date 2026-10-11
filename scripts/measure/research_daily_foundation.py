"""Read-only bridge from frozen C-level KRX archives to the new daily kernel.

This bridge inherits the old price-only pilot's score/ticker tie-break proxy.
It cannot recover the original eight-factor ranks or verify historical source
arrival time. Its paired result is a development consistency check only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from zipfile import ZipFile
from zoneinfo import ZoneInfo

from scripts.measure.holding_horizon_portfolio_lab import (
    SIDE_COST, first_buy_decisions, replay_portfolio,
)
from scripts.measure.krx_price_replay_pilot import ROOT, _number, load_krx_archives, run_pilot
from scripts.measure.price_challenger_lab import (
    ARCHIVES, HOLDOUT_ARCHIVES, HOLDOUT_WINDOWS, PILOTS, PricePanel,
)
from signal_desk import market_clock
from signal_desk.signals.research_daily_simulator import DailyCostModel, simulate_daily
from signal_desk.signals.research_decision import (
    Candidate, CompletedDailyBar, DailyPolicy, DailySnapshot,
)

def _source_ids(paths: list[Path]) -> dict[str, str]:
    source_by_day = {}
    for path in paths:
        with ZipFile(path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        for entry in manifest["entries"].values():
            source_by_day[entry["session"]] = entry["sha256"]
    return source_by_day


def _frozen_inputs(data_dir: Path, window: str) -> tuple[dict, dict, dict, str, str, str]:
    earlier = {name: (start, end) for name, start, end in HOLDOUT_WINDOWS}
    if window in PILOTS:
        pilot_name, expected_hash = PILOTS[window]
        pilot_bytes = (data_dir / pilot_name).read_bytes()
        pilot_hash = hashlib.sha256(pilot_bytes).hexdigest()
        if pilot_hash != expected_hash:
            raise ValueError("frozen pilot SHA mismatch")
        pilot = json.loads(pilot_bytes)
        paths = [data_dir / name for name in ARCHIVES]
        days, sources = load_krx_archives(paths)
        if sources != pilot.get("sources"):
            raise ValueError("price archive differs from the frozen pilot provenance")
        start, end = pilot["signal_dates"][0], pilot["signal_dates"][-1]
    elif window in earlier:
        start, end = earlier[window]
        paths = [data_dir / name for name in HOLDOUT_ARCHIVES]
        days, _sources = load_krx_archives(paths)
        pilot = run_pilot(days, start=start, end=end)
        # The prior holdout was already inspected. Its fresh code-level replay
        # gets a distinct digest and never becomes independent test evidence.
        pilot_hash = hashlib.sha256(json.dumps(pilot, sort_keys=True,
                                              allow_nan=False).encode()).hexdigest()
    else:
        raise ValueError("unknown inspected development window")
    if (pilot.get("source_level") != "C_offline_price_only_current_engine"
            or pilot.get("registered_verdict") is not False
            or pilot.get("market") != "kr"
            or not isinstance(pilot.get("signal_dates"), list)
            or not pilot["signal_dates"]
            or pilot["signal_dates"] != sorted(set(pilot["signal_dates"]))
            or pilot["signal_dates"][0] != start
            or pilot["signal_dates"][-1] != end or end >= "2026-08-05"):
        raise ValueError("pilot is not the expected inspected development window")
    source_by_day = _source_ids(paths)
    return pilot, days, source_by_day, pilot_hash, start, end


def build_snapshots(pilot: dict, days: dict, source_by_day: dict[str, str], *,
                    pilot_hash: str, start: str, end: str,
                    horizon: int) -> list[DailySnapshot]:
    """Expose only current-session bars and already recorded first-buy episodes."""
    panel = PricePanel(days)
    decisions = first_buy_decisions(pilot, panel)
    affected = {row["ticker"] for candidates in decisions.values() for row in candidates}
    rows_by_day: dict[str, list[dict]] = defaultdict(list)
    for row in pilot["rows"]:
        if start <= row["date"] <= end:
            rows_by_day[row["date"]].append(row)
    rank_by_day = {
        day: {row["ticker"]: index for index, row in enumerate(
            sorted(rows, key=lambda item: (-item["score"], item["ticker"])), 1)}
        for day, rows in rows_by_day.items()
    }
    last_needed = market_clock.next_sessions("kr", end, horizon + 1)
    if len(last_needed) != horizon + 1 or last_needed[-1] not in panel.index:
        raise ValueError("frozen source does not cover the last possible exit open")
    sessions = [day for day in panel.sessions if start <= day <= last_needed[-1]]
    snapshots = []
    for day in sessions:
        # The 16:30 time is a study assumption; the KRX archive's actual arrival
        # time was not recorded and is not claimed as observed availability.
        as_of = dt.datetime.combine(dt.date.fromisoformat(day), dt.time(16, 30),
                                    tzinfo=ZoneInfo("Asia/Seoul"))
        bars = []
        for raw in days[day]:
            ticker = str(raw.get("ISU_CD") or "")
            if ticker not in affected:
                continue
            raw_shares = _number(raw.get("LIST_SHRS"))
            opened = _number(raw.get("TDD_OPNPRC"))
            closed = _number(raw.get("TDD_CLSPRC"))
            # Zero/blank source fields mean no verified execution/mark price.
            # Keep the row and its source ID; never interpolate a missing bar.
            opened = opened if opened is not None and opened > 0 else None
            closed = closed if closed is not None and closed > 0 else None
            shares = (int(raw_shares) if raw_shares is not None and raw_shares > 0
                      and raw_shares.is_integer() else None)
            bars.append(CompletedDailyBar(
                "kr", day, ticker, opened, closed, as_of, source_by_day[day],
                status="trading" if opened is not None and closed is not None else "unknown",
                listed_shares=shares,
            ))
        candidates = tuple(Candidate(
            ticker=row["ticker"], rank=rank_by_day[day][row["ticker"]],
            score=float(row["score"]), eligible=True,
            thesis_id=f"C-first-buy:{day}:{row['ticker']}",
            reason="stored_price_only_first_buy_score_ticker_rank_proxy",
        ) for row in decisions.get(day, []))
        input_payload = {"schema": "C-daily-input-v1", "pilot_sha256": pilot_hash,
                         "raw_session_sha256": source_by_day[day], "session": day,
                         "candidates": [asdict(item) for item in candidates]}
        input_id = hashlib.sha256(json.dumps(input_payload, sort_keys=True,
                                             separators=(",", ":")).encode()).hexdigest()
        snapshots.append(DailySnapshot(
            "kr", day, as_of, f"C-daily-input-v1:{input_id}", tuple(bars), candidates,
            source_level="C_price_only_rank_proxy_simulated_availability",
        ))
    return snapshots


def run_development(data_dir: Path, *, window: str = "2026q2early-v5",
                    horizon: int = 5) -> dict:
    if horizon not in (5, 20):
        raise ValueError("only the frozen h5/h20 development horizons are supported")
    pilot, days, source_by_day, pilot_hash, start, end = _frozen_inputs(data_dir, window)
    snapshots = build_snapshots(pilot, days, source_by_day, pilot_hash=pilot_hash,
                                start=start, end=end, horizon=horizon)
    policy = DailyPolicy(max_positions=6, hold_sessions=horizon)
    result = simulate_daily(snapshots, policy=policy, costs=DailyCostModel(
        buy_fraction=SIDE_COST, sell_fraction=SIDE_COST))
    old_share_counts = None
    if window not in PILOTS:
        old_share_counts = {}
        for day, raw_rows in days.items():
            for raw in raw_rows:
                shares = _number(raw.get("LIST_SHRS"))
                old_share_counts[day, str(raw.get("ISU_CD") or "")] = (
                    int(shares) if shares is not None and shares > 0 and shares.is_integer()
                    else None)
    panel = PricePanel(days)
    old = replay_portfolio(panel, first_buy_decisions(pilot, panel), start=start,
                           end=end, horizon=horizon, share_counts=old_share_counts)
    old_trades = [(item["side"], item["day"], item["ticker"],
                   item["price"], item["shares"]) for item in old.get("fills", [])]
    new_trades = [(item["side"], item["session"], item["ticker"],
                   item["price"], item["shares"]) for item in result["fills"]]
    parity = (result["state"] == old["state"] == "complete"
              and new_trades == old_trades
              and result["net_return_pct"] == old.get("net_return_pct"))
    source_ids = sorted(result["source_ids"])
    source_digest = hashlib.sha256("\n".join(source_ids).encode()).hexdigest()
    old_fills = old.get("fills")
    return {"schema": "research-daily-foundation-development-v1", "market": "kr",
            "window": window, "horizon": horizon, "source_level": "C_price_only_rank_proxy",
            "registered_verdict": False, "live_order_eligible": False,
            "pilot_sha256": pilot_hash, "raw_source_count": len(source_ids),
            "raw_source_ids_sha256": source_digest,
            "sessions": len(snapshots), "result_state": result["state"],
            "old_state": old["state"], "new_net_return_pct": result["net_return_pct"],
            "old_net_return_pct": old.get("net_return_pct"),
            "legacy_replay_parity": parity,
            "cash_baseline_pct": 0.0,
            "new_fills": len(result["fills"]),
            "old_fills": old_fills if isinstance(old_fills, int) else len(old_fills or []),
            "new_full_fill_assumptions": result.get("full_fill_assumptions"),
            "research_kernel_sha256": hashlib.sha256(b"".join(
                (ROOT / relative).read_bytes() for relative in (
                    "src/signal_desk/signals/research_decision.py",
                    "src/signal_desk/signals/research_daily_simulator.py",
                    "scripts/measure/research_daily_foundation.py",
                ))).hexdigest(),
            "limitations": [
                "Already inspected development dates; this is not independent OOS.",
                "Original eight-factor inputs, historical gate, and source arrival times are not recovered.",
                "Score/ticker tie-break is a rank proxy; actual rank was not preserved in the audit export.",
                "Completed daily prices do not prove opening auction fills or corporate-action-free total return.",
            ]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data/research")
    parser.add_argument("--window", choices=(*PILOTS, *(name for name, _, _ in HOLDOUT_WINDOWS)),
                        default="2026q2early-v5")
    parser.add_argument("--horizon", type=int, default=5, choices=(5, 20))
    args = parser.parse_args()
    print(json.dumps(run_development(args.data_dir, window=args.window, horizon=args.horizon),
                     ensure_ascii=False, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
