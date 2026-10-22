"""One offline entry-only correction, using completed h5 labels at decision time."""
from __future__ import annotations

import argparse
from collections import defaultdict
from copy import deepcopy
from itertools import combinations
import json
import math
from pathlib import Path
from statistics import mean
import subprocess

from scripts.measure import kr_integer_risk_budget as parent
from scripts.measure import kr_frozen_forecast_policy as frozen

PLAN_PATH = "docs/kr-entry-maturity-plan-2026-10-23.json"
PLAN_SHA = "1e916c7508f5bb98a12d2405532e2cb3a4a41999537046105dc84faa360d64b4"
PLAN_COMMIT = "9cbf3273ad5c0860aafdfb84b4194bdf3a998a6c"
CODE_PATHS = ("scripts/measure/kr_entry_maturity.py", "tests/test_kr_entry_maturity.py")
OLD = {"prior_integer": "candidate", **{a: a for a in frozen.CONTROLS}}


def archive_by_day(archive):
    result = defaultdict(dict)
    for row in archive["rows"]:
        d, t = row["session"], row["ticker"]
        if t in result[d] or not math.isfinite(row["forecast"]):
            raise ValueError("duplicate_or_invalid_archive_prediction")
        result[d][t] = row
    return dict(result)


def matured_bias(day, archive, fixed_bias, window=20):
    # Derive readiness from the exchange calendar BEFORE accessing any outcome.
    ready = [(d, frozen.market_clock.next_sessions("kr", d, 6)[-1]) for d in sorted(archive) if d < day]
    ready = [(d, end) for d, end in ready if end <= day][-window:]
    if len(ready) != window:
        raise frozen.DataGap(day, "*", "entry_calibration_insufficient_mature_dates")
    records = []
    for d, end in ready:
        rows = archive[d]
        top = sorted(rows, key=lambda t: (-rows[t]["forecast"], t))[:6]
        if len(top) != 6:
            raise frozen.DataGap(day, "*", "entry_calibration_top6_missing")
        values = []
        for t in top:
            label = rows[t]["label"]
            y = label["gross_return"]
            if (type(y) not in (int, float) or not math.isfinite(y)
                    or label.get("exit_on") != end or label.get("missing") is not None):
                raise frozen.DataGap(day, t, "entry_calibration_label_unavailable")
            values.append(rows[t]["forecast"]-y)
        records.append({"forecast_on": d, "matures_on": end, "top6": top, "mean_error": mean(values)})
    error = mean(r["mean_error"] for r in records)
    return {"decision_on": day, "entry_bias": max(fixed_bias, error), "held_bias": fixed_bias,
        "window_dates": window, "label_count": window*6, "mean_error": error,
        "latest_maturity": max(r["matures_on"] for r in records), "records": records,
        "evidence_sha256": frozen.encoded_sha(records)}


def row_stats(rows):
    known = [r for r in rows if r["gross_return"] is not None]
    pos = [r for r in rows if r["mu"] > 0]
    wrong = sum(r["gross_return"] is not None and r["gross_return"] < 0 for r in pos)
    unknown_pos = sum(r["gross_return"] is None for r in pos)
    return {"rows": len(rows), "tickers": len({r["ticker"] for r in rows}),
        "known": len(known), "unknown": len(rows)-len(known),
        "known_only_mean_forecast": mean(r["mu"] for r in known) if known else None,
        "known_only_mean_gross_return": mean(r["gross_return"] for r in known) if known else None,
        "known_only_mean_error": mean(r["mu"]-r["gross_return"] for r in known) if known else None,
        "full_mean_error": mean(r["mu"]-r["gross_return"] for r in known) if known and len(known)==len(rows) else None,
        "positive_forecasts": len(pos), "positive_but_negative": wrong,
        "positive_negative_fraction_bounds": [wrong/len(pos), (wrong+unknown_pos)/len(pos)] if pos else [None, None]}


