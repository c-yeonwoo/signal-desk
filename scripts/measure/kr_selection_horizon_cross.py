"""Fixed offline selection x holding-horizon comparison; no production calls."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
from statistics import mean
import subprocess

from scripts.measure import kr_frozen_forecast_policy as f

DESIGN = "docs/KR-WHOLE-CHAIN-DIAGNOSIS-2026-10-23.md"
DESIGN_COMMIT = "b1f1119067c4e6576af515f639b7d00e02bb7029"
CODE = ("scripts/measure/kr_selection_horizon_cross.py", "tests/test_kr_selection_horizon_cross.py")
ARMS = ("legacy_h5", "legacy_h20", "cap_h5", "cap_h20")
SCENARIOS = ("75bp_full", "120bp_full", "75bp_half", "75bp_delay1")


def selection(inputs, day):
    # Only contemporaneous fields define eligibility. Never look at outcomes.
    pool = {t for t in inputs.forecasts[day]
            if f.positive(f.number(inputs.rows[day].get(t, {}).get("TDD_CLSPRC")))
            and f.positive(f.number(inputs.rows[day].get(t, {}).get("MKTCAP")))}
    legacy = inputs.legacy[day]
    if not set(legacy) <= pool:
        raise f.DataGap(day, "*", "legacy_outside_contemporaneous_pool")
    caps = sorted(pool, key=lambda t: (-f.field(inputs, day, t, "MKTCAP"), t))[:len(legacy)]
    return {"legacy": list(legacy), "cap": caps, "pool_count": len(pool)}


def preflight(root, commit):
    budget = f.Budget()
    body = (root / DESIGN).read_bytes()
    if body != subprocess.check_output(["git", "show", f"{DESIGN_COMMIT}:{DESIGN}"], cwd=root):
        raise ValueError("design_not_equal_to_precommit")
    spec, inputs = f.preflight(root, commit, budget)
    for path in CODE:
        source = (root / path).read_bytes()
        if source != subprocess.check_output(["git", "show", f"{commit}:{path}"], cwd=root):
            raise ValueError("implementation_not_precommitted")
        inputs.provenance["code_sha256"][path] = f.sha(source)
    period = spec["periods"][0]
    period.update(decision_end="2024-11-15", arms=list(ARMS))
    inputs.forecasts = {d: v for d, v in inputs.forecasts.items() if d <= period["decision_end"]}
    selected = {d: selection(inputs, d) for d in sorted(inputs.forecasts)}
    potential, decision_errors = [], []
    for day, choices in selected.items():
        names = set(choices["legacy"]) | set(choices["cap"])
        for t in sorted(names):
            for field in ("TDD_CLSPRC", "ACC_TRDVOL", "LIST_SHRS"):
                try:
                    f.field(inputs, day, t, field, allow_zero=field == "ACC_TRDVOL")
                except f.DataGap as exc:
                    decision_errors.append(exc.detail)
            # Conservative possible exposure interval, not a future eligibility mask.
            last = f.market_clock.next_sessions("kr", day, 2+20+1+5)[-1]
            settle = f.market_clock.next_sessions("kr", last, 2)[-1]
            if settle > period["tail_through"]:
                raise ValueError("settlement_tail_outside_preserved_calendar")
            for d in f.calendar(f.market_clock.next_sessions("kr", day, 1)[0], last):
                try:
                    f.unit_audit(inputs, d, {t})
                    for field in ("TDD_OPNPRC", "TDD_CLSPRC"):
                        f.field(inputs, d, t, field)
                except f.DataGap as exc:
                    potential.append(exc.detail)
    report = {"state": "blocked" if decision_errors else "ready",
        "design_commit": DESIGN_COMMIT, "design_sha256": f.sha(body),
        "decision_dates": len(selected), "selections": selected,
        "possible_names": sorted({t for s in selected.values() for a in ("legacy", "cap") for t in s[a]}),
        "decision_errors": decision_errors,
        "potential_exposure_gaps": list({f.encoded_sha(r): r for r in potential}.values()),
        "potential_gaps_are_not_selection_filters": True}
    return spec, inputs, report


def submit_preserving(book, day, target, closes, volumes, nav, priority, input_id):
    """Use the kernel's order builder while retaining unrelated pending exits."""
    builder = f.ExecutionBook(book.spec, book.scenario)
    builder.holdings = dict(book.holdings)
    orders = builder.submit(day, target, closes, volumes, nav,
                            priority=priority, input_id=input_id, budget=1.)
    pending_names = {o["ticker"] for o in book.pending}
    orders = [o for o in orders if o["ticker"] not in pending_names]
    book.pending.extend(orders)
    book.last_decision = day
    return deepcopy(orders)


