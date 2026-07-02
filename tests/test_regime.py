import pytest

from signal_desk.signals import regime


def _trend(daily_pct: float, days: int = 90, base: float = 100.0):
    closes = [base]
    for _ in range(days):
        closes.append(closes[-1] * (1 + daily_pct))
    return closes


def test_strong_uptrend_is_bull_or_overheat():
    prices = {f"T{i}": _trend(0.01) for i in range(10)}  # 매일 +1% -> 강한 상승
    out = regime.classify(prices)
    assert out["ready"] is True
    assert out["breadth_pct"] == 100.0
    assert out["regime"] in ("강세", "과열")


def test_strong_downtrend_is_bear_or_correction():
    prices = {f"T{i}": _trend(-0.01) for i in range(10)}  # 매일 -1% -> 강한 하락
    out = regime.classify(prices)
    assert out["ready"] is True
    assert out["breadth_pct"] == 0.0
    assert out["regime"] in ("약세", "조정")


def test_flat_series_is_neutral():
    prices = {f"T{i}": [100.0] * 90 for i in range(10)}
    out = regime.classify(prices)
    assert out["ready"] is True
    assert out["avg_momentum_pct"] == pytest.approx(0.0)


def test_insufficient_samples_is_not_ready():
    out = regime.classify({"SHORT": [100.0] * 10})
    assert out == {"ready": False, "regime": None, "breadth_pct": None, "avg_momentum_pct": None, "n": 0}