def diagnose(previous, archive):
    rows = []
    for g in previous["groups"]:
        if g["scenario"] != "75bp_full":
            continue
        for tr in g["trace"]:
            d = tr["decision"]
            if not d or d["terminal"]:
                continue
            day, holdings = tr["session"], tr["books"]["candidate"]["holdings"]
            quantities = d["plans"]["candidate"]["choice"]["quantities"]
            pool = archive[day]
            top = sorted(pool, key=lambda t: (-pool[t]["forecast"], t))[:6]
            for t, v in sorted(pool.items()):
                past = d["context"]["stocks"][t]["return5"]
                rows.append({"session": day, "ticker": t, "phase": g["phase"],
                    "role": "held" if holdings.get(t, 0) else "top6_unheld" if t in top else "other_unheld",
                    "selected": quantities.get(t, 0)>0,
                    "past_move": "unknown" if past is None else "down" if past<0 else "not_down",
                    "mu": v["forecast"]-previous["bias_reused"], "gross_return": v["label"]["gross_return"],
                    "missing": v["label"]["missing"]})
    if len({(r["session"], r["ticker"]) for r in rows}) != len(rows):
        raise ValueError("diagnostic_duplicate_date_ticker")
    groups = {}
    for role in ("held", "top6_unheld", "other_unheld"):
        for selection in ("all", "selected"):
            for state in ("all", "down", "not_down", "unknown"):
                subset = [r for r in rows if r["role"] == role and (selection=="all" or r["selected"])
                    and (state=="all" or r["past_move"]==state)]
                groups[f"{role}/{selection}/{state}"] = row_stats(subset)
    return {"all": row_stats(rows), "dates": len({r["session"] for r in rows}),
        "groups": groups, "rows": rows, "role_is_policy_dependent": True,
        "labels_are_gross_h5_not_account_returns": True, "independent_samples": False}


def choose(forecasts, holdings, nav, context, spec, calibration):
    """Preserve the raw top-six pool even though held/unheld corrections differ."""
    top = sorted(forecasts, key=lambda t: (-forecasts[t], t))[:spec["candidate"]["new_candidate_count"]]
    if set(holdings)-set(forecasts):
        raise ValueError("held_forecast_missing")
    coords = parent.coordinates(set(top)|set(holdings), set(top), holdings, nav, context, spec)
    rows = {t: parent.quantity_choice(r, forecasts[t]-(calibration["held_bias"] if holdings.get(t,0)
        else calibration["entry_bias"]), nav, spec) for t, r in coords.items()}
    mandatory = {t for t,r in rows.items() if r["minimum"]>0}
    optional = sorted(t for t,r in rows.items() if t not in mandatory and r["quantity"]>0)
    remaining = spec["portfolio"]["max_positions"]-len(mandatory)
    if remaining < 0:
        parent.infeasible(context["session"], "*", "entry_mandatory_positions")
    alternatives = []
    for n in range(min(remaining,len(optional))+1):
        for names in combinations(optional,n):
            selected = mandatory|set(names)
            q = {t:r["quantity"] if t in selected else 0 for t,r in rows.items()}
            utility = sum(r["utility"] if t in selected else r["zero_utility"] for t,r in rows.items())
            turnover = sum(abs(q[t]-r["held"])*r["price"]/nav for t,r in rows.items())
            alternatives.append((utility,turnover,tuple(sorted(selected)),q))
    best = max(v[0] for v in alternatives)
    win = min((v for v in alternatives if best-v[0]<=parent.EPS),key=lambda v:(v[1],v[2]))
    return win[3], {"coordinates":rows,"utility":win[0],"raw_top6":top,"calibration":calibration}


def selector(fixed_bias):
    old = parent.selector(fixed_bias)
    def select(arm, forecasts, caps, legacy, holdings, closes, nav, spec, *, context):
        if arm == "prior_integer":
            ctx = {k:deepcopy(v) for k,v in context.items() if k not in ("candidate_risk_budget", "entry_calibration")}
            return old("candidate", forecasts,caps,legacy,holdings,closes,nav,spec,context=ctx)
        if arm in frozen.CONTROLS:
            return frozen.targets(arm,forecasts,caps,legacy,holdings,closes,nav,spec)
        if arm in parent.MATCHED:
            return old(arm,forecasts,caps,legacy,holdings,closes,nav,spec,context=context)
        if arm != "candidate" or "candidate_risk_budget" in context:
            raise ValueError("unknown_arm_or_reused_context")
        cal = context["entry_calibration"]
        if cal["decision_on"] != context["session"] or cal["latest_maturity"] > context["session"]:
            raise ValueError("entry_future_calibration")
        q, choice = choose(forecasts,holdings,nav,context,spec,cal)
        weights, orders = parent.encode_quantities(q,holdings,nav,context,spec)
        charge = parent.risk_charge(q,nav,context,spec)
        context["candidate_risk_budget"] = {"session":context["session"],"charge":charge,
            "plan_sha256":frozen.encoded_sha({"quantities":q,"holdings":holdings,"nav":nav,"context":context})}
        return weights,{t:choice["coordinates"][t]["mu"] for t in weights},{**choice,
            "quantities":q,"roundtrip_orders":orders,"planned_charge":charge,"live_order_eligible":False}
    return select


