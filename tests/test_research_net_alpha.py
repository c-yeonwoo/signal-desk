"""Necessary numeric and source/time boundaries for the frozen research candidate."""

import copy
import json
from pathlib import Path

import numpy as np
import pytest

from signal_desk.signals import research_net_alpha as lab
from scripts.measure.kr_net_alpha_sources import validate_rows, PLAN_SHA, PROSE_SHA, digest


def spec():
    return {**json.loads(Path("docs/kr-net-alpha-plan-2026-10-17.json").read_text()),
            "min_feature_eligible": 2, "min_complete_train_dates": 2,
            "min_complete_calibration_dates": 1}


def samples():
    x = [[-1.]*11, [1.]*11]
    return [{"date": d, "exit_date": e, "tickers": ["A", "B"],
             "features": x, "market_features": [i, 1, .01, .5], "returns": [-.01+i*.001, .03+i*.001],
             "label_basis": "verified_economic_total_return", "source_evidence_ids": ["synthetic-test-only"]}
            for i, (d, e) in enumerate((("2021-01-04", "2021-01-12"), ("2021-02-01", "2021-02-09")))]


def test_contract_pin_and_official_classification_do_not_use_code_suffix():
    p = json.loads(Path("docs/kr-net-alpha-plan-2026-10-17.json").read_text())
    assert digest(Path("docs/kr-net-alpha-plan-2026-10-17.json").read_bytes()) == PLAN_SHA
    assert digest(Path(p["contract"]).read_bytes()) == PROSE_SHA
    assert p["expected_paths"] == len(p["arms"])*len(p["scenarios"])*p["phase_count"] == 210
    rows = [{"ISU_SRT_CD": f"{i:06d}", "ISU_CD": f"ISIN{i}",
             "KIND_STKCERT_TP_NM": "우선주" if i == 10 else "보통주"} for i in range(200)]
    result = validate_rows("stk_isu_base_info", "2021-01-04", json.dumps({"OutBlock_1": rows}).encode())
    assert result[10]["KIND_STKCERT_TP_NM"] == "우선주"
    rows[10].pop("KIND_STKCERT_TP_NM")
    with pytest.raises(ValueError, match="class"):
        validate_rows("stk_isu_base_info", "2021-01-04", json.dumps({"OutBlock_1": rows}).encode())


def test_market_is_one_row_per_date_and_selection_removes_common_move():
    p = spec(); rows = samples(); model = lab.fit(rows, p)
    alpha, market = lab.predict(rows[0]["features"], rows[0]["market_features"], model, p)
    assert model["training_dates"] == 2 and model["training_rows"] == 4
    assert alpha.sum() == pytest.approx(0) and alpha[1] > alpha[0]
    shifted = copy.deepcopy(rows)
    for row in shifted:
        row["returns"] = [v+.20 for v in row["returns"]]
    moved = lab.fit(shifted, p)
    assert moved["selection_coefficients"] == pytest.approx(model["selection_coefficients"])
    assert lab.predict(rows[0]["features"], rows[0]["market_features"], moved, p)[1]-market == pytest.approx(.20)
    saved = copy.deepcopy(model)
    lab.predict(np.full((2, 11), 1e8), np.full(4, 1e8), model, p)
    assert model == saved


@pytest.mark.parametrize("change", ["future_label", "future_date", "duplicate", "raw_label", "missing_company"])
def test_source_gate_does_not_train_on_incomplete_or_future_labels(change):
    rows = samples()
    if change == "future_label": rows[0]["exit_date"] = "2022-01-04"
    if change == "future_date": rows[0]["date"] = "2022-01-04"
    if change == "duplicate": rows[1] = copy.deepcopy(rows[0])
    if change == "raw_label": rows[0]["label_basis"] = "raw_price_only"
    if change == "missing_company": rows[0]["returns"] = [0.01]
    with pytest.raises(ValueError):
        lab.fit(rows, spec())


def test_calibration_fixes_only_bias_without_refitting():
    p = spec(); rows = samples(); model = lab.fit(rows, p); saved = copy.deepcopy(model)
    row = {**rows[0], "date": "2022-01-04", "exit_date": "2022-01-12", "returns": [-.10, -.10]}
    correction = lab.calibrate([row], model, p)
    assert correction["bias"] > .09 and correction["refit"] is False and model == saved