def replay(inputs, spec, scenario, phase, budget):
    period = spec["periods"][0]
    dates = sorted(inputs.forecasts)
    scheduled = set(dates[phase::5])
    initial = spec["portfolio"]["initial_cash_krw"]
    books = {a: f.ExecutionBook(f.execution_spec(spec, period), scenario) for a in ARMS}
    entry, submitted = {a: {} for a in ARMS}, {a: set() for a in ARMS}
    curves, trace = {a: [] for a in ARMS}, []
    previous_close, previous_nav = {}, {a: float(initial) for a in ARMS}
    stop = None
    for day in f.calendar(period["decision_start"], period["tail_through"]):
        try:
            budget.check()
            names = set().union(*(set(b.holdings) | {o["ticker"] for o in b.pending} for b in books.values()))
            f.unit_audit(inputs, day, names)
            opens = {t: {"price": f.field(inputs, day, t, "TDD_OPNPRC", allow_zero=True),
                         "source_id": f"preserved-raw:{day}:{t}"} for t in names}
            held = set().union(*(set(b.holdings) for b in books.values()))
            for t, quote in opens.items():
                quote["open_tradable"] = quote["price"] > 0
                if t in held and not quote["open_tradable"]:
                    raise f.DataGap(day, t, "held_open_unavailable")
            before = {a: dict(b.holdings) for a, b in books.items()}
            old_fees = {a: b.fees for a, b in books.items()}
            for arm, book in books.items():
                for fill in book.open_session(day, opens):
                    t = fill["ticker"]
                    if fill["side"] == "buy":
                        if t in entry[arm]:
                            raise ValueError("unexpected_additional_buy")
                        entry[arm][t] = day
                    elif t not in book.holdings:
                        entry[arm].pop(t)
                        submitted[arm].discard(t)
                f.reconcile(book, day, initial)
            owned = set().union(*(set(b.holdings) for b in books.values()))
            closes = {t: f.field(inputs, day, t, "TDD_CLSPRC") for t in names | owned}
            row = {"session": day, "books": {}, "orders": {}}
            for arm, book in books.items():
                nav = book.price_nav(closes)
                gap = sum(q*(opens[t]["price"]-previous_close[t]) for t, q in before[arm].items())
                intraday = sum(q*(closes[t]-opens[t]["price"]) for t, q in book.holdings.items())
                fee = book.fees-old_fees[arm]
                f.close_enough(nav-previous_nav[arm], gap+intraday-fee, "horizon_daily_nav")
                r = {"session": day, "nav": nav,
                     "exposure": sum(q*closes[t] for t, q in book.holdings.items())/nav}
                curves[arm].append(r)
                row["books"][arm] = {**r, "holdings": dict(book.holdings),
                    "gap_pnl": gap, "intraday_pnl": intraday, "fees": fee}
                previous_nav[arm] = nav
            trace.append(row)
            previous_close = closes
            choices = selection(inputs, day) if day in scheduled else None
            if choices:
                row["selection"] = choices
            for arm, book in books.items():
                h = int(arm.rsplit("h", 1)[1])
                next_day = f.market_clock.next_sessions("kr", day, 1)[0]
                exits = {t for t, d in entry[arm].items() if t not in submitted[arm]
                         and f.market_clock.next_sessions("kr", d, h)[-1] == next_day}
                new = []
                if choices:
                    free = spec["portfolio"]["max_positions"]-len(book.holdings)
                    new = [t for t in choices[arm.split("_", 1)[0]]
                           if t not in book.holdings and t not in {o["ticker"] for o in book.pending}][:free]
                if not exits and not new:
                    continue
                order_names = set(book.holdings) | set(new)
                close = {t: f.field(inputs, day, t, "TDD_CLSPRC") for t in order_names}
                volume = {t: f.field(inputs, day, t, "ACC_TRDVOL", allow_zero=True) for t in order_names}
                nav = book.price_nav(close)
                target = {t: q*close[t]/nav for t, q in book.holdings.items() if t not in exits}
                target.update({t: 1/spec["portfolio"]["max_positions"] for t in new})
                # Never resize retained positions. Reject new orders that exceed own NAV.
                while sum(target.values()) > 1+1e-12 and new:
                    target.pop(new.pop())
                input_id = f.encoded_sha({"day": day, "arm": arm, "choices": choices,
                    "entry": entry[arm], "exits": sorted(exits), "target": target,
                    "closes": close, "volumes": volume, "nav": nav})
                row["orders"][arm] = submit_preserving(book, day, target, close, volume, nav,
                    {t: 0. for t in target} | {t: float(len(new)-i) for i, t in enumerate(new)}, input_id)
                submitted[arm].update(exits)
        except f.DataGap as exc:
            stop = exc.detail
            break
    return {"scenario": scenario, "phase": phase, "source_stop": stop,
            "metrics": {a: f.metrics(b, curves[a], initial, stop) for a, b in books.items()}, "trace": trace}


