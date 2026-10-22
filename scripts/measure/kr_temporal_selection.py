"""Frozen expanding-month selection comparison, offline cohort returns only."""
from __future__ import annotations

import argparse
from collections import defaultdict, Counter
import datetime as dt
import json
from pathlib import Path
from statistics import mean
import subprocess

from scripts.measure import kr_ridge_selection_lab as old
from scripts.measure import kr_frozen_forecast_policy as frozen
from signal_desk.signals import research_ridge as ridge

PLAN = "docs/kr-temporal-selection-plan-2026-10-23.json"
SHA = "fb13ace7021c56fb9b355bdc9ff40bff9bd690846afd6b12f14665997d3e4cc5"
COMMIT = "6520da6bcca2a6b20ba126c2bde3216d4347806a"
CODE = ("scripts/measure/kr_temporal_selection.py", "tests/test_kr_temporal_selection.py",
    "scripts/measure/kr_ridge_selection_lab.py", "src/signal_desk/signals/research_ridge.py",
    "scripts/measure/kr_financial_input_sources.py", "scripts/measure/kr_original_financial_sources.py",
    "scripts/measure/price_challenger_lab.py", "scripts/measure/krx_price_replay_pilot.py",
    "src/signal_desk/signals/research_financial_inputs.py")


def training(rows, start):
    selected = [r for r in rows if r["date"] < start and r["state"] == "matured" and r["exit_date"] < start]
    if not selected:
        raise ValueError("empty_training")
    return selected


def fit_fold(rows, cfg, start):
    train = training(rows, start)
    cutoff = (dt.date.fromisoformat(start)-dt.timedelta(days=1)).isoformat()
    fit_cfg = {**cfg, "train_end": max(r["date"] for r in train), "fit_through": cutoff, "evaluation_start": start}
    return ridge.fit(train, fit_cfg)


def cohort(rows, scores):
    if set(scores) != {r["ticker"] for r in rows} or len(rows) < 160:
        raise ValueError("incomplete_feature_universe")
    top = sorted(scores, key=lambda t: (-scores[t], t))[:6]
    pool = {r["ticker"]: r for r in rows}
    missing = [{"ticker": t, "state": pool[t]["state"]} for t in top if pool[t]["state"] != "matured"]
    targets = [pool[t].get("target") for t in top]
    full = all(r["state"] == "matured" for r in rows)
    return {"selected": top, "targets": targets, "missing": missing,
        "gross": mean(targets) if not missing else None,
        "universe_mean": mean(r["target"] for r in rows) if full else None,
        "universe_missing": sum(r["state"] != "matured" for r in rows)}


def path(records, arm, bps, phase):
    chosen = records[phase::5]
    balance, missing = 1., []
    for r in chosen:
        if r["common_missing"]:
            missing.append(r["date"])
            continue
        side = bps/20000
        net = mean((1+y)*(1-side)/(1+side)-1 for y in r[arm]["targets"])
        balance *= 1+net
    return {"arm": arm, "cost_bps": bps, "phase": phase, "cohorts": len(chosen),
        "missing_dates": missing, "complete": bool(chosen) and not missing,
        "net_cohort_return_pct": (balance-1)*100 if chosen and not missing else None}


def verdict(folds):
    violations, unknown = [], []
    for f in folds:
        for cost in (75, 120):
            paths = [p for p in f["paths"] if p["cost_bps"] == cost]
            pairs = [(next(p for p in paths if p["arm"] == "ridge14" and p["phase"] == ph),
                next(p for p in paths if p["arm"] == "momentum12_skip21" and p["phase"] == ph)) for ph in range(5)]
            for a, b in pairs:
                if a["complete"] and b["complete"] and a["net_cohort_return_pct"] < b["net_cohort_return_pct"]:
                    violations.append(f"{f['start']}/{cost}/phase{a['phase']}:negative_phase_excess")
            if all(a["complete"] and b["complete"] for a,b in pairs):
                deltas = [a["net_cohort_return_pct"]-b["net_cohort_return_pct"] for a,b in pairs]
                if mean(deltas) <= 0: violations.append(f"{f['start']}/{cost}:mean_excess_nonpositive")
            else:
                unknown.append(f"{f['start']}/{cost}:incomplete_selected_labels")
        rs = f["records"]
        if all(r[a]["gross"] is not None
               for r in rs for a in ("ridge14", "momentum12_skip21")):
            # Identical daily universe: the unknown common market mean cancels.
            lifts = {a: mean(r[a]["gross"] for r in rs)
                for a in ("ridge14", "momentum12_skip21")}
            if lifts["ridge14"] <= lifts["momentum12_skip21"]:
                violations.append(f"{f['start']}:lift_not_improved")
        else:
            unknown.append(f"{f['start']}:selected_lift_difference_unavailable")
    return {"decision": "rejected" if violations else "not_evaluable" if unknown else "research_selection_support",
        "violations": violations, "unavailable": unknown, "live_order_eligible": False}


