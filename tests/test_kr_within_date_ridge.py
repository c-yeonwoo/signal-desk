"""Synthetic model isolation, date boundaries and execution parity; no market files."""
from copy import deepcopy
import json
from pathlib import Path

import numpy as np
import pytest

from scripts.measure import kr_within_date_ridge as lab
from scripts.measure import kr_integer_risk_budget as parent
from scripts.measure import kr_frozen_forecast_policy as frozen


def training():
    standard = {"features": ["common", "stock"], "medians": [0., 0.],
        "means": [0., 0.], "scales": [1., 1.], "clip": 8.}
    cfg = {"train_start": "2024-09-01", "train_end": "2024-09-03",
        "fit_through": "2024-09-12", "evaluation_start": "2024-10-02", "alpha_weighted_mean_objective": .1}
    rows = [{"date": d, "exit_date": "2024-09-12", "ticker": str(i),
        "features": [common, float(i)], "target": common*.02+i*.01}
        for d, common, count in (("2024-09-02", -1., 3), ("2024-09-03", 2., 5)) for i in range(count)]
    return rows, standard, cfg


def test_within_fit_removes_common_market_and_weights_dates_not_rows():
    rows, s, cfg = training(); m = lab.fit_within(rows, s, cfg)
    assert m["coefficients"][0] == pytest.approx(0, abs=1e-14)
    assert m["coefficients"][1] > 0
    assert m["common_return"] == pytest.approx((-.01+.06)/2)
    assert m["training_dates"] == 2 and m["training_rows"] == 8 and m["fit_calls"] == 1
    assert m["within_covariance_xy"][0] == pytest.approx(0)
    assert m["between_covariance_xy"][0] > 0
    x = np.array([r["features"] for r in rows]); y = np.array([r["target"] for r in rows])
    dates = [r["date"] for r in rows]; xc = lab.centered(x, dates); yc = lab.centered(y, dates)
    w = np.array([1/6]*3+[1/10]*5)
    expected = np.linalg.solve(xc.T@(w[:,None]*xc)+.1*np.eye(2), xc.T@(w*yc))
    assert m["coefficients"][:2] == pytest.approx(expected)


def test_predictions_use_only_today_cross_section_no_future_labels():
    rows, s, cfg = training(); m = lab.fit_within(rows, s, cfg)
    today = [{"date": "2024-10-02", "ticker": str(i), "features": [-2., float(i)]} for i in range(4)]
    predictions = lab.predict_day(today, s, m)
    assert np.mean(predictions) == pytest.approx(m["common_return"])
    assert predictions == sorted(predictions)
    shifted = deepcopy(today)
    for r in shifted:
        r["features"][0] = 3.  # market-wide movement changes no relative feature
        r["target"] = -999.  # targets are not a prediction input
    assert lab.predict_day(shifted, s, m) == pytest.approx(predictions)
    shuffled = list(reversed(today))
    assert lab.predict_day(shuffled, s, m) == pytest.approx(list(reversed(predictions)))
    shifted[0]["date"] = "2024-10-03"
    with pytest.raises(ValueError, match="prediction_contract"):
        lab.predict_day(shifted, s, m)


def test_fit_leakage_duplicates_nonfinite_and_prediction_preprocessing_guard():
    rows, s, cfg = training()
    for bad in ([*rows, rows[0]], [{**r, "exit_date": "2024-10-03"} for r in rows],
                [{**r, "date": "2024-10-03"} for r in rows]):
        with pytest.raises(ValueError, match="training_contract"):
            lab.fit_within(bad, s, cfg)
    bad = deepcopy(rows); bad[0]["target"] = float("nan")
    with pytest.raises(ValueError, match="invalid_centering"):
        lab.fit_within(bad, s, cfg)
    m = lab.fit_within(rows, s, cfg)
    today = [{**rows[0], "date": "2024-10-02"}]
    with pytest.raises(ValueError, match="prediction_contract"):
        lab.predict_day(today, {**s, "clip": 3.}, m)
    with pytest.raises(ValueError, match="prediction_contract"):
        lab.predict_day(rows[:1], s, m)


