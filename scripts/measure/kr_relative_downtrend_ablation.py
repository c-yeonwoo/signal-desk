"""One frozen downtrend-exception ablation; offline, no production writes.

Uses existing receipt-dated engine selections and same-date price labels. This
is a development selection diagnostic, not a portfolio or certified total return.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
from statistics import mean

from scripts.measure.krx_price_replay_pilot import ROOT

PLAN = ROOT / "docs/kr-relative-downtrend-plan-2026-10-19.json"
PLAN_COMMIT = "fe97476"
PLAN_SHA256 = "baadc14c3a0b75f963de619c9504fa793f542142975fcb505a8ee501b507fd08"
ARMS = ("legacy", "candidate", "simple_momentum", "cash")
CAPACITY = 6


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def average(values):
    return mean(values) if values and all(v is not None for v in values) else None


def select(rows, legacy):
    """Decision inputs only; future outcomes are deliberately not consulted."""
    by_ticker = {r["ticker"]: r for r in rows}
    if len(by_ticker) != len(rows) or len(rows) > 200 or len(set(legacy)) != len(legacy):
        raise ValueError("duplicate or oversized decision cohort")
    if len(legacy) > CAPACITY or not set(legacy) <= by_ticker.keys():
        raise ValueError("invalid frozen legacy selection")
    removed = []
    for ticker in legacy:
        row = by_ticker[ticker]
        if type(row.get("downtrend_confirmed")) is not bool or type(row.get("downtrend_blocking")) is not bool:
            raise ValueError("unknown legacy trend inputs")
        if row["downtrend_confirmed"] and not row["downtrend_blocking"]:
            removed.append(ticker)
    if any(not finite(row.get("recent20_pct")) for row in rows):
        raise ValueError("unknown simple baseline decision input")
    simple = sorted(rows, key=lambda r: (-r["recent20_pct"], r["ticker"]))[:CAPACITY]
    return {"legacy": list(legacy), "candidate": [t for t in legacy if t not in removed],
            "simple_momentum": [r["ticker"] for r in simple], "cash": []}, removed


def slot_result(tickers, by_ticker, cost_bps):
    """Only actual selections incur costs; missing fills never become cash."""
    if len(tickers) > CAPACITY:
        raise ValueError("capacity exceeded")
    missing, values = [], []
    for ticker in tickers:
        outcome = by_ticker[ticker]["horizons"]["5"]
        if outcome.get("state") != "matured" or not finite(outcome.get("net_pct")):
            missing.append({"ticker": ticker, "state": outcome.get("state", "unknown")})
        else:
            values.append(outcome["net_pct"] - (cost_bps - 25) / 100)
    return {"selected": len(tickers), "missing": missing,
            "slot_net_pct": sum(values) / CAPACITY if not missing else None}


def summarize(daily, arm):
    values = [d["arms"][arm]["slot_net_pct"] for d in daily]
    return {"dates": len(daily), "unresolved_dates": sum(v is None for v in values),
            "selected_labels": sum(d["arms"][arm]["selected"] for d in daily),
            "cash_only_dates": sum(d["arms"][arm]["selected"] == 0 for d in daily),
            "equal_day_slot_net_pct": average(values),
            "complete_dates_diagnostic_mean_pct": mean(v for v in values if v is not None)
            if any(v is not None for v in values) else None,
            "phase_means_pct": [average(values[p::5]) for p in range(5)]}


def compare_daily(daily, baseline):
    differences = [(d["arms"]["candidate"]["slot_net_pct"], d["arms"][baseline]["slot_net_pct"])
                   for d in daily]
    values = [a - b if a is not None and b is not None else None for a, b in differences]
    return {"equal_day_delta_pp": average(values), "complete_paired_dates": sum(v is not None for v in values),
            "complete_pair_diagnostic_delta_pp": mean(v for v in values if v is not None)
            if any(v is not None for v in values) else None,
            "phase_deltas_pp": [average(values[p::5]) for p in range(5)]}


def support_checks(windows, affected, costs):
    """Fixed descriptive gates, not a statistical or live promotion decision."""
    checks = {}
    checks["complete_all_dates"] = all(
        arm["unresolved_dates"] == 0 for w in windows.values()
        for c in w["costs"].values() for arm in c["arms"].values())
    checks["affected_sample"] = (len(affected) >= 40 and len({r["ticker"] for r in affected}) >= 10
                                 and len({r["window"] for r in affected}) >= 3)
    summary = {}
    for cost in costs:
        key = str(cost)
        means = {arm: average([w["costs"][key]["arms"][arm]["equal_day_slot_net_pct"]
                                for w in windows.values()]) for arm in ARMS}
        worst = {arm: min(v) if v and all(x is not None for x in v) else None
                 for arm in ARMS
                 for v in [[w["costs"][key]["arms"][arm]["equal_day_slot_net_pct"] for w in windows.values()]]}
        winning = sum((w["costs"][key]["vs_legacy"]["equal_day_delta_pp"] or 0) > 0 for w in windows.values())
        phases = [average([w["costs"][key]["vs_legacy"]["phase_deltas_pp"][p] for w in windows.values()])
                  for p in range(5)]
        complete = all(v is not None for v in means.values())
        checks[f"{cost}bp_positive_beats_both"] = complete and means["candidate"] > max(0, means["legacy"], means["simple_momentum"])
        checks[f"{cost}bp_five_windows_improve"] = winning >= 5
        checks[f"{cost}bp_worst_window"] = complete and worst["candidate"] >= max(worst["legacy"], worst["simple_momentum"])
        checks[f"{cost}bp_all_phases_improve"] = all(v is not None and v > 0 for v in phases)
        impacts = defaultdict(float)
        for row in affected:
            if row["h5"].get("state") == "matured" and finite(row["h5"].get("net_pct")):
                # Match the equal-window/equal-day primary metric rather than
                # giving longer windows more weight in the concentration gate.
                weight = 1 / (len(windows) * windows[row["window"]]["dates"] * CAPACITY)
                impacts[row["ticker"]] -= (row["h5"]["net_pct"] - (cost - 25) / 100) * weight
        total = sum(abs(value) for value in impacts.values())
        concentration = max(map(abs, impacts.values())) / total if total else None
        checks[f"{cost}bp_concentration"] = concentration is not None and concentration <= .35
        summary[key] = {"equal_window_slot_net_pct": means, "worst_window_slot_net_pct": worst,
                        "windows_improved_vs_legacy": winning, "phase_equal_window_deltas_pp": phases,
                        "largest_ticker_absolute_impact_share": concentration}
    return checks, summary


def evaluate(scope, selection, plan):
    if list(scope["windows"]) != plan["windows"] or list(selection["windows"]) != plan["windows"]:
        raise ValueError("window scope changed")
    for data in (scope, selection):
        if data.get("registered_verdict") is not False or data.get("strict_pit_eligible") is not False:
            raise ValueError("not the declared development inputs")
    windows, affected = {}, []
    all_dates = set()
    costs = plan["comparison"]["cost_bps"]
    for name in plan["windows"]:
        grouped = defaultdict(list)
        for row in scope["windows"][name]["selection"]["rows"]:
            grouped[row["date"]].append(row)
        days = selection["windows"][name]["daily"]
        dates = [d["date"] for d in days]
        if dates != sorted(set(dates)) or set(dates) != set(grouped) or not dates:
            raise ValueError("decision date mismatch")
        if dates[-1] >= plan["protected_start"] or all_dates.intersection(dates):
            raise ValueError("protected or duplicated decision dates")
        all_dates.update(dates)
        by_cost = {str(cost): [] for cost in costs}
        for day in days:
            date = day["date"]
            rows = grouped[date]
            # Pass only fixed decision fields into the candidate and baseline.
            features = [{k: row[k] for k in ("ticker", "recent20_pct", "downtrend_confirmed", "downtrend_blocking")}
                        for row in rows]
            arms, removed = select(features, day["annual_financial"]["buy_tickers"])
            by_ticker = {r["ticker"]: r for r in rows}
            for ticker in removed:
                row = by_ticker[ticker]
                affected.append({"window": name, "date": date, "ticker": ticker, "name": row["name"],
                                 "recent20_pct": row["recent20_pct"], "h5": row["horizons"]["5"]})
            for cost in costs:
                by_cost[str(cost)].append({"date": date, "tickers": arms,
                                          "arms": {arm: slot_result(arms[arm], by_ticker, cost) for arm in ARMS}})
        windows[name] = {"dates": len(dates), "scored_ticker_days": sum(map(len, grouped.values())),
                         "unscored_top200_slots": 200 * len(dates) - sum(map(len, grouped.values())),
                         "costs": {cost: {"arms": {arm: summarize(daily, arm) for arm in ARMS},
                                          "vs_legacy": compare_daily(daily, "legacy"),
                                          "vs_simple_momentum": compare_daily(daily, "simple_momentum"),
                                          "daily": daily} for cost, daily in by_cost.items()}}
    if len(all_dates) != plan["decision_dates"]:
        raise ValueError("fixed 293-date scope differs")
    checks, summary = support_checks(windows, affected, costs)
    return {"schema": "kr-relative-downtrend-ablation-v1", "candidate_id": plan["candidate_id"],
            "development_supported": all(checks.values()), "checks": checks,
            "failed_checks": [k for k, passed in checks.items() if not passed],
            "decision": "development_support_only" if all(checks.values()) else "not_supported_do_not_apply",
            "production_changed": False, "live_order_eligible": False, "registered_verdict": False,
            "strict_pit_eligible": False, "verified_total_return": False,
            "decision_dates": len(all_dates), "affected_labels": len(affected),
            "affected_tickers": len({r["ticker"] for r in affected}),
            "affected_by_ticker": dict(Counter(r["ticker"] for r in affected)),
            "avoided_loss_labels_25bp": sum(r["h5"].get("net_pct", 0) < 0 for r in affected),
            "missed_winner_labels_25bp": sum(r["h5"].get("net_pct", 0) > 0 for r in affected),
            "affected_unresolved_labels": sum(r["h5"].get("state") != "matured" for r in affected),
            "summary": summary, "windows": windows, "affected": affected,
            "limitations": plan["limits"], "paperclip_used": False, "new_network_data_requests": 0}


def run():
    raw = PLAN.read_bytes()
    # Squash merges do not retain the preregistration commit in a fresh clone.
    # Keep that ID as provenance, but validate its frozen content directly.
    if hashlib.sha256(raw).hexdigest() != PLAN_SHA256:
        raise ValueError("plan differs from frozen development contract")
    plan = json.loads(raw)
    loaded = {}
    for key, declared in plan["inputs"].items():
        data = (ROOT / declared["path"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != declared["sha256"]:
            raise ValueError(f"frozen {key} hash differs")
        if key != "engine":
            loaded[key] = json.loads(data)
    expected_engine = plan["inputs"]["engine"]["sha256"]
    if any(data["code_sha256"].get("src/signal_desk/signals/engine.py") != expected_engine for data in loaded.values()):
        raise ValueError("input engine versions differ")
    result = evaluate(loaded["scope"], loaded["selection"], plan)
    result.update({"plan_commit": PLAN_COMMIT, "plan_sha256": hashlib.sha256(raw).hexdigest(),
                   "input_sha256": {k: v["sha256"] for k, v in plan["inputs"].items()},
                   "code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("refusing to overwrite result")
    report = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        handle.write("\n")
    print(json.dumps({k: report[k] for k in ("decision", "decision_dates", "affected_labels", "affected_tickers", "failed_checks", "summary")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