def verdict(groups):
    violations, means, effects = [], {}, {}
    complete = sum(m["complete"] for g in groups for m in g["metrics"].values())
    for sc in SCENARIOS:
        gs = [g for g in groups if g["scenario"] == sc]
        means[sc] = {a: mean(g["metrics"][a]["net_return_pct"] for g in gs)
                     if len(gs) == 5 and all(g["metrics"][a]["complete"] for g in gs) else None for a in ARMS}
        m = means[sc]
        if all(v is not None for v in m.values()):
            effects[sc] = {"selection_h5_pp": m["legacy_h5"]-m["cap_h5"],
                "selection_h20_pp": m["legacy_h20"]-m["cap_h20"],
                "holding_legacy_pp": m["legacy_h5"]-m["legacy_h20"],
                "holding_cap_pp": m["cap_h5"]-m["cap_h20"],
                "interaction_pp": m["legacy_h5"]-m["cap_h5"]-m["legacy_h20"]+m["cap_h20"]}
        if m["legacy_h5"] is not None:
            if m["legacy_h5"] <= 0:
                violations.append(sc+":candidate_mean_nonpositive")
            for control in ("legacy_h20", "cap_h5"):
                if m[control] is not None and m["legacy_h5"] <= m[control]:
                    violations.append(sc+":mean_not_above_"+control)
        if sc in ("75bp_full", "120bp_full"):
            for g in gs:
                c = g["metrics"]["legacy_h5"]
                if not c["complete"]:
                    continue
                if c["price_mdd_pct"] < -10:
                    violations.append(sc+f"/phase{g['phase']}:mdd_below_limit")
                for control in ("legacy_h20", "cap_h5"):
                    b = g["metrics"][control]
                    if b["complete"] and c["net_return_pct"] < b["net_return_pct"]:
                        violations.append(sc+f"/phase{g['phase']}:below_"+control)
            cdds = [g["metrics"]["legacy_h5"]["price_mdd_pct"] for g in gs if g["metrics"]["legacy_h5"]["complete"]]
            bdds = [g["metrics"]["legacy_h20"]["price_mdd_pct"] for g in gs if g["metrics"]["legacy_h20"]["complete"]]
            if cdds and len(bdds) == 5 and min(cdds) < min(bdds):
                violations.append(sc+":worst_mdd_worse_than_legacy_h20")
    return {"decision": "rejected" if violations else "inconclusive_data" if complete != 80
            else "conditional_development_support", "violations": violations,
            "completed_paths": complete, "scenario_means_pct": means, "effects": effects,
            "production_change_authorized": False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--implementation-commit", required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--preflight-only", action="store_true")
    args = p.parse_args()
    root = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    if not output.is_relative_to(root / "data/research"):
        p.error("output must remain in data/research")
    with output.open("x", encoding="utf-8") as stream:
        spec, inputs, audit = preflight(root, args.implementation_commit)
        groups = []
        if audit["state"] == "ready" and not args.preflight_only:
            budget = f.Budget()
            groups = [replay(inputs, spec, sc, phase, budget) for sc in SCENARIOS for phase in range(5)]
        result = {"candidate_id": "kr-selection-horizon-cross-v1", "preflight": audit,
            "provenance": inputs.provenance, "groups": groups, "path_count": len(groups)*4,
            "verdict": verdict(groups), "live_order_eligible": False, "verified_total_return": False}
        json.dump(result, stream, ensure_ascii=False, allow_nan=False)
    print(json.dumps({"preflight": audit["state"], "paths": len(groups)*4,
        "verdict": result["verdict"], "output_sha256": f.sha(output.read_bytes())}, ensure_ascii=False))


if __name__ == "__main__":
    main()
