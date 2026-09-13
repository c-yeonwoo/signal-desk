from types import SimpleNamespace

from signal_desk.signals import portfolio_candidates as pc


def _series(scale=1.0):
    moves = [0.012, -0.006, 0.009, -0.011, 0.004, 0.007] * 11
    closes = [100.0]
    for move in moves:
        closes.append(closes[-1] * (1 + move * scale))
    dates = [f"2026-{i // 28 + 1:02d}-{i % 28 + 1:02d}" for i in range(len(closes))]
    return dates, closes


def _profile():
    return {"cash": 100, "min_cash_pct": 5, "max_single_position_pct": 20, "max_sector_pct": 35}


def test_candidate_requires_buy_and_independence_from_existing_holdings():
    dates, held = _series(1)
    _, high_corr = _series(1)
    _, independent = _series(-1)
    signals = {
        "HIGH": SimpleNamespace(kind="BUY", score=3.0, event_risk=False),
        "LOW": SimpleNamespace(kind="BUY", score=2.0, event_risk=False),
    }
    out = pc.evaluate(
        holdings=[{"ticker": "HELD", "value": held[-1], "sector": "tech", "history_ready": True}],
        universe=[{"ticker": "HIGH", "name": "고상관", "sector": "tech"},
                  {"ticker": "LOW", "name": "독립", "sector": "health"}],
        signal_by_ticker=signals, prices={"HELD": held, "HIGH": high_corr, "LOW": independent},
        dates_by={"HELD": dates, "HIGH": dates, "LOW": dates}, profile=_profile(),
    )
    assert out["ready"] is True
    assert [item["ticker"] for item in out["candidates"]] == ["LOW"]
    assert out["candidates"][0]["proposed_weight_pct"] == 20.0
    assert any(item["ticker"] == "HIGH" and "고상관" in item["reason"] for item in out["rejected"])


def test_candidate_does_not_spend_required_cash():
    dates, prices = _series()
    out = pc.evaluate(
        holdings=[], universe=[{"ticker": "A", "name": "A", "sector": "tech"}],
        signal_by_ticker={"A": SimpleNamespace(kind="BUY", score=2, event_risk=False)},
        prices={"A": prices}, dates_by={"A": dates},
        profile={"cash": 100, "min_cash_pct": 100, "max_single_position_pct": 20, "max_sector_pct": 35},
    )
    assert out["ready"] is False and "현금 여유" in out["reason"]


def test_event_risk_candidate_is_never_selected():
    dates, prices = _series()
    out = pc.evaluate(
        holdings=[], universe=[{"ticker": "A", "name": "A", "sector": "tech"}],
        signal_by_ticker={"A": SimpleNamespace(kind="BUY", score=2, event_risk=True)},
        prices={"A": prices}, dates_by={"A": dates}, profile=_profile(),
    )
    assert out["ready"] is False
    assert out["rejected"] == [{"ticker": "A", "name": "A", "reason": "이벤트 위험 감지"}]
