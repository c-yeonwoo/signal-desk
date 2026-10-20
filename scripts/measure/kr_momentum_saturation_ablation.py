"""Restore only clipped momentum information for a frozen offline rank study."""

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

PLAN = ROOT / "docs/kr-momentum-saturation-plan-2026-10-20.json"
PLAN_SHA = "acbcfe100363f67652e4f21c9140fc4ab0905a02edb2ebecac1e2366fa619b20"
PLAN_COMMIT = "1d6bb54"


def rank_values(signals, cfg):
    """Use only the as-of raw return and the engine's actual contribution ratio.

    Keep under-cap values byte-for-byte unchanged. The original score remains
    the eligibility floor; this auxiliary value is never a displayed score.
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
                value += (raw - capped) * coefficient
                saturated.append({"ticker": result.ticker, "momentum_ret": result.momentum_ret,
                                  "base_score": result.score, "raw_norm": raw, "capped_norm": capped,
                                  "coefficient": coefficient, "research_rank_value": value})
        if not finite(value):
            raise ValueError("nonfinite ranking value")
        values[result.ticker] = value
    return values, saturated


def uncapped_rank(signals, _recent20, cfg, *, audit=None):
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
                        ranker=partial(uncapped_rank, audit=audit), schema="kr-momentum-saturation-ablation-v1")
    dates = [(window, d["date"]) for window, rows in report["windows"].items()
             for d in rows["costs"]["25"]["daily"]]
    if len(audit) != len(dates):
        raise ValueError("incomplete as-of saturation audit")
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
