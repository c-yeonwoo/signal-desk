"""Explain the frozen 29 changed selections, with all 61 surrounding dates as controls.

No new policy, weight tuning, network calls or registered/operating state writes.
Score accounting explains selection, not the economic cause of subsequent prices.
"""

from __future__ import annotations

import argparse
from collections import Counter
import datetime as dt
import hashlib
import json
from pathlib import Path
from statistics import mean

from scripts.measure.kr_financial_input_sources import SCOPE, SCOPE_SHA, load as load_financials
from scripts.measure.kr_original_financial_sources import FINANCIAL, FINANCIAL_SHA, load as load_versions
from scripts.measure.kr_receipt_financial_lab import daily_outcome
from scripts.measure.krx_price_replay_pilot import ROOT, _number, load_krx_archives
from scripts.measure.price_challenger_lab import PricePanel
from signal_desk.signals import engine
from signal_desk.signals.research_financial_inputs import metrics_asof

BEFORE = ROOT / "data/research/kr-receipt-financial-sensitivity-2026-10-17-v2.json"
BEFORE_SHA = "8f1d95fb36de71037c33d993053b676f5197a666fa915f0773962e3b6d8432f9"
AFTER = ROOT / "data/research/kr-original-financial-sensitivity-2026-10-17-v3.json"
AFTER_SHA = "579cf23032a4c8fe12be91ab797fa85c040253fd93c402c3ad3fc34df25a0ab6"
VERSIONS = ROOT / "data/research/kr-original-financial-versions-2026-10-17-v1.zip"
VERSIONS_SHA = "eec040523329401b48859727808bc1e6d9fa5d837cb6d590f6fcab55b9576979"
WINDOWS = ("2026q2early-v5", "2026jun-expanded-v5")


def _checked(path, digest):
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("frozen attribution input differs")
    return json.loads(raw)


def pack(signal):
    contributions = dict(signal.score_contributions)
    if abs(sum(contributions.values()) - signal.score) > .00501:
        raise ValueError("effective contributions do not account for rounded score")
    return {"score": signal.score, "kind": signal.kind, "rank": signal.rank,
            "coverage": signal.data_coverage, "weight_sum_ratio": signal.weight_sum_ratio,
            "gates": list(signal.gates), "relaxed_gates": list(signal.gates_relaxed),
            "gate_blocked": signal.gate_blocked, "low_coverage": signal.low_coverage,
            "raw_factors": dict(signal.factor_scores), "contributions": contributions,
            "suppressed": dict(signal.factor_suppressed), "valuation_percentile": signal.valuation_percentile,
            "quality_points": signal.quality_points, "quality_evaluable": signal.quality_evaluable,
            "momentum_ret": signal.momentum_ret, "reasons": list(signal.reasons)}


def decision_change(before, after, metrics_before, metrics_after):
    """Classify only contemporaneous input/decision differences, without outcomes."""
    old_buy, new_buy = engine.is_buy(before["kind"]), engine.is_buy(after["kind"])
    role = ("retained_buy" if old_buy and new_buy else "added_buy" if new_buy
            else "removed_buy" if old_buy else "not_buy")
    own = metrics_before != metrics_after
    peer = not own and before["valuation_percentile"] != after["valuation_percentile"]
    changed = any(before[k] != after[k] for k in
                  ("score", "contributions", "gates", "relaxed_gates", "gate_blocked", "low_coverage", "coverage"))
    mechanism = ("own_financial_inputs" if own else "peer_valuation" if peer
                 else "score_or_gate_change" if changed else "rank_only_displacement"
                 if role in ("added_buy", "removed_buy") and before["rank"] != after["rank"]
                 else "no_input_or_score_change")
    keys = set(before["contributions"]) | set(after["contributions"])
    delta = {k: after["contributions"].get(k, 0) - before["contributions"].get(k, 0) for k in sorted(keys)}
    positive = {k: v for k, v in after["contributions"].items() if v > 0}
    leaders = sorted(k for k, v in positive.items() if abs(v - max(positive.values())) <= 1e-12) if positive else []
    return {"role": role, "mechanism": mechanism,
            "changed_metric_keys": sorted(k for k in set(metrics_before) | set(metrics_after)
                                          if metrics_before.get(k) != metrics_after.get(k)),
            "contribution_delta": delta,
            "largest_positive_factors_after": leaders}


