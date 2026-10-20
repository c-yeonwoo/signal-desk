"""Frozen score-tie experiment using existing engine and preserved inputs only."""

from __future__ import annotations

import argparse
from collections import defaultdict
from copy import deepcopy
import datetime as dt
import hashlib
import json
from pathlib import Path

from scripts.measure.kr_financial_input_sources import load as load_financials
from scripts.measure.kr_original_financial_sources import load as load_versions
from scripts.measure.kr_receipt_financial_lab import daily_outcome
from scripts.measure.kr_relative_downtrend_ablation import (
    ARMS, CAPACITY, average, compare_daily, finite, slot_result, summarize,
)
from scripts.measure.krx_price_replay_pilot import ROOT, _number, load_krx_archives
from scripts.measure.price_challenger_lab import PricePanel
from signal_desk.signals import engine
from signal_desk.signals.research_financial_inputs import metrics_asof

PLAN = ROOT / "docs/kr-score-tie-plan-2026-10-19.json"
PLAN_SHA = "34d67115299c5c68252f6fa45f2c1978c9b785cf4c178595ae683ad8f1b1de40"
PLAN_COMMIT = "b165b2b"  # Provenance only: a squash merge need not retain this object.


def tie_rank(signals, recent20, cfg):
    """Change only exact score ties; no labels or future bars are accepted."""
    if any(not finite(r.score) or not finite(recent20.get(r.ticker)) for r in signals):
        raise ValueError("missing tie decision inputs")
    ranked = deepcopy(signals)
    ranked.sort(key=lambda r: (r.score, recent20[r.ticker]), reverse=True)
    return engine.apply_cross_sectional(ranked, cfg)


def simple_pair(daily, excluded):
    """Exclude only the baseline's PREDECLARED missing dates, without rephasing."""
    kept = [(i, d) for i, d in enumerate(daily) if d["date"] not in excluded]
    def values(arm):
        return [d["arms"][arm]["slot_net_pct"] for _, d in kept]
    def delta(d):
        a, b = d["arms"]["candidate"]["slot_net_pct"], d["arms"]["simple_momentum"]["slot_net_pct"]
        return a - b if a is not None and b is not None else None
    return {"dates": len(kept), "coverage": len(kept) / len(daily),
            "paired_means_pct": {arm: average(values(arm)) for arm in ARMS},
            "delta_pp": average([delta(d) for _, d in kept]),
            "phase_deltas_pp": [average([delta(d) for i, d in kept if i % 5 == p]) for p in range(5)]}


def assess(windows, changes, costs):
    checks = {
        "all_legacy_candidate_prices_known": all(
            w["costs"][str(cost)]["arms"][arm]["unresolved_dates"] == 0
            for w in windows.values() for cost in costs for arm in ("legacy", "candidate")),
        "changed_sample": (sum(w["changed_buy_dates"] for w in windows.values()) >= 40
                           and len({r["ticker"] for r in changes}) >= 10
                           and sum(w["changed_buy_dates"] > 0 for w in windows.values()) >= 3),
    }
    summary = {}
    for cost in costs:
        key = str(cost)
        metrics = [w["costs"][key] for w in windows.values()]
        means = {arm: average([m["arms"][arm]["equal_day_slot_net_pct"] for m in metrics]) for arm in ARMS}
        paired_means = {arm: average([m["simple_pair"]["paired_means_pct"][arm] for m in metrics]) for arm in ARMS}
        paired_delta = average([m["simple_pair"]["delta_pp"] for m in metrics])
        phase_delta = [average([m["vs_legacy"]["phase_deltas_pp"][p] for m in metrics]) for p in range(5)]
        def worst(arm):
            values = [m["arms"][arm]["equal_day_slot_net_pct"] for m in metrics]
            return min(values) if all(v is not None for v in values) else None
        winning = sum(m["vs_legacy"]["equal_day_delta_pp"] is not None
                      and m["vs_legacy"]["equal_day_delta_pp"] > 0 for m in metrics)
        impacts = defaultdict(float)
        for row in changes:
            out = row["h5"]
            if out.get("state") == "matured" and finite(out.get("net_pct")):
                sign = 1 if row["side"] == "added" else -1
                impacts[row["ticker"]] += sign * (out["net_pct"] - (cost - 25) / 100) / (
                    CAPACITY * len(windows) * windows[row["window"]]["dates"])
        total = sum(abs(v) for v in impacts.values())
        concentration = max(map(abs, impacts.values())) / total if total else None
        pair_count = sum(m["simple_pair"]["dates"] for m in metrics)
        checks[f"{cost}bp_pair_coverage"] = (pair_count / sum(w["dates"] for w in windows.values()) >= .9
                                            and all(m["simple_pair"]["coverage"] >= .8 for m in metrics))
        checks[f"{cost}bp_positive_beats_legacy"] = (means["candidate"] is not None and means["legacy"] is not None
                                                   and means["candidate"] > max(0, means["legacy"]))
        checks[f"{cost}bp_beats_simple_on_fixed_pairs"] = paired_delta is not None and paired_delta > 0
        checks[f"{cost}bp_five_windows"] = winning >= 5
        checks[f"{cost}bp_worst_window"] = (worst("candidate") is not None and worst("legacy") is not None
                                           and worst("candidate") >= worst("legacy"))
        checks[f"{cost}bp_all_phases"] = all(v is not None and v > 0 for v in phase_delta)
        checks[f"{cost}bp_concentration"] = concentration is not None and concentration <= .35
        summary[key] = {"equal_window_slot_net_pct": means, "paired_simple_dates": pair_count,
                        "paired_equal_window_slot_net_pct": paired_means,
                        "paired_simple_delta_pp": paired_delta, "windows_improved_vs_legacy": winning,
                        "worst_window_pct": {arm: worst(arm) for arm in ("legacy", "candidate")},
                        "phase_equal_window_deltas_pp": phase_delta,
                        "largest_ticker_absolute_impact_share": concentration}
    return checks, summary


