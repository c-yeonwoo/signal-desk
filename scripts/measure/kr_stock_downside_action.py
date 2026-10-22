"""One frozen stock-downside sizing hypothesis; offline, conditional research.

Risk is observed adverse variation, not a probability or a learned forecast.
The zero-risk structural control isolates sizing/action changes from risk.
"""
from __future__ import annotations

import argparse
from itertools import combinations
import json
import math
from pathlib import Path
import subprocess

from scripts.measure import kr_frozen_forecast_policy as frozen
from scripts.measure import kr_frozen_bias_calibration as calibration
from scripts.measure import kr_joint_weakness_regime as parent_tools

PLAN_PATH = "docs/kr-stock-downside-action-plan-2026-10-23.json"
PLAN_SHA = "c2b59c9543b77b8afd38b42989950824c8a65d96be2266ea28d0153110eb973d"
PLAN_COMMIT = "cf66a6691e3957ee0b31fa967b2700025e4adfc8"
CODE_PATHS = ("scripts/measure/kr_stock_downside_action.py",
              "tests/test_kr_stock_downside_action.py", *parent_tools.CODE_PATHS)


def downside_history(inputs, dates, ticker):
    record = parent_tools.historical_return(inputs, dates, ticker)
    if record["missing"] is not None:
        return {**record, "downside": None}
    closes = [frozen.field(inputs, d, ticker, "TDD_CLSPRC") for d in dates]
    downside = math.sqrt(sum(min(b/a-1, 0.)**2 for a, b in zip(closes, closes[1:])))
    return {**record, "downside": downside}


def decision_context(inputs, day, forecasts):
    dates = parent_tools.history_sessions(day)
    stocks = {t: downside_history(inputs, dates, t) for t in sorted(forecasts)}
    return {"session": day, "history_sessions": dates, "stocks": stocks,
            "unknown": sum(r["downside"] is None for r in stocks.values())}


def coordinate_utility(w, previous, mu, downside, cap, buffer, cost):
    return ((mu-buffer)*w-cost*(abs(w-previous)+w)
            -.5*downside*w*w/cap)


def coordinate_choice(previous, mu, downside, cap, upper, buffer, cost, tolerance):
    values = (previous, mu, downside, cap, upper, buffer, cost, tolerance)
    if (not all(type(v) in (int, float) and math.isfinite(v) for v in values)
            or not 0 <= previous <= 1 or not 0 <= upper <= cap <= 1
            or cap <= 0 or min(downside, buffer, cost, tolerance) < 0):
        raise ValueError("invalid_coordinate_input")
    # Endpoints and the kink suffice when d=0. For d>0 include the stationary
    # point in each interval; clip to that interval, never across the kink.
    kink = min(previous, upper)
    choices = {0., upper, kink}
    if downside > 0:
        choices.add(max(0., min(kink, cap*(mu-buffer)/downside)))
        if previous <= upper:
            choices.add(max(previous, min(upper, cap*(mu-buffer-2*cost)/downside)))
    utilities = {w: coordinate_utility(w, previous, mu, downside, cap, buffer, cost) for w in choices}
    best = max(utilities.values())
    weight = min((w for w in choices if best-utilities[w] <= tolerance),
                 key=lambda w: (abs(w-previous), w))
    return {"weight": weight, "utility": utilities[weight],
            "zero_utility": utilities[0.], "gain_over_exit": utilities[weight]-utilities[0.]}


def action(previous, target):
    if previous == 0:
        return "new" if target > 0 else "cash"
    if target == 0:
        return "exit"
    if abs(target-previous) <= 1e-12:
        return "hold"
    return "add" if target > previous else "reduce"


