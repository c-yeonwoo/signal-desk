"""One frozen, offline execution-policy comparison, conditional on saved forecasts.

No fit, feature reconstruction, data acquisition, production signal or live order.
Only the CLI loads preserved market files; imports and synthetic tests are inert.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import time

import numpy as np

from signal_desk import market_clock
from signal_desk.signals import research_net_alpha as core
from signal_desk.signals.research_net_alpha_execution import ExecutionBook

PLAN_PATH = "docs/kr-frozen-forecast-policy-plan-2026-10-21.json"
PLAN_COMMIT = "3b7a8e498b4fcdce917a99fcc33a8613ac8f9ecc"
PLAN_SHA = "a5f9ab2763763d589b180626621a6eab1d2d6f5ee4d682c14c799e8a201a8454"
CODE_PATHS = (
    "scripts/measure/kr_frozen_forecast_policy.py",
    "tests/test_kr_frozen_forecast_policy.py",
    "src/signal_desk/signals/research_net_alpha.py",
    "src/signal_desk/signals/research_net_alpha_execution.py",
    "src/signal_desk/market_clock.py",
    "scripts/measure/krx_price_replay_pilot.py",
)
CONTROLS = ("ridge_net", "legacy_selection", "cap6")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded_sha(value):
    return sha(json.dumps(value, sort_keys=True, allow_nan=False).encode())


def number(value):
    try:
        parsed = float(str(value).replace(",", ""))
        return parsed if math.isfinite(parsed) else None
    except (ValueError, TypeError):
        return None


def positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def fixed_bytes(path, expected):
    raw = Path(path).read_bytes()
    if sha(raw) != expected:
        raise ValueError("frozen_file_hash_differs:" + str(path))
    return raw


def calendar(first, last):
    start, end = map(dt.date.fromisoformat, (first, last))
    return [(start + dt.timedelta(days=i)).isoformat()
            for i in range((end-start).days+1)
            if market_clock.is_session("kr", start + dt.timedelta(days=i))]


class Budget:
    def __init__(self, seconds=600):
        self.start = time.monotonic()
        self.seconds = seconds

    def check(self):
        if time.monotonic()-self.start >= self.seconds:
            raise TimeoutError("frozen_wall_budget_exceeded")


class DataGap(Exception):
    def __init__(self, day, ticker, field):
        self.detail = {"session": day, "ticker": ticker, "field": field,
                       "reason": "source_gap", "return": None}
        super().__init__(f"{day}:{ticker}:{field}")


@dataclass
class Inputs:
    forecasts: dict
    legacy: dict
    rows: dict
    previous: dict
    reviewed: dict
    provenance: dict


def preflight(root, implementation_commit, budget):
    """Verify committed plan/code and saved inputs without running any strategy."""
    root = root.resolve()
    if Path(__file__).resolve() != root / CODE_PATHS[0]:
        raise ValueError("runner_outside_declared_repository")
    raw = fixed_bytes(root / PLAN_PATH, PLAN_SHA)
    committed = subprocess.check_output(["git", "show", f"{PLAN_COMMIT}:{PLAN_PATH}"],
                                        cwd=root, timeout=15)
    if raw != committed:
        raise ValueError("plan_not_equal_to_precommit")
    spec = json.loads(raw)
    code_hashes = {}
    for name in CODE_PATHS:
        budget.check()
        source = (root / name).read_bytes()
        saved = subprocess.check_output(["git", "show", f"{implementation_commit}:{name}"],
                                        cwd=root, timeout=15)
        if source != saved:
            raise ValueError("code_not_equal_to_precommit:" + name)
        code_hashes[name] = sha(source)
    for name, expected in spec["implementation_scope"]["required_code_hashes"].items():
        if code_hashes[name] != expected:
            raise ValueError("frozen_core_differs:" + name)
    for module, name in ((core, CODE_PATHS[2]),
                         (sys.modules[ExecutionBook.__module__], CODE_PATHS[3]),
                         (market_clock, CODE_PATHS[4])):
        if Path(module.__file__).resolve() != root / name:
            raise ValueError("import_outside_declared_repository")

    loaded, hashes = {}, {}
    for key in ("forecast_archive", "q4_legacy_and_prediction_crosscheck"):
        item = spec["inputs"][key]
        body = fixed_bytes(root / item["path"], item["sha256"])
        loaded[key] = json.loads(body)
        hashes[item["path"]] = item["sha256"]
    archive, old = loaded["forecast_archive"], loaded["q4_legacy_and_prediction_crosscheck"]
    all_rows = archive["predictions"]["dev_2024q4"]
    restored, forecasts = {}, {}
    for row in all_rows:
        day, ranked = row["date"], row["ranked"]
        if (day in forecasts or row["feature_names"] != len(ranked)
                or len({t for t, _ in ranked}) != len(ranked)
                or any(not isinstance(t, str) or len(t) != 6 or
                       type(p) not in (int, float) or not math.isfinite(p) for t, p in ranked)
                or ranked != sorted(ranked, key=lambda v: (-v[1], v[0]))):
            raise ValueError("invalid_saved_forecasts:" + day)
        forecasts[day] = dict(ranked)
        restored.update({(day, t): p for t, p in ranked})
    original = {(r["date"], r["ticker"]): r["predicted_gross"] for r in old["predictions"]}
    if (len(original) != len(old["predictions"]) or restored != original
            or sorted(forecasts) != old["evaluation_decision_dates"]):
        raise ValueError("original_all_forecasts_crosscheck_failed")

    # The existing loader checks every ZIP member and the market calendar.
    from scripts.measure.krx_price_replay_pilot import load_krx_archives
    if Path(sys.modules[load_krx_archives.__module__].__file__).resolve() != root / CODE_PATHS[5]:
        raise ValueError("raw_loader_outside_declared_repository")
    sources = archive["sources"]
    if len(sources) != 9 or sources != old["sources"]:
        raise ValueError("archive_manifest_differs")
    paths = []
    for source in sources:
        name = source["archive"]
        if Path(name).name != name:
            raise ValueError("invalid_archive_path")
        path = root / "data/research" / name
        fixed_bytes(path, source["sha256"])
        paths.append(path)
    days, actual_sources = load_krx_archives(paths)
    budget.check()
    if actual_sources != sources:
        raise ValueError("raw_archive_provenance_differs")
    rows = {d: {str(r["ISU_CD"]): r for r in rs} for d, rs in days.items()}
    dates = sorted(rows)
    previous = {day: dates[i-1] for i, day in enumerate(dates) if i}
    period = spec["periods"][0]
    selected_dates = calendar(period["decision_start"], period["decision_end"])
    forecasts = {d: forecasts[d] for d in selected_dates}
    if (len(forecasts) != period["expected_decision_dates"]
            or sum(map(len, forecasts.values())) != period["expected_prediction_rows"]
            or len(set().union(*(set(r) for r in forecasts.values()))) !=
               spec["scope_availability"]["forecast_distinct_tickers"]
            or any(d not in rows or d not in previous for d in
                   calendar(period["decision_start"], period["tail_through"]))):
        raise ValueError("bounded_input_calendar_or_counts_differ")
    legacy = {}
    for day in selected_dates:
        picked = old["selections"][day]["selected"]["legacy"]
        legacy[day] = [t for t, _ in picked]
        if len(set(legacy[day])) != len(picked) or len(picked) > spec["portfolio"]["max_positions"]:
            raise ValueError("invalid_saved_legacy_selection")
    reviewed = {}
    for fact in archive["reviewed_facts"]:
        key = fact["listing_session"], fact["ticker"]
        if key in reviewed:
            raise ValueError("duplicate_reviewed_unit_fact")
        reviewed[key] = fact
    if len(reviewed) != 12:
        raise ValueError("reviewed_fact_inventory_differs")
    proof = {"plan_commit": PLAN_COMMIT, "plan_sha256": PLAN_SHA,
             "implementation_commit": implementation_commit, "code_sha256": code_hashes,
             "input_sha256": hashes, "raw_sources": sources,
             "full_q4_forecasts_crosschecked": len(original),
             "used_forecast_dates": len(forecasts),
             "used_forecast_rows": sum(map(len, forecasts.values())),
             "reviewed_unit_facts": len(reviewed)}
    return spec, Inputs(forecasts, legacy, rows, previous, reviewed, proof)


def field(inputs, day, ticker, name, *, allow_zero=False):
    value = number(inputs.rows.get(day, {}).get(ticker, {}).get(name))
    if value is None or (value < 0 if allow_zero else value <= 0):
        raise DataGap(day, ticker, name)
    return value


def unit_audit(inputs, day, names):
    """Ex-post admissibility of occupied/pending units, never an order signal."""
    for ticker in sorted(names):
        before = field(inputs, inputs.previous[day], ticker, "LIST_SHRS")
        after = field(inputs, day, ticker, "LIST_SHRS")
        if before != after:
            fact = inputs.reviewed.get((day, ticker), {})
            if (fact.get("holder_quantity_effect") != "unchanged"
                    or fact.get("previous_session") != inputs.previous[day]
                    or fact.get("listed_shares_before") != before
                    or fact.get("listed_shares_after") != after):
                raise DataGap(day, ticker, "unreviewed_investment_unit_change")


def chooser_spec(spec):
    p, c = spec["portfolio"], spec["candidate"]
    return {**c, "min_feature_eligible": p["min_forecast_names"],
            "max_positions": p["max_positions"],
            "keep_name_weight_cap": p["keep_name_weight_cap"]}


def execution_spec(spec, period):
    return {**spec["execution"], "initial_cash_krw": spec["portfolio"]["initial_cash_krw"],
            "evaluation_start": period["decision_start"],
            "evaluation_end": period["decision_end"],
            "liquidation_through": period["tail_through"],
            "max_positions": spec["portfolio"]["max_positions"],
            "decision_one_way_cost": spec["candidate"]["decision_one_way_cost"]}


def targets(arm, forecasts, caps, legacy, holdings, closes, nav, spec):
    """Only t's forecast, close and actual book state enter the target."""
    p, c = spec["portfolio"], spec["candidate"]
    if len(forecasts) < p["min_forecast_names"]:
        raise ValueError("forecast_count_below_frozen_minimum")
    ranked = sorted(forecasts, key=lambda t: (-forecasts[t], t))
    top = ranked[:c["new_candidate_count"]]
    if arm == "candidate":
        if set(holdings)-set(forecasts):
            raise ValueError("held_forecast_missing")
        previous = {t: q*closes[t]/nav for t, q in holdings.items()}
        names = sorted(set(forecasts) | set(previous))
        choice = core.choose(ranked, forecasts, forecasts, previous,
                             np.zeros((len(names), len(names))), p["budget"], chooser_spec(spec))
        weights = choice["weights"]
        if any(t not in top or forecasts[t] <= c["new_entry_min_gross"]
               for t in set(weights)-set(holdings)):
            raise ValueError("candidate_new_entry_outside_fixed_pool")
        return weights, {t: forecasts[t] for t in weights}, choice
    if arm == "ridge_net":
        selected = [t for t in top if forecasts[t] > c["new_entry_min_gross"]]
        priority = {t: forecasts[t] for t in selected}
    elif arm == "legacy_selection":
        selected = list(legacy)
        priority = {t: -i for i, t in enumerate(selected)}
    elif arm == "cap6":
        selected = sorted(forecasts, key=lambda t: (-caps[t], t))[:p["max_positions"]]
        priority = {t: caps[t] for t in selected}
    else:
        raise ValueError("unknown_arm")
    return {t: p["slot_weight"] for t in selected}, priority, None


