"""Read-only market-center/selection error diagnostic of frozen forecasts.

Unknown labels stay in the universe. Median order-statistic and pairwise bounds
avoid imputing prices or reporting a complete-universe mean from known rows.
These are missing-data bounds, not confidence intervals or strategy returns.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
from statistics import mean, median
import subprocess

from scripts.measure import kr_frozen_forecast_policy as frozen

PLAN_PATH = "docs/kr-forecast-error-attribution-plan-2026-10-22.json"
PLAN_SHA = "2d0fe27872c93ef387f54d9c2ae023e40bf86114a5b48ab064b490645aed6feb"
PLAN_COMMIT = "38cdef1a1b61be3867cb8c59afa3efe77986fdcb"
CODE_PATHS = ("scripts/measure/kr_forecast_error_attribution.py",
              "tests/test_kr_forecast_error_attribution.py")


def complete_mean(values):
    return mean(values) if values and all(v is not None for v in values) else None


def median_bounds(known, missing):
    if type(missing) is not int or missing < 0 or any(not math.isfinite(v) for v in known):
        raise ValueError("invalid_median_inputs")
    ordered = sorted(known)
    n = len(ordered)+missing
    if not n:
        return [None, None]
    positions = [(n-1)//2, n//2]
    lower = [ordered[j-missing] if j >= missing else None for j in positions]
    upper = [ordered[j] if j < len(ordered) else None for j in positions]
    return [complete_mean(lower), complete_mean(upper)]


def pairwise_bounds(selected, rest):
    points, unknown, ties = 0., 0, 0
    for left in selected:
        for right in rest:
            if left is None or right is None:
                unknown += 1
            else:
                points += float(left > right)+.5*float(left == right)
                ties += left == right
    total = len(selected)*len(rest)
    return {"pairs": total, "unknown_pairs": unknown, "ties": ties,
            "bounds": [points/total, (points+unknown)/total] if total else [None, None]}


def target_label(inputs, day, ticker, sessions=None):
    sessions = sessions or frozen.market_clock.next_sessions("kr", day, 6)
    try:
        for session in sessions:
            frozen.field(inputs, session, ticker, "LIST_SHRS")
        entry = frozen.field(inputs, sessions[0], ticker, "TDD_OPNPRC")
        exit_ = frozen.field(inputs, sessions[-1], ticker, "TDD_OPNPRC")
        for session in sessions[1:]:
            frozen.unit_audit(inputs, session, {ticker})
        value = exit_/entry-1
        if not math.isfinite(value):
            raise frozen.DataGap(day, ticker, "nonfinite_target")
        return {"gross_return": value, "entry_on": sessions[0], "exit_on": sessions[-1],
                "entry_open": entry, "exit_open": exit_, "missing": None}
    except (frozen.DataGap, KeyError) as exc:
        return {"gross_return": None, "missing": getattr(exc, "detail", {"field": "missing_calendar"})}


def error_bounds(top_forecast, top_return, forecast_center, realized_center_bounds):
    lo, hi = realized_center_bounds
    if top_return is None or lo is None or hi is None:
        return {"total_error": None if top_return is None else top_forecast-top_return,
                "center_error_bounds": [None, None], "relative_error_bounds": [None, None]}
    total = top_forecast-top_return
    common = [forecast_center-hi, forecast_center-lo]
    relative = [total-common[1], total-common[0]]
    for center in (lo, hi):
        first = forecast_center-center
        second = (top_forecast-forecast_center)-(top_return-center)
        if not math.isclose(first+second, total, rel_tol=0, abs_tol=1e-12):
            raise ValueError("error_identity_mismatch")
    return {"total_error": total, "center_error_bounds": common, "relative_error_bounds": relative}


def category(top_return, center):
    if top_return is None:
        return "unknown_top6"
    if top_return >= 0:
        return "nonnegative_top6"
    lo, hi = center
    if lo is not None and lo > 0:
        return "selection_loss_in_positive_proxy"
    if hi is not None and hi < 0:
        if top_return >= hi:
            return "negative_proxy_but_relative_outperformance"
        if lo is not None and top_return < lo:
            return "proxy_and_selection_weak"
    return "ambiguous_negative"


def day_record(day, forecasts, caps, labels):
    """Fix selections from as-of inputs before joining any realized labels."""
    if set(forecasts) != set(caps) or set(labels) != set(forecasts) or len(forecasts) < 6:
        raise ValueError("daily_universe_mismatch")
    top = sorted(forecasts, key=lambda t: (-forecasts[t], t))[:6]
    cap = sorted(caps, key=lambda t: (-caps[t], t))[:6]
    realized = {t: labels[t]["gross_return"] for t in forecasts}
    known = [v for v in realized.values() if v is not None]
    center = median_bounds(known, len(forecasts)-len(known))
    top_return = complete_mean([realized[t] for t in top])
    predicted = mean(forecasts[t] for t in top)
    forecast_center = median(forecasts.values())
    return {"session": day, "universe": len(forecasts), "known": len(known),
        "unknown": len(forecasts)-len(known), "top6": top, "cap6": cap,
        "forecast_center": forecast_center, "realized_center_bounds": center,
        "top6_forecast_mean": predicted, "top6_return_mean": top_return,
        "cap6_return_mean": complete_mean([realized[t] for t in cap]),
        "top6_relative_return_bounds": ([top_return-center[1], top_return-center[0]]
            if top_return is not None and all(v is not None for v in center) else [None, None]),
        "pairwise": pairwise_bounds([realized[t] for t in top], [v for t, v in realized.items() if t not in top]),
        "error": error_bounds(predicted, top_return, forecast_center, center),
        "negative_day_category": category(top_return, center)}


def summarize_days(days):
    return {"dates": len(days), "fully_known_universe_dates": sum(d["unknown"] == 0 for d in days),
        "top6_forecast_mean": complete_mean([d["top6_forecast_mean"] for d in days]),
        "top6_return_mean": complete_mean([d["top6_return_mean"] for d in days]),
        "cap6_return_mean": complete_mean([d["cap6_return_mean"] for d in days]),
        "forecast_center_mean": complete_mean([d["forecast_center"] for d in days]),
        "realized_center_mean_bounds": [complete_mean([d["realized_center_bounds"][i] for d in days]) for i in (0, 1)],
        "top6_relative_return_mean_bounds": [complete_mean([d["top6_relative_return_bounds"][i] for d in days]) for i in (0, 1)],
        "pairwise_date_equal_mean_bounds": [complete_mean([d["pairwise"]["bounds"][i] for d in days]) for i in (0, 1)],
        "total_error_mean": complete_mean([d["error"]["total_error"] for d in days]),
        "center_error_mean_bounds": [complete_mean([d["error"]["center_error_bounds"][i] for d in days]) for i in (0, 1)],
        "relative_error_mean_bounds": [complete_mean([d["error"]["relative_error_bounds"][i] for d in days]) for i in (0, 1)],
        "day_categories": dict(Counter(d["negative_day_category"] for d in days))}


def summarize_rows(rows):
    known = [r for r in rows if r["label"]["gross_return"] is not None]
    negative = sum(r["label"]["gross_return"] < 0 for r in known)
    missing = len(rows)-len(known)
    return {"rows": len(rows), "known": len(known), "unknown": missing, "negative_known": negative,
        "negative_fraction_bounds": [negative/len(rows), (negative+missing)/len(rows)] if rows else [None, None],
        "full_mean_gross_return": complete_mean([r["label"]["gross_return"] for r in rows]),
        "known_only_mean_gross_return": mean(r["label"]["gross_return"] for r in known) if known else None,
        "known_only_mean_error": mean(r["forecast"]-r["label"]["gross_return"] for r in known) if known else None}


def run(root, implementation_commit):
    budget = frozen.Budget()
    raw = frozen.fixed_bytes(root/PLAN_PATH, PLAN_SHA)
    if raw != subprocess.check_output(["git", "show", f"{PLAN_COMMIT}:{PLAN_PATH}"], cwd=root, timeout=15):
        raise ValueError("diagnostic_plan_not_precommitted")
    plan = json.loads(raw)
    if plan["parent_plan"] != {"path": frozen.PLAN_PATH, "sha256": frozen.PLAN_SHA}:
        raise ValueError("parent_plan_differs")
    _, inputs = frozen.preflight(root, implementation_commit, budget)
    if Path(__file__).resolve() != root/CODE_PATHS[0] or Path(frozen.__file__).resolve() != root/frozen.CODE_PATHS[0]:
        raise ValueError("import_outside_declared_repository")
    for path in CODE_PATHS:
        body = (root/path).read_bytes()
        if body != subprocess.check_output(["git", "show", f"{implementation_commit}:{path}"], cwd=root, timeout=15):
            raise ValueError("diagnostic_code_not_precommitted:"+path)
        inputs.provenance["code_sha256"][path] = frozen.sha(body)
    rows, days = [], []
    for day, forecasts in sorted(inputs.forecasts.items()):
        budget.check()
        sessions = frozen.market_clock.next_sessions("kr", day, 6)
        labels = {t: target_label(inputs, day, t, sessions) for t in forecasts}
        caps = {t: frozen.field(inputs, day, t, "MKTCAP") for t in forecasts}
        record = day_record(day, forecasts, caps, labels)
        ranks = {t: i+1 for i, t in enumerate(sorted(forecasts, key=lambda t: (-forecasts[t], t)))}
        rows.extend({"session": day, "ticker": t, "name": inputs.rows[day][t].get("ISU_NM"),
            "forecast": forecasts[t], "rank": ranks[t], "top6": t in record["top6"], "label": labels[t]}
            for t in forecasts)
        days.append(record)
    reasons = Counter(r["label"]["missing"]["field"] for r in rows if r["label"]["missing"])
    available = plan["availability_before_return_analysis"]
    if (len(rows) != plan["period"]["forecasts"] or len(days) != plan["period"]["dates"]
            or sum(r["label"]["gross_return"] is not None for r in rows) != available["known_labels"]
            or reasons != {"unreviewed_investment_unit_change": available["unknown_unit_labels"],
                           "TDD_OPNPRC": available["missing_endpoint_open_labels"]}
            or sum(d["unknown"] == 0 for d in days) != available["fully_known_universe_dates"]
            or sum(r["top6"] and r["label"]["gross_return"] is not None for r in rows) != available["top6_known_labels"]):
        raise ValueError("availability_differs_from_preregistered_counts")
    thresholds = plan["cohorts"]["forecast_bins"]
    bins = {str(i): [] for i in range(len(thresholds)+1)}
    for row in rows:
        index = next((i for i, threshold in enumerate(thresholds) if row["forecast"] <= threshold), len(thresholds))
        bins[str(index)].append(row)
    examples = sorted((r for r in rows if r["top6"] and r["forecast"] > .017
        and r["label"]["gross_return"] is not None and r["label"]["gross_return"] < 0),
        key=lambda r: (-(r["forecast"]-r["label"]["gross_return"]), r["session"], r["ticker"]))[:5]
    inputs.provenance["diagnostic_plan"] = {"path": PLAN_PATH, "sha256": PLAN_SHA, "commit": PLAN_COMMIT}
    return {"schema": "kr-forecast-error-attribution-result-v1", "state": "diagnostic_complete_with_explicit_label_gaps",
        "provenance": inputs.provenance, "summary": summarize_days(days),
        "months": {m: summarize_days([d for d in days if d["session"].startswith(m)]) for m in sorted({d["session"][:7] for d in days})},
        "row_summary": summarize_rows(rows), "top6_row_summary": summarize_rows([r for r in rows if r["top6"]]),
        "positive_forecast_summary": summarize_rows([r for r in rows if r["forecast"] > 0]),
        "forecast_bin_thresholds": thresholds, "forecast_bins": {i: summarize_rows(rs) for i, rs in bins.items()},
        "missing_reason_counts": dict(reasons), "days": days, "rows": rows, "worst_top6_positive_examples": examples,
        "interpretation": "missing-data bounds, not confidence intervals; proxy median, not KOSPI index; overlapping h5 observations, not strategy returns",
        "runs": 1, "wall_seconds": frozen.time.monotonic()-budget.start,
        "strategy_paths": 0, "fit_calls": 0, "new_market_data_requests": 0,
        "production_engine_changed": False, "live_order_eligible": False, "adoption_test": False,
        "strict_pit_eligible": False, "verified_total_return": False, "pristine_oos": False,
        "integrated_roadmap_percent": 40}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--implementation-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root, output = args.repo.resolve(), args.output.resolve()
    if not output.is_relative_to(root/"data/research"):
        parser.error("output must stay in local research directory")
    with output.open("x", encoding="utf-8") as stream:
        try:
            result = run(root, args.implementation_commit)
        except Exception as exc:
            json.dump({"state": "implementation_error", "error": str(exc), "production_engine_changed": False}, stream)
            raise
        json.dump(result, stream, ensure_ascii=False, allow_nan=False)
    print(json.dumps({"summary": result["summary"], "row_summary": result["row_summary"],
        "missing_reason_counts": result["missing_reason_counts"], "sha256": frozen.sha(output.read_bytes())}, ensure_ascii=False))


if __name__ == "__main__":
    main()