def choose(forecasts, previous, risks, spec):
    p, c = spec["portfolio"], spec["candidate"]
    cap, maximum = p["slot_weight"], p["max_positions"]
    if (len(forecasts) < p["min_forecast_names"] or set(previous)-set(forecasts)
            or len(previous) > maximum or cap*maximum > p["budget"]+1e-12
            or any(not math.isfinite(v) for v in forecasts.values())
            or any(not math.isfinite(w) or w < 0 for w in previous.values())
            or sum(previous.values()) > 1+1e-12):
        raise ValueError("invalid_action_universe_or_budget")
    top = sorted(forecasts, key=lambda t: (-forecasts[t], t))[:c["new_candidate_count"]]
    names = sorted(set(top) | set(previous))
    records = {}
    for t in names:
        old = previous.get(t, 0.)
        upper = cap if t in top else min(old, cap)
        records[t] = coordinate_choice(old, forecasts[t], risks[t], cap, upper,
            c["return_buffer"], c["decision_one_way_cost"], c["objective_tie_tolerance"])
        records[t].update(previous_weight=old, mu=forecasts[t], downside=risks[t], upper=upper)
    positive = [t for t in names if records[t]["weight"] > 0]
    base = sum(r["zero_utility"] for r in records.values())
    alternatives = []
    for size in range(min(maximum, len(positive))+1):
        for subset in combinations(positive, size):
            weights = {t: records[t]["weight"] for t in subset}
            utility = base+sum(records[t]["gain_over_exit"] for t in subset)
            turnover = sum(abs(weights.get(t, 0.)-previous.get(t, 0.)) for t in names)
            alternatives.append((utility, turnover, subset, weights))
    best = max(r[0] for r in alternatives)
    winner = min((r for r in alternatives if best-r[0] <= c["objective_tie_tolerance"]),
                 key=lambda r: (r[1], r[2]))
    weights = winner[3]
    for t, record in records.items():
        record.update(target_weight=weights.get(t, 0.),
                      action=action(previous.get(t, 0.), weights.get(t, 0.)))
    return {"weights": weights, "utility": winner[0], "turnover": winner[1],
            "choice": "stock_downside_action", "alternatives": len(alternatives),
            "coordinates": records, "budget": p["budget"], "live_order_eligible": False}


def selector(bias):
    base = calibration.selector(bias)

    def select(arm, forecasts, caps, legacy, holdings, closes, nav, spec, *, context):
        if arm not in ("candidate", "action_only"):
            return base("candidate" if arm == "prior_calibrated" else arm,
                        forecasts, caps, legacy, holdings, closes, nav, spec)
        calibrated = {t: p-bias for t, p in forecasts.items()}
        previous = {t: q*closes[t]/nav for t, q in holdings.items()}
        top = sorted(calibrated, key=lambda t: (-calibrated[t], t))[:spec["candidate"]["new_candidate_count"]]
        risks = {}
        for t in set(top) | set(previous):
            value = context["stocks"].get(t, {}).get("downside")
            if value is None or not math.isfinite(value) or value < 0:
                raise frozen.DataGap(context["session"], t, "required_stock_downside_unknown")
            risks[t] = value if arm == "candidate" else 0.
        choice = choose(calibrated, previous, risks, spec)
        for t, row in choice["coordinates"].items():
            row["raw_forecast"] = forecasts[t]
        weights = choice["weights"]
        if any(t not in top or calibrated[t] <= spec["candidate"]["new_entry_min_gross"]
               for t in set(weights)-set(holdings)):
            raise ValueError("action_new_entry_outside_fixed_pool")
        return weights, {t: calibrated[t] for t in weights}, {
            **choice, "calibration_bias": bias, "risk_enabled": arm == "candidate",
            "context_sha256": frozen.encoded_sha(context)}
    return select


def verdict(groups, spec):
    out = frozen.verdict(groups, spec)
    comparisons = {}
    for baseline in ("prior_calibrated", "action_only"):
        failures, unknown = [], bool(out["incomplete_paths"])
        for scenario, means in out["scenario_mean_price_return_pct"].items():
            c, p = means["candidate"], means[baseline]
            if c is None or p is None:
                unknown = True
                continue
            primary = scenario in ("75bp_full", "120bp_full")
            if c <= p if primary else c < p:
                failures.append(scenario+":mean_not_improved" if primary else scenario+":mean_regression")
            rows = [g for g in groups if g["scenario"] == scenario]
            if min(g["metrics"]["candidate"]["price_mdd_pct"] for g in rows) < min(
                    g["metrics"][baseline]["price_mdd_pct"] for g in rows):
                failures.append(scenario+":worst_mdd_regression")
        comparisons[baseline] = {"decision": "incremental_rejection" if failures else
            "not_evaluable_source_or_tail" if unknown else "incremental_support", "violations": failures}
    out["incremental_assessments"] = comparisons
    decisions = [out["decision"], *(r["decision"] for r in comparisons.values())]
    out["adoption"] = ("rejected" if any("rejection" in d for d in decisions) else
        "not_evaluable_source_or_tail" if out["incomplete_paths"] else "conditional_development_support")
    return out


