"""One frozen, bounded-overflow momentum rank study on preserved inputs only."""

from __future__ import annotations

import argparse
from copy import deepcopy
from functools import partial
import hashlib
import json
from pathlib import Path

from scripts.measure import kr_score_tie_ablation as replay
from scripts.measure.kr_relative_downtrend_ablation import finite
from scripts.measure.krx_price_replay_pilot import ROOT
from signal_desk.signals import engine

PLAN = ROOT / "docs/kr-bounded-momentum-plan-2026-10-20.json"
PLAN_SHA = "e1524872bd8b11df07faec6060b93cbc778b3d09cd5c1e2deea35df376ccb355"
PLAN_COMMIT = "89e4c1e"  # Provenance; squash merges need not retain the object.


def rank_values(signals, cfg):
    """Restore a bounded fraction of overflow; never change the base score.

    For rounded raw normalization x, e = max(abs(x)-1, 0). The additional
    contribution is sign(x)*e/(1+e) times the actual fired coefficient.
    Computed tail values can coincide at floating-point precision, but the
    absolute increment never exceeds one existing momentum contribution.
    """
    scale = cfg.momentum_scale
    if not finite(scale) or scale <= 0:
        raise ValueError("invalid frozen momentum scale")
    values, saturated = {}, []
    for result in signals:
        if result.ticker in values or not finite(result.score):
            raise ValueError("invalid ranking inputs")
        value = result.score
        if result.has_momentum:
            if not finite(result.momentum_ret):
                raise ValueError("unknown raw momentum")
            raw = result.momentum_ret / scale
            if not finite(raw):
                raise ValueError("invalid raw normalization")
            if abs(raw) > 1:
                capped = result.factor_scores.get("momentum")
                contribution = result.score_contributions.get("momentum")
                expected = 1.0 if raw > 0 else -1.0
                if capped != expected or not finite(contribution):
                    raise ValueError("clipped factor/contribution mismatch")
                coefficient = contribution / capped
                if coefficient < 0:
                    raise ValueError("unexpected negative momentum coefficient")
                raw = round(raw, 3)
                overflow = max(abs(raw) - 1, 0)
                increment = capped * (overflow / (1 + overflow))
                value += coefficient * increment
                saturated.append({"ticker": result.ticker, "momentum_ret": result.momentum_ret,
                                  "base_score": result.score, "raw_norm": raw, "capped_norm": capped,
                                  "coefficient": coefficient, "bounded_increment": increment,
                                  "research_rank_value": value})
        if not finite(value):
            raise ValueError("nonfinite ranking value")
        values[result.ticker] = value
    return values, saturated


def bounded_rank(signals, _recent20, cfg, *, audit=None):
    values, saturated = rank_values(signals, cfg)
    ranked = deepcopy(signals)
    ranked.sort(key=lambda r: values[r.ticker], reverse=True)
    for result in ranked:
        result.research_rank_value = values[result.ticker]
    if audit is not None:
        audit.append({"saturated": saturated,
                      "rank_value_changed_names": sum(values[r.ticker] != r.score for r in signals)})
    return engine.apply_cross_sectional(ranked, cfg)


def run():
    audit = []
    report = replay.run(plan_path=PLAN, plan_sha=PLAN_SHA, plan_commit=PLAN_COMMIT,
                        ranker=partial(bounded_rank, audit=audit), schema="kr-bounded-momentum-ablation-v1")
    dates = [(window, d["date"]) for window, rows in report["windows"].items()
             for d in rows["costs"]["25"]["daily"]]
    if len(audit) != len(dates):
        raise ValueError("incomplete as-of bounded momentum audit")
    report["saturation_audit"] = [{"window": window, "date": day, **row}
                                  for (window, day), row in zip(dates, audit)]
    report["saturated_ticker_days"] = sum(len(row["saturated"]) for row in audit)
    report["rank_value_changed_ticker_days"] = sum(row["rank_value_changed_names"] for row in audit)
    report["replay_code_sha256"] = report.pop("code_sha256")
    report["code_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return report


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
    print(json.dumps({k: report[k] for k in ("decision", "saturated_ticker_days",
                                            "rank_value_changed_ticker_days", "failed_checks", "summary")},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
