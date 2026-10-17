"""Paired price-only vs receipt-dated annual sensitivity, not eight-factor PIT."""

from __future__ import annotations

import argparse
from collections import Counter
import datetime as dt
import hashlib
import json
from pathlib import Path
from statistics import mean

from scripts.measure.kr_financial_input_sources import SCOPE, SCOPE_SHA, load
from scripts.measure.krx_price_replay_pilot import ROOT, _number, load_krx_archives
from scripts.measure.price_challenger_lab import PricePanel
from signal_desk.signals import engine
from signal_desk.signals.research_financial_inputs import metrics_asof


def daily_outcome(signals: list, rows: list[dict]) -> dict:
    """Same-day cohort and missing outcomes retained; no account-return claim."""
    observed = {r["ticker"]: r["horizons"]["5"] for r in rows}
    selected = [s for s in signals if engine.is_buy(s.kind)]
    priced = [observed[s.ticker]["net_pct"] for s in selected if observed[s.ticker]["state"] == "matured"]
    cohort = [v["net_pct"] for v in observed.values() if v["state"] == "matured"]
    missing = len(selected) - len(priced)
    # One missing selected return must not disappear from a favorable average.
    net = mean(priced) if priced and not missing else None
    return {"buy_tickers": [s.ticker for s in selected], "buy_labels": len(selected),
            "selected_missing_outcomes": missing, "h5_mean_net_pct": net,
            "h5_same_day_cohort_mean_net_pct": mean(cohort) if cohort else None,
            "h5_excess_pp": net - mean(cohort) if net is not None and cohort else None}


def run(source: Path, versions_path: Path | None = None) -> dict:
    raw = SCOPE.read_bytes()
    if hashlib.sha256(raw).hexdigest() != SCOPE_SHA:
        raise ValueError("domestic census changed")
    frozen = json.loads(raw)
    financials = load(source)
    versions = None
    if versions_path is not None:
        from scripts.measure.kr_original_financial_sources import load as load_versions
        versions = load_versions(versions_path)
        if versions["financial_archive_sha256"] != financials["archive_sha256"]:
            raise ValueError("version chain bound to different financial source")
    days, sources = load_krx_archives([ROOT / "data/research" / s["archive"] for s in frozen["sources"]])
    if sources != frozen["sources"]:
        raise ValueError("domestic price source hashes changed")
    panel = PricePanel(days)
    windows = {}
    for name, window in frozen["windows"].items():
        by_day = {}
        for row in window["selection"]["rows"]:
            by_day.setdefault(row["date"], []).append(row)
        daily, missing = [], Counter()
        for day, rows in sorted(by_day.items()):
            if day >= "2026-08-05":
                raise ValueError("protected date")
            names = {r["ticker"]: r["name"] for r in rows}
            universe = [{"ticker": r["ISU_CD"], "name": str(r.get("ISU_NM") or r["ISU_CD"])}
                        for r in sorted((r for r in days[day] if len(str(r["ISU_CD"])) == 6
                                         and str(r["ISU_CD"]).endswith("0") and (_number(r["MKTCAP"]) or 0) > 0),
                                        key=lambda r: (-_number(r["MKTCAP"]), r["ISU_CD"]))[:200]]
            if len(universe) != 200:
                raise ValueError("incomplete daily top200")
            index = panel.index[day]
            prices = {t: [panel.bars.get((d, t), (None, None))[1]
                           for d in panel.sessions[index - 252:index + 1]] for t in names}
            if any(len(v) != 253 or any(p is None or p <= 0 for p in v) for v in prices.values()):
                raise ValueError("scored census lost continuous close inputs")
            caps = {r["ISU_CD"]: _number(r["MKTCAP"]) for r in days[day] if r["ISU_CD"] in names}
            metrics, excluded = metrics_asof(financials["records"], day, caps, versions=versions)
            missing.update(excluded.values())
            kwargs = {"today": dt.date.fromisoformat(day), "config": engine.SignalConfig(),
                      "flows": {}, "shorts": {}, "sentiment": {}, "earnings_dates": {}}
            baseline = engine.evaluate(universe, prices, fundamentals={},
                                       unavailable=("fundamental", "valuation", "quality", "flow", "short"), **kwargs)
            extended = engine.evaluate(universe, prices, fundamentals=metrics,
                                       unavailable=("flow", "short"), **kwargs)
            a, b = daily_outcome(baseline, rows), daily_outcome(extended, rows)
            selection = engine.selection_summary(extended, kwargs["config"])
            daily.append({"date": day, "candidate_slots": 200, "priced_names": len(prices),
                          "financial_names": len(metrics), "price_only": a, "annual_financial": b,
                          "annual_blocked_rank_slots": selection["gates"]["blocked_in_window"],
                          "buy_set_changed": set(a["buy_tickers"]) != set(b["buy_tickers"])})
        def summary(arm):
            complete = all(d[arm]["h5_excess_pp"] is not None for d in daily)
            return {"days": len(daily), "comparable_all_dates": complete,
                    "no_buy_dates": sum(d[arm]["buy_labels"] == 0 for d in daily),
                    "buy_labels": sum(d[arm]["buy_labels"] for d in daily),
                    "missing_selected_outcomes": sum(d[arm]["selected_missing_outcomes"] for d in daily),
                    "equal_day_h5_mean_net_pct": mean(d[arm]["h5_mean_net_pct"] for d in daily) if complete else None,
                    "equal_day_h5_excess_pp": mean(d[arm]["h5_excess_pp"] for d in daily) if complete else None}
        windows[name] = {"price_only": summary("price_only"), "annual_financial": summary("annual_financial"),
                         "changed_buy_dates": sum(d["buy_set_changed"] for d in daily),
                         "financial_missing_ticker_days": dict(missing), "daily": daily}
        print(json.dumps({"window": name, "changed_buy_dates": windows[name]["changed_buy_dates"]}), flush=True)
    return {"schema": "kr-receipt-financial-sensitivity-v1", "windows": windows,
            "financial_archive_sha256": financials["archive_sha256"], "scope_sha256": SCOPE_SHA,
            "financial_status_counts": financials["status_counts"],
            "original_version_archive_sha256": versions["archive_sha256"] if versions else None,
            "original_version_status_counts": versions["status_counts"] if versions else None,
            "registered_verdict": False, "strict_pit_eligible": False, "live_order_eligible": False,
            "adoption": "not_adopted_receipt_date_sensitivity_only",
            "code_sha256": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in (
                "scripts/measure/kr_receipt_financial_lab.py", "scripts/measure/kr_financial_input_sources.py",
                "src/signal_desk/signals/research_financial_inputs.py", "src/signal_desk/signals/engine.py",
                "scripts/measure/kr_original_financial_sources.py")},
            "limits": ["Same frozen 293 development dates, not independent OOS",
                       "Annual April FY convention with receipt-next-session delay is an assumption",
                       "Only optional frozen 20-company FY2025 originals restored; no full historic as-run input claim",
                       "Current sector mapping unverified historically; flow/short/events/dividend yield absent",
                       "Overlapping h5 next-open-to-close labels with assumed 25bp costs, not portfolio returns",
                       "Missing selected outcomes or empty buy dates block a full-window mean",
                       "Corporate-action total return and real execution remain unverified"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--versions", type=Path, help="Optional receipt-specific original version archive")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("refusing to overwrite sensitivity result")
    result = run(args.source, args.versions)
    with args.output.open("x") as stream:
        json.dump(result, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    print(json.dumps({"output": str(args.output), "adoption": result["adoption"]}))


if __name__ == "__main__":
    main()
