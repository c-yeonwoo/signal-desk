"""Frozen, offline separated-alpha mathematics; never a production signal/order.

Callers must first certify as-of price units and economic-return label sources.
This module does not turn raw closes or stable listed shares into total returns.
"""

from __future__ import annotations

from itertools import combinations
import math

import numpy as np

from signal_desk.signals.research_action_coverage import ACTION_KINDS, CoverageCertificate
from signal_desk import market_clock


def _finite(values, shape=None):
    a = np.asarray(values, dtype=float)
    if not np.isfinite(a).all() or (shape is not None and a.shape != shape):
        raise ValueError("nonfinite or incorrectly shaped research input")
    return a


def features(closes, volumes, caps):
    """Exactly 253 as-of, action-unit-consistent closes; 21 volume observations."""
    c, v, cap = _finite(closes), _finite(volumes), _finite(caps)
    if (c.ndim != 2 or c.shape[0] != 253 or c.shape[1] < 2
            or v.shape != (21, c.shape[1]) or cap.shape != (c.shape[1],)
            or (c <= 0).any() or (cap <= 0).any() or (v < 0).any()
            or (v[:-1].mean(axis=0) <= 0).any()):
        raise ValueError("incomplete price/cap/volume feature history")
    daily = c[1:] / c[:-1] - 1
    basket = daily[-60:].mean(axis=1)
    variance = np.var(basket)
    if variance <= 1e-12:
        raise ValueError("market beta denominator unavailable")
    cov = ((daily[-60:] - daily[-60:].mean(axis=0))
           * (basket - basket.mean())[:, None]).mean(axis=0)
    r20 = c[-1] / c[-21] - 1
    r60 = c[-1] / c[-61] - 1
    ma60 = c[-60:].mean(axis=0)
    sigma20 = float(np.std(basket[-20:]))
    x = np.column_stack((daily[-1], c[-1] / c[-6] - 1,
        r20 - r20.mean(), r60, c[-22] / c[0] - 1,
        daily[-20:].std(axis=0), c[-1] / c[-20:].mean(axis=0) - 1,
        c[-1] / ma60 - 1, np.log(cap), v[-1] / v[:-1].mean(axis=0), cov / variance))
    return {"stock": x, "market": np.asarray([r20.mean(), r60.mean(), sigma20,
             np.mean(c[-1] > ma60)]), "daily_returns": daily[-60:],
            "market_sigma20": sigma20}


def risk(daily_returns, market_sigma20, spec):
    r = _finite(daily_returns)
    if (r.ndim != 2 or r.shape[0] != spec["covariance_sessions"]
            or not math.isfinite(market_sigma20) or market_sigma20 < 0):
        raise ValueError("incomplete risk history")
    centered = r - r.mean(axis=0)
    sample = centered.T @ centered / len(r)
    shrink = spec["covariance_diagonal_shrinkage"]
    covariance = spec["label_horizon_sessions"] * ((1-shrink)*sample
        + shrink*np.diag(np.diag(sample)) + np.eye(r.shape[1])*spec["covariance_daily_diagonal_epsilon"])
    budget = max(spec["budget_floor"], min(1.0, spec["vol_target_annual"]
        / max(market_sigma20*math.sqrt(252), spec["vol_denominator_floor"])))
    return budget, covariance


def economic_sample(date, exit_date, tickers, stock_features, market_features,
                    outcomes, certificates, spec):
    """All U labels or no date. A raw-price number cannot certify its own basis.

    Outcome calculation and archived source verification belong to the existing
    settlement layer. This boundary accepts only covered, sourced economic marks.
    """
    gaps, evidence, returns = [], set(), []
    for ticker in tickers:
        cert, outcome = certificates.get(ticker), outcomes.get(ticker)
        if (not isinstance(cert, CoverageCertificate) or cert.market != "kr"
                or cert.security_id != f"ticker:{ticker}"
                or cert.start > date or cert.end < exit_date
                or not (ACTION_KINDS-{"unsupported"}) <= cert.required_kinds
                or cert.state != "complete_for_scope"):
            gaps.append({"ticker": ticker, "reason": "action_scope_missing_or_partial"})
            continue
        if (not isinstance(outcome, dict) or outcome.get("decision_date") != date
                or outcome.get("exit_date") != exit_date
                or outcome.get("basis") != "verified_economic_total_return"
                or not outcome.get("source_evidence_ids")
                or not isinstance(outcome.get("return"), (float, int))
                or not math.isfinite(outcome["return"])):
            gaps.append({"ticker": ticker, "reason": "economic_outcome_missing_or_unverified"})
            continue
        evidence.update(outcome["source_evidence_ids"])
        evidence.add(cert.reconciliation_sha256)
        returns.append(outcome["return"])
    if gaps:
        return {"state": "blocked_data", "date": date, "gaps": gaps, "sample": None}
    row = {"date": date, "exit_date": exit_date, "tickers": list(tickers),
        "features": _finite(stock_features, (len(tickers), len(spec["selection_features"]))).tolist(),
        "market_features": _finite(market_features, (len(spec["market_features"]),)).tolist(),
        "returns": returns, "label_basis": "verified_economic_total_return",
        "source_evidence_ids": sorted(evidence)}
    return {"state": "complete", "gaps": [], "sample": row}


