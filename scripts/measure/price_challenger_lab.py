"""Frozen, pre-registration C-level challenger comparison; never writes live state.

The inputs are the four already inspected price-only engine replays and official
KRX raw archives. This is a development ablation, not a fresh OOS verdict or a
historical reconstruction of the eight-factor production engine.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from collections import defaultdict
from pathlib import Path

from scripts.measure.krx_price_replay_pilot import ROOT, _number, load_krx_archives, run_pilot

ROUNDTRIP_COST_PCT = 0.25
ARCHIVES = (
    "krx-daily-2024-10-12-v1.zip", "krx-daily-2025-01-04-v1.zip",
    "krx-daily-2025-05-08-v1.zip", "krx-daily-2025-09-12-v1.zip",
    "krx-daily-2026-01-05-v1.zip", "krx-daily-2026-06-07-v1.zip",
)
PILOTS = {
    "2025q4-v5": ("krx-price-only-development-2025q4-v5.json",
                  "f649016afc4e29c827621639be96ff9ccf9c27dddb73872ac45278841011e66a"),
    "2026q1-v5": ("krx-price-only-development-2026q1-v5.json",
                  "70886b4a221b38675f5a8e2ea3d883e98c8b39f676163d6a4341e87a0463c27a"),
    "2026q2early-v5": ("krx-price-only-development-2026q2early-v5.json",
                       "930ac4b9cc547c579a188c18082662434bcaa1fa7b82557e2363d290e336da47"),
    "2026jun-expanded-v5": ("krx-price-only-development-2026jun-expanded-v5.json",
                            "68614fd3eb88457607efef6fdc97e40fc8e2bed5beaa387fb5bb141844b58888"),
}
HOLDOUT_ARCHIVES = (
    "krx-daily-2023-q1-v1.zip", "krx-daily-2023-q2-v1.zip",
    "krx-daily-2023-q3-v1.zip", "krx-daily-2023-q4-v1.zip",
    "krx-daily-2024-q1-v1.zip", "krx-daily-2024-q2-v1.zip",
    "krx-daily-2024-q3-v1.zip", "krx-daily-2024-10-12-v1.zip",
)
HOLDOUT_WINDOWS = (
    ("2024-apr-may", "2024-04-01", "2024-05-31"),
    ("2024-jun-jul", "2024-06-03", "2024-07-31"),
    ("2024-aug-sep", "2024-08-01", "2024-09-30"),
)


class PricePanel:
    """Calendar-indexed bars; a missing ticker-session invalidates that window."""

    def __init__(self, days: dict[str, list[dict]]) -> None:
        self.sessions = sorted(days)
        self.index = {day: i for i, day in enumerate(self.sessions)}
        self.bars: dict[tuple[str, str], tuple[float | None, float | None]] = {}
        self._features: dict[tuple[str, str], tuple[float, float, float] | None] = {}
        for day, rows in days.items():
            for row in rows:
                ticker = str(row.get("ISU_CD") or "")
                if len(ticker) == 6:
                    self.bars[day, ticker] = (_number(row.get("TDD_OPNPRC")),
                                              _number(row.get("TDD_CLSPRC")))

    def bar(self, day: str, ticker: str) -> tuple[float, float] | None:
        opened, closed = self.bars.get((day, ticker), (None, None))
        if opened is None or opened <= 0 or closed is None or closed <= 0:
            return None
        return opened, closed

    def closes(self, day: str, ticker: str, length: int) -> list[float] | None:
        index = self.index.get(day)
        if index is None or index + 1 < length:
            return None
        bars = [self.bar(session, ticker)
                for session in self.sessions[index - length + 1:index + 1]]
        if any(bar is None for bar in bars):
            return None
        return [bar[1] for bar in bars]

    def trend_features(self, day: str, ticker: str) -> tuple[float, float, float] | None:
        key = day, ticker
        if key not in self._features:
            closes = self.closes(day, ticker, 61)
            self._features[key] = ((closes[-1], statistics.mean(closes[-60:]),
                                    closes[-1] / closes[-21] - 1) if closes else None)
        return self._features[key]


def _matured(episode: dict, horizon: str) -> dict | None:
    outcome = episode[horizon]
    return outcome if outcome.get("state") == "matured" else None


def _round(value: float | None) -> float | None:
    return round(value, 4) if value is not None else None


def _mean(values: list[float]) -> float | None:
    return _round(statistics.mean(values)) if values else None


def _top_ticker_impact_share(rows: list[dict], key: str) -> float | None:
    by_ticker: dict[str, float] = defaultdict(float)
    for row in rows:
        by_ticker[row["ticker"]] += row[key]
    denominator = sum(abs(value) for value in by_ticker.values())
    return _round(max((abs(value) for value in by_ticker.values()), default=0) / denominator) if denominator else None


def _trend_decision(panel: PricePanel, date: str, ticker: str,
                    cohort_return20: float | None) -> str:
    features = panel.trend_features(date, ticker)
    if features is None or cohort_return20 is None:
        return "unknown"
    close, ma60, ret20 = features
    return "accept" if close > ma60 and ret20 > cohort_return20 else "reject"


def _risk_exit(panel: PricePanel, episode: dict) -> dict:
    """After a close at least 5% below entry and below MA20, exit NEXT open.

    This is one fixed research hypothesis, not a recommended stop level. If a
    next-session open is unavailable, no fill is invented.
    """
    outcome = _matured(episode, "h20")
    if not outcome:
        return {"state": "h20_unavailable"}
    ticker, entry_day, exit_day = episode["ticker"], outcome["entry_date"], outcome["exit_date"]
    entry_open = _number(outcome.get("entry_open"))
    if not entry_open or not panel.bar(entry_day, ticker):
        return {"state": "entry_unverified"}
    if abs(panel.bar(entry_day, ticker)[0] - entry_open) > 1e-6:
        return {"state": "entry_mismatch"}
    start, end = panel.index.get(entry_day), panel.index.get(exit_day)
    if start is None or end is None or end < start:
        return {"state": "calendar_missing"}
    # The historical h20 baseline itself must agree with the frozen raw close.
    final = panel.bar(exit_day, ticker)
    if final is None:
        return {"state": "baseline_exit_untradeable"}
    if abs(final[1] - float(outcome["exit_close"])) > 1e-6:
        return {"state": "baseline_price_mismatch"}
    first_loss_day = None
    for i in range(start, end + 1):
        day = panel.sessions[i]
        close = panel.bars.get((day, ticker), (None, None))[1]
        if close is None or close <= 0:
            return {"state": "intervening_price_gap"}
        if first_loss_day is None and close <= entry_open * 0.90:
            first_loss_day = day
    for i in range(start, end):
        day, next_day = panel.sessions[i:i + 2]
        closes = panel.closes(day, ticker, 20)
        if closes is None:
            return {"state": "untradeable_price_window"}
        if closes[-1] < entry_open * 0.95 and closes[-1] < statistics.mean(closes):
            next_bar = panel.bar(next_day, ticker)
            if next_bar is None:
                return {"state": "trigger_without_next_open", "trigger_date": day}
            net = (next_bar[0] / entry_open - 1) * 100 - ROUNDTRIP_COST_PCT
            return {"state": "triggered", "trigger_date": day, "exit_date": next_day,
                    "exit_open": next_bar[0], "net_pct": _round(net),
                    "before_first_10pct_close": first_loss_day is not None and day < first_loss_day,
                    "first_10pct_close": first_loss_day}
    return {"state": "held", "net_pct": outcome["net_pct"],
            "first_10pct_close": first_loss_day}


def compare_window(panel: PricePanel, pilot: dict) -> dict:
    """Compare on the same first-BUY episode slots, retaining rejected winners."""
    sessions = pilot["signal_dates"]
    if not sessions or max(sessions) >= "2026-08-05":
        raise ValueError("registered period cannot be used for challenger selection")
    if pilot.get("registered_verdict") is not False or pilot.get("source_level") != "C_offline_price_only_current_engine":
        raise ValueError("not a frozen C-level price-only replay")
    rows_by_day: dict[str, list[str]] = defaultdict(list)
    for row in pilot["rows"]:
        rows_by_day[row["date"]].append(row["ticker"])
    medians = {}
    for day, tickers in rows_by_day.items():
        available = [features[2] for ticker in tickers
                     if (features := panel.trend_features(day, ticker)) is not None]
        # Do not silently compute a market-wide reference from a thin subset.
        medians[day] = statistics.median(available) if len(available) >= 160 else None
    evaluated = []
    excluded = defaultdict(int)
    for episode in pilot["buy_episodes"]["episodes"]:
        if episode["left_censored"]:
            excluded["left_censored"] += 1
            continue
        h5, h20 = _matured(episode, "h5"), _matured(episode, "h20")
        if not h5 or not h20:
            excluded["unmatured"] += 1
            continue
        if (episode.get("h5_share_status") != "listed_shares_unchanged_actions_unverified"
                or episode.get("h20_share_status") != "listed_shares_unchanged_actions_unverified"):
            excluded["listed_shares_change_or_missing"] += 1
            continue
        day, ticker = episode["first_buy_date"], episode["ticker"]
        decision = _trend_decision(panel, day, ticker, medians.get(day))
        if decision == "unknown":
            excluded["trend_inputs_unknown"] += 1
            continue
        risk = _risk_exit(panel, episode)
        if risk["state"] not in {"held", "triggered"}:
            excluded[f"risk_{risk['state']}"] += 1
            continue
        evaluated.append({"date": day, "ticker": ticker, "trend": decision,
                          "h5_baseline_net_pct": h5["net_pct"],
                          "h5_trend_delta": -h5["net_pct"] if decision == "reject" else 0.0,
                          "h20_baseline_net_pct": h20["net_pct"],
                          "h20_risk_net_pct": risk["net_pct"],
                          "h20_risk_delta": risk["net_pct"] - h20["net_pct"],
                          "risk": risk})
    accepted = [row for row in evaluated if row["trend"] == "accept"]
    rejected = [row for row in evaluated if row["trend"] == "reject"]
    triggered = [row for row in evaluated if row["risk"]["state"] == "triggered"]
    return {
        "decision_window": [min(sessions), max(sessions)], "episodes_in_input": pilot["buy_episodes"]["count"],
        "comparable_episodes": len(evaluated),
        "distinct_tickers": len({row["ticker"] for row in evaluated}),
        "distinct_entry_dates": len({row["date"] for row in evaluated}),
        "excluded": dict(sorted(excluded.items())),
        "trend_accepted": len(accepted), "trend_rejected": len(rejected),
        "h5_baseline_mean_net_pct": _mean([row["h5_baseline_net_pct"] for row in evaluated]),
        "h5_trend_per_original_slot_net_pct": _mean([
            row["h5_baseline_net_pct"] if row["trend"] == "accept" else 0.0 for row in evaluated]),
        "h5_baseline_double_cost_mean_net_pct": _mean([
            row["h5_baseline_net_pct"] - ROUNDTRIP_COST_PCT for row in evaluated]),
        "h5_trend_double_cost_per_original_slot_net_pct": _mean([
            row["h5_baseline_net_pct"] - ROUNDTRIP_COST_PCT if row["trend"] == "accept" else 0.0
            for row in evaluated]),
        "h5_accepted_conditional_net_pct": _mean([row["h5_baseline_net_pct"] for row in accepted]),
        "h5_rejected_opportunity_net_pct": _mean([row["h5_baseline_net_pct"] for row in rejected]),
        "h20_baseline_mean_net_pct": _mean([row["h20_baseline_net_pct"] for row in evaluated]),
        "h20_risk_mean_net_pct": _mean([row["h20_risk_net_pct"] for row in evaluated]),
        "h20_baseline_double_cost_mean_net_pct": _mean([
            row["h20_baseline_net_pct"] - ROUNDTRIP_COST_PCT for row in evaluated]),
        "h20_risk_double_cost_mean_net_pct": _mean([
            row["h20_risk_net_pct"] - ROUNDTRIP_COST_PCT for row in evaluated]),
        "risk_trigger_count": len(triggered),
        "risk_before_first_10pct_close": sum(row["risk"]["before_first_10pct_close"] for row in triggered),
        "risk_trigger_then_baseline_positive": sum(row["h20_baseline_net_pct"] > 0 for row in triggered),
        "risk_trigger_mean_delta_pct_points": _mean([
            row["h20_risk_net_pct"] - row["h20_baseline_net_pct"] for row in triggered]),
        "risk_worst_baseline_net_pct": min((row["h20_baseline_net_pct"] for row in evaluated), default=None),
        "risk_worst_overlay_net_pct": min((row["h20_risk_net_pct"] for row in evaluated), default=None),
        "trend_largest_ticker_abs_impact_share": _top_ticker_impact_share(evaluated, "h5_trend_delta"),
        "risk_largest_ticker_abs_impact_share": _top_ticker_impact_share(evaluated, "h20_risk_delta"),
        "cohort_median_20d_status": "at_least_160_of_200_current_universe_rows",
        "repeated_or_related_episodes_not_independent": True,
    }


def run(data_dir: Path) -> dict:
    days, sources = load_krx_archives([data_dir / name for name in ARCHIVES])
    panel = PricePanel(days)
    windows, pilot_hashes = {}, {}
    for name, (filename, expected_hash) in PILOTS.items():
        raw = (data_dir / filename).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != expected_hash:
            raise ValueError(f"{filename}: frozen pilot digest mismatch")
        pilot = json.loads(raw)
        if pilot.get("sources") != sources:
            raise ValueError(f"{filename}: KRX source digest mismatch")
        windows[name] = compare_window(panel, pilot)
        pilot_hashes[name] = digest
    return {
        "schema": "price-challenger-development-v1", "market": "kr", "source_level": "C_price_only",
        "registered_verdict": False, "live_order_eligible": False,
        "hypotheses_fixed_before_this_run": {
            "entry": "first complete BUY episode; close>MA60 and 20-session return>same-day 200-name median",
            "holder_risk": "first close<0.95*entry_open AND close<MA20; exit NEXT session open if observed",
            "roundtrip_cost_pct": ROUNDTRIP_COST_PCT,
            "rejected_slot_cash_return_pct": 0,
        },
        "sources": sources, "frozen_pilot_sha256": pilot_hashes,
        "code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "windows": windows,
        "limitations": [
            "All four windows were previously inspected; this is development, not independent OOS.",
            "Price-only signals omit historical fundamental/flow/event inputs and actual order gates.",
            "Top-200 daily market-cap universe is a retrospective research approximation.",
            "Prices and first-BUY episodes are unadjusted; dividends and corporate actions unverified.",
            "Per-opportunity mean ignores overlapping positions, cash limits and portfolio capacity.",
            "The risk overlay assumes next-open liquidity, no partial fills and fixed roundtrip cost.",
        ],
    }


def run_holdout(data_dir: Path) -> dict:
    """First-price inspection of the fixed earlier window, no search over variants."""
    days, sources = load_krx_archives([data_dir / name for name in HOLDOUT_ARCHIVES])
    panel = PricePanel(days)
    windows = {}
    for name, start, end in HOLDOUT_WINDOWS:
        pilot = run_pilot(days, start=start, end=end)
        windows[name] = compare_window(panel, pilot)
    return {
        "schema": "price-challenger-historical-holdout-v1", "market": "kr",
        "source_level": "C_price_only", "registered_verdict": False,
        "live_order_eligible": False, "windows": windows, "sources": sources,
        "hypotheses": {"entry": "close>MA60 and ret20>cohort median ret20",
                       "holder_risk": "close<0.95*entry_open and close<MA20; next open exit",
                       "roundtrip_cost_pct": ROUNDTRIP_COST_PCT},
        "code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "limitations": ["Earlier raw prices were retrieved after hypothesis freeze, not an unseen future market.",
                        "C-level price-only replay, not production eight-factor or portfolio execution.",
                        "No adjusted total-return or fully verified corporate-action path."],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data/research")
    parser.add_argument("--cohort", choices=("development", "holdout"), default="development")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output exists; challenger results are not overwritten")
    result = run_holdout(args.data_dir) if args.cohort == "holdout" else run(args.data_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({"output": str(args.output), "windows": result["windows"],
                      "registered_verdict": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