def close_enough(a, b, name):
    if not math.isclose(a, b, rel_tol=0, abs_tol=.01):
        raise ValueError("accounting_mismatch:" + name)


def reconcile(book, day, initial):
    """Independent fill-based share, fee, signed T+2 cash reconciliation."""
    holdings, gross, fees, settled, pending = defaultdict(int), 0., 0., float(initial), []
    for fill in book.fills:
        sign = 1 if fill["side"] == "buy" else -1
        holdings[fill["ticker"]] += sign*fill["shares"]
        amount = fill["shares"]*fill["price"]
        flow = -sign*amount-fill["fee"]
        gross -= sign*amount
        fees += fill["fee"]
        due = market_clock.next_sessions("kr", fill["on"], 2)[-1]
        if due <= day:
            settled += flow
        else:
            pending.append({"on": due, "amount": flow, "input_id": fill["input_id"]})
    if {t: q for t, q in holdings.items() if q} != book.holdings or pending != book.settlements:
        raise ValueError("accounting_mismatch:holdings_or_settlement_queue")
    close_enough(book.fees, fees, "fees")
    close_enough(book.buying_power, initial+gross-fees, "buying_power")
    close_enough(book.settled_cash, settled, "settled_cash")
    return gross


def mdd(values, initial):
    peak, worst = float(initial), 0.
    for value in values:
        peak = max(peak, value)
        worst = min(worst, 100*(value/peak-1))
    return worst