def _complete_samples(samples, start, end, labels_through, minimum, spec):
    dates = [r["date"] for r in samples]
    if len(set(dates)) != len(dates) or len(dates) < minimum:
        raise ValueError("insufficient complete dates or duplicate date")
    for row in samples:
        tickers = row["tickers"]
        sessions = market_clock.next_sessions("kr", row["date"], spec["label_horizon_sessions"]+1)
        if (not start <= row["date"] <= end or not row["date"] < row["exit_date"] <= labels_through
                or not sessions or row["exit_date"] != sessions[-1]
                or len(tickers) < spec["min_feature_eligible"] or len(set(tickers)) != len(tickers)
                or row.get("label_basis") != "verified_economic_total_return"
                or not row.get("source_evidence_ids")):
            raise ValueError("incomplete economic labels or time leakage")
        _finite(row["features"], (len(tickers), len(spec["selection_features"])))
        _finite(row["market_features"], (len(spec["market_features"]),))
        _finite(row["returns"], (len(tickers),))
    return sorted(samples, key=lambda r: r["date"])


def fit(samples, spec):
    """Fit each head once, only after the caller's complete-label source gate."""
    rows = _complete_samples(samples, spec["train_start"], spec["train_end"],
        spec["train_labels_through"], spec["min_complete_train_dates"], spec)
    if not spec["train_labels_through"] < spec["calibration_start"] < spec["evaluation_start"]:
        raise ValueError("overlapping fit/calibration/evaluation")
    xs, ys, ws, market_x, market_y = [], [], [], [], []
    for row in rows:
        x, y = _finite(row["features"]), _finite(row["returns"])
        xs.append(x-x.mean(axis=0)); ys.append(y-y.mean())
        ws.extend([1/(len(rows)*len(y))]*len(y))
        market_x.append(row["market_features"]); market_y.append(float(y.mean()))
    x, y, w = np.vstack(xs), np.concatenate(ys), np.asarray(ws)
    variance = np.sum(w[:, None]*x*x, axis=0)
    scale = np.sqrt(variance); scale[variance <= 1e-12] = 1
    z = np.clip(x/scale, -spec["standardized_clip"], spec["standardized_clip"])
    beta = np.linalg.solve(z.T@(w[:, None]*z)
        + np.eye(z.shape[1])*spec["selection_ridge_penalty"], z.T@(w*y))
    mx, my = np.asarray(market_x), np.asarray(market_y)
    means, scales = mx.mean(axis=0), mx.std(axis=0)
    scales[scales <= 1e-12] = 1
    design = np.column_stack((np.ones(len(rows)), np.clip((mx-means)/scales,
        -spec["standardized_clip"], spec["standardized_clip"])))
    penalty = np.eye(design.shape[1])*spec["market_ridge_penalty"]; penalty[0, 0] = 0
    market_beta = np.linalg.solve(design.T@design/len(rows)+penalty, design.T@my/len(rows))
    return {"candidate_id": spec["candidate_id"], "selection_scale": scale.tolist(),
        "selection_coefficients": beta.tolist(), "market_means": means.tolist(),
        "market_scales": scales.tolist(), "market_coefficients": market_beta.tolist(),
        "training_dates": len(rows), "training_rows": len(x), "fits_per_head": 1,
        "first_training_date": rows[0]["date"], "last_training_date": rows[-1]["date"],
        "last_label_exit": max(row["exit_date"] for row in rows),
        "source_evidence_ids": sorted({s for row in rows for s in row["source_evidence_ids"]}),
        "live_order_eligible": False}


def predict(x, market_x, model, spec):
    x, m = _finite(x), _finite(market_x, (len(spec["market_features"]),))
    if (x.ndim != 2 or x.shape[1] != len(spec["selection_features"])
            or len(x) < spec["min_feature_eligible"] or model["candidate_id"] != spec["candidate_id"]):
        raise ValueError("prediction schema or universe differs")
    clip = spec["standardized_clip"]
    z = np.clip((x-x.mean(axis=0))/_finite(model["selection_scale"]), -clip, clip)
    alpha = z@_finite(model["selection_coefficients"]); alpha -= alpha.mean()
    market_z = np.clip((m-_finite(model["market_means"]))/model["market_scales"], -clip, clip)
    market = float(np.r_[1, market_z]@_finite(model["market_coefficients"]))
    return alpha, market


