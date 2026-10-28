"""One offline diagnosis of archived picks against outcome-blind date controls.

Uses preserved KRX prices, never a reconstructed live decision, account return,
registered verdict or proposed trading rule. Imports do not load environment keys.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import io
import json
import math
from pathlib import Path
from statistics import mean, stdev
import subprocess
from zipfile import ZipFile

import pandas as pd

from signal_desk import market_clock
from scripts.measure.historical_signal_audit import _registered_start
from scripts.measure.krx_price_replay_pilot import load_krx_archives, _number

PLAN = "docs/llm-free-selection-audit-plan-2026-10-29.json"
CODE = (PLAN, "scripts/measure/recorded_selection_diagnosis.py",
        "tests/test_recorded_selection_diagnosis.py", "src/signal_desk/market_clock.py",
        "scripts/measure/krx_price_replay_pilot.py")


def positive(value):
    number = _number(value)
    return number if number is not None and number > 0 else None


def diagnose(signals, raw_days, archived_prices, plan):
    boundary = min(plan["protected_start"], _registered_start())
    first, last = plan["signal_from"], min(plan["signal_until"], boundary)
    frame = signals[(signals.date.astype(str) >= first)
                    & (signals.date.astype(str) <= last)
                    & (signals.date.astype(str) < boundary)].copy()
    if frame.duplicated(["date", "ticker"]).any():
        raise ValueError("ambiguous recorded decision")
    # Remove protected dates before any numeric price operation.
    days = {d: {r["ISU_CD"]: r for r in rows} for d, rows in raw_days.items()
            if d < boundary and d <= plan["price_until"]}
    bars = archived_prices[archived_prices.date.astype(str) < boundary]
    if bars.duplicated(["date", "ticker"]).any():
        raise ValueError("ambiguous archived price")
    archived = {(str(r["date"]), str(r["ticker"])): positive(r["close"])
                for r in bars.to_dict("records")}
    result = {"schema":"recorded-selection-diagnosis-v1", "market":"kr",
              "source_level":"legacy_snapshot_unverified", "daily":[],
              "excluded_non_session_dates":[], "price_disagreements":[],
              "strict_pit_eligible":False, "live_order_eligible":False,
              "profitability_verified":False, "adoption":"diagnostic_only_no_trading_change"}
    agreement_n = 0
    for day, group in frame.groupby("date", sort=True):
        day = str(day)
        if not market_clock.is_session("kr", day):
            result["excluded_non_session_dates"].append(day)
            continue
        pool = sorted(group.ticker.astype(str))
        picks = sorted(group[group.kind.isin(["BUY", "STRONG_BUY"])].ticker.astype(str))
        for ticker in pool:
            official = positive(days.get(day, {}).get(ticker, {}).get("TDD_CLSPRC"))
            old = archived.get((day, ticker))
            if official is not None and old is not None:
                agreement_n += 1
                if abs(official - old) > max(1e-8, abs(official)*1e-9):
                    result["price_disagreements"].append({"date":day,"ticker":ticker,
                        "archived_close":old,"krx_close":official})
        warmup = sorted(d for d in days if d <= day)[-21:]
        complete_warmup = (len(warmup) == 21 and warmup[-1] == day and all(
            market_clock.consecutive_sessions("kr", a, b) for a,b in zip(warmup,warmup[1:])))
        volatility = {}
        for ticker in pool:
            closes = [positive(days[d].get(ticker, {}).get("TDD_CLSPRC")) for d in warmup]
            if complete_warmup and all(v is not None for v in closes):
                volatility[ticker] = stdev([b/a-1 for a,b in zip(closes,closes[1:])])
        missing_risk = sorted(set(pool)-set(volatility))
        groups = {t: min(4, i*5//len(pool)) for i,t in enumerate(
            sorted(volatility, key=lambda t:(volatility[t],t)))} if not missing_risk else {}
        chosen_groups = Counter(groups[t] for t in picks) if groups else {}
        for h in plan["horizons"]:
            sessions = market_clock.next_sessions("kr", day, h)
            row = {"date":day,"horizon":h,"universe":len(pool),"selected":picks,
                   "entry_date":sessions[0],"exit_date":sessions[-1],
                   "missing_risk_tickers":missing_risk}
            result["daily"].append(row)
            if not picks:
                row["state"] = "no_archived_buy"
                continue
            if sessions[-1] >= boundary or sessions[-1] > plan["price_until"]:
                row["state"] = "outside_frozen_price_window"
                continue
            missing, returns = [], {}
            for ticker in pool:
                entry = positive(days.get(sessions[0],{}).get(ticker,{}).get("TDD_OPNPRC"))
                closes = [positive(days.get(d,{}).get(ticker,{}).get("TDD_CLSPRC")) for d in sessions]
                reference = positive(days.get(day,{}).get(ticker,{}).get("TDD_CLSPRC"))
                if entry is None or reference is None or any(v is None for v in closes):
                    missing.append(ticker)
                    continue
                returns[ticker] = {"gross_pct":(closes[-1]/entry-1)*100,
                    "close_reference_pct":(closes[-1]/reference-1)*100,
                    "overnight_gap_pct":(entry/reference-1)*100}
            if missing:
                row.update(state="incomplete_whole_cohort", missing_price_tickers=missing)
                continue  # Never compare selected survivors with a censored control.
            selected = mean(returns[t]["gross_pct"] for t in picks)
            uniform = mean(r["gross_pct"] for r in returns.values())
            matched, group_rows = [], []
            if groups:
                for bucket, count in sorted(chosen_groups.items()):
                    controls = sorted(t for t in pool if groups[t] == bucket and t not in picks)
                    group_rows.append({"bucket":bucket,"selected_count":count,
                                       "nonselected_controls":len(controls)})
                    if len(controls) < count:
                        break
                    matched.extend([mean(returns[t]["gross_pct"] for t in controls)]*count)
            risk_return = mean(matched) if len(matched) == len(picks) else None
            row.update(state="complete_cohort", selected_gross_pct=selected,
                uniform_expected_gross_pct=uniform, risk_matched_expected_gross_pct=risk_return,
                selected_minus_uniform_pp=selected-uniform,
                selected_minus_risk_matched_pp=selected-risk_return if risk_return is not None else None,
                risk_group_counts=group_rows,
                selected_cases=[{"ticker":t, **returns[t],
                    "close_reference_minus_next_open_pp":returns[t]["close_reference_pct"]-returns[t]["gross_pct"]}
                    for t in picks],
                friction_scenarios=[{"roundtrip_pct":cost,"selected_net_pct":selected-cost,
                    "uniform_expected_net_pct":uniform-cost,
                    "risk_matched_expected_net_pct":risk_return-cost if risk_return is not None else None}
                    for cost in plan["friction_roundtrip_pct"]])
    result["price_pairs_compared"] = agreement_n
    result["summary"] = {}
    for h in plan["horizons"]:
        all_rows = [r for r in result["daily"] if r["horizon"] == h]
        ready = [r for r in all_rows if r["state"] == "complete_cohort"]
        summary = {"status_counts":dict(Counter(r["state"] for r in all_rows)),
            "dates":len(ready),"selected_ticker_dates":sum(len(r["selected"]) for r in ready),
            "unique_selected_tickers":len({t for r in ready for t in r["selected"]})}
        for field in ("selected_gross_pct","uniform_expected_gross_pct","risk_matched_expected_gross_pct",
                      "selected_minus_uniform_pp","selected_minus_risk_matched_pp"):
            values = [r[field] for r in ready if r[field] is not None]
            summary[field] = mean(values) if values else None
        summary["risk_matched_dates"] = sum(r["risk_matched_expected_gross_pct"] is not None for r in ready)
        last_exit, windows = "", 0
        for r in ready:
            if r["entry_date"] > last_exit:
                windows += 1
                last_exit = r["exit_date"]
        summary["nonoverlapping_windows_not_independence_proof"] = windows
        result["summary"][str(h)] = summary
    result["limitations"] = ["Old recorded labels, not the current engine or full input replay",
        "Cohort averages and expected K-name controls, not integer-account or executable bid/ask PnL",
        "Volatility-group counts match; realized total risk, beta and correlation are not matched",
        "No intraday stop/exit, corporate-action/dividend certification or source publication-time proof",
        "Short, previously observed development dates; no significance or promotion inference",
        "Decision-close reference is unexecutable diagnostic, not a proposed entry or achievable profit"]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--signals-archive", type=Path, required=True)
    parser.add_argument("--implementation-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    if not args.output.resolve().is_relative_to(root/"data/research"):
        parser.error("output must remain in local research directory")
    hashes = {}
    for name in CODE:
        raw = (root/name).read_bytes()
        if raw != subprocess.check_output(["git","show",f"{args.implementation_commit}:{name}"],cwd=root):
            raise ValueError("code or plan not frozen: "+name)
        hashes[name] = hashlib.sha256(raw).hexdigest()
    plan = json.loads((root/PLAN).read_text())
    source = args.signals_archive.read_bytes()
    if hashlib.sha256(source).hexdigest() != plan["inputs"]["signals_archive_sha256"]:
        raise ValueError("signal archive differs")
    with ZipFile(io.BytesIO(source)) as z:
        manifest = json.loads(z.read("manifest.json"))
        raw = z.read("signals.parquet")
        prices = z.read("prices.parquet")
        if (manifest["market"] != "kr" or hashlib.sha256(raw).hexdigest() !=
                plan["inputs"]["signals_member_sha256"] or hashlib.sha256(prices).hexdigest() != manifest["prices_sha256"]):
            raise ValueError("recorded member differs")
        signals = pd.read_parquet(io.BytesIO(raw))
        archived_prices = pd.read_parquet(io.BytesIO(prices))
    krx = root/plan["inputs"]["krx_archive"]
    if hashlib.sha256(krx.read_bytes()).hexdigest() != plan["inputs"]["krx_archive_sha256"]:
        raise ValueError("KRX source differs")
    days, sources = load_krx_archives([krx])
    result = diagnose(signals, days, archived_prices, plan)
    result.update(code_sha256=hashes, sources=sources, plan_id=plan["id"],
                  implementation_commit=args.implementation_commit)
    with args.output.open("x") as stream:
        json.dump(result,stream,ensure_ascii=False,allow_nan=False,indent=2)
    print(json.dumps({"summary":result["summary"],"price_disagreements":len(result["price_disagreements"]),
                      "adoption":result["adoption"]},ensure_ascii=False))


if __name__ == "__main__":
    main()
