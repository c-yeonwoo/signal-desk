from signal_desk.signals import portfolio_risk as pr


def test_correlated_holdings_are_grouped_and_concentration_is_reported():
    dates = [f"2026-01-{i:02d}" for i in range(1, 31)]
    # A/B는 같은 수익 경로, C는 반대 경로로 만든다.
    returns = [0.01, -0.02, 0.03, 0.01, -0.01] * 6
    def prices(start, scale):
        out = [start]
        for ret in returns[1:]:
            out.append(out[-1] * (1 + ret * scale))
        return out
    a, b, c = prices(100, 1), prices(50, 2), prices(100, -1)
    out = pr.diagnostics(
        [{"ticker": "A", "qty": 1, "price": a[-1]}, {"ticker": "B", "qty": 1, "price": b[-1]},
         {"ticker": "C", "qty": 1, "price": c[-1]}],
        dates_by={"A": dates, "B": dates, "C": dates}, closes_by={"A": a, "B": b, "C": c},
        sector_by={"A": "tech", "B": "tech", "C": "health"}, correlation_threshold=0.75,
    )
    assert out["pair_coverage"]["known"] == 3
    assert out["high_correlation_pairs"] == [{"a": "A", "b": "B", "correlation": 1.0}]
    assert out["clusters"][0]["tickers"] == ["A", "B"]
    assert out["sector_hhi"] is not None and out["sector_hhi"] > 0.5


def test_missing_history_is_reported_not_assumed_uncorrelated():
    out = pr.diagnostics(
        [{"ticker": "A", "qty": 1, "price": 100}, {"ticker": "B", "qty": 1, "price": 100}],
        dates_by={"A": ["2026-01-01"], "B": ["2026-01-01"]}, closes_by={"A": [100], "B": [100]},
        sector_by={},
    )
    assert out["pair_coverage"] == {"known": 0, "total": 1, "pct": 0.0}
    assert len(out["clusters"]) == 2
