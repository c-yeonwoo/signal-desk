import copy
import datetime as dt
import gzip
import hashlib
import json

import pytest

from signal_desk import db
from signal_desk.signals import decision_price_sensitivity as audit, decision_snapshot, engine, execution_gate


def bundle(tmp_path, monkeypatch, *, extra_price=False, close_only=False):
    monkeypatch.chdir(tmp_path)
    days = [(dt.date(2025, 1, 1) + dt.timedelta(days=i)).isoformat() for i in range(260)]
    prices = {"AAA": [100. + i * .1 for i in range(260)] + [130.],
              "BBB": [100. + i * .08 for i in range(260)] + [125.]}
    if extra_price:
        prices["OLD"] = list(prices["AAA"])
    if close_only:
        prices = {t: p[:-1] for t, p in prices.items()}
    dates = {t: days for t in prices}
    today = dt.date(2026, 10, 26)
    stamp = dt.datetime(2026, 10, 26, 14, 40, 30, tzinfo=dt.timezone.utc).timestamp()
    inputs = {"universe": [{"ticker": t, "name": t} for t in ("AAA", "BBB")], "fundamentals": {},
              "config": engine.SignalConfig(), "today": today}
    gate = {"today": today.isoformat(), "hist_by": {}, "events_by": {}}
    results = engine.evaluate(inputs["universe"], prices, fundamentals={}, config=inputs["config"], today=today)
    execution_gate.apply(results, hist_by={}, events_by={}, closes_by=prices, dates_by=dates, today=today.isoformat())
    refs = decision_snapshot.persist_signal_decision("us", prices=prices, dates=dates,
        engine_inputs=inputs, gate_inputs=gate, results=results,
        quote_snapshot={"captured_at": stamp, "quotes": {} if close_only else {t: p[-1] for t, p in prices.items()},
                        "quote_updated": {t: stamp for t in prices},
                        "quote_meta": {t: {"observation_id": t} for t in prices}})
    return audit.export_bundle("us", refs["signal_output_id"])


def external(b, prices=None):
    bars = {t: [{"t": "2026-10-26T14:39:00Z", "c": p}] for t, p in (prices or {"AAA": 130., "BBB": 125.}).items()}
    raw = json.dumps({"bars": bars, "next_page_token": None})
    return {"request": audit.reference_request(b), "source_url": "https://data.alpaca.markets/v2/stocks/bars",
            "status": "complete", "bars": bars, "pages": [{"raw": raw, "sha256": hashlib.sha256(raw.encode()).hexdigest()}]}


def test_exact_baseline_zero_price_change_no_write(tmp_path, monkeypatch):
    b = bundle(tmp_path, monkeypatch)
    original = copy.deepcopy(b)
    before = db.decision_artifact_storage()
    r = audit.compare(b, external(b))
    assert r["baseline_exact_match"] and r["status"] == "reference_sensitivity"
    assert r["changes"] == [] and r["replaced_quotes"] == 2
    assert r["profitability_verdict"] == "not_tested"
    assert b == original and db.decision_artifact_storage() == before


def test_price_only_replacement_does_not_change_other_inputs(tmp_path, monkeypatch):
    b = bundle(tmp_path, monkeypatch)
    original = copy.deepcopy(b)
    calls = []
    real = decision_snapshot.evaluate_preserved_inputs
    def observed(*args):
        calls.append(copy.deepcopy(args))
        return real(*args)
    monkeypatch.setattr(decision_snapshot, "evaluate_preserved_inputs", observed)
    r = audit.compare(b, external(b, {"AAA": 60., "BBB": 125.}))
    assert r["baseline_exact_match"] and r["universe_size"] == 2
    assert calls[0][2]["AAA"][-1] == 130. and calls[1][2]["AAA"][-1] == 60.
    assert calls[0][:2] == calls[1][:2] and calls[0][3] == calls[1][3]
    assert calls[0][2]["AAA"][:-1] == calls[1][2]["AAA"][:-1]
    assert b == original


def test_missing_reference_keeps_full_universe_and_does_not_fill(tmp_path, monkeypatch):
    b = bundle(tmp_path, monkeypatch)
    r = audit.compare(b, external(b, {"AAA": 130.}))
    assert r["status"] == "partial_reference_sensitivity" and r["missing_symbols"] == ["BBB"]
    assert r["universe_size"] == 2 and r["replaced_quotes"] == 1


def test_non_universe_price_history_is_preserved_not_requested(tmp_path, monkeypatch):
    b = bundle(tmp_path, monkeypatch, extra_price=True)
    r = audit.compare(b, external(b))
    assert audit.reference_request(b)["symbols"] == "AAA,BBB"
    assert r["universe_size"] == 2 and r["captured_quotes"] == 2
    assert r["non_universe_quotes_unchanged"] == ["OLD"]
    assert r["changes"] == [] and r["baseline_exact_match"]


def test_source_and_artifact_tamper_fail_closed(tmp_path, monkeypatch):
    b = bundle(tmp_path, monkeypatch)
    e = external(b)
    e["bars"]["AAA"][0]["c"] = 999.
    with pytest.raises(ValueError, match="preserved pages"):
        audit.compare(b, e)
    b["artifacts"]["engine_input"]["payload"]["data"]["today"] = "2026-10-27"
    with pytest.raises(ValueError, match="hash mismatch"):
        audit.validate_bundle(b)


