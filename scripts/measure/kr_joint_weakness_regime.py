"""One as-of joint market/stock weakness veto, offline research only.

Saved forecast ranks, the parent's frozen bias and the execution core are reused.
No new fit, source request, production change or unregistered live promotion.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import subprocess

from scripts.measure import kr_frozen_forecast_policy as frozen
from scripts.measure import kr_frozen_bias_calibration as calibration
from scripts.measure import kr_forecast_error_attribution as audit

PLAN_PATH = "docs/kr-joint-weakness-regime-plan-2026-10-22.json"
PLAN_SHA = "a6eed9dad125dc14575761df96ada94f92ade1f5df5c069d978495eaaf0f76d4"
PLAN_COMMIT = "04042a14f5f17f094940fe1a19158457c57cffcd"
CODE_PATHS = ("scripts/measure/kr_joint_weakness_regime.py", "tests/test_kr_joint_weakness_regime.py")
PARENT_ARMS = {"prior_calibrated": "candidate", "prior_cost_aware": "prior_cost_aware",
               **{a: a for a in frozen.CONTROLS}}


def history_sessions(day):
    dates = [day]
    for _ in range(5):
        previous = frozen.market_clock.previous_session("kr", dates[0])
        if previous is None:
            raise frozen.DataGap(day, "*", "regime_history_calendar")
        dates.insert(0, previous)
    return dates


def historical_return(inputs, dates, ticker):
    try:
        observations = [[d, frozen.field(inputs, d, ticker, "TDD_CLSPRC"),
                         frozen.field(inputs, d, ticker, "LIST_SHRS")] for d in dates]
        for before, after in zip(dates, dates[1:]):
            if inputs.previous.get(after) != before:
                raise frozen.DataGap(after, ticker, "regime_history_calendar")
            frozen.unit_audit(inputs, after, {ticker})
        value = observations[-1][1]/observations[0][1]-1
        if not math.isfinite(value):
            raise frozen.DataGap(dates[-1], ticker, "regime_nonfinite_return")
        return {"return5": value, "history_sha256": frozen.encoded_sha(observations), "missing": None}
    except frozen.DataGap as exc:
        return {"return5": None, "history_sha256": None, "missing": exc.detail}


def market_state(bounds):
    lo, hi = bounds
    if lo is None or hi is None or not all(math.isfinite(v) for v in bounds) or lo > hi:
        raise ValueError("unavailable_market_bounds")
    return "weak" if hi < 0 else "not_weak" if lo >= 0 else "ambiguous"


def decision_context(inputs, day, forecasts):
    dates = history_sessions(day)
    stocks = {t: historical_return(inputs, dates, t) for t in sorted(forecasts)}
    known = [r["return5"] for r in stocks.values() if r["return5"] is not None]
    bounds = audit.median_bounds(known, len(stocks)-len(known))
    try:
        state = market_state(bounds)
    except ValueError as exc:
        raise frozen.DataGap(day, "*", "unavailable_market_history_median") from exc
    return {"session": day, "history_sessions": dates, "universe": len(stocks),
        "known": len(known), "unknown": len(stocks)-len(known),
        "market_return5_median_bounds": bounds, "market_state": state, "stocks": stocks}


def veto(weights, holdings, closes, nav, context):
    blocked = []
    if context["market_state"] == "weak":
        for t in sorted(weights):
            r = context["stocks"].get(t, {}).get("return5")
            if r is None or not math.isfinite(r):
                raise frozen.DataGap(context["session"], t, "required_stock_regime_unknown")
            if r < 0:
                blocked.append(t)
    return {t: w for t, w in weights.items() if t not in blocked}, {
        "blocked": blocked, "blocked_new_entry_intent": [t for t in blocked if t not in holdings],
        "blocked_held_exit_intent": [t for t in blocked if t in holdings],
        "blocked_held_addition_notional_intent": [t for t in blocked if t in holdings
            and weights[t]*nav-holdings[t]*closes[t] > 1e-6],
        "intents_are_not_executed_orders": True}


def selector(bias):
    base = calibration.selector(bias)

    def select(arm, forecasts, caps, legacy, holdings, closes, nav, spec, *, context):
        weights, priority, choice = base("candidate" if arm == "prior_calibrated" else arm,
            forecasts, caps, legacy, holdings, closes, nav, spec)
        if arm != "candidate":
            return weights, priority, choice
        final, reasons = veto(weights, holdings, closes, nav, context)
        return final, {t: priority[t] for t in final}, {
            "choice": "joint_weakness_veto" if reasons["blocked"] else "unchanged_calibrated",
            "baseline_weights": weights, "baseline_choice": choice, **reasons,
            "context_sha256": frozen.encoded_sha(context)}

    return select


def without_ids(value):
    if isinstance(value, dict):
        return {k: without_ids(v) for k, v in value.items() if k != "input_id"}
    if isinstance(value, list):
        return [without_ids(v) for v in value]
    return value


def parent_crosscheck(group, parent):
    records = []
    for arm, old in PARENT_ARMS.items():
        actual, expected = group["metrics"][arm], parent["metrics"][old]
        if without_ids(actual["fills"]) != without_ids(expected["fills"][:len(actual["fills"])]):
            raise ValueError("parent_fill_prefix_mismatch:"+arm)
        by_day = {r["session"]: r["books"][old] for r in parent["trace"]}
        for row in group["trace"]:
            if arm in row["books"] and without_ids(row["books"][arm]) != without_ids(by_day[row["session"]]):
                raise ValueError("parent_daily_book_mismatch:"+arm)
        if actual["complete"] and without_ids(actual) != without_ids(expected):
            raise ValueError("parent_complete_metric_mismatch:"+arm)
        records.append({"arm": arm, "parent_arm": old, "complete_match": actual["complete"],
                        "known_prefix_match": True})
    return records


def verdict(groups, spec):
    out = frozen.verdict(groups, spec)
    failures, unknown = [], bool(out["incomplete_paths"])
    for scenario, means in out["scenario_mean_price_return_pct"].items():
        c, p = means["candidate"], means["prior_calibrated"]
        if c is None or p is None:
            unknown = True
            continue
        primary = scenario in ("75bp_full", "120bp_full")
        failed_mean = c <= p if primary else c < p
        if failed_mean:
            failures.append(scenario+":mean_not_improved" if primary else scenario+":mean_regression")
        rows = [g for g in groups if g["scenario"] == scenario]
        if min(g["metrics"]["candidate"]["price_mdd_pct"] for g in rows) < min(
                g["metrics"]["prior_calibrated"]["price_mdd_pct"] for g in rows):
            failures.append(scenario+":worst_mdd_regression")
    out["incremental_assessment"] = {"baseline": "prior_calibrated",
        "decision": "incremental_rejection" if failures else
                    "not_evaluable_source_or_tail" if unknown else "incremental_support",
        "violations": failures, "production_change_authorized": False}
    return out


def preflight(root, implementation_commit, budget):
    raw = frozen.fixed_bytes(root/PLAN_PATH, PLAN_SHA)
    if raw != subprocess.check_output(["git", "show", f"{PLAN_COMMIT}:{PLAN_PATH}"], cwd=root, timeout=15):
        raise ValueError("regime_plan_not_precommitted")
    plan = json.loads(raw)
    if plan["parent_plan"] != {"path": calibration.PLAN_PATH, "sha256": calibration.PLAN_SHA}:
        raise ValueError("parent_calibration_plan_differs")
    old_plan, spec, inputs = calibration.preflight(root, implementation_commit, budget)
    item = plan["parent_result"]
    parent = json.loads(frozen.fixed_bytes(root/item["path"], item["sha256"]))
    if (parent["path_count"] != 100 or parent["completed_paths"] != 100
            or parent["verdict"]["decision"] != "observed_rejection"
            or parent["verdict"]["incremental_assessment"]["decision"] != "incremental_support"
            or parent["provenance"]["input_sha256"] != inputs.provenance["input_sha256"]
            or parent["provenance"]["raw_sources"] != inputs.provenance["raw_sources"]):
        raise ValueError("parent_result_or_sources_differ")
    for path in CODE_PATHS:
        body = (root/path).read_bytes()
        if body != subprocess.check_output(["git", "show", f"{implementation_commit}:{path}"], cwd=root, timeout=15):
            raise ValueError("regime_code_not_precommitted:"+path)
        inputs.provenance["code_sha256"][path] = frozen.sha(body)
    if Path(__file__).resolve() != root/CODE_PATHS[0] or Path(calibration.__file__).resolve() != root/calibration.EXTRA_CODE_PATHS[0]:
        raise ValueError("regime_import_outside_repository")
    inputs.provenance["regime_plan"] = {"path": PLAN_PATH, "sha256": PLAN_SHA, "commit": PLAN_COMMIT}
    inputs.provenance["parent_calibration_result"] = item
    evaluation, spec = calibration.evaluation_inputs(inputs, spec, old_plan)
    if any(old_plan["evaluation"][k] != plan["evaluation"][k] for k in ("start", "end", "dates", "forecast_rows", "tail_through")):
        raise ValueError("regime_evaluation_period_differs")
    spec["candidate_id"] = plan["candidate_id"]
    spec["periods"][0]["arms"] = plan["comparison"]["arms"]
    return plan, spec, evaluation, parent


def run(root, implementation_commit):
    budget = frozen.Budget()
    plan, spec, inputs, parent = preflight(root, implementation_commit, budget)
    bias = parent["calibration"]["bias"]
    select = selector(bias)
    contract = frozen.encoded_sha({"plan_sha256": PLAN_SHA, "parent_result_sha256": plan["parent_result"]["sha256"], "bias": bias})
    previous = {(g["scenario"], g["phase"]): g for g in parent["groups"]}
    contexts, groups = {}, []

    def context_factory(day, forecasts):
        if day not in contexts:
            contexts[day] = decision_context(inputs, day, forecasts)
        return contexts[day]

    for scenario in spec["execution"]["scenarios"]:
        for phase in spec["schedule"]["phases"]:
            group = frozen.replay_group(inputs, spec, scenario, phase, budget,
                target_selector=select, decision_contract_sha=contract,
                forecast_required_arms=("candidate", "prior_calibrated", "prior_cost_aware"),
                decision_context_factory=context_factory)
            group["parent_crosscheck"] = parent_crosscheck(group, previous[scenario, phase])
            c, p = group["metrics"]["candidate"], group["metrics"]["prior_calibrated"]
            group["deltas"]["prior_calibrated"] = ({k: c[k]-p[k] for k in
                ("net_pnl_krw", "before_cost_price_pnl_krw", "fees_krw")} if c["complete"] and p["complete"] else None)
            groups.append(group)
    paths = sum(len(g["metrics"]) for g in groups)
    if paths != plan["comparison"]["trading_paths"]:
        raise ValueError("regime_path_count_differs")
    completed = sum(m["complete"] for g in groups for m in g["metrics"].values())
    return {"schema": "kr-joint-weakness-regime-result-v1", "candidate_id": plan["candidate_id"],
        "state": "comparison_complete" if completed == paths else "comparison_incomplete",
        "provenance": inputs.provenance, "bias_reused": bias, "decision_contract_sha256": contract,
        "evaluation": plan["evaluation"], "regime_contexts": contexts,
        "regime_dates": dict(Counter(c["market_state"] for c in contexts.values())),
        "groups": groups, "path_count": paths, "completed_paths": completed,
        "parent_complete_matches": sum(c["complete_match"] for g in groups for c in g["parent_crosscheck"]),
        "verdict": verdict(groups, spec), "cash": parent["cash"], "historical_runs": 1,
        "fit_calls": 0, "forecast_reconstruction_calls": 0, "new_market_data_requests": 0,
        "wall_seconds": frozen.time.monotonic()-budget.start,
        "production_engine_changed": False, "live_order_eligible": False,
        "strict_pit_eligible": False, "verified_total_return": False, "pristine_oos": False,
        "price_only_conditional_forecasts": True, "integrated_roadmap_percent": 40}


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
    print(json.dumps({"paths": result["path_count"], "completed": result["completed_paths"],
        "parent_matches": result["parent_complete_matches"], "verdict": result["verdict"],
        "regime_dates": result["regime_dates"], "sha256": frozen.sha(output.read_bytes())}, ensure_ascii=False))


if __name__ == "__main__":
    main()
