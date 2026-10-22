"""One temporally separated, frozen intercept correction of saved forecasts.

Offline research only. A completed calibration block fixes one nonnegative bias;
no evaluation label enters the selector and no forecast rank or core is changed.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import math
from pathlib import Path
from statistics import mean
import subprocess

from scripts.measure import kr_frozen_forecast_policy as frozen
from scripts.measure import kr_forecast_error_attribution as audit

PLAN_PATH = "docs/kr-frozen-bias-calibration-plan-2026-10-22.json"
PLAN_SHA = "3ac2fc720b5221e08a10fd9cc6ec205d007e8952560cb73d3cda1487a4bba9c9"
PLAN_COMMIT = "b3c61b4dc5097feeac7568bdefa6ac6f6d246435"
EXTRA_CODE_PATHS = ("scripts/measure/kr_frozen_bias_calibration.py",
    "tests/test_kr_frozen_bias_calibration.py", *audit.CODE_PATHS)


def top6_labels(inputs, dates):
    records = []
    for day in dates:
        forecasts = inputs.forecasts[day]
        top = sorted(forecasts, key=lambda t: (-forecasts[t], t))[:6]
        if len(top) != 6:
            raise ValueError("six_forecasts_required")
        sessions = frozen.market_clock.next_sessions("kr", day, 6)
        labels = {t: audit.target_label(inputs, day, t, sessions) for t in top}
        actual = audit.complete_mean([labels[t]["gross_return"] for t in top])
        predicted = mean(forecasts[t] for t in top)
        records.append({"session": day, "tickers": top, "labels": labels,
            "matures_on": sessions[-1], "forecast_mean": predicted, "actual_mean": actual,
            "error": None if actual is None else predicted-actual})
    return records


def fit_calibration(inputs, plan):
    c, evaluation = plan["calibration"], plan["evaluation"]
    dates = sorted(inputs.forecasts)[:c["dates"]]
    if len(dates) != c["dates"] or (dates[0], dates[-1]) != (c["start"], c["end"]):
        raise ValueError("calibration_calendar_differs")
    # The label reader receives training dates only, not future evaluation rows.
    records = top6_labels(inputs, dates)
    maturity = max(r["matures_on"] for r in records)
    if maturity != c["latest_target_maturity"] or maturity >= evaluation["start"]:
        raise ValueError("calibration_target_not_mature_before_evaluation")
    errors = [r["error"] for r in records]
    if any(e is None or not math.isfinite(e) for e in errors):
        raise ValueError("calibration_requires_all_top6_labels")
    if sum(len(r["tickers"]) for r in records) != c["stock_date_labels"]:
        raise ValueError("calibration_label_count_differs")
    return {"bias": max(0., mean(errors)), "unconstrained_mean_error": mean(errors),
        "dates": len(records), "stock_date_labels": c["stock_date_labels"],
        "latest_target_maturity": maturity, "fit_calls": 1, "records": records,
        "target_and_rank_unchanged": True, "evaluation_labels_used": 0}


def selector(bias):
    if type(bias) not in (int, float) or not math.isfinite(bias) or bias < 0:
        raise ValueError("nonnegative_finite_bias_required")

    def select(arm, forecasts, caps, legacy, holdings, closes, nav, spec):
        if arm != "candidate":
            return frozen.targets("candidate" if arm == "prior_cost_aware" else arm,
                forecasts, caps, legacy, holdings, closes, nav, spec)
        calibrated = {t: p-bias for t, p in forecasts.items()}
        rank = lambda values: sorted(values, key=lambda t: (-values[t], t))
        if any(not math.isfinite(p) for p in calibrated.values()) or rank(forecasts) != rank(calibrated):
            raise ValueError("calibration_must_preserve_finite_rank")
        weights, priority, choice = frozen.targets("candidate", calibrated, caps, legacy,
            holdings, closes, nav, spec)
        return weights, priority, {**choice, "calibration_bias": bias,
            "raw_forecasts_in_pool": {t: forecasts[t] for t in sorted(set(rank(forecasts)[:6]) | set(holdings))}}

    return select


def evaluation_inputs(inputs, spec, plan):
    e = plan["evaluation"]
    dates = [d for d in sorted(inputs.forecasts) if d > plan["calibration"]["latest_target_maturity"]]
    if ((dates[0], dates[-1], len(dates)) != (e["start"], e["end"], e["dates"])
            or sum(len(inputs.forecasts[d]) for d in dates) != e["forecast_rows"]):
        raise ValueError("evaluation_calendar_or_count_differs")
    data = frozen.Inputs({d: dict(inputs.forecasts[d]) for d in dates},
        {d: list(inputs.legacy[d]) for d in dates}, inputs.rows, inputs.previous, inputs.reviewed, inputs.provenance)
    out = deepcopy(spec)
    out["candidate_id"] = plan["candidate_id"]
    out["periods"][0].update(decision_start=e["start"], decision_end=e["end"],
        tail_through=e["tail_through"], arms=plan["comparison"]["arms"],
        expected_decision_dates=e["dates"], expected_prediction_rows=e["forecast_rows"])
    return data, out


def verdict(groups, spec):
    out = frozen.verdict(groups, spec)
    failures = []
    unknown = bool(out["incomplete_paths"])
    for scenario in spec["execution"]["scenarios"]:
        avg = out["scenario_mean_price_return_pct"][scenario]
        c, p = avg["candidate"], avg["prior_cost_aware"]
        if c is None or p is None:
            unknown = True
            continue
        primary = scenario in ("75bp_full", "120bp_full")
        failed_mean = c <= p if primary else c < p
        if failed_mean:
            failures.append(scenario+":mean_not_improved" if primary else scenario+":mean_regression")
        rows = [g for g in groups if g["scenario"] == scenario]
        candidate_mdd = min(g["metrics"]["candidate"]["price_mdd_pct"] for g in rows)
        prior_mdd = min(g["metrics"]["prior_cost_aware"]["price_mdd_pct"] for g in rows)
        if candidate_mdd < prior_mdd:
            failures.append(scenario+":worst_mdd_regression")
    out["incremental_assessment"] = {"decision": "incremental_rejection" if failures else
        "not_evaluable_source_or_tail" if unknown else "incremental_support",
        "violations": failures, "production_change_authorized": False}
    return out


def prediction_diagnostic(records, bias):
    def stats(errors):
        if not errors or any(e is None for e in errors):
            return {"mean_error": None, "mae": None, "rmse": None}
        return {"mean_error": mean(errors), "mae": mean(abs(e) for e in errors),
            "rmse": math.sqrt(mean(e*e for e in errors))}
    raw = [r["error"] for r in records]
    corrected = [None if e is None else e-bias for e in raw]
    return {"dates": len(records), "unknown_dates": sum(e is None for e in raw),
        "units": "return_fraction_not_probability", "raw": stats(raw), "calibrated": stats(corrected),
        "records": records, "overlapping_h5_not_independent": True}


def preflight(root, implementation_commit, budget):
    raw = frozen.fixed_bytes(root/PLAN_PATH, PLAN_SHA)
    if raw != subprocess.check_output(["git", "show", f"{PLAN_COMMIT}:{PLAN_PATH}"], cwd=root, timeout=15):
        raise ValueError("calibration_plan_not_precommitted")
    plan = json.loads(raw)
    if plan["parent_plan"] != {"path": frozen.PLAN_PATH, "sha256": frozen.PLAN_SHA}:
        raise ValueError("parent_plan_differs")
    spec, inputs = frozen.preflight(root, implementation_commit, budget)
    for path in EXTRA_CODE_PATHS:
        body = (root/path).read_bytes()
        if body != subprocess.check_output(["git", "show", f"{implementation_commit}:{path}"], cwd=root, timeout=15):
            raise ValueError("calibration_code_not_precommitted:"+path)
        inputs.provenance["code_sha256"][path] = frozen.sha(body)
    for module, path in ((audit, audit.CODE_PATHS[0]), (frozen, frozen.CODE_PATHS[0])):
        if Path(module.__file__).resolve() != root/path:
            raise ValueError("import_outside_declared_repository")
    if Path(__file__).resolve() != root/EXTRA_CODE_PATHS[0]:
        raise ValueError("runner_outside_declared_repository")
    inputs.provenance["calibration_plan"] = {"path": PLAN_PATH, "sha256": PLAN_SHA, "commit": PLAN_COMMIT}
    return plan, spec, inputs


def run(root, implementation_commit):
    budget = frozen.Budget()
    plan, spec, inputs = preflight(root, implementation_commit, budget)
    calibration = fit_calibration(inputs, plan)
    evaluation, spec = evaluation_inputs(inputs, spec, plan)
    contract = frozen.encoded_sha({"plan_sha256": PLAN_SHA, "calibration": calibration})
    groups = []
    for scenario in spec["execution"]["scenarios"]:
        for phase in spec["schedule"]["phases"]:
            group = frozen.replay_group(evaluation, spec, scenario, phase, budget,
                target_selector=selector(calibration["bias"]), decision_contract_sha=contract,
                forecast_required_arms=("candidate", "prior_cost_aware"))
            c, p = group["metrics"]["candidate"], group["metrics"]["prior_cost_aware"]
            group["deltas"]["prior_cost_aware"] = ({k: c[k]-p[k] for k in
                ("net_pnl_krw", "before_cost_price_pnl_krw", "fees_krw")} if c["complete"] and p["complete"] else None)
            groups.append(group)
    paths = sum(len(g["metrics"]) for g in groups)
    if paths != plan["comparison"]["trading_paths"]:
        raise ValueError("calibration_path_count_differs")
    completed = sum(m["complete"] for g in groups for m in g["metrics"].values())
    # Post-hoc diagnostic only; this label reader never enters selection or fitting.
    prediction = prediction_diagnostic(top6_labels(evaluation, sorted(evaluation.forecasts)), calibration["bias"])
    return {"schema": "kr-frozen-bias-calibration-result-v1", "candidate_id": plan["candidate_id"],
        "state": "comparison_complete" if paths == completed else "comparison_incomplete",
        "provenance": inputs.provenance, "calibration": calibration, "decision_contract_sha256": contract,
        "evaluation": plan["evaluation"], "prediction_diagnostic": prediction,
        "groups": groups, "path_count": paths, "completed_paths": completed, "verdict": verdict(groups, spec),
        "cash": {"initial_krw": spec["portfolio"]["initial_cash_krw"], "net_return_pct": 0},
        "historical_runs": 1, "calibration_fit_calls": 1, "base_model_fit_calls": 0,
        "new_market_data_requests": 0, "forecast_reconstruction_calls": 0,
        "wall_seconds": frozen.time.monotonic()-budget.start,
        "production_engine_changed": False, "live_order_eligible": False,
        "verified_total_return": False, "strict_pit_eligible": False, "pristine_oos": False,
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
    print(json.dumps({"bias": result["calibration"]["bias"], "paths": result["path_count"],
        "completed": result["completed_paths"], "verdict": result["verdict"],
        "sha256": frozen.sha(output.read_bytes())}, ensure_ascii=False))


if __name__ == "__main__":
    main()