def verdict(groups,spec):
    base = frozen.verdict(groups,spec)
    violations, matching, insuff = [],[],[]
    for sc,means in base["scenario_mean_price_return_pct"].items():
        gs=[g for g in groups if g["scenario"]==sc]
        c,p=means["candidate"],means["prior_integer"]
        primary=sc in ("75bp_full","120bp_full")
        if c is not None and p is not None and (c<=p if primary else c<p):
            violations.append(sc+":entry_mean_not_improved")
        for a in ("prior_integer",*parent.MATCHED):
            if c is not None and means[a] is not None and min(g["metrics"]["candidate"]["price_mdd_pct"] for g in gs)<min(g["metrics"][a]["price_mdd_pct"] for g in gs):
                violations.append(sc+":worst_mdd_worse_than_"+a)
        for a in parent.MATCHED:
            if primary:
                for g in gs:
                    cm,bm=g["metrics"]["candidate"],g["metrics"][a]
                    if cm["complete"] and bm["complete"] and cm["net_return_pct"]<=bm["net_return_pct"]:
                        violations.append(sc+f"/phase{g['phase']}:not_above_"+a)
            elif c is not None and means[a] is not None and c<=means[a]:
                violations.append(sc+":mean_not_above_"+a)
            ds=[t["decision"] for g in gs for t in g["trace"] if t["decision"] and not t["decision"]["terminal"]]
            positive=[d for d in ds if d["context"]["candidate_risk_budget"]["charge"]>0]
            total=sum(d["context"]["candidate_risk_budget"]["charge"] for d in positive)
            used=sum(d["plans"][a]["choice"]["planned_charge"] for d in positive)
            ratio=used/total if total else None
            matching.append({"scenario":sc,"arm":a,"positive_decisions":len(positive),"zero_decisions":len(ds)-len(positive),"utilization":ratio})
            if ratio is None or ratio<.8:
                insuff.append(sc+":"+a)
        if primary:
            if any(g["metrics"]["candidate"]["complete"] and g["metrics"]["candidate"]["net_return_pct"]<=0 for g in gs):
                violations.append(sc+":not_above_cash_every_phase")
        elif c is not None and c<=0:
            violations.append(sc+":mean_not_above_cash")
    return {"legacy_full_budget":base,"violations":violations,"risk_matching":matching,
        "comparability_failures":insuff,"decision":"rejected" if violations else
        "not_evaluable" if base["incomplete_paths"] else "risk_match_insufficient" if insuff else "conditional_development_support",
        "production_change_authorized":False}


