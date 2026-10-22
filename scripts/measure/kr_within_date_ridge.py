"""Attribute the saved Ridge and fit one within-date research challenger.

Offline, existing raw sources only. No production imports execute this module.
No post-result tuning or daily market-return regression is performed.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import json
import math
from pathlib import Path
from statistics import mean
import subprocess

import numpy as np

from scripts.measure import kr_entry_maturity as evaluation
from scripts.measure import kr_integer_risk_budget as parent
from scripts.measure import kr_frozen_forecast_policy as frozen
from scripts.measure import kr_ridge_selection_lab as original
from signal_desk.signals import research_ridge as ridge

PLAN_PATH = "docs/kr-within-date-ridge-plan-2026-10-23.json"
PLAN_SHA = "576b691b462197da13a8b512c9eb495008462d6330931c70904473dae5474575"
PLAN_COMMIT = "7914907b29cbdce543176bc9b1038bc4ba9828af"
MODEL_PATH = "data/research/kr-ridge-r3-model-2026-10-17-v1.json"
MODEL_SHA = "81de9f736b6c3e853bfa7a8a30009551aa5757783e50adf8128e2726d737fdd0"
CODE_PATHS = ("scripts/measure/kr_within_date_ridge.py", "tests/test_kr_within_date_ridge.py",
    "scripts/measure/kr_ridge_selection_lab.py", "src/signal_desk/signals/research_ridge.py",
    "scripts/measure/kr_financial_input_sources.py", "scripts/measure/kr_original_financial_sources.py",
    "scripts/measure/price_challenger_lab.py", "src/signal_desk/signals/research_financial_inputs.py",
    "scripts/measure/kr_entry_maturity.py", "tests/test_kr_entry_maturity.py")


def centered(values, dates):
    """Date blocks, never a rolling window including a future prediction date."""
    x = np.asarray(values, dtype=float)
    if len(x) != len(dates) or not len(x) or not np.isfinite(x).all():
        raise ValueError("invalid_centering_input")
    out = np.empty_like(x)
    indices = defaultdict(list)
    for i, d in enumerate(dates):
        indices[d].append(i)
    for idx in indices.values():
        out[idx] = x[idx]-x[idx].mean(axis=0)
    return out


def fit_within(rows, standardizer, cfg):
    if (not rows or cfg["fit_through"] >= cfg["evaluation_start"]
            or any(not cfg["train_start"] <= r["date"] <= cfg["train_end"]
                or not r["date"] < r["exit_date"] <= cfg["fit_through"] for r in rows)
            or len({(r["date"], r["ticker"]) for r in rows}) != len(rows)):
        raise ValueError("training_contract_violation")
    dates = [r["date"] for r in rows]
    counts = Counter(dates)
    w = np.array([1/(len(counts)*counts[d]) for d in dates])
    x = ridge.transform([r["features"] for r in rows], standardizer)[:, 1:]
    y = np.array([r["target"] for r in rows], dtype=float)
    xc, yc = centered(x, dates), centered(y, dates)
    alpha = cfg["alpha_weighted_mean_objective"]
    if not math.isfinite(alpha) or alpha <= 0:
        raise ValueError("invalid_alpha")
    beta = np.linalg.solve(xc.T @ (w[:, None]*xc)+alpha*np.eye(xc.shape[1]), xc.T @ (w*yc))
    if not np.isfinite(beta).all():
        raise ValueError("nonfinite_coefficients")
    common = float(w @ y)
    within = xc.T @ (w*yc)
    pooled = (x-w @ x).T @ (w*(y-common))
    return {"coefficients": beta.tolist(), "common_return": common, "alpha": alpha,
        "training_rows": len(rows), "training_dates": len(counts), "training_last_date": max(counts),
        "last_label_exit": max(r["exit_date"] for r in rows), "fit_calls": 1,
        "standardizer_sha256": frozen.encoded_sha(standardizer),
        "within_covariance_xy": within.tolist(), "between_covariance_xy": (pooled-within).tolist()}


def predict_day(rows, standardizer, model):
    if (not rows or len({r["date"] for r in rows}) != 1
            or len({r["ticker"] for r in rows}) != len(rows)
            or frozen.encoded_sha(standardizer) != model["standardizer_sha256"]
            or rows[0]["date"] <= model["last_label_exit"]):
        raise ValueError("prediction_contract_violation")
    x = ridge.transform([r["features"] for r in rows], standardizer)[:, 1:]
    result = model["common_return"]+(x-x.mean(axis=0)) @ np.asarray(model["coefficients"])
    if not np.isfinite(result).all():
        raise ValueError("nonfinite_prediction")
    return result.tolist()


def reconstruct(root, archive, budget):
    cfg = original.plan()
    scope = json.loads(frozen.fixed_bytes(original.SCOPE, original.SCOPE_SHA))
    frozen.fixed_bytes(original.FINANCIAL, original.FINANCIAL_SHA)
    days, sources = original.load_krx_archives([root/"data/research"/s["archive"] for s in scope["sources"][:9]])
    if sources != scope["sources"][:9]:
        raise ValueError("training_source_hashes_differ")
    days = {d: rs for d, rs in days.items() if d <= cfg["liquidate_through"]}
    by_day = {d: {r["ISU_CD"]: r for r in rs} for d, rs in days.items()}
    panel = original.PricePanel(days)
    financials = original.load_financials(original.FINANCIAL)["records"]
    roster = {r["ISU_CD"] for r in original.top200(days[cfg["financial_roster_asof"]])}
    saved = json.loads(frozen.fixed_bytes(root/MODEL_PATH, MODEL_SHA))
    if saved["fit_module_sha256"] != frozen.sha(Path(ridge.__file__).read_bytes()):
        raise ValueError("original_fit_module_changed")
    model = saved["model"]
    train, features, census = [], {}, Counter()
    for d in panel.sessions:
        budget.check()
        is_train = cfg["train_start"] <= d <= cfg["train_end"]
        if not is_train and d not in archive:
            continue
        rows, *_ = original.features_at(d, panel, by_day, financials, roster)
        if is_train:
            for r in rows:
                label = original.label_at(d, r["ticker"], panel, by_day, cfg["horizon_open_to_open_sessions"])
                census[label["state"]] += 1
                if label["state"] == "matured":
                    train.append({**r, **label})
        else:
            predictions = ridge.predict([r["features"] for r in rows], model)
            if {r["ticker"] for r in rows} != set(archive[d]):
                raise ValueError("original_prediction_keys_differ")
            for r, mu in zip(rows, predictions):
                if not math.isclose(mu, archive[d][r["ticker"]]["forecast"], abs_tol=1e-14, rel_tol=1e-12):
                    raise ValueError("original_prediction_drift")
            features[d] = rows
    if original.encoded_digest(train) != saved["training_rows_sha256"] or set(features) != set(archive):
        raise ValueError("original_training_or_evaluation_drift")
    return cfg, model, train, features, {"sources": sources, "census": dict(census),
        "training_rows_sha256": saved["training_rows_sha256"], "original_model_sha256": MODEL_SHA,
        "financial_sha256": original.FINANCIAL_SHA, "scope_sha256": original.SCOPE_SHA}


def describe(rows, model):
    x = ridge.transform([r["features"] for r in rows], model)
    contributions = x*np.array(model["coefficients"])
    prediction = contributions.sum(axis=1)
    known = [i for i, r in enumerate(rows) if r["target"] is not None]
    names = ["intercept"]+model["features"]+["missing_"+n for n in model["features"]]
    return {"rows": len(rows), "known": len(known), "unknown": len(rows)-len(known),
        "mean_forecast": float(prediction.mean()),
        "known_mean_actual": mean(rows[i]["target"] for i in known) if known else None,
        "known_mean_error": mean(prediction[i]-rows[i]["target"] for i in known) if known else None,
        "contributions": dict(zip(names, contributions.mean(axis=0).tolist()))}


def diagnose(train, features, archive, model):
    ev = [{**r, "target": archive[d][r["ticker"]]["label"]["gross_return"],
        "top6": archive[d][r["ticker"]]["top6"]} for d, rs in features.items() for r in rs]
    groups = {"training": describe(train, model), "evaluation": describe(ev, model),
        "evaluation_top6": describe([r for r in ev if r["top6"]], model),
        "evaluation_top6_down5": describe([r for r in ev if r["top6"] and r["features"][1]<0], model)}
    monthly = []
    for source, rows in (("training", train), ("evaluation", ev)):
        by_day = defaultdict(list)
        for r in rows:
            by_day[r["date"]].append(r)
        for month in sorted({d[:7] for d in by_day}):
            dates = [d for d in sorted(by_day) if d.startswith(month)]
            market = [by_day[d][0]["features"][8] for d in dates]
            actual = [mean(r["target"] for r in by_day[d] if r["target"] is not None) for d in dates]
            corr = float(np.corrcoef(market, actual)[0, 1]) if min(np.std(market), np.std(actual))>0 else None
            monthly.append({"source": source, "month": month, "dates": len(dates),
                "market20_mean": mean(market), "market20_min": min(market), "market20_max": max(market),
                "known_date_equal_h5_mean": mean(actual), "market20_h5_correlation": corr,
                "missing_labels": sum(r["target"] is None for d in dates for r in by_day[d])})
    return {"groups": groups, "monthly": monthly, "contribution_is_causal": False,
        "independent_rows": False, "pristine_oos": False}


def selector(bias):
    old = parent.selector(bias)
    def select(arm, forecasts, caps, legacy, holdings, closes, nav, spec, *, context):
        if arm == "prior_integer":
            ctx = {k: deepcopy(v) for k, v in context.items() if k != "candidate_risk_budget"}
            return old("candidate", forecasts, caps, legacy, holdings, closes, nav, spec, context=ctx)
        if arm in frozen.CONTROLS:
            return frozen.targets(arm, forecasts, caps, legacy, holdings, closes, nav, spec)
        if arm == "candidate":
            new = context["within_date_forecasts"]
            if set(new) != set(forecasts):
                raise ValueError("candidate_forecast_universe_differs")
            return old(arm, new, caps, legacy, holdings, closes, nav, spec, context=context)
        return old(arm, forecasts, caps, legacy, holdings, closes, nav, spec, context=context)
    return select


def prediction_comparison(features, archive, predictions, dates):
    result = {}
    for pool in ("all", "old_top6", "candidate_top6"):
        values = []
        for d in dates:
            selected = set(sorted(predictions[d], key=lambda t: (-predictions[d][t], t))[:6])
            for r in features[d]:
                t = r["ticker"]; a = archive[d][t]
                if pool == "old_top6" and not a["top6"] or pool == "candidate_top6" and t not in selected:
                    continue
                values.append((a["forecast"], predictions[d][t], a["label"]["gross_return"]))
        known = [v for v in values if v[2] is not None]
        result[pool] = {"rows": len(values), "known": len(known), "unknown": len(values)-len(known),
            "known_mean_actual": mean(v[2] for v in known) if known else None,
            **{name: {"known_mean_forecast": mean(v[i] for v in known),
                "known_mean_error": mean(v[i]-v[2] for v in known),
                "known_mae": mean(abs(v[i]-v[2]) for v in known)} for i, name in enumerate(("original", "candidate"))}}
    return result


def run(root, commit):
    budget = frozen.Budget()
    raw = frozen.fixed_bytes(root/PLAN_PATH, PLAN_SHA)
    if raw != subprocess.check_output(["git", "show", f"{PLAN_COMMIT}:{PLAN_PATH}"], cwd=root):
        raise ValueError("plan_not_precommitted")
    plan = json.loads(raw)
    _, spec, inputs, _ = parent.preflight(root, commit, budget)
    for path in CODE_PATHS:
        body = (root/path).read_bytes()
        if body != subprocess.check_output(["git", "show", f"{commit}:{path}"], cwd=root):
            raise ValueError("model_code_not_precommitted:"+path)
        inputs.provenance["code_sha256"][path] = frozen.sha(body)
    def load(key):
        item = plan[key]
        return json.loads(frozen.fixed_bytes(root/item["path"], item["sha256"]))
    previous, labels = load("parent_result"), load("label_archive")
    if any(r["provenance"]["input_sha256"] != inputs.provenance["input_sha256"] for r in (previous, labels)):
        raise ValueError("policy_sources_differ")
    archive = evaluation.archive_by_day(labels)
    cfg, model, train, features, source = reconstruct(root, archive, budget)
    if (source["training_rows_sha256"] != plan["training"]["rows_sha256"] or
            len(train) != plan["training"]["rows"] or len({r["date"] for r in train}) != plan["training"]["dates"]):
        raise ValueError("training_plan_differs")
    diagnostic = diagnose(train, features, archive, model)
    new_model = fit_within(train, model, cfg)  # The sole new historical fit.
    predictions = {d: {r["ticker"]: mu for r, mu in zip(rs, predict_day(rs, model, new_model))}
        for d, rs in features.items()}
    if (sum(map(len, predictions.values())) != plan["evaluation"]["diagnostic_rows"]
            or len(inputs.forecasts) != plan["evaluation"]["policy_dates"]
            or sum(map(len, inputs.forecasts.values())) != plan["evaluation"]["policy_rows"]):
        raise ValueError("evaluation_coverage_differs")
    for d, f in inputs.forecasts.items():
        if {t: r["forecast"] for t, r in archive[d].items()} != f:
            raise ValueError("policy_original_forecast_drift")
    spec["periods"][0]["arms"] = plan["arms"]
    spec["candidate_id"] = plan["candidate_id"]
    contexts = {}
    def factory(d, f):
        if d not in contexts:
            contexts[d] = parent.decision_context(inputs, d, f)
            contexts[d]["within_date_forecasts"] = predictions[d]
            contexts[d]["within_date_model_sha256"] = frozen.encoded_sha(new_model)
        return deepcopy(contexts[d])
    groups, matches = [], 0
    old = {(g["scenario"], g["phase"]): g for g in previous["groups"]}
    clean = parent.parent.parent_tools.without_ids
    for sc in spec["execution"]["scenarios"]:
        for phase in spec["schedule"]["phases"]:
            g = frozen.replay_group(inputs, spec, sc, phase, budget,
                target_selector=selector(previous["bias_reused"]), decision_contract_sha=PLAN_SHA,
                forecast_required_arms=("candidate", "prior_integer"), decision_context_factory=factory,
                additional_quote_names=lambda d, f: sorted(predictions[d], key=lambda t: (-predictions[d][t], t))[:6])
            prior = old[sc, phase]
            for a, b in evaluation.OLD.items():
                m, before = g["metrics"][a], prior["metrics"][b]
                if clean(m["fills"]) != clean(before["fills"][:len(m["fills"])]):
                    raise ValueError("parent_fill_prefix_differs")
                byday = {r["session"]: r["books"][b] for r in prior["trace"]}
                if any(clean(t["books"][a]) != clean(byday[t["session"]]) for t in g["trace"] if a in t["books"]):
                    raise ValueError("parent_daily_book_differs")
                if m["complete"]:
                    if clean(m) != clean(before):
                        raise ValueError("parent_economics_differ")
                    matches += 1
            groups.append(g)
    paths = sum(len(g["metrics"]) for g in groups)
    if paths != plan["paths"]:
        raise ValueError("path_count_differs")
    return {"schema": "kr-within-date-ridge-result-v1", "provenance": inputs.provenance,
        "plan_sha256": PLAN_SHA, "plan_commit": PLAN_COMMIT, "source": source,
        "diagnostic": diagnostic, "model": new_model, "original_model": model,
        "predictions": predictions, "prediction_comparison": {
            "all49dates": prediction_comparison(features, archive, predictions, sorted(features)),
            "policy23dates": prediction_comparison(features, archive, predictions, sorted(inputs.forecasts))},
        "bias_reused": previous["bias_reused"], "groups": groups,
        "verdict": evaluation.verdict(groups, spec), "paths": paths,
        "completed": sum(m["complete"] for g in groups for m in g["metrics"].values()),
        "parent_complete_matches": matches, "fit_calls": 1, "historical_runs": 1,
        "new_market_data_requests": 0, "wall_seconds": frozen.time.monotonic()-budget.start,
        "production_engine_changed": False, "pristine_oos": False, "strict_pit_eligible": False,
        "verified_total_return": False, "live_order_eligible": False, "integrated_roadmap_percent": 40}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--implementation-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(); root = args.repo.resolve(); output = args.output.resolve()
    if not output.is_relative_to(root/"data/research"):
        parser.error("local research output required")
    with output.open("x", encoding="utf-8") as stream:
        try:
            result = run(root, args.implementation_commit)
        except Exception as exc:
            json.dump({"state": "implementation_error", "error": str(exc)}, stream)
            raise
        json.dump(result, stream, ensure_ascii=False, allow_nan=False)
    print(json.dumps({k: result[k] for k in ("paths", "completed", "parent_complete_matches", "verdict")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
