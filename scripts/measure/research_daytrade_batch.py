"""Offline fixed-batch daytrade comparison; no credentials or live stores.

Daily raw-source verification reuses the existing KRX archive loader. The next
session is passed only to execution, never feature extraction or selection.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import statistics
from collections import Counter, defaultdict, deque
from dataclasses import replace
from pathlib import Path

from scripts.measure.krx_price_replay_pilot import ROOT, _number, load_krx_archives
from scripts.measure.price_challenger_lab import ARCHIVES, HOLDOUT_ARCHIVES
from signal_desk import market_clock
from signal_desk.signals import engine
from signal_desk.signals.historical_replay import _PRICE_ONLY_UNAVAILABLE
from signal_desk.signals.research_daytrade import (
    CHALLENGERS, POLICIES, Features, grade_validation, replay, select,
)

PLAN = ROOT / "docs/DAYTRADE-BENCHMARK-2026-10-17.md"
DEVELOPMENT = (
    ("2024-apr-may", "2024-04-01", "2024-05-31"),
    ("2024-jun-jul", "2024-06-03", "2024-07-31"),
    ("2024-aug-sep", "2024-08-01", "2024-09-30"),
    ("2025q4", "2025-10-20", "2025-12-30"),
    ("2026q1", "2026-01-02", "2026-03-31"),
    ("2026-apr-may", "2026-04-01", "2026-05-29"),
    ("2026-jun", "2026-06-01", "2026-06-30"),
)
VALIDATION = (
    ("2025-jan-feb", "2025-01-02", "2025-02-28"),
    ("2025-mar-apr", "2025-03-04", "2025-04-30"),
    ("2025-may-jun", "2025-05-02", "2025-06-30"),
    ("2025-jul-aug", "2025-07-01", "2025-08-28"),
)
WINDOWS = DEVELOPMENT + VALIDATION


def _bar(row: dict) -> dict:
    return {key: _number(row.get(raw)) for key, raw in (
        ("open", "TDD_OPNPRC"), ("close", "TDD_CLSPRC"),
        ("high", "TDD_HGPRC"), ("low", "TDD_LWPRC"),
        ("volume", "ACC_TRDVOL"))}


def build_inputs(days: dict[str, list[dict]]) -> tuple[dict[str, list[dict]], dict]:
    sessions = sorted(days)
    if not sessions or sessions[-1] >= "2026-08-05":
        raise ValueError("protected-period or empty data in daytrade batch")
    history, volumes = defaultdict(list), defaultdict(lambda: deque(maxlen=21))
    last_index, consecutive = {}, Counter()
    compiled = {name: [] for name, _, _ in WINDOWS}
    census = Counter()
    cfg = engine.SignalConfig()
    selected_hash = hashlib.sha256()
    for index, day in enumerate(sessions):
        rows = days[day]
        current = {str(row.get("ISU_CD") or ""): row for row in rows}
        for ticker, row in current.items():
            close = _number(row.get("TDD_CLSPRC"))
            opened = _number(row.get("TDD_OPNPRC"))
            if close is None or close <= 0 or opened is None or opened <= 0:
                consecutive[ticker] = 0
                continue
            consecutive[ticker] = consecutive[ticker] + 1 if last_index.get(ticker) == index - 1 else 1
            last_index[ticker] = index
            history[ticker].append(close)
            volumes[ticker].append(_number(row.get("ACC_TRDVOL")))
        belongs = [name for name, start, end in WINDOWS if start <= day <= end]
        if not belongs:
            continue
        if index + 1 >= len(sessions) or not market_clock.consecutive_sessions("kr", day, sessions[index + 1]):
            raise ValueError(f"{day}: next execution session is missing")
        caps = []
        for ticker, row in current.items():
            cap = _number(row.get("MKTCAP"))
            if len(ticker) == 6 and ticker.endswith("0") and cap is not None and cap > 0:
                caps.append((cap, ticker, row))
        caps.sort(key=lambda item: (-item[0], item[1]))
        universe = caps[:200]
        if len(universe) != 200:
            raise ValueError(f"{day}: top-200 source incomplete")
        features, prices = [], {}
        for cap, ticker, raw in universe:
            census["universe_slots"] += 1
            if consecutive[ticker] < 253:
                census["noncontinuous_253_session_history"] += 1
                continue
            bars = _bar(raw)
            close, opened, high, low = (bars[key] for key in ("close", "open", "high", "low"))
            vols = list(volumes[ticker])
            if (len(vols) < 21 or any(value is None or value <= 0 for value in vols)
                    or high is None or low is None or low <= 0 or high < low
                    or not low <= min(close, opened) <= max(close, opened) <= high):
                census["incomplete_volume_or_ohlc_features"] += 1
                continue
            closes = history[ticker]
            prices[ticker] = list(closes)
            features.append(Features(
                ticker=ticker, close=close, volume=vols[-1], market_cap=cap,
                ma60=statistics.mean(closes[-60:]), ret5=close / closes[-6] - 1,
                ret20=close / closes[-21] - 1, ret60=close / closes[-61] - 1,
                relative_volume=vols[-1] / statistics.mean(vols[:-1]),
                close_location=(close - low) / (high - low) if high > low else 0.5,
                day_return=close / opened - 1))
        # Full 200-name decision-day denominator; missing common inputs are not
        # replaced by smaller-cap names. Only history through this day is passed.
        results = engine.evaluate(
            [{"ticker": ticker, "name": str(raw.get("ISU_NM") or ticker)}
             for _, ticker, raw in universe], prices, fundamentals={},
            config=cfg, today=dt.date.fromisoformat(day), unavailable=_PRICE_ONLY_UNAVAILABLE)
        legacy = {row.ticker: row for row in results}
        features = tuple(replace(row, legacy_score=legacy[row.ticker].score,
                                 legacy_buy=engine.is_buy(legacy[row.ticker].kind)) for row in features)
        selections = {policy: select(features, policy) for policy in POLICIES}
        next_day = sessions[index + 1]
        next_bars = {str(row.get("ISU_CD") or ""): _bar(row) for row in days[next_day]}
        item = {"decision_session": day, "entry_session": next_day, "selected": selections,
                "feature_tickers": tuple(row.ticker for row in features), "next_bars": next_bars}
        for name in belongs:
            compiled[name].append(item)
        census["decision_sessions"] += 1
        census["common_feature_slots"] += len(features)
        census["thin_input_abstention_sessions"] += len(features) < 160
        selected_hash.update(json.dumps({"session": day, "selected": {
            policy: [row.ticker for row in chosen] for policy, chosen in selections.items()}},
            sort_keys=True).encode())
    for name, start, end in WINDOWS:
        expected = [day for day in sessions if start <= day <= end]
        if not expected or [row["decision_session"] for row in compiled[name]] != expected:
            raise ValueError(f"{name}: missing decision inputs")
    return compiled, {**dict(census), "selection_sha256": selected_hash.hexdigest()}


def run(data_dir: Path) -> dict:
    paths = [data_dir / name for name in dict.fromkeys(HOLDOUT_ARCHIVES + ARCHIVES)]
    days, sources = load_krx_archives(paths)
    compiled, census = build_inputs(days)
    windows = {}
    for name, _, _ in WINDOWS:
        scenarios = {}
        for cost in (45, 75, 120):
            for label, fraction in (("full", 1.0), ("half", 0.5), ("none", 0.0)):
                scenarios[f"{cost}bp_{label}"] = {
                    policy: replay(compiled[name], policy, cost_bps=cost, fill_fraction=fraction)
                    for policy in POLICIES}
        windows[name] = scenarios
    validation = {name: windows[name] for name, _, _ in VALIDATION}
    return {
        "schema": "research-daytrade-batch-v1", "market": "kr",
        "registered_verdict": False, "strict_pit_eligible": False, "live_order_eligible": False,
        "phase": "historical_reconstruction_fixed_batch_not_future_oos",
        "sources": sources, "census": census,
        "validation_verdicts": {policy: grade_validation(validation, policy) for policy in CHALLENGERS},
        "development_windows": [name for name, _, _ in DEVELOPMENT],
        "validation_windows": [name for name, _, _ in VALIDATION],
        "plan_sha256": hashlib.sha256(PLAN.read_text(encoding="utf-8").split(
            "\n## 실행 결과", 1)[0].encode()).hexdigest(),
        "code_sha256": {relative: hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
                        for relative in ("src/signal_desk/signals/research_daytrade.py",
                                         "scripts/measure/research_daytrade_batch.py",
                                         "src/signal_desk/signals/engine.py")},
        "assumptions": {"starting_cash_per_block": 10_000_000, "slots": 6,
                        "price_buffer": 1.30, "previous_volume_cap_fraction": 0.01,
                        "roundtrip_cost_bps": [45, 75, 120], "fill_fraction": [1, 0.5, 0],
                        "overnight_holdings": False, "auction_liquidity_verified": False,
                        "corporate_action_adjusted_features": False,
                        "historical_publication_vintage_verified": False},
        "windows": windows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data/research")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("existing research output cannot be overwritten")
    result = run(args.data_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    print(json.dumps({"output": str(args.output), "census": result["census"],
                      "verdicts": result["validation_verdicts"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
