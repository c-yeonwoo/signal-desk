from datetime import date, timedelta
from types import SimpleNamespace

import numpy as np
import pytest

from signal_desk import llm
from signal_desk.signals import meta_entry, portfolio_candidates, portfolio_construction, portfolio_decision


def series(start=date(2026, 1, 1), n=85, reverse=False):
    dates = [(start + timedelta(days=i)).isoformat() for i in range(n)]
    values = [100.0]
    for i in range(1, n):
        values.append(values[-1] * (1 + (.01 if i % 2 else -.008) * (-1 if reverse else 1)))
    return dates, [v / values[-1] * 10 for v in values]


def test_meta_training_never_uses_a_later_calendar_date():
    old = series(date(2025, 1, 1), 700)
    new = series(date(2026, 7, 1), 100)
    records = [{"ticker": t, "date": day, "kind": "BUY", "score": 1}
               for t, day in [("NEW", "2026-08-01"), ("OLD", "2025-12-01")]]
    rows = meta_entry.build_labeled_rows(records, {"OLD": old, "NEW": new},
                                        meta_entry.TripleBarrierConfig(horizon_days=5))
    assert rows[0]["date"] == "2025-12-01"
    assert rows[0]["entry_date"] == "2025-12-02"
    for train, test in meta_entry.purged_folds(rows, folds=2):
        for a in train:
            for b in test:
                assert rows[a]["label_end_date"] < rows[b]["entry_date"]


def test_meta_folds_keep_a_session_together():
    rows = [{"entry_index": i, "label_end_index": i + 1} for i in range(10) for _ in range(3)]
    for train, test in meta_entry.purged_folds(rows, folds=4):
        for i in test:
            assert all(j in test for j, r in enumerate(rows) if r["entry_index"] == rows[i]["entry_index"])
        assert not set(train) & set(test)


def test_erc_converges_for_negative_correlation_covariance():
    cov = np.array([[1, -.325, 0], [-.325, 1, 0], [0, 0, 1.]])
    weights = portfolio_construction._risk_parity(cov)
    rc = weights * (cov @ weights)
    assert np.max(np.abs(rc / rc.sum() - 1 / 3)) < 1e-8


def test_erc_rejects_invalid_covariance():
    with pytest.raises(ValueError):
        portfolio_construction._risk_parity(np.array([[1, 2], [2, 1]]))


def test_cluster_cap_is_applied_even_across_different_sectors():
    dates, values = series()
    rows = [{"ticker": t, "sector": t, "value": 100, "history_ready": True} for t in ("A", "B")]
    out = portfolio_construction.propose(rows, dates_by={"A": dates, "B": dates},
        closes_by={"A": values, "B": values}, profile={"cash": 100, "min_cash_pct": 5,
        "max_single_position_pct": 70, "max_sector_pct": 80, "max_cluster_pct": 30})
    assert out["ready"]
    assert sum(i["target_weight_pct"] for i in out["items"]) <= 30 + 1e-9


def test_invalid_or_mixed_price_dates_block_joint_plan():
    dates, values = series()
    rows = [{"ticker": t, "sector": t, "value": 100, "qty": 10, "price": 10, "history_ready": True}
            for t in ("A", "B")]
    out = portfolio_decision.decide(rows=rows, universe=[], signal_by_ticker={}, market="kr",
        dates_by={"A": dates, "B": dates[:-1]}, prices={"A": values, "B": values[:-1]},
        profile={"cash": 100, "min_cash_pct": 5, "max_single_position_pct": 70, "max_sector_pct": 80})
    assert out["trade_plan"]["ready"] is False
    assert "기준일" in out["decision"]["reason"]


def test_candidate_missing_holdings_history_fails_closed():
    dates, values = series()
    result = portfolio_candidates.evaluate(
        holdings=[{"ticker": "H", "value": 500, "sector": "tech", "history_ready": False}],
        universe=[{"ticker": "N", "sector": "health"}],
        signal_by_ticker={"N": SimpleNamespace(kind="BUY", score=2, event_risk=False)},
        dates_by={"N": dates}, prices={"N": values},
        profile={"cash": 500, "min_cash_pct": 5, "max_single_position_pct": 95, "max_sector_pct": 100})
    assert result["ready"] is False


def test_joint_plan_has_one_cash_budget_and_candidate_instructions():
    dates, values = series()
    _, other = series(reverse=True)
    out = portfolio_decision.decide(
        rows=[{"ticker": "H", "qty": 50, "value": 500, "price": 10, "price_as_of": dates[-1],
               "sector": "tech", "history_ready": True, "entry_allowed": True}],
        universe=[{"ticker": "N", "sector": "health"}],
        signal_by_ticker={"N": SimpleNamespace(kind="BUY", score=2, event_risk=False)},
        dates_by={"H": dates, "N": dates}, prices={"H": values, "N": other}, market="kr",
        profile={"cash": 500, "min_cash_pct": 5, "max_single_position_pct": 95, "max_sector_pct": 100,
                 "max_cluster_pct": 100})
    plan = out["trade_plan"]
    assert plan["ready"]
    assert plan["estimated"]["cash_after"] >= 50
    assert out["decision"]["live_eligible"] is False
    candidate = out["entry_candidates"]["candidates"][0]
    buy = next(i for i in plan["instructions"] if i["ticker"] == "N")
    assert candidate["proposed_qty"] == buy["qty"]
    assert candidate["proposed_value"] == buy["qty"] * candidate["price"]


def test_tools_success_records_usage_and_returns_response(monkeypatch):
    monkeypatch.setattr(llm, "_post_json", lambda *a, **k: {
        "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
        "usage": {"input_tokens": 100, "output_tokens": 10}})
    recorded = []
    monkeypatch.setattr(llm, "_record_usage", lambda *a, **k: recorded.append(k))
    assert llm.messages_with_tools("system", [], [], purpose="test")["stop_reason"] == "end_turn"
    assert recorded == [{"kind": "test:tools"}]