def replay_day(day, rows, days, panel, financials, versions, *, ranker=tie_rank):
    """Same preserved input path as the frozen annual-financial sensitivity."""
    if day >= "2026-08-05":
        raise ValueError("protected date")
    names = {r["ticker"] for r in rows}
    raw = sorted((r for r in days[day] if len(str(r["ISU_CD"])) == 6 and str(r["ISU_CD"]).endswith("0")
                  and (_number(r["MKTCAP"]) or 0) > 0), key=lambda r: (-_number(r["MKTCAP"]), r["ISU_CD"]))[:200]
    if len(raw) != 200 or not names <= {r["ISU_CD"] for r in raw}:
        raise ValueError("point-date candidate universe differs")
    i = panel.index[day]
    prices = {ticker: [panel.bars.get((s, ticker), (None, None))[1]
                       for s in panel.sessions[i - 252:i + 1]] for ticker in names}
    if any(len(v) != 253 or any(p is None or p <= 0 for p in v) for v in prices.values()):
        raise ValueError("continuous close inputs differ")
    caps = {r["ISU_CD"]: _number(r["MKTCAP"]) for r in days[day] if r["ISU_CD"] in names}
    metrics, _ = metrics_asof(financials["records"], day, caps, versions=versions)
    cfg = engine.SignalConfig()
    signals = engine.evaluate([{"ticker": r["ISU_CD"], "name": str(r.get("ISU_NM") or r["ISU_CD"])} for r in raw],
                              prices, fundamentals=metrics, config=cfg, today=dt.date.fromisoformat(day),
                              unavailable=("flow", "short"), flows={}, shorts={}, sentiment={}, earnings_dates={})
    recent = {t: (v[-1] / v[-21] - 1) * 100 for t, v in prices.items()}
    if any(abs(recent[r["ticker"]] - r["recent20_pct"]) > 1e-8 for r in rows):
        raise ValueError("frozen recent returns differ")
    return signals, ranker(signals, recent, cfg), recent


