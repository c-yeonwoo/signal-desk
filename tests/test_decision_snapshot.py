"""가격 기준본·잠정 관측 분리와 정확한 수치 재생."""

import pytest

from signal_desk import db
from signal_desk.signals import decision_snapshot
from signal_desk.signals.engine import SignalResult


def test_output_digest_preserves_market_order_and_result_values():
    rows = [SignalResult(ticker=ticker, name=ticker, score=score, kind="BUY",
                         confidence=0.5, technical_score=0.0, fundamental_score=0.0,
                         has_fundamental=False, reasons=[])
            for ticker, score in (("AAA", 1.0), ("BBB", 2.0))]
    digest = decision_snapshot.output_rows_digest("kr", rows)
    assert digest == decision_snapshot.output_rows_digest(
        "kr", [vars(row) for row in rows])
    assert digest != decision_snapshot.output_rows_digest("kr", list(reversed(rows)))
    assert digest != decision_snapshot.output_rows_digest("us", rows)
    rows[0].score = 1.1
    assert digest != decision_snapshot.output_rows_digest("kr", rows)
    with pytest.raises(ValueError, match="unsupported"):
        decision_snapshot.output_rows_digest("kr", [{"score": float("nan")}])


def _observation(price, observation_id, *, received_at=999):
    return {"captured_at": 1000, "quotes": {"AAPL": price},
            "quote_updated": {"AAPL": received_at},
            "quote_meta": {"AAPL": {"observation_id": observation_id,
                                     "provider": "test", "source_time_verified": False}}}


def test_one_base_is_shared_across_quote_revisions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    dates = {"AAPL": ["2026-10-06", "2026-10-07"], "MSFT": ["2026-10-06", "2026-10-07"]}
    first_prices = {"AAPL": [100.0, 101.0, 102.0], "MSFT": [200.0, 201.0]}
    second_prices = {"AAPL": [100.0, 101.0, 103.0], "MSFT": [200.0, 201.0]}
    first = decision_snapshot.persist_price_inputs("us", first_prices, dates, _observation(102, "one"))
    second = decision_snapshot.persist_price_inputs("us", second_prices, dates, _observation(103, "two"))

    assert first["price_base_id"] == second["price_base_id"]
    assert first["quote_delta_id"] != second["quote_delta_id"]
    assert first["structure_status"] == second["structure_status"] == "verified"
    assert first["strict_pit_eligible"] is False
    assert decision_snapshot.load_price_inputs("us", first["price_base_id"],
                                               first["quote_delta_id"]) == (first_prices, dates)
    assert decision_snapshot.load_price_inputs("us", second["price_base_id"],
                                               second["quote_delta_id"]) == (second_prices, dates)
    assert {r["kind"]: r["count"] for r in db.decision_artifact_storage("us")} == {
        "price_base": 1, "quote_delta": 2}


def test_unmatched_undated_tail_is_preserved_but_not_called_a_quote(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    prices = {"AAPL": [100.0, 101.0]}
    dates = {"AAPL": ["2026-10-06"]}
    saved = decision_snapshot.persist_price_inputs("us", prices, dates, _observation(999, "other"))

    assert saved["structure_status"] == "partial"
    assert saved["issues"] == [{"ticker": "AAPL", "reason": "undated_price_not_matching_quote"}]
    delta = db.decision_artifact_get(saved["quote_delta_id"])["data"]
    assert delta["quotes"] == {} and delta["unclassified"] == {"AAPL": [101.0]}
    assert decision_snapshot.load_price_inputs("us", saved["price_base_id"],
                                               saved["quote_delta_id"]) == (prices, dates)


def test_missing_observation_id_does_not_become_verified(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    saved = decision_snapshot.persist_price_inputs(
        "us", {"AAPL": [100.0, 101.0]}, {"AAPL": ["2026-10-06"]},
        _observation(101, None))
    assert saved["structure_status"] == "partial"
    assert saved["issues"] == [{"ticker": "AAPL", "reason": "observation_id_missing"}]


def test_stale_quote_not_applied_to_base(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    prices = {"AAPL": [100.0]}
    dates = {"AAPL": ["2026-10-06"]}
    saved = decision_snapshot.persist_price_inputs(
        "us", prices, dates, _observation(101, "stale", received_at=300))
    delta = db.decision_artifact_get(saved["quote_delta_id"])["data"]
    assert saved["structure_status"] == "verified"
    assert delta["quotes"] == {}
    assert decision_snapshot.load_price_inputs("us", saved["price_base_id"],
                                               saved["quote_delta_id"]) == (prices, dates)


def test_cross_market_and_nonfinite_prices_are_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="nonfinite"):
        decision_snapshot.persist_price_inputs("us", {"AAPL": [float("nan")]},
                                               {"AAPL": ["2026-10-06"]}, {})
    saved = decision_snapshot.persist_price_inputs("us", {"AAPL": [100.0]},
                                                    {"AAPL": ["2026-10-06"]}, {})
    with pytest.raises(ValueError, match="market mismatch"):
        decision_snapshot.load_price_inputs("kr", saved["price_base_id"], saved["quote_delta_id"])