def metrics(book, curve, initial, stop):
    complete = stop is None and not (book.holdings or book.pending or book.settlements)
    result = {"complete": complete, "reason": stop or (None if complete else "incomplete_tail"),
              "net_return_pct": None, "price_mdd_pct": None, "net_pnl_krw": None,
              "before_cost_price_pnl_krw": None, "fees_krw": None,
              "prefix": {"sessions": len(curve), "last": curve[-1] if curve else None,
                         "assumed_fees_krw": book.fees},
              "fills": deepcopy(book.fills), "events": deepcopy(book.events),
              "end_state": book.state()}
    if not complete:
        return result
    nav = book.price_nav({})
    principal = sum(f["shares"]*f["price"] for f in book.fills)
    per_ticker = defaultdict(float)
    for f in book.fills:
        per_ticker[f["ticker"]] += (1 if f["side"] == "sell" else -1)*f["shares"]*f["price"]-f["fee"]
    close_enough(sum(per_ticker.values()), nav-initial, "closed_symbol_pnl")
    result.update(net_return_pct=100*(nav/initial-1),
                  price_mdd_pct=mdd([r["nav"] for r in curve], initial),
                  net_pnl_krw=nav-initial, before_cost_price_pnl_krw=nav-initial+book.fees,
                  fees_krw=book.fees, turnover_notional_over_initial=principal/initial,
                  mean_close_exposure=float(np.mean([r["exposure"] for r in curve])),
                  buy_fills=sum(f["side"] == "buy" for f in book.fills),
                  entry_sessions=len({f["on"] for f in book.fills if f["side"] == "buy"}),
                  distinct_bought_tickers=len({f["ticker"] for f in book.fills if f["side"] == "buy"}),
                  pnl_by_ticker=dict(per_ticker))
    return result


