"""Separate repeated BUY labels from first-entry skill in frozen price-only pilots.

Research diagnostic only: no live score, historical eight-factor reconstruction,
corporate-action-adjusted total return, or registered-period outcomes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from collections import defaultdict
from pathlib import Path

from scripts.measure.research_action_input_readiness import LEDGER, load_ledger
from scripts.measure.krx_price_replay_pilot import ROOT, load_krx_archives, run_pilot
from scripts.measure.price_challenger_lab import (ARCHIVES, HOLDOUT_ARCHIVES,
                                                  HOLDOUT_WINDOWS, PILOTS)

PROTECTED_START = "2026-08-05"
BUY_KINDS = frozenset({"BUY", "STRONG_BUY"})
STABLE_SHARE_FLAG = "listed_shares_unchanged_actions_unverified"


def _mean(values: list[float]) -> float | None:
    return round(statistics.mean(values), 4) if values else None


def summarize_window(pilot: dict) -> dict:
    dates = pilot.get("signal_dates") or []
    if (pilot.get("registered_verdict") is not False
            or pilot.get("source_level") != "C_offline_price_only_current_engine"
            or not dates or dates != sorted(set(dates)) or dates[-1] >= PROTECTED_START):
        raise ValueError("entry audit: invalid frozen development scope")

    by_day: dict[str, list[dict]] = defaultdict(list)
    for row in pilot["rows"]:
        if row["date"] not in dates:
            raise ValueError("entry audit: row outside decision dates")
        by_day[row["date"]].append(row)
    daily = []
    for day in dates:
        matured = [row for row in by_day[day]
                   if row["outcomes"]["1"].get("state") == "matured"]
        buys = [row for row in matured if row["kind"] in BUY_KINDS]
        if len(matured) < 150 or not buys:
            continue
        buy_net = [float(row["outcomes"]["1"]["net_pct"]) for row in buys]
        cohort_net = [float(row["outcomes"]["1"]["net_pct"]) for row in matured]
        daily.append({"date": day, "cohort_names": len(matured), "buy_labels": len(buys),
                      "buy_mean_net_pct": statistics.mean(buy_net),
                      "cohort_mean_net_pct": statistics.mean(cohort_net),
                      "buy_labels_negative": sum(value < 0 for value in buy_net),
                      "buy_labels_ge_3pct": sum(value >= 3 for value in buy_net)})
    if len(daily) != len(dates):
        raise ValueError("entry audit: incomplete daily BUY/cohort comparison")

    reasons: dict[str, int] = defaultdict(int)
    comparable = []
    for episode in pilot["buy_episodes"]["episodes"]:
        if episode["left_censored"]:
            reasons["left_censored"] += 1
            continue
        if (episode["h5"].get("state") != "matured"
                or episode["h20"].get("state") != "matured"):
            reasons["unmatured_or_price_gap"] += 1
            continue
        cohort = pilot["cohort_comparison"].get(f"{episode['first_buy_date']}:5")
        if not cohort or cohort.get("n", 0) < 150:
            reasons["same_day_cohort_missing"] += 1
            continue
        comparable.append({"ticker": episode["ticker"],
                           "date": episode["first_buy_date"],
                           "h5_net_pct": float(episode["h5"]["net_pct"]),
                           "h20_net_pct": float(episode["h20"]["net_pct"]),
                           "h5_excess_pp": (float(episode["h5"]["net_pct"])
                                            - float(cohort["mean_net_pct"])),
                           "share_flag": episode["h5_share_status"] == STABLE_SHARE_FLAG
                           and episode["h20_share_status"] == STABLE_SHARE_FLAG})
    clean = [row for row in comparable if row["share_flag"]]

    def episode_metrics(rows: list[dict]) -> dict:
        return {"count": len(rows), "distinct_tickers": len({row["ticker"] for row in rows}),
                "h5_mean_net_pct": _mean([row["h5_net_pct"] for row in rows]),
                "h5_mean_excess_vs_same_day_cohort_pp": _mean([row["h5_excess_pp"] for row in rows]),
                "h5_median_excess_vs_same_day_cohort_pp": (
                    round(statistics.median(row["h5_excess_pp"] for row in rows), 4) if rows else None),
                "h5_negative_count": sum(row["h5_net_pct"] < 0 for row in rows),
                "h20_mean_net_pct": _mean([row["h20_net_pct"] for row in rows])}

    if sum(reasons.values()) + len(comparable) != pilot["buy_episodes"]["count"]:
        raise ValueError("entry audit: episode accounting differs")
    return {"decision_window": [dates[0], dates[-1]],
            "daily_h1": {"days": len(daily), "buy_labels": sum(row["buy_labels"] for row in daily),
                         "equal_day_buy_mean_net_pct": _mean([
                             row["buy_mean_net_pct"] for row in daily]),
                         "equal_day_cohort_mean_net_pct": _mean([
                             row["cohort_mean_net_pct"] for row in daily]),
                         "days_buy_mean_ge_3pct": sum(
                             row["buy_mean_net_pct"] >= 3 for row in daily),
                         "negative_buy_labels": sum(
                             row["buy_labels_negative"] for row in daily)},
            "first_buy_episodes": episode_metrics(comparable),
            "share_count_stable_subset_actions_unverified": episode_metrics(clean),
            "excluded_episodes": dict(sorted(reasons.items())),
            "share_count_changed_or_missing": len(comparable) - len(clean),
            "repeated_labels_are_not_independent_trades": True}


def run(data_dir: Path) -> dict:
    frozen, _, ledger_sha = load_ledger(LEDGER)
    holdout_days, holdout_sources = load_krx_archives([
        data_dir / name for name in HOLDOUT_ARCHIVES])
    development_days, development_sources = load_krx_archives([
        data_dir / name for name in ARCHIVES])

    def source_digest(sources: list[dict]) -> str:
        return hashlib.sha256("\n".join(
            f"{source['archive']}:{source['sha256']}" for source in sources
        ).encode()).hexdigest()

    expected = {"2024": {row["price_archive_digest"] for row in frozen["windows"][:3]},
                "2025_2026": {row["price_archive_digest"] for row in frozen["windows"][3:]}}
    if (expected["2024"] != {source_digest(holdout_sources)}
            or expected["2025_2026"] != {source_digest(development_sources)}):
        raise ValueError("entry audit: frozen KRX archive digest differs")
    windows, pilot_hashes = {}, {}
    for name, start, end in HOLDOUT_WINDOWS:
        windows[name] = summarize_window(run_pilot(holdout_days, start=start, end=end))
    for name, (filename, expected_hash) in PILOTS.items():
        raw = (data_dir / filename).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != expected_hash:
            raise ValueError(f"entry audit: frozen pilot hash differs {name}")
        pilot = json.loads(raw)
        if pilot.get("sources") != development_sources:
            raise ValueError(f"entry audit: KRX source list differs {name}")
        windows[name] = summarize_window(pilot)
        pilot_hashes[name] = digest
    return {"schema": "research-entry-edge-audit-v1", "market": "kr",
            "source_level": "C_price_only_development", "registered_verdict": False,
            "strict_pit_eligible": False, "verified_total_return": False,
            "live_order_eligible": False, "windows": windows,
            "sources": {"2024": holdout_sources, "2025_2026": development_sources},
            "gap_ledger_sha256": ledger_sha,
            "frozen_pilot_sha256": pilot_hashes,
            "code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "limitations": [
                "Price-only reconstructed labels are not historical eight-factor decisions.",
                "The 2024 windows were previously inspected; none is independent OOS.",
                "Daily labels overlap; equal-day means are not a portfolio return.",
                "First BUY episodes can repeat the same ticker and are not independent trades.",
                "Unadjusted actions, PIT availability and actual next-open fills remain unverified.",
                "Share-count stability does not prove absence of dividends or other events.",
            ]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data/research")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists; no research artifact is overwritten")
    report = run(args.data_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        handle.write("\n")
    print(json.dumps({"output": str(args.output), "windows": len(report["windows"]),
                      "live_order_eligible": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