def run(root, commit):
    plan = json.loads(frozen.fixed_bytes(root/PLAN, SHA))
    if (root/PLAN).read_bytes() != subprocess.check_output(["git", "show", f"{COMMIT}:{PLAN}"], cwd=root):
        raise ValueError("plan_not_precommitted")
    code = {}
    for name in CODE:
        body = (root/name).read_bytes()
        if body != subprocess.check_output(["git", "show", f"{commit}:{name}"], cwd=root):
            raise ValueError("code_not_precommitted")
        code[name] = frozen.sha(body)
    cfg = old.plan()
    scope = json.loads(frozen.fixed_bytes(old.SCOPE, old.SCOPE_SHA))
    frozen.fixed_bytes(old.FINANCIAL, old.FINANCIAL_SHA)
    days, sources = old.load_krx_archives([root/"data/research"/r["archive"] for r in scope["sources"][:9]])
    if sources != scope["sources"][:9]: raise ValueError("source_drift")
    panel = old.PricePanel(days)
    by_day = {d: {r["ISU_CD"]: r for r in rs} for d, rs in days.items()}
    financial = old.load_financials(old.FINANCIAL)["records"]
    roster = {r["ISU_CD"] for r in old.top200(days[cfg["financial_roster_asof"]])}
    rows, grouped, census = [], {}, Counter()
    for d in panel.sessions:
        if not cfg["train_start"] <= d <= cfg["train_end"]: continue
        feature_rows, *_ = old.features_at(d, panel, by_day, financial, roster)
        grouped[d] = []
        for r in feature_rows:
            label = old.label_at(d, r["ticker"], panel, by_day, 5)
            value = {**r, **label}; rows.append(value); grouped[d].append(value); census[label["state"]] += 1
    train_sha = old.encoded_digest([r for r in rows if r["state"] == "matured"])
    if train_sha != "0a028a5c3eb67371829f5916f06d91a2eb61b753c1c9da80eac36f26d66b1a9a":
        raise ValueError("original_training_drift")
    folds = []
    for start, end in plan["folds"]:
        model = fit_fold(rows, cfg, start)
        records = []
        for d, rs in grouped.items():
            if not start <= d <= end: continue
            predictions = ridge.predict([r["features"] for r in rs], model)
            record = {"date": d, "ridge14": cohort(rs, dict(zip([r["ticker"] for r in rs], predictions))),
                "momentum12_skip21": cohort(rs, {r["ticker"]: r["features"][4] for r in rs})}
            record["common_missing"] = bool(record["ridge14"]["missing"] or record["momentum12_skip21"]["missing"])
            records.append(record)
        folds.append({"start": start, "end": end, "model": model,
            "training_sha256": old.encoded_digest(training(rows, start)), "records": records,
            "paths": [path(records, a, c, ph) for a in plan["arms"] for c in (75,120) for ph in range(5)]})
    return {"schema": "kr-temporal-selection-result-v1", "plan_sha256": SHA, "plan_commit": COMMIT,
        "implementation_commit": commit, "code_sha256": code, "sources": sources,
        "financial_sha256": old.FINANCIAL_SHA, "scope_sha256": old.SCOPE_SHA,
        "training_rows_sha256": train_sha, "census": dict(census), "folds": folds,
        "paths": sum(len(f["paths"]) for f in folds),
        "completed": sum(p["complete"] for f in folds for p in f["paths"]), "verdict": verdict(folds),
        "fit_calls": 4, "historical_runs": 1, "new_market_data_requests": 0,
        "account_returns": False, "pristine_oos": False, "verified_total_return": False,
        "production_engine_changed": False, "integrated_roadmap_percent": 40}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", type=Path, required=True); p.add_argument("--implementation-commit", required=True)
    p.add_argument("--output", type=Path, required=True); args = p.parse_args()
    root, output = args.repo.resolve(), args.output.resolve()
    if not output.is_relative_to(root/"data/research"): p.error("research output required")
    with output.open("x") as stream:
        result = run(root, args.implementation_commit)
        json.dump(result, stream, ensure_ascii=False, allow_nan=False)
    print(json.dumps({k: result[k] for k in ("paths", "completed", "verdict")}))


if __name__ == "__main__": main()
