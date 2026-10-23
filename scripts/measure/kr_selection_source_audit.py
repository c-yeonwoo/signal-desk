"""Read-only input/selection attribution; no outcomes, fits or trading paths."""
from __future__ import annotations

import argparse
from collections import Counter
import datetime as dt
import io
import json
import subprocess
from pathlib import Path
from zipfile import ZipFile

import pandas as pd

from scripts.measure import kr_frozen_forecast_policy as f
from scripts.measure import kr_ridge_selection_lab as r3


def summarize(signals):
    return {"rows": len(signals), "buys": [s.ticker for s in signals if r3.engine.is_buy(s.kind)],
            "top_window": [{"ticker": s.ticker, "score": s.score, "kind": s.kind,
                "coverage": s.data_coverage, "missing": s.missing_factors,
                "low_coverage": s.low_coverage, "gates": s.gates}
                for s in signals[:r3.engine.rank_slots(len(signals), 3.)]]}


def recorded_inventory(path):
    """Only signal metadata inventory. Protected outcomes are never calculated."""
    with ZipFile(path) as z:
        manifest = json.loads(z.read("manifest.json"))
        if manifest.get("schema") != "historical-inputs-v1" or manifest.get("market") != "kr":
            raise ValueError("not_domestic_recorded_archive")
        for name, key in (("signals.parquet", "signals_sha256"), ("prices.parquet", "prices_sha256")):
            if f.sha(z.read(name)) != manifest[key]:
                raise ValueError("recorded_member_hash_differs")
        signals = pd.read_parquet(io.BytesIO(z.read("signals.parquet")))
    if signals.duplicated(["date", "ticker"]).any():
        raise ValueError("duplicate_recorded_signal")
    valid = signals[signals["date"].map(lambda d: f.market_clock.is_session("kr", str(d)))]
    return {"archive_sha256": f.sha(path.read_bytes()), "manifest": manifest,
        "regular_session_rows": len(valid), "regular_session_dates": valid["date"].nunique(),
        "non_session_rows": len(signals)-len(valid),
        "observed_at_present": int(valid["observed_at"].notna().sum()),
        "bar_asof_present": int(valid["bar_asof"].notna().sum()),
        "factor_output_present": {k: int(valid[k].notna().sum()) for k in
            ("fundamental", "valuation", "flow", "quality", "momentum", "short")},
        "raw_factor_inputs_verified": False, "protected_performance_aggregates": False}


def run(root, commit, recorded_zip):
    _, inputs = f.preflight(root, commit, f.Budget())
    code_path = "scripts/measure/kr_selection_source_audit.py"
    body = (root / code_path).read_bytes()
    if body != subprocess.check_output(["git", "show", f"{commit}:{code_path}"], cwd=root):
        raise ValueError("audit_code_not_precommitted")
    inputs.provenance["code_sha256"][code_path] = f.sha(body)
    original = json.loads(f.fixed_bytes(root / "data/research/kr-ridge-r3-result-2026-10-17-v4.json",
        "2cf98e96eff44d415c0264bd1ec1b6f2b036804b9e2bbeb28476dc807994abfd"))
    # Explain exactly the saved engine, rather than silently changing its code.
    for p in ("scripts/measure/kr_ridge_selection_lab.py", "src/signal_desk/signals/engine.py"):
        f.fixed_bytes(root / p, original["code_sha256"][p])
    f.fixed_bytes(r3.FINANCIAL, r3.FINANCIAL_SHA)
    financials = r3.load_financials(r3.FINANCIAL)["records"]
    # Reuse the source-checked raw panel without reading a future return label.
    panel = r3.PricePanel({d: list(v.values()) for d, v in inputs.rows.items()})
    roster = set(original["financial_roster"])
    min_names = r3.plan()["min_price_feature_names"]
    daily = []
    for day in f.calendar("2024-10-02", "2024-11-15"):
        rows, prices, metrics, universe, excluded = r3.features_at(day, panel, inputs.rows, financials, roster)
        pool = {r["ticker"] for r in rows}
        names = [{"ticker": r["ISU_CD"], "name": r["ISU_NM"]} for r in universe]
        cfg = r3.engine.SignalConfig()
        kwargs = {"config": cfg, "today": dt.date.fromisoformat(day)}
        research = r3.engine.evaluate(names, prices, fundamentals=metrics, unavailable=("flow", "short"), **kwargs)
        restored = [(s.ticker, s.score) for s in research if r3.engine.is_buy(s.kind) and s.ticker in pool]
        if len(rows) < min_names:
            restored = []
        if restored != [tuple(p) for p in original["selections"][day]["selected"]["legacy"]]:
            raise ValueError("saved_legacy_selection_not_reproduced:"+day)
        # Contract probes, NOT reconstructed production inputs or new strategies.
        missing_kr = r3.engine.evaluate(names, prices, fundamentals=metrics, unavailable=(), **kwargs)
        price_only = r3.engine.evaluate(names, prices, fundamentals={},
            unavailable=("fundamental", "valuation", "quality", "flow", "short"), **kwargs)
        daily.append({"date": day, "feature_pool": len(pool), "fundamental_names": len(metrics),
            "financial_exclusions": excluded, "research": summarize(research),
            "saved_legacy": [t for t, _ in restored],
            "post_pool_removed": [s.ticker for s in research if r3.engine.is_buy(s.kind) and s.ticker not in pool],
            "kr_missing_flow_contract_probe": summarize(missing_kr),
            "price_only_contract_probe": summarize(price_only)})
    stages = ("research", "kr_missing_flow_contract_probe", "price_only_contract_probe")
    return {"schema": "kr-selection-source-audit-v1", "provenance": inputs.provenance,
        "financial_sha256": r3.FINANCIAL_SHA, "daily": daily,
        "summary": {s: {"buy_rows": sum(len(d[s]["buys"]) for d in daily),
            "buy_tickers": sorted({t for d in daily for t in d[s]["buys"]}),
            "zero_buy_dates": sum(not d[s]["buys"] for d in daily)} for s in stages},
        "post_pool_removed_rows": sum(len(d["post_pool_removed"]) for d in daily),
        "saved_legacy_count_distribution": dict(Counter(len(d["saved_legacy"]) for d in daily)),
        "recorded_inventory": recorded_inventory(recorded_zip),
        "production_decisions_reproduced": False, "outcome_calculations": 0,
        "fit_calls": 0, "trading_paths": 0, "live_order_eligible": False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--implementation-commit", required=True)
    p.add_argument("--recorded-zip", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    root = Path(__file__).resolve().parents[2]
    if not args.output.resolve().is_relative_to(root / "data/research"):
        p.error("output must remain in local research directory")
    result = run(root, args.implementation_commit, args.recorded_zip)
    with args.output.open("x") as stream:
        json.dump(result, stream, ensure_ascii=False, allow_nan=False)
    print(json.dumps({"summary": result["summary"], "pool_removed": result["post_pool_removed_rows"],
        "saved_count_distribution": result["saved_legacy_count_distribution"],
        "recorded_inventory": result["recorded_inventory"], "sha256": f.sha(args.output.read_bytes())}, ensure_ascii=False))


if __name__ == "__main__":
    main()
