"""Frozen domestic development census, not an eight-factor or live verdict."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import statistics

from scripts.measure.krx_price_replay_pilot import ROOT, _number, load_krx_archives, run_pilot
from scripts.measure.price_challenger_lab import ARCHIVES, HOLDOUT_ARCHIVES, HOLDOUT_WINDOWS, PILOTS, PricePanel
from scripts.measure.holding_horizon_portfolio_lab import first_buy_decisions, replay_portfolio
from scripts.measure.research_reviewed_share_changes import replay_facts, review
from signal_desk import strategy
from signal_desk.signals import engine

PLAN = ROOT / "docs/KR-SELECTION-HOLDING-AUDIT-2026-10-17.md"


def _mean(values):
    return round(statistics.mean(values), 5) if values else None


def selection_census(pilot: dict, days: dict) -> dict:
    dates = pilot["signal_dates"]
    if (pilot.get("registered_verdict") is not False or not dates
            or dates != sorted(set(dates)) or dates[-1] >= "2026-08-05"
            or pilot.get("source_level") != "C_offline_price_only_current_engine"):
        raise ValueError("not a frozen domestic development pilot")
    by_day = defaultdict(list)
    for row in pilot["rows"]:
        if row["date"] not in dates:
            raise ValueError("row outside frozen window")
        by_day[row["date"]].append(row)
    first = sorted((row for row in days[dates[0]] if len(str(row["ISU_CD"])) == 6
                    and str(row["ISU_CD"]).endswith("0") and (_number(row.get("MKTCAP")) or 0) > 0),
                   key=lambda row: (-_number(row["MKTCAP"]), str(row["ISU_CD"])))[:20]
    majors = {str(row["ISU_CD"]): str(row["ISU_NM"]) for row in first}
    panels = PricePanel(days)
    counts = Counter()
    cfg = engine.SignalConfig()
    groups, names, missing, detail = defaultdict(list), {}, Counter(), []
    per_ticker = defaultdict(list)
    for day in dates:
        rows = by_day[day]
        tickers = [r["ticker"] for r in rows]
        if len(tickers) != len(set(tickers)) or len(rows) > 200:
            raise ValueError("duplicate or oversized decision cohort")
        # The engine consumes closes, not opens. A missing historical open must
        # not silently remove a stock from the market-return baseline here.
        close_histories = {}
        for ticker in tickers:
            index = panels.index[day]
            history = [panels.bars.get((d, ticker), (None, None))[1]
                       for d in panels.sessions[max(0, index - 252):index + 1]]
            if len(history) == 253 and all(p is not None and p > 0 for p in history):
                close_histories[ticker] = history
        market_return = engine.market_return_pct(close_histories, 20)
        matured = [r for r in rows if r["outcomes"]["5"].get("state") == "matured"]
        comparison = _mean([r["outcomes"]["5"]["net_pct"] for r in matured])
        # Future returns only label outcomes here; they never select candidates.
        best = max(matured, key=lambda r: r["outcomes"]["5"]["net_pct"]) if matured else None
        for raw in days[day]:
            names[str(raw["ISU_CD"])] = str(raw.get("ISU_NM") or raw["ISU_CD"])
        counts["decision_days"] += 1
        counts["top200_candidate_slots"] += 200
        counts["unscored_candidate_slots"] += 200 - len(rows)
        if best:
            counts["days_highest_h5_name_not_buy"] += best["kind"] not in ("BUY", "STRONG_BUY")
        for row in rows:
            ticker = row["ticker"]
            buy = row["kind"] in ("BUY", "STRONG_BUY")
            counts["scored_ticker_days"] += 1
            counts["buy_label_days"] += buy
            closes = close_histories.get(ticker)
            recent = (closes[-1] / closes[-21] - 1) * 100 if closes else None
            momentum = (closes[-22] / closes[0] - 1) * 100 if closes else None
            conflict = recent is not None and recent < 0 and momentum > 0
            confirmed = blocked = None
            if closes:
                last = len(closes) - 1
                series = {"ma_short": {last: statistics.mean(closes[-cfg.ma_short:])},
                          "ma_mid": {last: statistics.mean(closes[-cfg.ma_mid:])}}
                confirmed = engine._downtrend_confirmed(closes, series, last, cfg)
                blocked = engine._downtrend_blocking(closes, series, last, cfg, market_return)
            else:
                counts["missing_trend_diagnostic"] += 1
            counts["buy_recent_down_midterm_up"] += buy and conflict
            counts["buy_downtrend_gate_relaxed"] += buy and confirmed is True and blocked is False
            entry = {"date": day, "ticker": ticker, "name": names.get(ticker, ticker),
                     "kind": row["kind"], "score": row["score"],
                     "recent20_pct": recent, "momentum_12_minus_1_pct": momentum,
                     "market20_median_pct": market_return,
                     "downtrend_confirmed": confirmed, "downtrend_blocking": blocked,
                     "horizons": {}, "same_day_h5_cohort_mean_net_pct": comparison,
                     "historical_full_engine": False}
            for horizon in ("1", "5", "20"):
                out = row["outcomes"][horizon]
                if out.get("state") != "matured":
                    missing[horizon + ":" + out.get("state", "unknown")] += 1
                    entry["horizons"][horizon] = {"state": out.get("state")}
                    continue
                net = float(out["net_pct"])
                entry["horizons"][horizon] = {"state": "matured", "net_pct": net}
                if horizon == "5":
                    bucket = "buy" if buy else "nonbuy"
                    groups[bucket].append(net)
                    if buy:
                        counts["buy_h5_negative"] += net < 0
                        counts["buy_h5_below_same_day_cohort"] += comparison is not None and net < comparison
                        groups["buy_conflict" if conflict else "buy_no_conflict"].append(net)
                    if net >= 3:
                        counts["h5_ge3_label_days"] += 1
                        counts["h5_ge3_not_buy_label_days"] += not buy
            detail.append(entry)
            per_ticker[ticker].append(entry)
    def summarize(rows):
        buys = [r["horizons"]["5"]["net_pct"] for r in rows
                if r["kind"] in ("BUY", "STRONG_BUY") and r["horizons"]["5"].get("state") == "matured"]
        nonbuys = [r["horizons"]["5"]["net_pct"] for r in rows
                   if r["kind"] not in ("BUY", "STRONG_BUY") and r["horizons"]["5"].get("state") == "matured"]
        return {"scored_days": len(rows), "buy_label_days": sum(r["kind"] in ("BUY", "STRONG_BUY") for r in rows),
                "h5_buy_matured_labels": len(buys), "h5_buy_mean_net_pct": _mean(buys),
                "h5_nonbuy_matured_labels": len(nonbuys), "h5_nonbuy_mean_net_pct": _mean(nonbuys),
                "h5_all_label_mean_net_pct": _mean([r["horizons"]["5"]["net_pct"] for r in rows
                                                     if r["horizons"]["5"].get("state") == "matured"])}
    return {"counts": dict(counts), "unique_scored_tickers": len(per_ticker),
            "label_day_mean_net_pct": {k: _mean(v) for k, v in groups.items()},
            "missing_outcomes": dict(missing),
            "major_tickers_frozen_at_first_decision": [
                {"ticker": t, "name": name, **summarize(per_ticker[t])} for t, name in majors.items()],
            "ticker_summary": [{"ticker": t, "name": names.get(t, t), **summarize(rows)}
                               for t, rows in sorted(per_ticker.items())],
            "rows": detail,
            "not_independent_trade_samples_or_portfolio_returns": True}


def run(data_dir: Path) -> dict:
    days, sources = load_krx_archives([data_dir / name for name in dict.fromkeys(HOLDOUT_ARCHIVES + ARCHIVES)])
    share_review = review(data_dir)
    facts = replay_facts(data_dir)
    panel = PricePanel(days)
    shares = {(day, str(row["ISU_CD"])): _number(row.get("LIST_SHRS"))
              for day, rows in days.items() for row in rows}
    pilots = {}
    for name, start, end in HOLDOUT_WINDOWS:
        pilots[name] = run_pilot(days, start=start, end=end)
    for name, (filename, digest) in PILOTS.items():
        raw = (data_dir / filename).read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("frozen pilot hash mismatch")
        pilots[name] = json.loads(raw)
    windows = {}
    for name, pilot in pilots.items():
        print(json.dumps({"window": name, "phase": "domestic_census"}), flush=True)
        selection = selection_census(pilot, days)
        candidates = first_buy_decisions(pilot, panel)
        start, end = pilot["signal_dates"][0], pilot["signal_dates"][-1]
        policies, reviewed_policies = {}, {}
        for cost in (25, 75, 120):
            policies[str(cost)] = {}
            reviewed_policies[str(cost)] = {}
            for style in (None, *strategy.STYLES):
                policy = "hold20" if style is None else style
                policies[str(cost)][policy] = replay_portfolio(
                    panel, candidates, start=start, end=end, horizon=20,
                    side_cost=cost / 20_000, share_counts=shares,
                    risk_config=strategy.risk_config(style) if style else None)
                reviewed_policies[str(cost)][policy] = replay_portfolio(
                    panel, candidates, start=start, end=end, horizon=20,
                    side_cost=cost / 20_000, share_counts=shares,
                    risk_config=strategy.risk_config(style) if style else None,
                    reviewed_share_changes=facts)
        windows[name] = {"selection": selection, "holding_policies": policies,
                         "reviewed_fact_holding_policies": reviewed_policies}
    return {"schema": "kr-selection-holding-audit-v1", "market": "kr",
            "registered_verdict": False, "strict_pit_eligible": False,
            "live_order_eligible": False, "adoption": "not_adopted_development_only",
            "sources": sources, "windows": windows,
            "reviewed_share_changes": share_review,
            "plan_sha256": hashlib.sha256(PLAN.read_bytes().split("\n## 실행 결과".encode())[0]).hexdigest(),
            "code_sha256": {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in (
                "scripts/measure/kr_selection_holding_audit.py",
                "scripts/measure/holding_horizon_portfolio_lab.py",
                "scripts/measure/research_reviewed_share_changes.py",
                "src/signal_desk/signals/engine.py", "src/signal_desk/signals/risk.py")},
            "limits": ["Previously inspected development data, not independent OOS",
                       "Price-only selection is not the production eight-factor engine",
                       "Repeated labels and horizons overlap; row means are not account returns",
                       "Close-observed risk executes next open, not intraday stop-limit fills",
                       "Share discontinuity blocks return; unchanged shares do not verify all actions/dividends",
                       "Costs are assumptions, not historical account costs"]}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", type=Path, default=ROOT / "data/research")
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    if args.output.exists():
        p.error("refusing to overwrite an earlier audit")
    result = run(args.data_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        json.dump(result, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    print(json.dumps({"output": str(args.output), "windows": len(result["windows"]),
                      "adoption": result["adoption"]}))


if __name__ == "__main__":
    main()
