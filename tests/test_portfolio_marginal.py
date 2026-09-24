"""Marginal risk comparison uses the same dates and the same initial capital."""

import datetime

from signal_desk.signals import portfolio_marginal


def _panel():
    start = datetime.date(2026, 1, 1)
    dates = [(start + datetime.timedelta(days=i)).isoformat() for i in range(61)]
    held, candidate = [100.0], [100.0]
    for i in range(60):
        move = 0.02 if i % 2 else -0.02
        held.append(held[-1] * (1 + move))
        candidate.append(candidate[-1] * (1 - move))
    return dates, held, candidate


def test_cash_funded_candidate_can_reduce_observed_portfolio_volatility():
    dates, held, candidate = _panel()
    out = portfolio_marginal.assess(
        holdings=[{"ticker": "H", "value": 500}],
        candidate={"ticker": "N", "proposed_value": 100}, cash=500,
        dates_by={"H": dates, "N": dates}, closes_by={"H": held, "N": candidate})
    assert out["ready"] is True
    assert out["observations"] == 60
    assert out["candidate_weight_pct"] == 10
    assert out["delta_volatility_pp"] < 0
    assert out["live_eligible"] is False


def test_marginal_risk_blocks_mismatched_closes_and_sales_funded_candidate():
    dates, held, candidate = _panel()
    args = {"holdings": [{"ticker": "H", "value": 500}],
            "candidate": {"ticker": "N", "proposed_value": 100}, "cash": 500,
            "dates_by": {"H": dates, "N": dates}, "closes_by": {"H": held, "N": candidate}}
    assert portfolio_marginal.assess(**{**args, "cash": 50})["ready"] is False
    assert portfolio_marginal.assess(**{**args, "dates_by": {"H": dates, "N": dates[:-1]}})["ready"] is False
    shifted = [(datetime.date.fromisoformat(dates[0]) - datetime.timedelta(days=1)).isoformat(),
               *dates[:59], dates[-1]]
    # Even when latest dates match, 59 common returns are not rounded up to 60.
    assert portfolio_marginal.assess(**{**args, "dates_by": {"H": dates, "N": shifted}})["ready"] is False