def test_missing_mask_is_centered_and_finite_without_dropping_rows():
    rows, s, cfg = training(); rows[0]["features"][1] = None
    m = lab.fit_within(rows, s, cfg)
    assert len(m["coefficients"]) == 4 and m["training_rows"] == 8
    today = [{**r, "date": "2024-10-02"} for r in rows[:3]]
    assert all(np.isfinite(lab.predict_day(today, s, m)))


@pytest.fixture
def market():
    root = Path(__file__).resolve().parents[1]
    spec = json.loads((root/frozen.PLAN_PATH).read_text())
    spec["portfolio"].update(min_forecast_names=2, initial_cash_krw=100000)
    spec["periods"][0]["arms"] = json.loads((root/lab.PLAN_PATH).read_text())["arms"]
    dates = frozen.calendar("2024-09-20", "2024-11-01")
    first = dates.index("2024-10-02"); decisions = dates[first:first+6]
    spec["periods"][0].update(decision_start=decisions[0], decision_end=decisions[-1], tail_through=dates[-1])
    rows = {d: {t: {"TDD_CLSPRC": 100., "TDD_OPNPRC": 100., "LIST_SHRS": 1000000.,
        "ACC_TRDVOL": 1000000., "MKTCAP": 1000-i} for i, t in enumerate("ABCDEFGH")} for d in dates}
    forecasts = {d: {t: .04-i*.004 for i, t in enumerate("ABCDEFGH")} for d in decisions}
    inputs = frozen.Inputs(forecasts, {d: ["A"] for d in decisions}, rows,
        {d: dates[i-1] for i, d in enumerate(dates) if i}, {}, {})
    return spec, inputs


def replay(spec, inputs, new, extra=True):
    def factory(d, f):
        ctx = parent.decision_context(inputs, d, f)
        ctx["within_date_forecasts"] = new[d]
        return ctx
    return frozen.replay_group(inputs, spec, "75bp_full", 0, frozen.Budget(),
        target_selector=lab.selector(.004), forecast_required_arms=("candidate", "prior_integer"),
        decision_context_factory=factory,
        additional_quote_names=(lambda d, f: sorted(new[d], key=lambda t: (-new[d][t], t))[:6]) if extra else None)


def test_identity_model_preserves_parent_and_empty_extension_is_inert(market):
    spec, inputs = market
    result = replay(spec, inputs, inputs.forecasts)
    default = replay(spec, inputs, inputs.forecasts, extra=False)
    clean = parent.parent.parent_tools.without_ids
    assert all(m["complete"] for m in result["metrics"].values())
    assert clean(result["metrics"]["candidate"]) == clean(result["metrics"]["prior_integer"])
    assert clean(result["metrics"]) == clean(default["metrics"])


def test_new_ranked_name_gets_quotes_without_changing_old_control(market):
    spec, inputs = market
    old = replay(spec, inputs, inputs.forecasts)
    new = {d: {t: .06 if t == "G" else .001 for t in f} for d, f in inputs.forecasts.items()}
    result = replay(spec, inputs, new)
    assert all(m["complete"] for m in result["metrics"].values())
    assert any(f["ticker"] == "G" and f["side"] == "buy" for f in result["metrics"]["candidate"]["fills"])
    clean = parent.parent.parent_tools.without_ids
    for a in ("prior_integer", *frozen.CONTROLS):
        assert clean(old["metrics"][a]) == clean(result["metrics"][a])
    old_id = next(r["decision"]["input_id"] for r in old["trace"] if r["decision"])
    new_id = next(r["decision"]["input_id"] for r in result["trace"] if r["decision"])
    assert old_id != new_id


def test_new_name_source_gap_stops_every_arm_and_universe_cannot_expand(market):
    spec, inputs = market
    new = {d: {t: .06 if t == "G" else .001 for t in f} for d, f in inputs.forecasts.items()}
    first = min(inputs.forecasts); inputs.rows[first]["G"]["ACC_TRDVOL"] = None
    result = replay(spec, inputs, new)
    assert all(not m["complete"] for m in result["metrics"].values())
    assert result["source_stop"]["ticker"] == "G"
    new[first]["OUTSIDE"] = 1.
    result = replay(spec, inputs, new)
    assert result["source_stop"]["field"] == "additional_quote_outside_forecast_universe"