def replay_group(inputs, spec, scenario, phase, budget, *, target_selector=None,
                 decision_contract_sha=None, forecast_required_arms=("candidate",),
                 decision_context_factory=None, additional_quote_names=None):
    """Common source stops across declared arms, including the liquidation tail."""
    period = spec["periods"][0]
    dates = sorted(inputs.forecasts)
    scheduled = set(dates[phase::spec["schedule"]["decision_every_sessions"]])
    terminal = market_clock.next_sessions("kr", max(scheduled), 5)[-1]
    initial = spec["portfolio"]["initial_cash_krw"]
    books = {a: ExecutionBook(execution_spec(spec, period), scenario) for a in period["arms"]}
    curves, trace = {a: [] for a in books}, []
    previous_close, previous_nav = {}, {a: float(initial) for a in books}
    stop = None
    for day in calendar(period["decision_start"], period["tail_through"]):
        try:
            budget.check()
            names = set().union(*(set(b.holdings) | {o["ticker"] for o in b.pending}
                                  for b in books.values()))
            unit_audit(inputs, day, names)
            opens = {}
            held_names = set().union(*(set(b.holdings) for b in books.values()))
            for t in sorted(names):
                price = field(inputs, day, t, "TDD_OPNPRC", allow_zero=True)
                if t in held_names and not positive(price):
                    raise DataGap(day, t, "held_open_unavailable")
                opens[t] = {"price": price, "open_tradable": price > 0,
                            "source_id": f"preserved-raw:{day}:{t}"}
            before = {a: dict(b.holdings) for a, b in books.items()}
            fees_before = {a: b.fees for a, b in books.items()}
            for book in books.values():
                book.open_session(day, opens)
                reconcile(book, day, initial)
            owned = set().union(*(set(b.holdings) for b in books.values()))
            closes = {t: field(inputs, day, t, "TDD_CLSPRC") for t in names | owned}
            row = {"session": day, "books": {}, "decision": None}
            for arm, book in books.items():
                nav = book.price_nav(closes)
                gap = sum(q*(opens[t]["price"]-previous_close[t]) for t, q in before[arm].items())
                intraday = sum(q*(closes[t]-opens[t]["price"]) for t, q in book.holdings.items())
                fees = book.fees-fees_before[arm]
                close_enough(nav-previous_nav[arm], gap+intraday-fees, "gap_intraday_fee_nav")
                invested = sum(q*closes[t] for t, q in book.holdings.items())
                curve_row = {"session": day, "nav": nav, "exposure": invested/nav}
                curves[arm].append(curve_row)
                row["books"][arm] = {**curve_row, "holdings": dict(book.holdings),
                    "buying_power": book.buying_power, "settled_cash": book.settled_cash,
                    "settlements": deepcopy(book.settlements), "gap_pnl": gap,
                    "intraday_pnl": intraday, "fees": fees}
                previous_nav[arm] = nav
            trace.append(row)
            previous_close = closes
            if day not in scheduled and day != terminal:
                continue
            terminal_day = day == terminal
            forecasts = inputs.forecasts.get(day, {}) if not terminal_day else {}
            if not terminal_day and len(forecasts) < spec["portfolio"]["min_forecast_names"]:
                raise DataGap(day, "*", "forecast_count_below_160")
            if not terminal_day:
                for arm in forecast_required_arms:
                    for t in books[arm].holdings:
                        if t not in forecasts:
                            raise DataGap(day, t, "held_forecast_missing")
                caps = {t: field(inputs, day, t, "MKTCAP") for t in forecasts}
                ranked = sorted(forecasts, key=lambda t: (-forecasts[t], t))
                newcomers = {t for t in ranked[:6] if forecasts[t] > spec["candidate"]["new_entry_min_gross"]}
                newcomers |= set(inputs.legacy[day])
                newcomers |= set(sorted(forecasts, key=lambda t: (-caps[t], t))[:6])
                if additional_quote_names is not None:
                    extra = set(additional_quote_names(day, forecasts))
                    if not extra <= set(forecasts):
                        raise DataGap(day, "*", "additional_quote_outside_forecast_universe")
                    newcomers |= extra
            else:
                caps, newcomers = {}, set()
            order_names = owned | newcomers
            closes.update({t: field(inputs, day, t, "TDD_CLSPRC") for t in order_names})
            volumes = {t: field(inputs, day, t, "ACC_TRDVOL", allow_zero=True) for t in order_names}
            for t in order_names:
                field(inputs, day, t, "LIST_SHRS")
            context = (decision_context_factory(day, forecasts)
                       if decision_context_factory is not None and not terminal_day else None)
            plans = {}
            for arm, book in books.items():
                nav = book.price_nav(closes)
                if terminal_day:
                    weights, priority, choice = {}, {}, None
                else:
                    args = (arm, forecasts, caps, inputs.legacy[day], book.holdings, closes, nav, spec)
                    select = target_selector or targets
                    weights, priority, choice = (select(*args, context=context)
                        if decision_context_factory is not None else select(*args))
                core.orders(weights, book.holdings, closes, volumes, nav, book.spec)
                plans[arm] = {"weights": weights, "priority": priority,
                              "sizing_nav": nav, "choice": choice}
            decision_input = {"date": day, "forecasts": forecasts,
                "legacy": inputs.legacy.get(day, []), "closes": closes, "volumes": volumes,
                "caps": caps, "plan_sha256": decision_contract_sha or PLAN_SHA, "terminal": terminal_day}
            if decision_context_factory is not None:
                decision_input["decision_context"] = context
            input_id = encoded_sha(decision_input)
            for arm, book in books.items():
                plan = plans[arm]
                plan["orders"] = book.submit(day, plan["weights"], closes, volumes,
                    plan["sizing_nav"], priority=plan["priority"], input_id=input_id,
                    budget=spec["portfolio"]["budget"])
            row["decision"] = {"input_id": input_id, "terminal": terminal_day,
                               "plans": plans}
            if decision_context_factory is not None:
                row["decision"]["context"] = context
        except DataGap as exc:
            stop = exc.detail
            break
        except TimeoutError:
            stop = {"session": day, "reason": "frozen_wall_budget_exceeded"}
            break
    result_metrics = {a: metrics(b, curves[a], initial, stop) for a, b in books.items()}
    deltas = {}
    for other in CONTROLS:
        c, o = result_metrics["candidate"], result_metrics[other]
        if not c["complete"] or not o["complete"]:
            deltas[other] = None
        else:
            d = {k: c[k]-o[k] for k in ("net_pnl_krw", "before_cost_price_pnl_krw", "fees_krw")}
            close_enough(d["net_pnl_krw"], d["before_cost_price_pnl_krw"]-d["fees_krw"], "paired_cost_attribution")
            deltas[other] = d
    return {"scenario": scenario, "phase": phase, "terminal": terminal,
            "source_stop": stop, "metrics": result_metrics, "deltas": deltas, "trace": trace}


