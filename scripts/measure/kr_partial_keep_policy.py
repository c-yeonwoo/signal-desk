"""One preregistered menu expansion; unchanged forecasts/objective/execution.

No training, acquisition, production or live orders. The shared replay receives
an explicit selector, never a global monkeypatch. Old 80 economic paths must
reproduce their preserved parent; the new contract IDs are allowed to differ.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import math
from pathlib import Path
import subprocess

from scripts.measure import kr_frozen_forecast_policy as frozen
from scripts.measure.kr_holding_intent_diagnostic import utility

PLAN_PATH = "docs/kr-partial-keep-policy-plan-2026-10-21.json"
PLAN_SHA = "e63870cca16c4a927bd22e19f3b466f9fe7b3a258769e7e8f755625c9ae30295"
PLAN_COMMIT = "da25cfd217bd0695c5f236e54f6c2d38785177b1"
EXTRA_CODE_PATHS = ("scripts/measure/kr_partial_keep_policy.py",
                    "tests/test_kr_partial_keep_policy.py",
                    "scripts/measure/kr_holding_intent_diagnostic.py")


def partial_choice(base, forecasts, previous, spec):
    """Compare original winner with capped keep subsets; no new coordinates."""
    c, p = spec["candidate"], spec["portfolio"]
    if c["risk_aversion"] != 0 or len(previous) > p["max_positions"]:
        raise ValueError("partial_keep_requires_frozen_zero_risk_and_position_cap")
    if (set(previous)-set(forecasts) or any(not math.isfinite(w) or w < 0 for w in previous.values())
            or sum(previous.values()) > 1+1e-12):
        raise ValueError("invalid_asof_previous")
    if not math.isclose(utility(base["weights"], previous, forecasts, c), base["utility"], abs_tol=1e-12, rel_tol=0):
        raise ValueError("base_utility_differs")
    keep = {t: min(w, p["keep_name_weight_cap"]) for t, w in previous.items() if w > 0}
    if sum(keep.values()) > p["budget"]:
        scale = p["budget"]/sum(keep.values())
        keep = {t: w*scale for t, w in keep.items()}
    names = sorted(keep)
    alternatives = [deepcopy(base)]
    for mask in range(1 << len(names)):
        weights = {t: keep[t] for i, t in enumerate(names) if mask & (1 << i)}
        alternatives.append({"weights": weights, "utility": utility(weights, previous, forecasts, c),
            "turnover": sum(abs(weights.get(t, 0)-previous.get(t, 0)) for t in set(weights)|set(previous)),
            "choice": "keep" if weights == keep else "partial_keep"})
    best = max(a["utility"] for a in alternatives)
    tied = [a for a in alternatives if best-a["utility"] <= c["objective_tie_tolerance"]]
    winner = min(tied, key=lambda a: (a["turnover"], a["choice"] != "keep", tuple(sorted(a["weights"]))))
    return {**winner, "alternatives": base["alternatives"]+(1 << len(names)),
            "partial_keep_alternatives": 1 << len(names), "budget": p["budget"],
            "baseline_weights": deepcopy(base["weights"]),
            "utility_gain_over_baseline_same_state": winner["utility"]-base["utility"],
            "live_order_eligible": False}


def targets(arm, forecasts, caps, legacy, holdings, closes, nav, spec):
    original_arm = "candidate" if arm == "prior_cost_aware" else arm
    weights, priority, base = frozen.targets(original_arm, forecasts, caps, legacy, holdings, closes, nav, spec)
    if arm != "candidate":
        return weights, priority, base
    previous = {t: q*closes[t]/nav for t, q in holdings.items()}
    choice = partial_choice(base, forecasts, previous, spec)
    return choice["weights"], {t: forecasts[t] for t in choice["weights"]}, choice


def without_contract_ids(value):
    if isinstance(value, dict):
        return {k: without_contract_ids(v) for k, v in value.items() if k != "input_id"}
    if isinstance(value, list):
        return [without_contract_ids(v) for v in value]
    return value


def parent_crosscheck(group, parent):
    """Known prefix for source stops, full economic identity for completed arms."""
    records = []
    for arm in ("prior_cost_aware", *frozen.CONTROLS):
        old_arm = "candidate" if arm == "prior_cost_aware" else arm
        actual, expected = group["metrics"][arm], parent["metrics"][old_arm]
        if without_contract_ids(actual["fills"]) != without_contract_ids(expected["fills"][:len(actual["fills"])]):
            raise ValueError("parent_fill_prefix_mismatch:"+arm)
        lookup = {r["session"]: r["books"][old_arm] for r in parent["trace"]}
        for row in group["trace"]:
            if arm in row["books"] and without_contract_ids(row["books"][arm]) != without_contract_ids(lookup[row["session"]]):
                raise ValueError("parent_daily_book_mismatch:"+arm)
        if actual["complete"] and without_contract_ids(actual) != without_contract_ids(expected):
            raise ValueError("parent_complete_metric_mismatch:"+arm)
        records.append({"arm": arm, "parent_arm": old_arm, "complete_match": actual["complete"],
                        "known_prefix_match": True})
    return records


def verdict(groups, spec):
    result = frozen.verdict(groups, spec)
    means = result["scenario_mean_price_return_pct"]
    violations = list(result["violations"])
    incremental_failures, incremental_unknown = [], bool(result["incomplete_paths"])
    for scenario in spec["execution"]["scenarios"]:
        avg = means[scenario]
        rows = [g for g in groups if g["scenario"] == scenario]
        values = [g["metrics"][a] for g in rows for a in ("candidate", "prior_cost_aware")]
        if avg["candidate"] is None or avg["prior_cost_aware"] is None:
            incremental_unknown = True
            continue
        primary = scenario in ("75bp_full", "120bp_full")
        fails = (avg["candidate"] <= avg["prior_cost_aware"] if primary
                 else avg["candidate"] < avg["prior_cost_aware"])
        if fails:
            label = scenario+":mean_does_not_improve_prior" if primary else scenario+":mean_worse_than_prior"
            violations.append(label)
            incremental_failures.append(label)
        if all(v["complete"] for v in values):
            new_mdd = min(g["metrics"]["candidate"]["price_mdd_pct"] for g in rows)
            old_mdd = min(g["metrics"]["prior_cost_aware"]["price_mdd_pct"] for g in rows)
            if new_mdd < old_mdd:
                incremental_failures.append(scenario+":worst_mdd_worse_than_prior")
    result["violations"] = violations
    result["decision"] = ("observed_rejection" if violations else
        "not_evaluable_source_or_tail" if result["incomplete_paths"] else "conditional_development_support")
    result["incremental_assessment"] = {
        "decision": "incremental_rejection" if incremental_failures else
                    "not_evaluable_source_or_tail" if incremental_unknown else "incremental_support",
        "violations": incremental_failures, "production_change_authorized": False}
    return result


def preflight(root, implementation_commit, budget):
    raw = frozen.fixed_bytes(root/PLAN_PATH, PLAN_SHA)
    if raw != subprocess.check_output(["git", "show", f"{PLAN_COMMIT}:{PLAN_PATH}"], cwd=root, timeout=15):
        raise ValueError("new_plan_not_precommitted")
    plan = json.loads(raw)
    spec, inputs = frozen.preflight(root, implementation_commit, budget)
    for name in EXTRA_CODE_PATHS:
        body = (root/name).read_bytes()
        if body != subprocess.check_output(["git", "show", f"{implementation_commit}:{name}"], cwd=root, timeout=15):
            raise ValueError("new_code_not_precommitted:"+name)
        inputs.provenance["code_sha256"][name] = frozen.sha(body)
    for module, name in ((frozen, frozen.CODE_PATHS[0]),
                         (__import__(utility.__module__, fromlist=["utility"]), EXTRA_CODE_PATHS[2])):
        if Path(module.__file__).resolve() != root/name:
            raise ValueError("import_outside_declared_repository")
    if Path(__file__).resolve() != root/EXTRA_CODE_PATHS[0]:
        raise ValueError("runner_outside_declared_repository")
    old = plan["parent_plan"]
    if old["sha256"] != frozen.PLAN_SHA or old["path"] != frozen.PLAN_PATH:
        raise ValueError("parent_plan_differs")
    item = plan["parent_result"]
    parent = json.loads(frozen.fixed_bytes(root/item["path"], item["sha256"]))
    if (parent["completed_paths"] != 80 or parent["path_count"] != 80
            or parent["verdict"]["decision"] != "observed_rejection"):
        raise ValueError("parent_result_not_expected_completed_rejection")
    item = plan["rationale_diagnostic"]
    frozen.fixed_bytes(root/item["path"], item["sha256"])
    inputs.provenance["partial_keep_plan"] = {"sha256": PLAN_SHA, "commit": PLAN_COMMIT}
    inputs.provenance["parent_result"] = plan["parent_result"]
    inputs.provenance["rationale_diagnostic"] = plan["rationale_diagnostic"]
    spec["periods"][0]["arms"] = plan["comparison"]["arms"]
    spec["candidate_id"] = plan["candidate_id"]
    return plan, spec, inputs, parent


def run(root, implementation_commit):
    budget = frozen.Budget()
    plan, spec, inputs, parent = preflight(root, implementation_commit, budget)
    old = {(g["scenario"], g["phase"]): g for g in parent["groups"]}
    groups = []
    for scenario in spec["execution"]["scenarios"]:
        for phase in spec["schedule"]["phases"]:
            group = frozen.replay_group(inputs, spec, scenario, phase, budget,
                target_selector=targets, decision_contract_sha=PLAN_SHA,
                forecast_required_arms=("candidate", "prior_cost_aware"))
            group["parent_crosscheck"] = parent_crosscheck(group, old[scenario, phase])
            c, p = group["metrics"]["candidate"], group["metrics"]["prior_cost_aware"]
            group["deltas"]["prior_cost_aware"] = ({k: c[k]-p[k] for k in
                ("net_pnl_krw", "before_cost_price_pnl_krw", "fees_krw")} if c["complete"] and p["complete"] else None)
            groups.append(group)
    paths = sum(len(g["metrics"]) for g in groups)
    if paths != plan["comparison"]["trading_paths"]:
        raise ValueError("new_path_count_differs")
    completed = sum(m["complete"] for g in groups for m in g["metrics"].values())
    return {"schema": "kr-partial-keep-policy-result-v1", "candidate_id": plan["candidate_id"],
        "state": "comparison_complete" if completed == paths else "comparison_incomplete",
        "provenance": inputs.provenance, "groups": groups, "path_count": paths,
        "completed_paths": completed, "parent_complete_matches": sum(c["complete_match"] for g in groups for c in g["parent_crosscheck"]),
        "verdict": verdict(groups, spec), "historical_runs": 1,
        "cash": {"initial_krw": spec["portfolio"]["initial_cash_krw"], "net_return_pct": 0},
        "wall_seconds": frozen.time.monotonic()-budget.start,
        "fit_calls": 0, "forecast_reconstruction_calls": 0, "new_market_data_requests": 0,
        "production_engine_changed": False, "live_order_eligible": False,
        "verified_total_return": False, "pristine_oos": False, "strict_pit_eligible": False,
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
            json.dump({"state": "implementation_error", "error_type": type(exc).__name__,
                       "error": str(exc), "production_engine_changed": False}, stream)
            raise
        json.dump(result, stream, ensure_ascii=False, allow_nan=False)
    print(json.dumps({"path_count": result["path_count"], "completed_paths": result["completed_paths"],
        "parent_complete_matches": result["parent_complete_matches"], "verdict": result["verdict"],
        "output_sha256": frozen.sha(output.read_bytes())}, ensure_ascii=False))


if __name__ == "__main__":
    main()