def run(*, plan_path=None, plan_sha=PLAN_SHA, plan_commit=PLAN_COMMIT,
        ranker=tie_rank, schema="kr-score-tie-ablation-v1"):
    plan_raw = (plan_path or PLAN).read_bytes()
    if hashlib.sha256(plan_raw).hexdigest() != plan_sha:
        raise ValueError("frozen tie contract differs")
    plan = json.loads(plan_raw)
    for key, declared in plan["inputs"].items():
        if hashlib.sha256((ROOT / declared["path"]).read_bytes()).hexdigest() != declared["sha256"]:
            raise ValueError(f"frozen {key} hash differs")
    def path(key):
        return ROOT / plan["inputs"][key]["path"]
    scope, frozen = json.loads(path("scope").read_text()), json.loads(path("legacy").read_text())
    if list(scope["windows"]) != plan["windows"] or list(frozen["windows"]) != plan["windows"]:
        raise ValueError("window scope differs")
    for data in (scope, frozen):
        if data.get("registered_verdict") is not False or data.get("strict_pit_eligible") is not False:
            raise ValueError("not the frozen development inputs")
    financials, versions = load_financials(path("financial")), load_versions(path("versions"))
    days, sources = load_krx_archives([ROOT / "data/research" / s["archive"] for s in scope["sources"]])
    if sources != scope["sources"]:
        raise ValueError("frozen price sources differ")
    panel = PricePanel(days)
    costs, excluded = plan["comparison"]["cost_bps"], set(plan["comparison"]["known_simple_unresolved_dates"])
    windows, changes, observed_excluded, all_dates = {}, [], set(), set()
    for name in plan["windows"]:
        grouped = defaultdict(list)
        for row in scope["windows"][name]["selection"]["rows"]:
            grouped[row["date"]].append(row)
        frozen_days = frozen["windows"][name]["daily"]
        dates = [r["date"] for r in frozen_days]
        if (dates != sorted(set(dates)) or set(dates) != set(grouped) or not dates
                or dates[-1] >= plan["protected_start"] or all_dates.intersection(dates)):
            raise ValueError("decision dates differ or protected date")
        all_dates.update(dates)
        daily_cost = {str(c): [] for c in costs}
        changed, boundary_ties, ceiling, kind_changes = 0, 0, 0, 0
        for day_record in frozen_days:
            day = day_record["date"]
            rows = grouped[day]
            signals, candidate, recent = replay_day(day, rows, days, panel, financials, versions, ranker=ranker)
            if daily_outcome(signals, rows) != day_record["annual_financial"]:
                raise ValueError("legacy does not reproduce frozen selection/outcomes")
            legacy = [r.ticker for r in signals if engine.is_buy(r.kind)]
            new = [r.ticker for r in candidate if engine.is_buy(r.kind)]
            simple = sorted(recent, key=lambda ticker: (-recent[ticker], ticker))[:CAPACITY]
            arms = {"legacy": legacy, "candidate": new, "simple_momentum": simple, "cash": []}
            by_ticker = {r["ticker"]: r for r in rows}
            by_signal = {r.ticker: r for r in signals}
            changed += set(legacy) != set(new)
            kind_changes += {r.ticker: r.kind for r in signals} != {r.ticker: r.kind for r in candidate}
            ceiling += sum(r.score == 3 for r in signals)
            boundary_ties += len(signals) > CAPACITY and signals[CAPACITY - 1].score == signals[CAPACITY].score
            for side, tickers in (("added", set(new) - set(legacy)), ("removed", set(legacy) - set(new))):
                for ticker in sorted(tickers):
                    rank_value = next(r for r in candidate if r.ticker == ticker)
                    changes.append({"window": name, "date": day, "ticker": ticker, "name": by_ticker[ticker]["name"],
                                    "side": side, "score": by_signal[ticker].score, "recent20_pct": recent[ticker],
                                    "research_rank_value": getattr(rank_value, "research_rank_value", rank_value.score),
                                    "momentum_ret": by_signal[ticker].momentum_ret,
                                    "h5": by_ticker[ticker]["horizons"]["5"]})
            for cost in costs:
                record = {"date": day, "tickers": arms,
                          "arms": {arm: slot_result(tickers, by_ticker, cost) for arm, tickers in arms.items()}}
                daily_cost[str(cost)].append(record)
                if record["arms"]["simple_momentum"]["missing"]:
                    observed_excluded.add(day)
        windows[name] = {"dates": len(dates), "changed_buy_dates": changed, "kind_changed_dates": kind_changes,
                         "boundary_tie_dates": boundary_ties, "ceiling_ticker_days": ceiling,
                         "costs": {cost: {"arms": {arm: summarize(daily, arm) for arm in ARMS},
                                          "vs_legacy": compare_daily(daily, "legacy"),
                                          "simple_pair": simple_pair(daily, excluded), "daily": daily}
                                   for cost, daily in daily_cost.items()}}
        print(json.dumps({"window": name, "days": len(dates), "changed_buy_dates": changed,
                          "boundary_tie_dates": boundary_ties}), flush=True)
    if len(all_dates) != plan["decision_dates"] or observed_excluded != excluded:
        raise ValueError("fixed decision/baseline missing-date scope differs")
    checks, summary = assess(windows, changes, costs)
    return {"schema": schema, "candidate_id": plan["candidate_id"],
            "decision": "development_support_only" if all(checks.values()) else "not_supported_do_not_apply",
            "development_supported": all(checks.values()), "failed_checks": [k for k, v in checks.items() if not v],
            "checks": checks, "summary": summary, "windows": windows, "changes": changes,
            "registered_verdict": False, "strict_pit_eligible": False, "live_order_eligible": False,
            "verified_total_return": False, "production_changed": False,
            "decision_dates": len(all_dates), "plan_sha256": plan_sha, "plan_commit": plan_commit,
            "input_sha256": {k: v["sha256"] for k, v in plan["inputs"].items()},
            "code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "limits": plan["limits"], "paperclip_used": False, "new_market_data_requests": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("refusing to overwrite research result")
    report = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        handle.write("\n")
    print(json.dumps({k: report[k] for k in ("decision", "failed_checks", "summary")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