def verdict(groups, spec):
    expected = {(s, p) for s in spec["execution"]["scenarios"] for p in spec["schedule"]["phases"]}
    keys = [(g["scenario"], g["phase"]) for g in groups]
    if len(keys) != len(expected) or set(keys) != expected:
        raise ValueError("verdict_requires_exact_frozen_grid")
    for group in groups:
        if set(group["metrics"]) != set(spec["periods"][0]["arms"]):
            raise ValueError("verdict_requires_all_arms")
        for metric in group["metrics"].values():
            if metric["complete"] and any(type(metric[k]) not in (int, float)
                    or not math.isfinite(metric[k]) for k in ("net_return_pct", "price_mdd_pct")):
                raise ValueError("complete_path_has_no_finite_metrics")
    violations, incomplete, scenario_means = [], [], {}
    for group in groups:
        label = f"{group['scenario']}/phase{group['phase']}"
        c = group["metrics"]["candidate"]
        for arm, result in group["metrics"].items():
            if not result["complete"]:
                incomplete.append(label+"/"+arm)
        if c["complete"] and c["price_mdd_pct"] < -10:
            violations.append(label+":candidate_mdd_below_minus10")
        if group["scenario"] in ("75bp_full", "120bp_full") and c["complete"]:
            if c["net_return_pct"] <= 0:
                violations.append(label+":candidate_does_not_beat_cash")
            for arm in CONTROLS:
                o = group["metrics"][arm]
                if o["complete"] and c["net_return_pct"] <= o["net_return_pct"]:
                    violations.append(label+":candidate_does_not_beat_"+arm)
    for scenario in spec["execution"]["scenarios"]:
        rows = [g for g in groups if g["scenario"] == scenario]
        means = {}
        for arm in spec["periods"][0]["arms"]:
            values = [g["metrics"][arm] for g in rows]
            means[arm] = (float(np.mean([v["net_return_pct"] for v in values]))
                          if len(values) == 5 and all(v["complete"] for v in values) else None)
        scenario_means[scenario] = means
        if scenario not in ("75bp_half", "75bp_delay1") or means["candidate"] is None:
            continue
        if means["candidate"] <= 0:
            violations.append(scenario+":candidate_mean_does_not_beat_cash")
        for arm in CONTROLS:
            if means[arm] is not None and means["candidate"] <= means[arm]:
                violations.append(scenario+":candidate_mean_does_not_beat_"+arm)
    decision = ("observed_rejection" if violations else
                "not_evaluable_source_or_tail" if incomplete else "conditional_development_support")
    return {"decision": decision, "violations": violations, "incomplete_paths": incomplete,
            "scenario_mean_price_return_pct": scenario_means,
            "production_change_authorized": False}