def run(root,commit):
    budget=frozen.Budget()
    plan=json.loads(frozen.fixed_bytes(root/PLAN_PATH,PLAN_SHA))
    if (root/PLAN_PATH).read_bytes()!=subprocess.check_output(["git","show",f"{PLAN_COMMIT}:{PLAN_PATH}"],cwd=root):
        raise ValueError("plan_not_precommitted")
    _,spec,inputs,previous=parent.preflight(root,commit,budget)
    item=plan["parent_result"]
    previous=json.loads(frozen.fixed_bytes(root/item["path"],item["sha256"]))
    item=plan["label_archive"]
    archive_raw=json.loads(frozen.fixed_bytes(root/item["path"],item["sha256"]))
    if archive_raw["provenance"]["input_sha256"]!=inputs.provenance["input_sha256"]:
        raise ValueError("label_source_contract_differs")
    archive=archive_by_day(archive_raw)
    for day,forecasts in inputs.forecasts.items():
        if {t:r["forecast"] for t,r in archive[day].items()}!=forecasts:
            raise ValueError("label_forecasts_differ")
    for path in CODE_PATHS:
        body=(root/path).read_bytes()
        if body!=subprocess.check_output(["git","show",f"{commit}:{path}"],cwd=root):
            raise ValueError("entry_code_not_precommitted")
        inputs.provenance["code_sha256"][path]=frozen.sha(body)
    diagnostic=diagnose(previous,archive)
    if (diagnostic["all"]["rows"],diagnostic["all"]["tickers"],diagnostic["dates"])!=(4391,201,23):
        raise ValueError("diagnostic_coverage_differs")
    fixed=previous["bias_reused"]
    # Materialize each snapshot with its own as-of readiness rule; selector sees no labels.
    calibrations={d:matured_bias(d,archive,fixed,plan["calibration"]["window_dates"]) for d in inputs.forecasts}
    spec["periods"][0]["arms"]=plan["arms"]
    spec["candidate_id"]=plan["candidate_id"]
    contexts={}
    def factory(d,f):
        if d not in contexts:
            contexts[d]=parent.decision_context(inputs,d,f)
            contexts[d]["entry_calibration"]=calibrations[d]
        return deepcopy(contexts[d])
    groups=[]
    old={(g["scenario"],g["phase"]):g for g in previous["groups"]}
    clean=parent.parent.parent_tools.without_ids
    matches=0
    for sc in spec["execution"]["scenarios"]:
        for phase in spec["schedule"]["phases"]:
            g=frozen.replay_group(inputs,spec,sc,phase,budget,target_selector=selector(fixed),
                decision_contract_sha=PLAN_SHA,forecast_required_arms=("candidate","prior_integer"),decision_context_factory=factory)
            prior=old[sc,phase]
            for a,b in OLD.items():
                m=g["metrics"][a]; before=prior["metrics"][b]
                if clean(m["fills"])!=clean(before["fills"][:len(m["fills"])]):
                    raise ValueError("parent_fill_prefix_differs")
                byday={r["session"]:r["books"][b] for r in prior["trace"]}
                if any(clean(t["books"][a])!=clean(byday[t["session"]]) for t in g["trace"] if a in t["books"]):
                    raise ValueError("parent_daily_book_differs")
                if m["complete"]:
                    if clean(m)!=clean(before):
                        raise ValueError("parent_economics_differ")
                    matches+=1
            groups.append(g)
    count=sum(len(g["metrics"]) for g in groups)
    if count!=plan["paths"]:
        raise ValueError("path_count_differs")
    inputs.provenance["entry_plan"]={"sha256":PLAN_SHA,"commit":PLAN_COMMIT}
    return {"schema":"kr-entry-maturity-result-v1","provenance":inputs.provenance,"parent_result":plan["parent_result"],
        "label_archive":plan["label_archive"],"diagnostic":diagnostic,"calibrations":calibrations,
        "groups":groups,"verdict":verdict(groups,spec),"paths":count,
        "completed":sum(m["complete"] for g in groups for m in g["metrics"].values()),"parent_complete_matches":matches,
        "historical_runs":1,"base_model_fit_calls":0,"calibration_snapshots":len(calibrations),
        "new_market_data_requests":0,"wall_seconds":frozen.time.monotonic()-budget.start,
        "production_engine_changed":False,"pristine_oos":False,"strict_pit_eligible":False,
        "verified_total_return":False,"live_order_eligible":False,"integrated_roadmap_percent":40}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo",type=Path,required=True)
    parser.add_argument("--implementation-commit",required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args();root=args.repo.resolve();out=args.output.resolve()
    if not out.is_relative_to(root/"data/research"):
        parser.error("local research output required")
    with out.open("x",encoding="utf-8") as stream:
        try:
            result=run(root,args.implementation_commit)
        except Exception as exc:
            json.dump({"state":"implementation_error","error":str(exc)},stream)
            raise
        json.dump(result,stream,ensure_ascii=False,allow_nan=False)
    print(json.dumps({k:result[k] for k in ("paths","completed","parent_complete_matches","verdict")},ensure_ascii=False))


if __name__=="__main__":
    main()
