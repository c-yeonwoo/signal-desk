"""Read an existing result, never rerun a strategy or search an exit threshold.

Fixed scope: every candidate holding on scheduled, non-terminal decisions in the
20 saved scenario/phase groups. Off-schedule forecast observations are separate.
The existing objective's buffer and cost define the only diagnostic boundaries.
Forward h5 is an ex-post stock-price label, not a hypothetical sell profit.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import statistics

from scripts.measure import kr_frozen_forecast_policy as frozen

RESULT_PATH = "data/research/kr-frozen-forecast-policy-2026-10-21-v1.json"
RESULT_SHA = "a0d0b43ba6c05147376228772f3ad0034458ffc840e2508c69257803f4d7eb7a"


def utility(weights, previous, forecasts, candidate):
    names = set(weights) | set(previous)
    if names-set(forecasts):
        raise ValueError("held_or_target_forecast_missing")
    return (sum(weights.get(t, 0)*forecasts[t] for t in names)
            - candidate["return_buffer"]*sum(weights.values())
            - candidate["decision_one_way_cost"]*(sum(weights.values())
                + sum(abs(weights.get(t, 0)-previous.get(t, 0)) for t in names)))


def decision_rows(day, book, plan, forecasts, closes, latest_buys, spec):
    """Join t's actual holding, target and forecast; no future labels enter here."""
    c = spec["candidate"]
    if c["risk_aversion"] != 0:
        raise ValueError("diagnostic_requires_frozen_zero_risk_term")
    ranks = {t: i+1 for i, t in enumerate(sorted(forecasts, key=lambda t: (-forecasts[t], t)))}
    previous = {t: q*closes[t]/book["nav"] for t, q in book["holdings"].items()}
    weights = plan["weights"]
    value = utility(weights, previous, forecasts, c)
    if not math.isclose(value, plan["choice"]["utility"], rel_tol=0, abs_tol=1e-12):
        raise ValueError("stored_choice_utility_mismatch")
    rows = []
    for t, q in sorted(book["holdings"].items()):
        mu = forecasts[t]
        w = weights.get(t, 0)
        removed = {name: weight for name, weight in weights.items() if name != t}
        delta = utility(removed, previous, forecasts, c)-value
        latest = latest_buys.get(t)
        if latest is None:
            raise ValueError("held_without_preserved_buy")
        rows.append({"session": day, "ticker": t, "shares": q,
            "close": closes[t], "forecast_gross": mu, "forecast_rank": ranks[t],
            "previous_weight": previous[t], "target_weight": w,
            "retained": w > 0, "outside_top6": ranks[t] > c["new_candidate_count"],
            "below_new_cost_hurdle": mu < c["return_buffer"]+2*c["decision_one_way_cost"],
            "below_hold_cash_boundary": mu < c["return_buffer"],
            "remove_target_utility_delta": delta,
            "removal_improves_objective": w > 0 and delta > c["objective_tie_tolerance"],
            "latest_buy": deepcopy(latest),
            "forecast_change_since_latest_buy_decision": mu-latest["forecast_gross"],
            "submitted_sell_shares": sum(o["shares"] for o in plan["orders"]
                                          if o["ticker"] == t and o["side"] == "sell")})
    return rows


def forward_label(day, ticker, inputs):
    """A separately attached, unit-audited h5 close label; no fills or new NAV."""
    try:
        sessions = frozen.market_clock.next_sessions("kr", day, 5)
        for future in sessions:
            frozen.unit_audit(inputs, future, {ticker})
        first = frozen.field(inputs, day, ticker, "TDD_CLSPRC")
        last = frozen.field(inputs, sessions[-1], ticker, "TDD_CLSPRC")
        return {"through": sessions[-1], "price_return_pct": 100*(last/first-1),
                "missing_reason": None}
    except (frozen.DataGap, KeyError) as exc:
        return {"through": None, "price_return_pct": None,
                "missing_reason": getattr(exc, "detail", "missing_calendar")}


def bucket(rows):
    labels = [r["h5"]["price_return_pct"] for r in rows]
    known = [v for v in labels if v is not None]
    return {"rows": len(rows), "known_h5": len(known),
            "missing_h5": len(rows)-len(known),
            "negative_h5": sum(v < 0 for v in known),
            # Never silently report a complete-group average from a known subset.
            "mean_h5_price_return_pct": statistics.mean(known) if known and len(known)==len(rows) else None}