def run(root, implementation_commit):
    budget = Budget()
    spec, inputs = preflight(root, implementation_commit, budget)
    groups = [replay_group(inputs, spec, scenario, phase, budget)
              for scenario in spec["execution"]["scenarios"] for phase in spec["schedule"]["phases"]]
    paths = sum(len(g["metrics"]) for g in groups)
    if paths != spec["implementation_scope"]["max_trading_paths"]:
        raise ValueError("frozen_grid_count_differs")
    complete_count = sum(m["complete"] for g in groups for m in g["metrics"].values())
    result = {"schema": "kr-frozen-forecast-policy-result-v1", "candidate_id": spec["candidate_id"],
        "state": "comparison_complete" if complete_count == paths else "comparison_incomplete",
        "provenance": inputs.provenance, "groups": groups,
        "path_count": paths, "completed_paths": complete_count,
        "verdict": verdict(groups, spec), "cash": {"initial_krw": spec["portfolio"]["initial_cash_krw"], "net_return_pct": 0},
        "wall_seconds": time.monotonic()-budget.start, "historical_runs": 1,
        "new_market_data_requests": 0, "fit_calls": 0, "forecast_reconstruction_calls": 0,
        "price_only_conditional_forecasts": True, "verified_total_return": False,
        "pristine_oos": False, "strict_pit_eligible": False, "live_order_eligible": False,
        "production_engine_changed": False, "integrated_roadmap_percent": 40}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--implementation-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root, output = args.repo.resolve(), args.output.resolve()
    if not output.is_relative_to(root / "data/research"):
        parser.error("output must stay in the local research directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation precedes expensive computation: never repeat a saved run.
    with output.open("x", encoding="utf-8") as stream:
        try:
            result = run(root, args.implementation_commit)
        except Exception as exc:
            json.dump({"state": "implementation_error", "error_type": type(exc).__name__,
                       "error": str(exc), "live_order_eligible": False}, stream, ensure_ascii=False)
            raise
        json.dump(result, stream, ensure_ascii=False, allow_nan=False)
    print(json.dumps({"path_count": result["path_count"], "completed_paths": result["completed_paths"],
                      "verdict": result["verdict"], "output_sha256": sha(output.read_bytes())}, ensure_ascii=False))


if __name__ == "__main__":
    main()
