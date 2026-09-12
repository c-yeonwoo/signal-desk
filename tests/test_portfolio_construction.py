from collections import defaultdict

from signal_desk.signals import portfolio_construction as pc


def _series(scale=1.0):
    returns = [0.01, -0.006, 0.013, -0.004, 0.007, -0.009] * 14
    closes = [100.0]
    for ret in returns:
        closes.append(closes[-1] * (1 + ret * scale))
    dates = [f"2026-{i // 28 + 1:02d}-{i % 28 + 1:02d}" for i in range(len(closes))]
    return dates, closes


def _profile(**overrides):
    return {"cash": 0, "min_cash_pct": 10, "max_single_position_pct": 45,
            "max_sector_pct": 50, **overrides}


def test_constrained_risk_parity_never_breaks_asset_or_sector_caps():
    dates, a = _series(1.0)
    _, b = _series(0.7)
    _, c = _series(-0.8)
    rows = [
        {"ticker": "A", "name": "A", "value": a[-1], "sector": "tech", "history_ready": True},
        {"ticker": "B", "name": "B", "value": b[-1], "sector": "tech", "history_ready": True},
        {"ticker": "C", "name": "C", "value": c[-1], "sector": "health", "history_ready": True},
    ]
    out = pc.propose(rows, dates_by={"A": dates, "B": dates, "C": dates},
                     closes_by={"A": a, "B": b, "C": c}, profile=_profile())
    assert out["ready"] is True and out["mode"] == "shadow"
    assert all(item["target_weight_pct"] <= 45 for item in out["items"])
    sector_total = defaultdict(float)
    for item in out["items"]:
        sector_total[item["sector"]] += item["target_weight_pct"]
    assert all(value <= 50.01 for value in sector_total.values())
    assert round(sum(item["target_weight_pct"] for item in out["items"]) + out["structural_cash_pct"], 1) == 90.0


def test_infeasible_caps_leave_structural_cash_instead_of_violating_limit():
    dates, a = _series()
    row = {"ticker": "A", "name": "A", "value": a[-1], "sector": "tech", "history_ready": True}
    out = pc.propose([row], dates_by={"A": dates}, closes_by={"A": a},
                     profile=_profile(max_single_position_pct=30, max_sector_pct=30))
    assert out["ready"] is True
    assert out["items"][0]["target_weight_pct"] == 30.0
    assert out["structural_cash_pct"] == 60.0


def test_missing_quality_data_blocks_target_allocation():
    out = pc.propose([{"ticker": "A", "value": 100, "sector": None, "history_ready": False}],
                     dates_by={}, closes_by={}, profile=_profile())
    assert out["ready"] is False