def diagnose(result, archive, inputs, spec):
    if (result["completed_paths"] != 80 or result["path_count"] != 80
            or result["verdict"]["decision"] != "observed_rejection"):
        raise ValueError("requires_exact_completed_parent")
    expected = {(s, p) for s in spec["execution"]["scenarios"] for p in spec["schedule"]["phases"]}
    if len(result["groups"]) != 20 or {(g["scenario"], g["phase"]) for g in result["groups"]} != expected:
        raise ValueError("parent_grid_mismatch")
    forecasts = {r["date"]: dict(r["ranked"]) for r in archive["predictions"]["dev_2024q4"]}
    all_rows, off_schedule, contexts = [], [], []
    for g in result["groups"]:
        latest, fills = {}, g["metrics"]["candidate"]["fills"]
        by_day = {}
        for f in fills:
            by_day.setdefault(f["on"], []).append(f)
        context = {"scenario": g["scenario"], "phase": g["phase"]}
        for row in g["trace"]:
            day = row["session"]
            for fill in by_day.get(day, []):
                if fill["side"] == "buy":
                    latest[fill["ticker"]] = {"filled_on": day, "decision_on": fill["decision_on"],
                        "forecast_gross": forecasts[fill["decision_on"]][fill["ticker"]]}
            if day > spec["periods"][0]["decision_end"]:
                continue
            book, decision = row["books"]["candidate"], row["decision"]
            if decision is None:
                for t in book["holdings"]:
                    mu = forecasts.get(day, {}).get(t)
                    off_schedule.append({**context, "session": day, "ticker": t,
                        "forecast_gross": mu, "forecast_missing": mu is None,
                        "below_hold_cash_boundary": mu is not None and mu < spec["candidate"]["return_buffer"]})
                continue
            if decision["terminal"]:
                continue
            closes = {t: frozen.field(inputs, day, t, "TDD_CLSPRC") for t in book["holdings"]}
            records = decision_rows(day, book, decision["plans"]["candidate"], forecasts[day], closes, latest, spec)
            # Attach future observations only after the as-of explanation is fixed.
            for record in records:
                record.update(context)
                record["h5"] = forward_label(day, record["ticker"], inputs)
                all_rows.append(record)
            contexts.append({**context, "session": day, "choice": decision["plans"]["candidate"]["choice"]["choice"],
                "actual_exposure": book["exposure"], "held_names": len(book["holdings"])})
    summaries = {}
    for scenario in spec["execution"]["scenarios"]:
        rows = [r for r in all_rows if r["scenario"] == scenario]
        retained = [r for r in rows if r["retained"]]
        summaries[scenario] = {"all_held_decisions": bucket(rows),
            "retained": bucket(retained), "exit_intents": bucket([r for r in rows if not r["retained"]]),
            "retained_outside_top6": bucket([r for r in retained if r["outside_top6"]]),
            "retained_below_new_cost_hurdle": bucket([r for r in retained if r["below_new_cost_hurdle"]]),
            "retained_below_hold_cash_boundary": bucket([r for r in retained if r["below_hold_cash_boundary"]]),
            "retained_forecast_declined_since_latest_buy": bucket([r for r in retained if r["forecast_change_since_latest_buy_decision"] < 0]),
            "removal_improves_objective": bucket([r for r in rows if r["removal_improves_objective"]])}
    return {"scenario_summary": summaries, "holding_decisions": all_rows,
        "decision_contexts": contexts, "off_schedule_observations": off_schedule,
        "off_schedule_counts": dict(Counter("missing" if r["forecast_missing"] else
            "below_hold_boundary" if r["below_hold_cash_boundary"] else "above_or_equal_hold_boundary"
            for r in off_schedule)), "new_backtest_paths": 0, "fit_calls": 0,
        "production_engine_changed": False, "posthoc_diagnostic_not_adoption_test": True,
        "observation_unit": "scenario/phase/session/ticker; overlapping accounts, not independent events",
        "cohorts_overlap": True, "forward_label_is_not_sell_profit": True,
        "integrated_roadmap_percent": 40}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root, output = args.repo.resolve(), args.output.resolve()
    if not output.is_relative_to(root/"data/research"):
        parser.error("output must stay in local research directory")
    if output.exists():
        parser.error("preserved diagnostic already exists; do not rerun it")
    result = json.loads(frozen.fixed_bytes(root/RESULT_PATH, RESULT_SHA))
    spec = json.loads(frozen.fixed_bytes(root/frozen.PLAN_PATH, frozen.PLAN_SHA))
    item = spec["inputs"]["forecast_archive"]
    archive = json.loads(frozen.fixed_bytes(root/item["path"], item["sha256"]))
    source = next(s for s in result["provenance"]["raw_sources"] if s["archive"] == "krx-daily-2024-10-12-v1.zip")
    path = root/"data/research"/source["archive"]
    frozen.fixed_bytes(path, source["sha256"])
    from scripts.measure.krx_price_replay_pilot import load_krx_archives
    days, _ = load_krx_archives([path])
    ordered = sorted(days)
    inputs = frozen.Inputs({}, {}, {d: {str(r["ISU_CD"]): r for r in rs} for d, rs in days.items()},
        {d: ordered[i-1] for i, d in enumerate(ordered) if i},
        {(f["listing_session"], f["ticker"]): f for f in archive["reviewed_facts"]}, {})
    report = diagnose(result, archive, inputs, spec)
    report["provenance"] = {"parent_result_sha256": RESULT_SHA, "plan_sha256": frozen.PLAN_SHA,
        "forecast_archive_sha256": item["sha256"], "raw_archive": source,
        "diagnostic_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, allow_nan=False)
    print(json.dumps({"scenario_summary": report["scenario_summary"],
        "off_schedule_counts": report["off_schedule_counts"],
        "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