def test_features_need_complete_asof_units_and_market_risk_is_separate():
    days = np.arange(253)
    closes = np.exp(np.column_stack((.001*days+.01*np.sin(days), .0005*days+.005*np.cos(days))))*100
    f = lab.features(closes, np.ones((21, 2))*1000, [1e9, 2e9])
    assert f["stock"].shape == (2, 11) and f["market"].shape == (4,)
    budget, covariance = lab.risk(f["daily_returns"], .04, spec())
    assert .1 <= budget < 1 and np.linalg.eigvalsh(covariance).min() > 0
    closes[0, 0] = float("nan")
    with pytest.raises(ValueError): lab.features(closes, np.ones((21, 2)), [1e9, 2e9])


def test_unchanged_holding_avoids_costs_and_new_trade_must_clear_cost():
    p = spec(); tickers = ["A", "B"]
    sigma = np.eye(2)*1e-8
    choice = lab.choose(tickers, {"A": .01, "B": 0}, {"A": .01, "B": 0}, {"A": 1/6}, sigma, 1, p)
    assert choice["choice"] == "keep" and choice["turnover"] == 0
    cash = lab.choose(tickers, {"A": .01, "B": 0}, {"A": .01, "B": 0}, {}, sigma, 1, p)
    assert cash["weights"] == {}
    buy = lab.choose(tickers, {"A": .04, "B": 0}, {"A": .04, "B": 0}, {}, sigma, 1, p)
    assert buy["weights"] == {"A": pytest.approx(1/6)}
    orders = lab.orders(choice["weights"], {"A": 10}, {"A": 100}, {"A": 10000}, 6000, p)
    assert orders == []


def test_known_universe_exit_is_sold_without_becoming_free_turnover():
    p = spec()
    choice = lab.choose(["A", "B"], {"A": 0, "B": 0}, {"A": 0, "B": 0}, {"Z": .1}, np.eye(3)*1e-8, 1, p)
    assert choice["weights"] == {} and choice["turnover"] == pytest.approx(.1)
    assert choice["utility"] == pytest.approx(-.0006)


def test_partial_sources_do_not_drop_one_company_to_train_the_rest():
    row = samples()[0]
    result = lab.economic_sample(row["date"], row["exit_date"], row["tickers"],
        row["features"], row["market_features"], {"A": {"return": .5}}, {}, spec())
    assert result["state"] == "blocked_data" and result["sample"] is None
    assert {r["ticker"] for r in result["gaps"]} == {"A", "B"}


def test_full_universe_optimization_keeps_small_exact_search():
    p = spec(); tickers = [f"{i:06d}" for i in range(200)]
    alpha = {t: .03-i*.0001 for i, t in enumerate(tickers)}
    previous = {t: .1 for t in tickers[-6:]}
    result = lab.choose(tickers, alpha, alpha, previous, np.eye(200)*1e-6, 1, p)
    assert result["alternatives"] == 2511
    assert len(result["weights"]) <= 6 and sum(result["weights"].values()) <= 1+1e-12


def test_request_is_counted_before_failure_and_secrets_are_not_logged(tmp_path, monkeypatch):
    from scripts.measure import kr_net_alpha_sources as src
    from types import SimpleNamespace
    c = src.Collector.__new__(src.Collector)
    c.destination = tmp_path; c.journal_path = tmp_path/"requests.json"
    c.journal = []; c.last_request = 0
    c.spec = {**spec(), "max_price_metadata_requests": 1}
    monkeypatch.setattr(c, "storage_bytes", lambda: 0)
    monkeypatch.setenv("KRX_API_KEY", "test-secret-no-network")

    def fail(request, timeout):
        saved = json.loads(c.journal_path.read_text())
        assert len(saved) == 1 and saved[0]["state"] == "reserved"
        raise ValueError("https://example.invalid/?secret=test-secret-no-network")

    monkeypatch.setattr(src.urllib.request, "build_opener", lambda *a: SimpleNamespace(open=fail))
    with pytest.raises(ValueError) as error:
        c.get("stk_bydd_trd", "2021-01-04")
    assert "test-secret" not in str(error.value) and "test-secret" not in c.journal_path.read_text()
    assert json.loads(c.journal_path.read_text())[0]["state"] == "failed"
    with pytest.raises(ValueError, match="budget"):
        c.get("stk_bydd_trd", "2021-01-04")
