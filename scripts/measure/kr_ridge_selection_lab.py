"""Frozen R3 one-fit historical experiment; no credentials, network or live state."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
from statistics import mean, median, pstdev
import subprocess
import time
from dataclasses import asdict
from zoneinfo import ZoneInfo

from scripts.measure.kr_financial_input_sources import SCOPE, SCOPE_SHA, load as load_financials
from scripts.measure.kr_original_financial_sources import FINANCIAL, FINANCIAL_SHA
from scripts.measure.krx_price_replay_pilot import ROOT, _number, load_krx_archives
from scripts.measure.price_challenger_lab import PricePanel
from signal_desk.signals import engine, research_ridge
from signal_desk.signals.research_decision import Candidate, CompletedDailyBar, DailyPolicy, DailySnapshot
from signal_desk.signals.research_daily_simulator import DailyCostModel, DailyExecutionScenario, simulate_daily
from signal_desk.signals.research_financial_inputs import metrics_asof

PLAN_COMMIT = "c2a0b3ee14ee8e3bca63782fbc41ec4611a61113"
PLAN_PATH = ROOT / "docs/kr-ridge-selection-plan-2026-10-17.json"
PLAN_SHA = "5bf03e849b6503332b3eb9468e69be5b3d3691f21c6c50819f44c71e5d50417f"
PROSE_PATH = ROOT / "docs/KR-RIDGE-SELECTION-PLAN-2026-10-17.md"
PROSE_SHA = "79714b331935648646c80d7f3aa439c470ce62335be592b2f66f073f552975e9"
KST = ZoneInfo("Asia/Seoul")
ARMS = ("ridge", "legacy", "cap", "random", "cash")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def encoded_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def plan():
    for path, expected in ((PLAN_PATH, PLAN_SHA), (PROSE_PATH, PROSE_SHA)):
        if digest(path) != expected:
            raise ValueError("precommitted plan differs")
        committed = subprocess.check_output(["git", "show", f"{PLAN_COMMIT}:{path.relative_to(ROOT)}"], cwd=ROOT)
        if hashlib.sha256(committed).hexdigest() != expected:
            raise ValueError("plan was not fixed in declared commit")
    return json.loads(PLAN_PATH.read_text())


def top200(rows):
    selected = sorted((r for r in rows if len(str(r["ISU_CD"])) == 6 and str(r["ISU_CD"]).endswith("0")
                       and (_number(r.get("MKTCAP")) or 0) > 0),
                      key=lambda r: (-_number(r["MKTCAP"]), r["ISU_CD"]))[:200]
    if len(selected) != 200:
        raise ValueError("incomplete point-date top200")
    return selected


def features_at(day, panel, by_day, financials, financial_roster):
    """Read prices only through day; no labels or later rows can enter features."""
    i = panel.index[day]
    universe = top200(by_day[day].values())
    prices = {}
    raw = {r["ISU_CD"]: r for r in universe}
    for ticker in raw:
        values = [panel.bars.get((s, ticker), (None, None))[1] for s in panel.sessions[max(0, i - 252):i + 1]]
        if len(values) == 253 and all(v is not None and v > 0 for v in values):
            prices[ticker] = values
    caps = {t: _number(raw[t]["MKTCAP"]) for t in prices}
    metrics, excluded = metrics_asof(financials, day, {t: c for t, c in caps.items() if t in financial_roster})
    market20 = median(v[-1] / v[-21] - 1 for v in prices.values()) if prices else 0
    rows = []
    for ticker, closes in prices.items():
        current = raw[ticker]
        if (_number(current.get("TDD_OPNPRC")) or 0) <= 0:
            continue
        volumes = [_number(by_day[s].get(ticker, {}).get("ACC_TRDVOL")) for s in panel.sessions[i - 20:i + 1]]
        rv = (volumes[-1] / mean(volumes[:-1]) if all(v is not None and v >= 0 for v in volumes)
              and mean(volumes[:-1]) > 0 else None)
        m = metrics.get(ticker, {})
        changes = [closes[j] / closes[j - 1] - 1 for j in range(len(closes) - 20, len(closes))]
        values = [closes[-1] / closes[-2] - 1, closes[-1] / closes[-6] - 1,
                  closes[-1] / closes[-21] - 1 - market20, closes[-1] / closes[-61] - 1,
                  closes[-22] / closes[0] - 1, pstdev(changes),
                  closes[-1] / mean(closes[-20:]) - 1, closes[-1] / mean(closes[-60:]) - 1,
                  market20, math.log(caps[ticker]), rv,
                  m.get("roe"), m.get("debt_ratio"), m.get("revenue_growth")]
        rows.append({"date": day, "ticker": ticker, "name": str(current.get("ISU_NM") or ticker),
                     "features": values, "market_cap": caps[ticker]})
    return rows, prices, metrics, universe, dict(Counter(excluded.values()))


def label_at(day, ticker, panel, by_day, horizon):
    i = panel.index[day]
    if i + 1 + horizon >= len(panel.sessions):
        return {"state": "unmatured"}
    path = panel.sessions[i + 1:i + 2 + horizon]
    entry, exit_ = panel.bars.get((path[0], ticker), (None, None))[0], panel.bars.get((path[-1], ticker), (None, None))[0]
    if entry is None or entry <= 0 or exit_ is None or exit_ <= 0:
        return {"state": "missing_entry_or_exit_open"}
    shares = [_number(by_day[s].get(ticker, {}).get("LIST_SHRS")) for s in path]
    if any(s is None or s <= 0 for s in shares) or len(set(shares)) != 1:
        return {"state": "share_history_unverified"}
    return {"state": "matured", "target": exit_ / entry - 1, "exit_date": path[-1]}


def portfolio_stats(result, side_cost):
    """Cost-inclusive closed-lot PnL, including split sells in half-fill stress."""
    lots, pnl, months = {}, defaultdict(float), defaultdict(float)
    trades, entry_dates, tickers = 0, set(), set()
    for fill in result["fills"]:
        ticker, quantity, price = fill["ticker"], fill["shares"], fill["price"]
        if fill["side"] == "buy":
            if ticker in lots:
                raise ValueError("unexpected averaging up in fixed-slot replay")
            month = fill["decision_input_id"].split("/")[3][:7]
            lots[ticker] = [quantity, price * (1 + side_cost), month]
            entry_dates.add(fill["session"])
            tickers.add(ticker)
        else:
            remaining, basis, month = lots[ticker]
            if quantity > remaining:
                raise ValueError("sell exceeds held shares")
            value = quantity * (price * (1 - side_cost) - basis)
            pnl[ticker] += value
            months[month] += value
            lots[ticker][0] -= quantity
            if lots[ticker][0] == 0:
                del lots[ticker]
                trades += 1
    denominator = sum(abs(v) for v in pnl.values())
    kept = {key: result[key] for key in ("state", "net_return_pct", "fills", "equity_curve")}
    kept.update({key: result[key] for key in ("session", "ticker", "max_drawdown_pct", "skips", "return_quality") if key in result})
    kept.update({"completed_trades": trades, "entry_dates": len(entry_dates), "distinct_tickers": len(tickers),
                 "top_ticker_pnl_share": max((abs(v) for v in pnl.values()), default=0) / denominator if denominator else None,
                 "pnl_by_ticker": dict(pnl), "pnl_by_entry_month": dict(months)})
    if result["state"] == "complete" and (lots or abs(sum(pnl.values()) - (result["cash_end"] - 10_000_000)) > .01):
        raise ValueError("closed-lot PnL does not reconcile with cash")
    return kept


def execute(compiled, by_day, panel, bars, cfg, *, scenario, arm, phase, reviewed=None):
    cost_bps = int(scenario.split("bp")[0])
    fraction = .5 if scenario.endswith("half") else 0.0 if scenario.endswith("none") else 1.0
    delay = 1 if scenario.endswith("delay1") else 0
    active = {}
    for index, (day, row) in enumerate(compiled.items()):
        if index % cfg["phase_count"] != phase:
            continue
        selected = row["selected"][arm]
        shifted = panel.sessions[panel.index[day] + delay]
        active[shifted] = (day, selected)
    snapshots = []
    for day in bars:
        original, selected = active.get(day, (day, []))
        candidates = tuple(Candidate(t, rank + 1, score, True, f"{arm}:{phase}:{original}:{t}")
                           for rank, (t, score) in enumerate(selected))
        snapshots.append(DailySnapshot("kr", day, dt.datetime.combine(dt.date.fromisoformat(day), dt.time(16), KST),
                                       f"r3/{arm}/{phase}/{original}/{PLAN_SHA}", bars[day], candidates))
    costs = DailyCostModel(f"r3-{cost_bps}bp", cost_bps / 20_000, cost_bps / 20_000)
    policy = DailyPolicy(f"{cfg['candidate_id']}:{arm}:phase{phase}", costs.model_id,
                         cfg["max_positions"], cfg["horizon_open_to_open_sessions"])
    result = simulate_daily(snapshots, policy=policy, costs=costs, starting_cash=cfg["starting_cash"],
                            execution=DailyExecutionScenario(scenario, assumed_fill_fraction=fraction),
                            reviewed_share_changes=reviewed)
    return portfolio_stats(result, costs.buy_fraction)


def run(model_path, listing_archive=None):
    started = time.monotonic()
    cfg = plan()
    if digest(SCOPE) != SCOPE_SHA or digest(FINANCIAL) != FINANCIAL_SHA:
        raise ValueError("frozen source differs")
    scope = json.loads(SCOPE.read_text())
    declared = scope["sources"][:9]
    days, sources = load_krx_archives([ROOT / "data/research" / s["archive"] for s in declared])
    if sources != declared:
        raise ValueError("KRX source hashes differ")
    # Do not expose post-liquidation bars even to the evaluation builder.
    days = {d: rows for d, rows in days.items() if d <= cfg["liquidate_through"]}
    by_day = {d: {r["ISU_CD"]: r for r in rows} for d, rows in days.items()}
    panel = PricePanel(days)
    from scripts.measure.kr_ridge_action_sources import reviewed_facts
    reviewed = reviewed_facts(listing_archive, by_day, panel) if listing_archive else {}
    financials = load_financials(FINANCIAL)["records"]
    roster = {r["ISU_CD"] for r in top200(days[cfg["financial_roster_asof"]])}
    train, census = [], Counter()
    for day in panel.sessions:
        if not cfg["train_start"] <= day <= cfg["train_end"]:
            continue
        rows, _, _, _, _ = features_at(day, panel, by_day, financials, roster)
        census["training_candidate_slots"] += 200
        census["training_feature_rows"] += len(rows)
        for row in rows:
            label = label_at(day, row["ticker"], panel, by_day, cfg["horizon_open_to_open_sessions"])
            if label["state"] != "matured":
                census["training_labels_excluded:" + label["state"]] += 1
                continue
            train.append({**row, **label})
    training_sha = encoded_digest(train)
    if model_path.exists():
        saved = json.loads(model_path.read_text())
        if saved["plan_sha256"] != PLAN_SHA or saved["training_rows_sha256"] != training_sha:
            raise ValueError("saved one-fit model scope differs")
        model = saved["model"]
    else:
        model = research_ridge.fit(train, cfg)
        saved = {"plan_sha256": PLAN_SHA, "plan_commit": PLAN_COMMIT, "training_rows_sha256": training_sha,
                 "model": model, "fit_module_sha256": digest(Path(research_ridge.__file__))}
        with model_path.open("x") as stream:
            json.dump(saved, stream, allow_nan=False, sort_keys=True)
    print(json.dumps({"phase": "model_frozen", "training_rows": model["training_rows"],
                      "training_dates": model["training_dates"], "model_sha256": digest(model_path)}), flush=True)
    compiled, predictions, needed = {}, [], set()
    for day in panel.sessions:
        if not cfg["evaluation_start"] <= day <= cfg["evaluation_end"]:
            continue
        rows, prices, metrics, universe, _ = features_at(day, panel, by_day, financials, roster)
        forecast = research_ridge.predict([r["features"] for r in rows], model) if rows else []
        ranked = sorted(zip(rows, forecast), key=lambda pair: (-pair[1], pair[0]["ticker"]))
        pool = {r["ticker"]: r for r in rows}
        chosen = [(r["ticker"], p) for r, p in ranked[:cfg["max_positions"]] if p > cfg["predicted_gross_entry_min"]]
        if len(rows) < cfg["min_price_feature_names"]:
            chosen = []
        legacy = engine.evaluate([{"ticker": r["ISU_CD"], "name": r["ISU_NM"]} for r in universe], prices,
                                 fundamentals=metrics, config=engine.SignalConfig(), today=dt.date.fromisoformat(day),
                                 unavailable=("flow", "short"))
        old = [(r.ticker, r.score) for r in legacy if engine.is_buy(r.kind) and r.ticker in pool]
        if len(rows) < cfg["min_price_feature_names"]:
            old = []
        cap = sorted(rows, key=lambda r: (-r["market_cap"], r["ticker"]))[:len(chosen)]
        random = sorted(rows, key=lambda r: hashlib.sha256(
            f"{cfg['random_control_seed']}:{day}:{r['ticker']}".encode()).hexdigest())[:len(chosen)]
        selected = {"ridge": chosen, "legacy": old, "cap": [(r["ticker"], 0) for r in cap],
                    "random": [(r["ticker"], 0) for r in random], "cash": []}
        compiled[day] = {"selected": selected, "feature_names": len(rows)}
        needed.update(pool)
        predictions.extend({"date": day, "ticker": r["ticker"], "predicted_gross": p} for r, p in ranked)
    bars = {}
    for day in panel.sessions:
        if not cfg["evaluation_start"] <= day <= cfg["liquidate_through"]:
            continue
        previous = panel.sessions[panel.index[day] - 1]
        stamp = dt.datetime.combine(dt.date.fromisoformat(day), dt.time(16), KST)
        current = []
        for ticker in sorted(needed):
            row, prior = by_day[day].get(ticker), by_day[previous].get(ticker, {})
            if row is None:
                continue
            opened, closed = panel.bars.get((day, ticker), (None, None))
            opened, closed = opened if opened and opened > 0 else None, closed if closed and closed > 0 else None
            shares, old_shares = _number(row.get("LIST_SHRS")), _number(prior.get("LIST_SHRS"))
            status = "trading" if opened and closed and shares and shares > 0 else "unknown"
            action = "unsupported" if shares and old_shares and shares != old_shares else None
            if (day, ticker) in reviewed:
                action = reviewed[day, ticker].kind
            capacity = int(max(0, _number(prior.get("ACC_TRDVOL")) or 0) * cfg["volume_capacity_fraction"])
            current.append(CompletedDailyBar("kr", day, ticker, opened, closed, stamp,
                                            f"{SCOPE_SHA}:{day}:{ticker}", status=status,
                                            listed_shares=int(shares) if shares and shares > 0 else None,
                                            corporate_action=action, open_fill_cap_shares=capacity))
        bars[day] = tuple(current)
    scenarios = {}
    for scenario in cfg["scenarios"]:
        scenarios[scenario] = {}
        for arm in ARMS:
            if time.monotonic() - started > cfg["cpu_budget_seconds"]:
                raise ValueError("fixed experiment time budget exceeded")
            scenarios[scenario][arm] = [execute(compiled, by_day, panel, bars, cfg, scenario=scenario, arm=arm, phase=p, reviewed=reviewed)
                                        for p in range(cfg["phase_count"])]
        print(json.dumps({"phase": "scenario_complete", "scenario": scenario,
                          "ridge_states": [r["state"] for r in scenarios[scenario]["ridge"]]}), flush=True)
    return {"schema": "kr-ridge-selection-result-v1", "candidate_id": cfg["candidate_id"],
            "plan_commit": PLAN_COMMIT, "plan_sha256": PLAN_SHA, "sources": sources,
            "financial_archive_sha256": FINANCIAL_SHA, "financial_roster": sorted(roster),
            "model_sha256": digest(model_path), "model": saved, "census": dict(census),
            "listing_archive_sha256": digest(listing_archive) if listing_archive else None,
            "reviewed_share_changes": [{**asdict(f), "source_observed_at": f.source_observed_at.isoformat()}
                                       for f in reviewed.values()],
            "evaluation_decision_dates": list(compiled), "selections": compiled, "predictions": predictions,
            "scenarios": scenarios, "verdict": research_ridge.grade(scenarios, cfg),
            "elapsed_seconds": time.monotonic() - started,
            "code_sha256": {p: digest(ROOT / p) for p in (
                "scripts/measure/kr_ridge_selection_lab.py", "src/signal_desk/signals/research_ridge.py",
                "scripts/measure/kr_ridge_action_sources.py", "src/signal_desk/signals/research_action_coverage.py",
                "src/signal_desk/signals/research_daily_simulator.py", "src/signal_desk/signals/research_decision.py",
                "src/signal_desk/signals/research_financial_inputs.py", "src/signal_desk/signals/engine.py")},
            "strict_pit_eligible": False, "live_order_eligible": False, "registered_verdict": False,
            "evaluation_is_pristine_unseen": False, "verified_total_return": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True, help="One-fit artifact, reused without refitting")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--listing-archive", type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("refusing to overwrite a historical result")
    result = run(args.model, args.listing_archive)
    with args.output.open("x") as stream:
        json.dump(result, stream, allow_nan=False, ensure_ascii=False, separators=(",", ":"))
    print(json.dumps({"output": str(args.output), "sha256": digest(args.output),
                      "verdict": result["verdict"], "seconds": result["elapsed_seconds"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
