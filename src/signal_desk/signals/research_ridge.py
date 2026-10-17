"""One frozen research Ridge, not a production score, probability or order."""

from __future__ import annotations

from collections import Counter
from statistics import mean

import numpy as np


def transform(values, model):
    x = np.asarray(values, dtype=float)
    if x.ndim != 2 or x.shape[1] != len(model["features"]) or np.isinf(x).any():
        raise ValueError("invalid Ridge feature matrix")
    missing = np.isnan(x)
    imputed = np.where(missing, np.asarray(model["medians"]), x)
    z = np.clip((imputed - model["means"]) / model["scales"], -model["clip"], model["clip"])
    return np.column_stack((np.ones(len(x)), z, missing.astype(float)))


def fit(rows, plan):
    """Only training rows and labels fully available at the frozen fit cutoff."""
    if (not rows or any(not plan["train_start"] <= r["date"] <= plan["train_end"]
                        or not r["date"] < r["exit_date"] <= plan["fit_through"] for r in rows)
            or plan["fit_through"] >= plan["evaluation_start"]):
        raise ValueError("training/label/evaluation time leakage")
    if len({(r["date"], r["ticker"]) for r in rows}) != len(rows):
        raise ValueError("duplicate training ticker-date")
    x = np.asarray([r["features"] for r in rows], dtype=float)
    y = np.asarray([r["target"] for r in rows], dtype=float)
    if (x.shape != (len(rows), len(plan["features"])) or np.isinf(x).any()
            or not np.isfinite(y).all() or plan["alpha_weighted_mean_objective"] <= 0):
        raise ValueError("invalid Ridge training values")
    medians = [float(np.median(col[np.isfinite(col)])) if np.isfinite(col).any() else 0.0 for col in x.T]
    imputed = np.where(np.isnan(x), medians, x)
    scales = np.std(imputed, axis=0)
    scales[scales < 1e-12] = 1.0
    model = {"features": list(plan["features"]), "medians": medians,
             "means": np.mean(imputed, axis=0).tolist(), "scales": scales.tolist(),
             "clip": plan["standardized_clip"], "alpha": plan["alpha_weighted_mean_objective"]}
    design = transform(x, model)
    counts = Counter(r["date"] for r in rows)
    weights = np.asarray([1 / (len(counts) * counts[r["date"]]) for r in rows])
    penalty = np.eye(design.shape[1]) * model["alpha"]
    penalty[0, 0] = 0.0
    coefficients = np.linalg.solve(design.T @ (weights[:, None] * design) + penalty,
                                   design.T @ (weights * y))
    if not np.isfinite(coefficients).all():
        raise ValueError("nonfinite fitted coefficients")
    model.update({"coefficients": coefficients.tolist(), "training_rows": len(rows),
                  "training_dates": len(counts), "training_last_date": max(counts),
                  "last_label_exit": max(r["exit_date"] for r in rows), "fit_trials": 1,
                  "numpy_version": np.__version__})
    return model


def predict(values, model):
    return (transform(values, model) @ np.asarray(model["coefficients"])).tolist()


def grade(scenarios, plan):
    """Precommitted descriptive admission screen, not a statistical significance test."""
    required = [(scenario, arm, phase) for scenario in plan["scenarios"]
                for arm in ("ridge", "legacy", "cap", "random", "cash")
                for phase in range(plan["phase_count"])]
    gaps = [{"scenario": s, "arm": a, "phase": p, "state": scenarios[s][a][p]["state"],
             "session": scenarios[s][a][p].get("session"), "ticker": scenarios[s][a][p].get("ticker")}
            for s, a, p in required if scenarios[s][a][p]["state"] != "complete"]
    if gaps:
        return {"decision": "inconclusive_source_quality", "gaps": gaps,
                "live_order_eligible": False}
    primary = scenarios["75bp_full"]
    candidate = primary["ridge"]
    checks = {"worst_phase_positive": min(r["net_return_pct"] for r in candidate) > 0,
              "worst_drawdown_within_limit": min(r["max_drawdown_pct"] for r in candidate) >= plan["max_drawdown_floor_pct"],
              "completed_trades_each_phase": all(r["completed_trades"] >= plan["min_completed_trades_each_phase"] for r in candidate),
              "active_dates_each_phase": all(r["entry_dates"] >= plan["min_entry_dates_each_phase"] for r in candidate),
              "distinct_tickers_each_phase": all(r["distinct_tickers"] >= plan["min_distinct_tickers_each_phase"] for r in candidate),
              "ticker_pnl_concentration": all(r["top_ticker_pnl_share"] is not None
                  and r["top_ticker_pnl_share"] <= plan["max_ticker_absolute_pnl_share"] for r in candidate),
              "mean_excess_legacy_positive": mean(r["net_return_pct"] - b["net_return_pct"]
                                                   for r, b in zip(candidate, primary["legacy"])) > 0}
    months = ("2024-10", "2024-11", "2024-12")
    checks["positive_entry_months"] = sum(mean(r["pnl_by_entry_month"].get(m, 0) for r in candidate) > 0
                                          for m in months) >= plan["min_positive_entry_months"]
    for control in ("cap", "random"):
        checks[f"worst_excess_{control}_positive"] = min(r["net_return_pct"] - b["net_return_pct"]
                                                        for r, b in zip(candidate, primary[control])) > 0
        stress = scenarios["120bp_full"]
        checks[f"stress_mean_excess_{control}_positive"] = mean(r["net_return_pct"] - b["net_return_pct"]
            for r, b in zip(stress["ridge"], stress[control])) > 0
    for scenario in ("120bp_full", "75bp_half", "75bp_delay1"):
        checks[f"{scenario}_mean_positive"] = mean(r["net_return_pct"] for r in scenarios[scenario]["ridge"]) > 0
    checks["no_fill_is_cash"] = all(r["net_return_pct"] == 0 for results in scenarios["75bp_none"].values() for r in results)
    return {"decision": "research_shadow_candidate" if all(checks.values()) else "rejected",
            "checks": checks, "live_order_eligible": False, "registered_verdict": False}
