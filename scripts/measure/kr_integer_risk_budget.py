"""Frozen integer-share policy and contemporaneous risk-ceiling controls.

Offline only. Risk matching uses planned as-of downside charges, never future
realized volatility. All execution still goes through the unchanged order book.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from itertools import combinations
import json
import math
from pathlib import Path
import subprocess

from scripts.measure import kr_stock_downside_action as parent
from scripts.measure import kr_frozen_forecast_policy as frozen

PLAN_PATH = "docs/kr-integer-risk-budget-plan-2026-10-23.json"
PLAN_SHA = "3b83a3d0ef33842e295f9e9b5fbc19e01630cf9d46fb1100a896fac912dfa289"
PLAN_COMMIT = "01529e3b6f996656a123f03cd328e78e366623c0"
CODE_PATHS = ("scripts/measure/kr_integer_risk_budget.py", "tests/test_kr_integer_risk_budget.py")
PARENT_ARMS = {"prior_downside": "candidate", **{a: a for a in
    ("action_only", "prior_calibrated", "prior_cost_aware", *frozen.CONTROLS)}}
MATCHED = ("matched_ridge", "matched_cap6")
EPS = 1e-12


def infeasible(day, ticker, field):
    error = frozen.DataGap(day, ticker, field)
    error.detail["reason"] = "execution_constraints_infeasible"
    raise error


def decision_context(inputs, day, forecasts):
    context = parent.decision_context(inputs, day, forecasts)
    for t, record in context["stocks"].items():
        row = inputs.rows.get(day, {}).get(t, {})
        record["close"] = frozen.number(row.get("TDD_CLSPRC"))
        record["volume"] = frozen.number(row.get("ACC_TRDVOL"))
    return context


def coordinates(names, may_add, holdings, nav, context, spec):
    if not frozen.positive(nav):
        raise ValueError("positive_own_nav_required")
    cap = spec["portfolio"]["slot_weight"]
    if cap*spec["portfolio"]["max_positions"] > spec["portfolio"]["budget"]+EPS:
        raise ValueError("coordinate_caps_exceed_budget")
    result = {}
    for t in sorted(names):
        s = context["stocks"].get(t, {})
        price, volume, d = s.get("close"), s.get("volume"), s.get("downside")
        if (not frozen.positive(price) or type(volume) not in (int, float)
                or not math.isfinite(volume) or volume < 0 or type(d) not in (int, float)
                or not math.isfinite(d) or d < 0):
            raise frozen.DataGap(context["session"], t, "integer_quote_or_downside_missing")
        h = holdings.get(t, 0)
        if type(h) is not int or h < 0:
            raise ValueError("integer_holding_required")
        participation = math.floor(volume*spec["execution"]["prior_volume_participation"])
        reserve = price*(1+spec["execution"]["buy_limit_above_close"])*(1+spec["candidate"]["decision_one_way_cost"])
        maximum = (h+math.floor((cap*nav-h*price)/reserve) if h*price <= cap*nav
                   else math.floor(cap*nav/price))
        maximum = min(maximum, h+participation, h if t not in may_add else maximum)
        minimum = max(0, h-participation)
        if minimum > maximum:
            infeasible(context["session"], t, "integer_participation_vs_cap")
        result[t] = {"price": price, "volume": volume, "downside": d,
            "held": h, "minimum": minimum, "maximum": maximum, "reserve_per_buy_share": reserve}
    return result


def quantity_choice(row, mu, nav, spec):
    c, cap = spec["candidate"], spec["portfolio"]["slot_weight"]
    h, price, d, lo, hi = (row[k] for k in ("held", "price", "downside", "minimum", "maximum"))
    candidates = {lo, hi, max(lo, min(h, hi))}
    if d > 0:
        for net in (mu-c["return_buffer"], mu-c["return_buffer"]-2*c["decision_one_way_cost"]):
            root = cap*net/d*nav/price
            candidates.update(max(lo, min(q, hi)) for q in (math.floor(root), math.ceil(root)))
    utility = lambda q: parent.coordinate_utility(q*price/nav, h*price/nav, mu, d, cap,
        c["return_buffer"], c["decision_one_way_cost"])
    values = {q: utility(q) for q in candidates}
    best = max(values.values())
    q = min((q for q in candidates if best-values[q] <= EPS), key=lambda q: (abs(q-h), q))
    return {**row, "mu": mu, "quantity": q, "utility": values[q],
            "zero_utility": utility(0), "gain_over_zero": values[q]-utility(0)}


def choose_quantities(forecasts, holdings, nav, context, spec):
    if len(forecasts) < spec["portfolio"]["min_forecast_names"] or set(holdings)-set(forecasts):
        raise ValueError("integer_forecast_universe_incomplete")
    if any(not math.isfinite(v) for v in forecasts.values()):
        raise ValueError("nonfinite_forecast")
    top = sorted(forecasts, key=lambda t: (-forecasts[t], t))[:spec["candidate"]["new_candidate_count"]]
    coords = coordinates(set(top) | set(holdings), set(top), holdings, nav, context, spec)
    rows = {t: quantity_choice(row, forecasts[t], nav, spec) for t, row in coords.items()}
    mandatory = {t for t, row in rows.items() if row["minimum"] > 0}
    optional = sorted(t for t, row in rows.items() if t not in mandatory and row["quantity"] > 0)
    maximum = spec["portfolio"]["max_positions"]
    if len(mandatory) > maximum:
        infeasible(context["session"], "*", "integer_mandatory_positions")
    alternatives = []
    for size in range(min(maximum-len(mandatory), len(optional))+1):
        for selected in combinations(optional, size):
            selected = mandatory | set(selected)
            quantities = {t: row["quantity"] if t in selected else 0 for t, row in rows.items()}
            utility = sum(row["utility"] if t in selected else row["zero_utility"] for t, row in rows.items())
            turnover = sum(abs(q-rows[t]["held"])*rows[t]["price"]/nav for t, q in quantities.items())
            alternatives.append((utility, turnover, tuple(sorted(selected)), quantities))
    best = max(a[0] for a in alternatives)
    winner = min((a for a in alternatives if best-a[0] <= EPS), key=lambda a: (a[1], a[2]))
    return winner[3], {"choice": "integer_downside_action", "utility": winner[0],
        "turnover": winner[1], "alternatives": len(alternatives), "coordinates": rows}


def order_spec(spec):
    return {**spec["execution"], "decision_one_way_cost": spec["candidate"]["decision_one_way_cost"]}


def encode_quantities(quantities, holdings, nav, context, spec):
    """Encode each chosen quantity in an order interval, then verify the roundtrip.

    Command weights reserve purchase buffers; they are not economic exposures.
    Tiny nextafter corrections only handle floating-point endpoint arithmetic.
    """
    names = sorted(set(quantities) | set(holdings))
    cap, weights = spec["portfolio"]["slot_weight"], {}
    closes = {t: context["stocks"][t]["close"] for t in names}
    volumes = {t: context["stocks"][t]["volume"] for t in names}
    for t in names:
        q, h, price = quantities.get(t, 0), holdings.get(t, 0), closes[t]
        if type(q) is not int or q < 0:
            raise ValueError("nonnegative_integer_target_required")
        if q > h:
            reserve = price*(1+spec["execution"]["buy_limit_above_close"])*(1+spec["candidate"]["decision_one_way_cost"])
            weight = min(cap, (h*price+(q-h+.5)*reserve)/nav)
        elif 0 < q < h:
            weight = min(cap, (q+.5)*price/nav)
        else:
            weight = q*price/nav
        for _ in range(16):
            commands = frozen.core.orders({t: weight} if weight else {}, {t: h} if h else {},
                {t: price}, {t: volumes[t]}, nav, order_spec(spec))
            actual = h+sum(o["shares"]*(1 if o["side"] == "buy" else -1) for o in commands)
            if actual == q:
                break
            weight = math.nextafter(weight, math.inf if actual < q else -math.inf)
        else:
            raise ValueError("integer_order_roundtrip_failed:"+t)
        if not 0 <= weight <= cap+EPS:
            raise ValueError("integer_command_exceeds_cap")
        if weight > 0:
            weights[t] = weight
    orders = frozen.core.orders(weights, holdings, closes, volumes, nav, order_spec(spec))
    post = dict(holdings)
    for o in orders:
        t = o["ticker"]
        post[t] = post.get(t, 0)+(1 if o["side"] == "buy" else -1)*o["shares"]
    if any(post.get(t, 0) != quantities.get(t, 0) for t in names):
        raise ValueError("integer_portfolio_roundtrip_failed")
    return weights, orders


def risk_charge(quantities, nav, context, spec):
    cap = spec["portfolio"]["slot_weight"]
    return sum(.5*context["stocks"][t]["downside"]*(q*context["stocks"][t]["close"]/nav)**2/cap
               for t, q in quantities.items() if q)


def matched_quantities(shape, holdings, nav, context, spec):
    match = context.get("candidate_risk_budget")
    if not match or match["session"] != context["session"]:
        raise ValueError("candidate_risk_budget_must_precede_control")
    ceiling = match["charge"]
    coords = coordinates(set(shape) | set(holdings), set(shape), holdings, nav, context, spec)
    def at(scale):
        return {t: max(r["minimum"], min(r["maximum"], math.floor(
            scale*shape.get(t, 0)*nav/r["price"]))) for t, r in coords.items()}
    def feasible(q):
        return (sum(v > 0 for v in q.values()) <= spec["portfolio"]["max_positions"]
                and risk_charge(q, nav, context, spec) <= ceiling+EPS)
    if not feasible(at(0.)):
        infeasible(context["session"], "*", "matched_unavoidable_risk_above_ceiling")
    if feasible(at(1.)):
        scale, high = 1., 1.
    else:
        scale, high = 0., 1.
        for _ in range(60):
            mid = (scale+high)/2
            if feasible(at(mid)):
                scale = mid
            else:
                high = mid
    q = at(scale)
    return q, {"choice": "integer_scaled_simple_control", "scale": scale,
        "ceiling": ceiling, "planned_charge": risk_charge(q, nav, context, spec),
        "next_scale": high, "next_scale_feasible": feasible(at(high)),
        "candidate_plan_sha256": match["plan_sha256"], "coordinates": coords}


def selector(bias):
    base = parent.selector(bias)
    def select(arm, forecasts, caps, legacy, holdings, closes, nav, spec, *, context):
        if arm in PARENT_ARMS:
            return base(PARENT_ARMS[arm], forecasts, caps, legacy, holdings, closes, nav, spec, context=context)
        if arm not in ("candidate", *MATCHED):
            raise ValueError("unknown_integer_arm")
        if arm == "candidate":
            if "candidate_risk_budget" in context:
                raise ValueError("reused_candidate_risk_budget")
            calibrated = {t: p-bias for t, p in forecasts.items()}
            quantities, choice = choose_quantities(calibrated, holdings, nav, context, spec)
            context["candidate_risk_budget"] = {"session": context["session"],
                "charge": risk_charge(quantities, nav, context, spec),
                "plan_sha256": frozen.encoded_sha({"quantities": quantities, "nav": nav,
                    "holdings": holdings, "context": context}), "nav": nav, "quantities": quantities}
            priorities = calibrated
        else:
            raw_arm = "ridge_net" if arm == "matched_ridge" else "cap6"
            shape, priorities, _ = frozen.targets(raw_arm, forecasts, caps, legacy, holdings, closes, nav, spec)
            quantities, choice = matched_quantities(shape, holdings, nav, context, spec)
        weights, orders = encode_quantities(quantities, holdings, nav, context, spec)
        economic = {t: q*context["stocks"][t]["close"]/nav for t, q in quantities.items() if q}
        return weights, {t: priorities.get(t, 0.) for t in weights}, {**choice,
            "quantities": quantities, "economic_weights": economic, "command_weights": weights,
            "roundtrip_orders": orders, "planned_charge": risk_charge(quantities, nav, context, spec),
            "context_sha256": frozen.encoded_sha(context), "live_order_eligible": False}
    return select


def parent_crosscheck(group, previous):
    records = []
    clean = parent.parent_tools.without_ids
    for a, old in PARENT_ARMS.items():
        actual, expected = group["metrics"][a], previous["metrics"][old]
        if clean(actual["fills"]) != clean(expected["fills"][:len(actual["fills"])]):
            raise ValueError("integer_parent_fill_prefix_mismatch:"+a)
        by_day = {r["session"]: r["books"][old] for r in previous["trace"]}
        for row in group["trace"]:
            if a in row["books"] and clean(row["books"][a]) != clean(by_day[row["session"]]):
                raise ValueError("integer_parent_daily_book_mismatch:"+a)
        if actual["complete"] and clean(actual) != clean(expected):
            raise ValueError("integer_parent_metric_mismatch:"+a)
        records.append({"arm": a, "parent_arm": old, "complete_match": actual["complete"], "prefix_match": True})
    return records


def verdict(groups, spec):
    legacy = parent.verdict(groups, spec)
    failures, comparability, risk_rows = [], [], []
    for scenario, means in legacy["scenario_mean_price_return_pct"].items():
        rows = [g for g in groups if g["scenario"] == scenario]
        c = means["candidate"]
        primary = scenario in ("75bp_full", "120bp_full")
        p = means["prior_downside"]
        if c is not None and p is not None and (c <= p if primary else c < p):
            failures.append(scenario+":integer_mean_not_improved")
        for baseline in ("prior_downside", *MATCHED):
            if c is not None and means[baseline] is not None and min(g["metrics"]["candidate"]["price_mdd_pct"] for g in rows) < min(
                    g["metrics"][baseline]["price_mdd_pct"] for g in rows):
                failures.append(scenario+":worst_mdd_worse_than_"+baseline)
        for baseline in MATCHED:
            if primary:
                for g in rows:
                    cm, bm = g["metrics"]["candidate"], g["metrics"][baseline]
                    if cm["complete"] and bm["complete"] and cm["net_return_pct"] <= bm["net_return_pct"]:
                        failures.append(scenario+f"/phase{g['phase']}:not_above_"+baseline)
            elif c is not None and means[baseline] is not None and c <= means[baseline]:
                failures.append(scenario+":mean_not_above_"+baseline)
            decisions = [r["decision"] for g in rows for r in g["trace"] if r["decision"] and not r["decision"]["terminal"]]
            positive = [d for d in decisions if d["context"]["candidate_risk_budget"]["charge"] > 0]
            total = sum(d["context"]["candidate_risk_budget"]["charge"] for d in positive)
            used = sum(d["plans"][baseline]["choice"]["planned_charge"] for d in positive)
            ratio = used/total if total > 0 else None
            risk_rows.append({"scenario": scenario, "arm": baseline, "positive_budget_decisions": len(positive),
                "zero_budget_decisions": len(decisions)-len(positive), "ceiling_sum": total,
                "planned_charge_sum": used, "utilization": ratio})
            if ratio is None or ratio < .80:
                comparability.append(scenario+":"+baseline+":risk_utilization_below80pct")
        if primary:
            if any(g["metrics"]["candidate"]["complete"] and g["metrics"]["candidate"]["net_return_pct"] <= 0 for g in rows):
                failures.append(scenario+":not_above_cash_every_phase")
        elif c is not None and c <= 0:
            failures.append(scenario+":mean_not_above_cash")
    incomplete = legacy["incomplete_paths"]
    return {"legacy_full_budget": legacy, "violations": failures, "risk_matching": risk_rows,
        "comparability_failures": comparability, "incomplete_paths": incomplete,
        "decision": "rejected" if failures else "not_evaluable_source_or_constraints" if incomplete else
                    "risk_match_insufficient" if comparability else "conditional_risk_budget_support",
        "production_change_authorized": False}


def preflight(root, implementation_commit, budget):
    raw = frozen.fixed_bytes(root/PLAN_PATH, PLAN_SHA)
    if raw != subprocess.check_output(["git", "show", f"{PLAN_COMMIT}:{PLAN_PATH}"], cwd=root, timeout=15):
        raise ValueError("integer_plan_not_precommitted")
    plan = json.loads(raw)
    if plan["parent_plan"] != {"path": parent.PLAN_PATH, "sha256": parent.PLAN_SHA}:
        raise ValueError("integer_parent_plan_differs")
    old_plan, spec, inputs, _ = parent.preflight(root, implementation_commit, budget)
    item = plan["parent_result"]
    previous = json.loads(frozen.fixed_bytes(root/item["path"], item["sha256"]))
    if (previous["path_count"] != 140 or previous["completed_paths"] != 140
            or previous["provenance"]["input_sha256"] != inputs.provenance["input_sha256"]
            or previous["provenance"]["raw_sources"] != inputs.provenance["raw_sources"]):
        raise ValueError("integer_parent_sources_differ")
    for path in CODE_PATHS:
        body = (root/path).read_bytes()
        if body != subprocess.check_output(["git", "show", f"{implementation_commit}:{path}"], cwd=root, timeout=15):
            raise ValueError("integer_code_not_precommitted:"+path)
        inputs.provenance["code_sha256"][path] = frozen.sha(body)
    if Path(__file__).resolve() != root/CODE_PATHS[0] or Path(parent.__file__).resolve() != root/parent.CODE_PATHS[0]:
        raise ValueError("integer_import_outside_repository")
    if any(old_plan["evaluation"][k] != plan["evaluation"][k] for k in ("start", "end", "dates", "forecast_rows", "tail_through")):
        raise ValueError("integer_evaluation_period_differs")
    inputs.provenance["integer_plan"] = {"path": PLAN_PATH, "sha256": PLAN_SHA, "commit": PLAN_COMMIT}
    inputs.provenance["parent_downside_result"] = item
    spec["candidate_id"] = plan["candidate_id"]
    spec["periods"][0]["arms"] = plan["comparison"]["arms"]
    return plan, spec, inputs, previous


def run(root, implementation_commit):
    budget = frozen.Budget()
    plan, spec, inputs, previous = preflight(root, implementation_commit, budget)
    if spec["periods"][0]["arms"][0] != "candidate":
        raise ValueError("candidate_must_set_asof_risk_budget_first")
    prior = {(g["scenario"], g["phase"]): g for g in previous["groups"]}
    contexts, groups = {}, []
    def context_factory(day, forecasts):
        if day not in contexts:
            contexts[day] = decision_context(inputs, day, forecasts)
        return deepcopy(contexts[day])  # each book group gets a fresh risk budget
    for scenario in spec["execution"]["scenarios"]:
        for phase in spec["schedule"]["phases"]:
            group = frozen.replay_group(inputs, spec, scenario, phase, budget,
                target_selector=selector(previous["bias_reused"]), decision_contract_sha=PLAN_SHA,
                forecast_required_arms=("candidate", "prior_downside", "action_only", "prior_calibrated", "prior_cost_aware"),
                decision_context_factory=context_factory)
            group["parent_crosscheck"] = parent_crosscheck(group, prior[scenario, phase])
            groups.append(group)
    paths = sum(len(g["metrics"]) for g in groups)
    if paths != plan["comparison"]["trading_paths"]:
        raise ValueError("integer_path_count_differs")
    complete = sum(m["complete"] for g in groups for m in g["metrics"].values())
    return {"schema": "kr-integer-risk-budget-result-v1", "candidate_id": plan["candidate_id"],
        "state": "comparison_complete" if paths == complete else "comparison_incomplete",
        "provenance": inputs.provenance, "evaluation": plan["evaluation"], "bias_reused": previous["bias_reused"],
        "groups": groups, "path_count": paths, "completed_paths": complete,
        "parent_complete_matches": sum(r["complete_match"] for g in groups for r in g["parent_crosscheck"]),
        "verdict": verdict(groups, spec), "wall_seconds": frozen.time.monotonic()-budget.start,
        "historical_runs": 1, "fit_calls": 0, "new_market_data_requests": 0,
        "production_engine_changed": False, "live_order_eligible": False,
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
    print(json.dumps({k: result[k] for k in ("state", "path_count", "completed_paths", "parent_complete_matches", "verdict")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