def preflight(root, implementation_commit, budget):
    raw = frozen.fixed_bytes(root/PLAN_PATH, PLAN_SHA)
    if raw != subprocess.check_output(["git", "show", f"{PLAN_COMMIT}:{PLAN_PATH}"], cwd=root, timeout=15):
        raise ValueError("action_plan_not_precommitted")
    plan = json.loads(raw)
    if plan["parent_plan"] != {"path": calibration.PLAN_PATH, "sha256": calibration.PLAN_SHA}:
        raise ValueError("action_parent_plan_differs")
    old_plan, spec, inputs = calibration.preflight(root, implementation_commit, budget)
    item = plan["parent_result"]
    parent = json.loads(frozen.fixed_bytes(root/item["path"], item["sha256"]))
    if (parent["path_count"] != 100 or parent["completed_paths"] != 100
            or parent["provenance"]["input_sha256"] != inputs.provenance["input_sha256"]
            or parent["provenance"]["raw_sources"] != inputs.provenance["raw_sources"]):
        raise ValueError("action_parent_sources_differ")
    for path in CODE_PATHS:
        body = (root/path).read_bytes()
        if body != subprocess.check_output(["git", "show", f"{implementation_commit}:{path}"], cwd=root, timeout=15):
            raise ValueError("action_code_not_precommitted:"+path)
        inputs.provenance["code_sha256"][path] = frozen.sha(body)
    if Path(__file__).resolve() != root/CODE_PATHS[0] or Path(parent_tools.__file__).resolve() != root/parent_tools.CODE_PATHS[0]:
        raise ValueError("action_import_outside_repository")
    inputs.provenance["action_plan"] = {"path": PLAN_PATH, "sha256": PLAN_SHA, "commit": PLAN_COMMIT}
    inputs.provenance["parent_calibration_result"] = item
    inputs, spec = calibration.evaluation_inputs(inputs, spec, old_plan)
    if any(old_plan["evaluation"][k] != plan["evaluation"][k] for k in ("start", "end", "dates", "forecast_rows", "tail_through")):
        raise ValueError("action_evaluation_period_differs")
    spec["candidate_id"] = plan["candidate_id"]
    spec["periods"][0]["arms"] = plan["comparison"]["arms"]
    return plan, spec, inputs, parent


def run(root, implementation_commit):
    budget = frozen.Budget()
    plan, spec, inputs, parent = preflight(root, implementation_commit, budget)
    bias = parent["calibration"]["bias"]
    contract = frozen.encoded_sha({"plan_sha256": PLAN_SHA, "parent_result": plan["parent_result"], "bias": bias})
    previous = {(g["scenario"], g["phase"]): g for g in parent["groups"]}
    contexts, groups = {}, []

    def context_factory(day, forecasts):
        if day not in contexts:
            contexts[day] = decision_context(inputs, day, forecasts)
        return contexts[day]

    for scenario in spec["execution"]["scenarios"]:
        for phase in spec["schedule"]["phases"]:
            group = frozen.replay_group(inputs, spec, scenario, phase, budget,
                target_selector=selector(bias), decision_contract_sha=contract,
                forecast_required_arms=("candidate", "action_only", "prior_calibrated", "prior_cost_aware"),
                decision_context_factory=context_factory)
            group["parent_crosscheck"] = parent_tools.parent_crosscheck(group, previous[scenario, phase])
            groups.append(group)
    paths = sum(len(g["metrics"]) for g in groups)
    if paths != plan["comparison"]["trading_paths"]:
        raise ValueError("action_path_count_differs")
    completed = sum(m["complete"] for g in groups for m in g["metrics"].values())
    return {"schema": "kr-stock-downside-action-result-v1", "candidate_id": plan["candidate_id"],
        "state": "comparison_complete" if paths == completed else "comparison_incomplete",
        "provenance": inputs.provenance, "bias_reused": bias, "decision_contract_sha256": contract,
        "evaluation": plan["evaluation"], "downside_contexts": contexts, "groups": groups,
        "path_count": paths, "completed_paths": completed,
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
        "sha256": frozen.sha(output.read_bytes())}, ensure_ascii=False))


if __name__ == "__main__":
    main()
