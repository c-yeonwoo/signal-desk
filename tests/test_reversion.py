import pytest

from signal_desk.signals import reversion


def _closes_with_return(pct: float, n: int = 10, base: float = 100.0):
    """마지막 n일 동안 base -> base*(1+pct)로 선형 변화하는 (n+1)개 종가 시퀀스."""
    end = base * (1 + pct)
    return [base + (end - base) * i / n for i in range(n + 1)]


def test_crash_with_oversold_confirms_rebound():
    closes = _closes_with_return(-0.20)  # 최근 10일 -20%
    rsi_series = [None] * len(closes)
    rsi_series[-1] = 25.0  # < 35 확인
    score, reasons = reversion.score(closes, rsi_series)
    assert score == pytest.approx(1.0, abs=1e-6)
    assert "낙폭과대" in reasons[0]


def test_rally_with_overbought_confirms_correction():
    closes = _closes_with_return(0.25)  # 최근 10일 +25%
    rsi_series = [None] * len(closes)
    rsi_series[-1] = 75.0  # > 70 확인
    score, reasons = reversion.score(closes, rsi_series)
    assert score == pytest.approx(-0.9375, abs=1e-6)
    assert "단기과열" in reasons[0]


def test_score_clamped_to_max():
    closes = _closes_with_return(-0.90)  # 극단적 급락
    rsi_series = [None] * len(closes)
    rsi_series[-1] = 10.0
    score, _ = reversion.score(closes, rsi_series)
    assert score == pytest.approx(1.5)  # max_score로 클램프


def test_crash_without_rsi_confirmation_is_neutral():
    closes = _closes_with_return(-0.20)
    rsi_series = [None] * len(closes)
    rsi_series[-1] = 50.0  # 과매도 아님
    score, reasons = reversion.score(closes, rsi_series)
    assert score == 0.0 and reasons == []


def test_insufficient_history_is_neutral():
    score, reasons = reversion.score([100.0, 101.0], [50.0, 50.0])
    assert score == 0.0 and reasons == []


def test_missing_rsi_is_neutral():
    closes = _closes_with_return(-0.20)
    rsi_series = [None] * len(closes)
    score, reasons = reversion.score(closes, rsi_series)
    assert score == 0.0 and reasons == []
