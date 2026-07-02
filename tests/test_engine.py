import pytest

from signal_desk.signals import engine, fundamental as fnd
from signal_desk.signals import indicators as ind


def test_technical_score_all_factors_align():
    # RSI 과매도 + MACD 골든크로스 + 정배열 상승추세가 동시에 겹치는 인위적 케이스 -> 만점 +3.0
    series = {
        "rsi": [None, None, 25.0],
        "macd": {"histogram": [None, -1.0, 2.0]},
        "ma_short": [None, None, 110.0],
        "ma_mid": [None, None, 100.0],
    }
    closes = [90.0, 95.0, 115.0]
    score, reasons = engine.technical_score_at(closes, series, 2)
    assert score == pytest.approx(3.0)
    assert len(reasons) == 3


def test_technical_score_no_data_is_zero():
    series = {"rsi": [None], "macd": {"histogram": [None]}, "ma_short": [None], "ma_mid": [None]}
    score, reasons = engine.technical_score_at([100.0], series, 0)
    assert score == 0.0
    assert reasons == []


def test_combine_technical_only_is_identity():
    fund = fnd.score({})  # has_data=False
    combined = engine.combine(1.5, ["r"], fund)
    assert combined["score"] == pytest.approx(1.5)
    assert combined["kind"] == "BUY"  # >= 1.2
    assert "재무데이터 없음" in combined["reasons"]


def test_combine_renormalizes_with_fundamental():
    fund = fnd.FundamentalResult(score=-2.0, has_data=True, reasons=["부정적 재무"])
    combined = engine.combine(3.0, ["기술 만점"], fund)
    # weighted = (1.0*.35 + (-1.0)*.30) / .65 = 0.05/.65 ; score = *3
    expected = (1.0 * 0.35 + (-1.0) * 0.30) / 0.65 * 3
    assert combined["score"] == pytest.approx(round(expected, 2))
    assert combined["kind"] == "HOLD"
    expected_conf = round(abs(2 * ind.sigmoid(expected) - 1) * 100) / 100
    assert combined["confidence"] == pytest.approx(expected_conf)


def test_evaluate_produces_sorted_signals_without_fundamentals():
    universe = [{"ticker": "005930", "name": "삼성전자"}]
    # 꾸준히 하락 -> RSI 과매도로 BUY 트리거 (짧은 시계열이라 MACD/MA는 미형성 -> RSI 단독)
    closes = [100 - i for i in range(20)]
    results = engine.evaluate(universe, {"005930": closes})
    assert len(results) == 1
    r = results[0]
    assert r.kind == "BUY"
    assert r.has_fundamental is False
    assert r.technical_score == pytest.approx(1.5)


def test_evaluate_skips_ticker_without_prices():
    universe = [{"ticker": "AAA", "name": "a"}, {"ticker": "BBB", "name": "b"}]
    results = engine.evaluate(universe, {"AAA": [100.0] * 20})
    assert len(results) == 1
    assert results[0].ticker == "AAA"


def test_backtest_summary_structure():
    # 완만한 상승 후 조정을 반복하는 합성 시계열 — 정확한 수치보단 계약(구조) 검증 목적
    closes = []
    price = 100.0
    for i in range(60):
        price += 1.5 if i % 10 < 7 else -3.0
        closes.append(price)
    out = engine.backtest_summary({"TEST": closes})
    assert out["method"] == "technical_only_v1"
    assert {row["kind"] for row in out["by_signal"]} == {"BUY", "SELL"}
    for row in out["by_signal"]:
        assert row["n"] >= 0
        if row["n"]:
            assert 0 <= row["winrate"] <= 100


def test_backtest_summary_ignores_short_series():
    out = engine.backtest_summary({"SHORT": [100.0] * 10})
    assert all(row["n"] == 0 for row in out["by_signal"])


def test_signal_zones_compresses_consecutive_buy_days():
    # 20일 연속 하락 -> RSI(14)가 정의되는 인덱스 14부터 계속 과매도(0) -> BUY 구간 하나로 압축
    closes = [100 - i for i in range(20)]
    dates = [f"2026-01-{i + 1:02d}" for i in range(20)]
    zones = engine.signal_zones(dates, closes)
    assert zones == [{"start": "2026-01-15", "end": "2026-01-20", "kind": "BUY"}]


def test_signal_zones_empty_for_flat_series():
    zones = engine.signal_zones(["2026-01-0" + str(i) for i in range(1, 6)], [100.0] * 5)
    assert zones == []