def summarize(rows):
    """Date-equal descriptive labels; missing selected prices never disappear."""
    by_day = {}
    for row in rows:
        by_day.setdefault(row["date"], []).append(row)
    missing = Counter(r["h5"]["state"] for r in rows if r["h5"]["state"] != "matured")
    complete = bool(rows) and not missing
    daily = [mean(r["h5"]["net_pct"] for r in group) for group in by_day.values()] if complete else []
    baseline = [group[0]["same_day_cohort_h5_net_pct"] for group in by_day.values()]
    return {"rows": len(rows), "tickers": len({r["ticker"] for r in rows}), "dates": len(by_day),
            "negative_rows": sum(r["h5"]["state"] == "matured" and r["h5"]["net_pct"] < 0 for r in rows),
            "missing_outcomes": dict(missing), "date_equal_h5_net_pct": mean(daily) if daily else None,
            "same_dates_cohort_h5_net_pct": mean(baseline) if baseline and all(v is not None for v in baseline) else None,
            "independent_trade_samples": False}


def run():
    frozen, old, new = _checked(SCOPE, SCOPE_SHA), _checked(BEFORE, BEFORE_SHA), _checked(AFTER, AFTER_SHA)
    if hashlib.sha256(FINANCIAL.read_bytes()).hexdigest() != FINANCIAL_SHA:
        raise ValueError("financial archive differs")
    if hashlib.sha256(VERSIONS.read_bytes()).hexdigest() != VERSIONS_SHA:
        raise ValueError("original version archive differs")
    # Explain the exact already-run engine, not whatever a later edit happens to do.
    for path, digest in new["code_sha256"].items():
        if hashlib.sha256((ROOT / path).read_bytes()).hexdigest() != digest:
            raise ValueError("attribution replay code differs from frozen comparison")
    financials, versions = load_financials(FINANCIAL), load_versions(VERSIONS)
    days, sources = load_krx_archives([ROOT / "data/research" / s["archive"] for s in frozen["sources"]])
    if sources != frozen["sources"]:
        raise ValueError("price archives differ")
    panel = PricePanel(days)
    detailed, dates = [], []
    for window in WINDOWS:
        by_day = {}
        for r in frozen["windows"][window]["selection"]["rows"]:
            by_day.setdefault(r["date"], []).append(r)
        old_days = {r["date"]: r for r in old["windows"][window]["daily"]}
        new_days = {r["date"]: r for r in new["windows"][window]["daily"]}
        if set(by_day) != set(old_days) or set(by_day) != set(new_days):
            raise ValueError("comparison date scope differs")
        for day, observations in sorted(by_day.items()):
            if not "2026-04-01" <= day <= "2026-07-31" or day >= "2026-08-05":
                raise ValueError("attribution outside frozen development scope")
            names = {r["ticker"] for r in observations}
            candidates = sorted((r for r in days[day] if len(str(r["ISU_CD"])) == 6
                                 and str(r["ISU_CD"]).endswith("0") and (_number(r["MKTCAP"]) or 0) > 0),
                                key=lambda r: (-_number(r["MKTCAP"]), r["ISU_CD"]))[:200]
            if len(candidates) != 200:
                raise ValueError("candidate denominator differs")
            universe = [{"ticker": r["ISU_CD"], "name": str(r.get("ISU_NM") or r["ISU_CD"])} for r in candidates]
            i = panel.index[day]
            prices = {t: [panel.bars.get((d, t), (None, None))[1] for d in panel.sessions[i - 252:i + 1]] for t in names}
            if any(len(v) != 253 or any(p is None or p <= 0 for p in v) for v in prices.values()):
                raise ValueError("continuous 253 closes lost")
            caps = {r["ISU_CD"]: _number(r["MKTCAP"]) for r in days[day] if r["ISU_CD"] in names}
            metrics0, _ = metrics_asof(financials["records"], day, caps)
            metrics1, _ = metrics_asof(financials["records"], day, caps, versions=versions)
            cfg = engine.SignalConfig()
            kwargs = dict(today=dt.date.fromisoformat(day), config=cfg, flows={}, shorts={}, sentiment={},
                          earnings_dates={}, unavailable=("flow", "short"))
            signals0 = engine.evaluate(universe, prices, fundamentals=metrics0, **kwargs)
            signals1 = engine.evaluate(universe, prices, fundamentals=metrics1, **kwargs)
            if (daily_outcome(signals0, observations) != old_days[day]["annual_financial"]
                    or daily_outcome(signals1, observations) != new_days[day]["annual_financial"]):
                raise ValueError("recomputed decisions differ from frozen result")
            old_signals, new_signals = {s.ticker: s for s in signals0}, {s.ticker: s for s in signals1}
            changed = set(old_days[day]["annual_financial"]["buy_tickers"]) != set(new_days[day]["annual_financial"]["buy_tickers"])
            dates.append({"date": day, "window": window, "changed_buy_set": changed,
                          "before": old_days[day]["annual_financial"], "after": new_days[day]["annual_financial"]})
            for obs in observations:
                ticker = obs["ticker"]
                a, b = pack(old_signals[ticker]), pack(new_signals[ticker])
                detailed.append({"date": day, "window": window, "ticker": ticker, "name": obs["name"],
                                 "changed_buy_date": changed, "before": a, "after": b,
                                 "inputs_before": metrics0.get(ticker, {}), "inputs_after": metrics1.get(ticker, {}),
                                 "change": decision_change(a, b, metrics0.get(ticker, {}), metrics1.get(ticker, {})),
                                 "recent20_pct": obs["recent20_pct"], "downtrend_confirmed": obs["downtrend_confirmed"],
                                 "h5": obs["horizons"]["5"],
                                 "same_day_cohort_h5_net_pct": obs["same_day_h5_cohort_mean_net_pct"]})
    if len(dates) != 61 or sum(d["changed_buy_set"] for d in dates) != 29:
        raise ValueError("frozen 61/29-date scope differs")
    groups = {}
    for scope, subset in (("all_61_dates", detailed), ("changed_29_dates", [r for r in detailed if r["changed_buy_date"]]),
                          ("unchanged_32_dates", [r for r in detailed if not r["changed_buy_date"]])):
        groups[scope] = {role: summarize([r for r in subset if r["change"]["role"] == role])
                         for role in ("added_buy", "removed_buy", "retained_buy", "not_buy")}
    bought = [r for r in detailed if engine.is_buy(r["after"]["kind"])]
    leader_groups = sorted({"+".join(r["change"]["largest_positive_factors_after"]) for r in bought})
    return {"schema": "kr-financial-selection-attribution-v1", "development_only": True,
            "candidate_slots": 200 * len(dates), "scored_ticker_days": len(detailed),
            "unscored_slots": 200 * len(dates) - len(detailed),
            "registered_verdict": False, "live_order_eligible": False, "strict_pit_eligible": False,
            "adoption": "diagnostic_only_no_new_policy", "groups": groups,
            "buy_transition_mechanisms": dict(Counter(r["change"]["mechanism"] for r in detailed
                                                       if r["change"]["role"] in ("added_buy", "removed_buy"))),
            "after_buy_largest_positive_factors": {
                key: summarize([r for r in bought if "+".join(r["change"]["largest_positive_factors_after"]) == key])
                for key in leader_groups},
            "after_buy_diagnostics": {
                "buy_label_rows": len(bought), "unique_tickers": len({r["ticker"] for r in bought}),
                "labels_by_ticker": dict(Counter(r["ticker"] for r in bought)),
                "rounded_score_at_ceiling_rows": sum(r["after"]["score"] == 3 for r in bought),
                "momentum_at_ceiling_rows": sum(r["after"]["raw_factors"].get("momentum") == 1 for r in bought),
                "fundamental_at_ceiling_rows": sum(r["after"]["raw_factors"].get("fundamental") == 1 for r in bought),
                "recent20_negative_rows": sum(r["recent20_pct"] is not None and r["recent20_pct"] < 0 for r in bought),
                "negative_technical_with_zero_contribution_rows": sum(
                    r["after"]["raw_factors"].get("technical", 0) < 0 and r["after"]["contributions"].get("technical", 0) == 0
                    for r in bought),
                "downtrend_confirmed_rows": sum(r["downtrend_confirmed"] for r in bought),
                "relaxed_gate_rows": sum(bool(r["after"]["relaxed_gates"]) for r in bought)},
            "dates": dates, "rows": detailed,
            "inputs": {str(p.relative_to(ROOT)): s for p, s in ((SCOPE, SCOPE_SHA), (BEFORE, BEFORE_SHA),
                       (AFTER, AFTER_SHA), (FINANCIAL, FINANCIAL_SHA), (VERSIONS, VERSIONS_SHA))},
            "replayed_code_sha256": new["code_sha256"], "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "limits": ["No new strategy, thresholds or operating changes; descriptive development attribution only",
                       "29 changed dates selected by input intervention, not returns; all 61 dates also retained",
                       "Effective contribution differences include active-weight renormalization, not isolated factor PnL",
                       "Largest positive factors retain ties; accounting is not proof that a factor caused price loss",
                       "Repeated ticker-days and overlapping h5 labels are not independent trades",
                       "Same frozen next-open h5 labels with assumed 25bp cost; no portfolio or intraday timing claim",
                       "Flow/short/qualitative, historical sector mapping and corporate-action certification remain absent"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("refusing to overwrite attribution result")
    result = run()
    with args.output.open("x") as stream:
        json.dump(result, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    print(json.dumps({k: result[k] for k in ("groups", "buy_transition_mechanisms", "after_buy_diagnostics")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