def calibrate(samples, model, spec):
    rows = _complete_samples(samples, spec["calibration_start"], spec["calibration_end"],
        spec["calibration_labels_through"], spec["min_complete_calibration_dates"], spec)
    if not model["last_label_exit"] < rows[0]["date"] or not spec["calibration_labels_through"] < spec["evaluation_start"]:
        raise ValueError("calibration leakage")
    errors = []
    for row in rows:
        alpha, market = predict(row["features"], row["market_features"], model, spec)
        top = sorted(range(len(alpha)), key=lambda i: (-alpha[i], row["tickers"][i]))[:spec["new_candidate_count"]]
        errors.append(float(np.mean((alpha+market-_finite(row["returns"]))[top])))
    return {"bias": max(0.0, float(np.mean(errors))), "dates": len(rows),
            "last_label_exit": max(r["exit_date"] for r in rows), "refit": False}


def choose(tickers, alpha, mu, previous, covariance, budget, spec):
    """Covariance order is sorted union(tickers, previous), including known exits."""
    if (len(set(tickers)) != len(tickers) or len(tickers) < spec["min_feature_eligible"]
            or set(alpha) != set(tickers) or set(mu) != set(tickers)
            or len(previous) > spec["max_positions"] or not 0 < budget <= 1):
        raise ValueError("invalid portfolio universe or budget")
    _finite([alpha[t] for t in tickers])
    _finite([mu[t] for t in tickers])
    full_names = sorted(set(tickers) | set(previous))
    full_sigma = _finite(covariance, (len(full_names), len(full_names)))
    if not np.allclose(full_sigma, full_sigma.T, atol=1e-12):
        raise ValueError("invalid covariance")
    top = sorted(tickers, key=lambda t: (-alpha[t], t))[:spec["new_candidate_count"]]
    # Only at most 12 coordinates can be nonzero. Avoid 2,511 x 200^2 work.
    names = sorted(set(top) | set(previous))
    indices = [full_names.index(t) for t in names]
    sigma = full_sigma[np.ix_(indices, indices)]
    w0 = _finite([previous.get(t, 0) for t in names])
    if (w0 < 0).any() or w0.sum() > 1+1e-12:
        raise ValueError("invalid previous portfolio")
    if np.linalg.eigvalsh(sigma).min() < -1e-12:
        raise ValueError("invalid covariance")
    pool = sorted(set(top) | (set(previous) & set(tickers)))
    index = {t: i for i, t in enumerate(names)}
    n = len(names)
    keep = np.asarray([min(previous.get(t, 0), spec["keep_name_weight_cap"])
                       if t in mu else 0 for t in names])
    if keep.sum() > budget:
        keep *= budget/keep.sum()
    alternatives, kinds = [keep], ["keep"]
    for size in range(min(spec["max_positions"], len(pool))+1):
        for subset in combinations(pool, size):
            w = np.zeros(n)
            for t in subset:
                w[index[t]] = budget/spec["max_positions"]
            alternatives.append(w); kinds.append("subset")
    weights = np.asarray(alternatives)
    expected = _finite([mu.get(t, 0) for t in names])
    turnover = np.abs(weights-w0).sum(axis=1)
    utility = (weights@expected-spec["return_buffer"]*weights.sum(axis=1)
        - spec["risk_aversion"]/2*np.einsum("ij,jk,ik->i", weights, sigma, weights, optimize=True)
        - spec["decision_one_way_cost"]*(turnover+weights.sum(axis=1)))
    best = float(utility.max()); tolerance = spec["objective_tie_tolerance"]
    tied = [i for i in range(len(weights)) if best-utility[i] <= tolerance]
    chosen = min(tied, key=lambda i: (turnover[i], kinds[i] != "keep",
        tuple(t for t, v in zip(names, weights[i]) if v > 0)))
    return {"weights": {t: float(v) for t, v in zip(names, weights[chosen]) if v > 0},
        "utility": float(utility[chosen]), "turnover": float(turnover[chosen]),
        "choice": kinds[chosen], "alternatives": len(weights), "budget": budget,
        "live_order_eligible": False}


def orders(target, holdings, closes, volumes, nav, spec):
    """Precommit quantities at close; the next open cannot enlarge these orders."""
    if not math.isfinite(nav) or nav <= 0:
        raise ValueError("invalid NAV")
    result = []
    for ticker in sorted(set(target) | set(holdings)):
        price, volume = closes[ticker], volumes[ticker]
        held, weight = holdings.get(ticker, 0), target.get(ticker, 0)
        if (not math.isfinite(price) or price <= 0 or not math.isfinite(volume) or volume < 0
                or not isinstance(held, int) or held < 0 or not math.isfinite(weight) or not 0 <= weight <= 1):
            raise ValueError("invalid as-of order inputs")
        difference = weight*nav-held*price
        limit = price*(1+spec["buy_limit_above_close"])
        if abs(difference) < 1e-6:
            continue
        side = "buy" if difference > 0 else "sell"
        quantity = (math.floor(difference/(limit*(1+spec["decision_one_way_cost"])))
                    if side == "buy" else min(held, math.ceil(-difference/price)))
        quantity = min(quantity, math.floor(volume*spec["prior_volume_participation"]))
        if quantity > 0:
            result.append({"ticker": ticker, "side": side, "shares": quantity,
                "limit": limit if side == "buy" else None, "basis_close": price})
    return result