def test_current_minute_is_not_used(tmp_path, monkeypatch):
    b = bundle(tmp_path, monkeypatch)
    e = external(b)
    for bars in e["bars"].values():
        bars[0]["t"] = "2026-10-26T14:40:00Z"
    raw = json.dumps({"bars": e["bars"], "next_page_token": None})
    e["pages"] = [{"raw": raw, "sha256": hashlib.sha256(raw.encode()).hexdigest()}]
    r = audit.compare(b, e)
    assert r["status"] == "blocked_no_aligned_reference"


def test_close_only_capture_is_not_an_intraday_receipt(tmp_path, monkeypatch):
    b = bundle(tmp_path, monkeypatch, close_only=True)
    with pytest.raises(ValueError, match="no quotes"):
        audit.reference_request(b)
    request = audit.reference_request(b, basis="recorded-close")
    assert request["timeframe"] == "1Day" and request["symbols"] == "AAA,BBB"
    targets = audit._close_targets(audit.validate_bundle(b))
    assert request["start"][:10] == targets["AAA"]["date"]


def test_recorded_close_comparison_preserves_dates_and_other_inputs(tmp_path, monkeypatch):
    b = bundle(tmp_path, monkeypatch, close_only=True, extra_price=True)
    original = copy.deepcopy(b)
    request = audit.reference_request(b, basis="recorded-close")
    data = audit.validate_bundle(b)
    prices, dates = audit._restore(data)
    bars = {t: [{"t": request["start"], "c": prices[t][-1]}] for t in ("AAA", "BBB")}
    raw = json.dumps({"bars": bars, "next_page_token": None})
    e = {"request": request, "source_url": "https://data.alpaca.markets/v2/stocks/bars",
         "status": "complete", "bars": bars,
         "pages": [{"raw": raw, "sha256": hashlib.sha256(raw.encode()).hexdigest()}]}
    calls = []
    real = decision_snapshot.evaluate_preserved_inputs
    def observed(*args):
        calls.append(copy.deepcopy(args))
        return real(*args)
    monkeypatch.setattr(decision_snapshot, "evaluate_preserved_inputs", observed)
    r = audit.compare(b, e, basis="recorded-close")
    assert r["baseline_exact_match"] and r["changes"] == []
    assert r["basis"] == "recorded-close" and r["captured_quotes"] == r["replaced_quotes"] == 0
    assert r["replaced_prices"] == 2 and r["comparison_targets"] == 2
    assert all(p["received_at"] is None for p in r["price_pairs"])
    assert calls[0] == calls[1] and b == original
    assert calls[1][2]["OLD"] == prices["OLD"] and calls[1][3] == dates
    bars["AAA"][0]["c"] = 60.
    bars.pop("BBB")
    raw = json.dumps({"bars": bars, "next_page_token": None})
    e["pages"] = [{"raw": raw, "sha256": hashlib.sha256(raw.encode()).hexdigest()}]
    r = audit.compare(b, e, basis="recorded-close")
    assert r["status"] == "partial_reference_sensitivity" and r["missing_symbols"] == ["BBB"]
    assert calls[-1][2]["AAA"][-1] == 60. and calls[-1][2]["AAA"][:-1] == prices["AAA"][:-1]
    assert calls[-1][2]["BBB"] == prices["BBB"] and calls[-1][3] == dates
    assert calls[-1][:2] == calls[0][:2]


def test_daily_mode_cannot_silently_replace_intraday_inputs(tmp_path, monkeypatch):
    b = bundle(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="close-only"):
        audit.reference_request(b, basis="recorded-close")
    with pytest.raises(ValueError, match="unsupported comparison basis"):
        audit.reference_request(b, basis="automatic")


def test_baseline_mismatch_never_compares_candidate(tmp_path, monkeypatch):
    b = bundle(tmp_path, monkeypatch)
    monkeypatch.setattr(decision_snapshot, "evaluate_preserved_inputs", lambda *a: [])
    r = audit.compare(b, external(b))
    assert r["status"] == "blocked_baseline_mismatch" and "changes" not in r


def test_admin_export_is_bounded_authorized_and_no_account_data(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from signal_desk import api
    b = bundle(tmp_path, monkeypatch)
    client = TestClient(api.app)
    params = {"market": "us", "signal_output_id": b["signal_output_id"]}
    url = "/api/admin/research/decision-price-bundle"
    assert client.get(url, params=params).status_code == 401
    monkeypatch.setattr(api, "_uid", lambda request: 1)
    monkeypatch.setattr(api, "_require_admin", lambda request: False)
    assert client.get(url, params=params).status_code == 403
    monkeypatch.setattr(api, "_require_admin", lambda request: True)
    r = client.get(url, params=params)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    exported = json.loads(gzip.decompress(r.content))
    assert exported == b and set(exported["artifacts"]) == set(audit.KINDS)
    assert client.get(url, params={**params, "signal_output_id": "../db"}).status_code == 400
    assert client.get(url, params={**params, "market": "kr"}).status_code == 409
