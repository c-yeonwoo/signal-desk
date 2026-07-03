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


def test_combine_single_component_is_identity():
    combined = engine.combine([(1.5 / 3.0, 0.35, ["r"])])
    assert combined["score"] == pytest.approx(1.5)
    assert combined["kind"] == "BUY"  # >= 1.2
    assert combined["reasons"] == ["r"]


def test_combine_zero_weight_component_excluded_from_average_but_reasons_kept():
    fund = fnd.score({})  # has_data=False -> engine이 가중치 0으로 넘김
    combined = engine.combine([(1.5 / 3.0, 0.35, ["r"]), (0.0, 0.0, fund.reasons)])
    assert combined["score"] == pytest.approx(1.5)
    assert "재무데이터 없음" in combined["reasons"]


def test_combine_renormalizes_across_weighted_components():
    combined = engine.combine([(1.0, 0.35, ["기술 만점"]), (-1.0, 0.30, ["부정적 재무"])])
    # weighted = (1.0*.35 + (-1.0)*.30) / .65 ; score = *3
    expected = (1.0 * 0.35 + (-1.0) * 0.30) / 0.65 * 3
    assert combined["score"] == pytest.approx(round(expected, 2))
    assert combined["kind"] == "HOLD"
    expected_conf = round(abs(2 * ind.sigmoid(expected) - 1) * 100) / 100
    assert combined["confidence"] == pytest.approx(expected_conf)


def test_evaluate_produces_sorted_signals_without_fundamentals_or_valuation():
    universe = [{"ticker": "005930", "name": "삼성전자"}]
    # 꾸준히 하락 -> RSI 과매도로 BUY 트리거 (짧은 시계열이라 MACD/MA는 미형성 -> RSI 단독)
    closes = [100 - i for i in range(20)]
    results = engine.evaluate(universe, {"005930": closes})
    assert len(results) == 1
    r = results[0]
    assert r.kind == "BUY"
    assert r.has_fundamental is False
    assert r.has_valuation is False  # fundamentals 미제공 -> PER/PBR 없음
    assert r.technical_score == pytest.approx(1.5)


def test_evaluate_skips_ticker_without_prices():
    universe = [{"ticker": "AAA", "name": "a"}, {"ticker": "BBB", "name": "b"}]
    results = engine.evaluate(universe, {"AAA": [100.0] * 20})
    assert len(results) == 1
    assert results[0].ticker == "AAA"


def test_evaluate_includes_valuation_when_per_pbr_available():
    universe = [
        {"ticker": "AAA", "name": "a"},
        {"ticker": "BBB", "name": "b"},
    ]
    prices = {"AAA": [100.0] * 20, "BBB": [100.0] * 20}
    fundamentals = {
        "AAA": {"per": 5.0, "pbr": 0.5},  # 가장 저평가
        "BBB": {"per": 50.0, "pbr": 5.0},  # 가장 고평가
    }
    results = {r.ticker: r for r in engine.evaluate(universe, prices, fundamentals)}
    assert results["AAA"].has_valuation is True
    assert results["AAA"].valuation_percentile == pytest.approx(0.0)
    assert any("저평가" in r for r in results["AAA"].reasons)
    assert results["BBB"].valuation_percentile == pytest.approx(100.0)
    assert any("고평가" in r for r in results["BBB"].reasons)


def test_evaluate_reports_reversion_factor_on_crash():
    universe = [{"ticker": "AAA", "name": "a"}]
    # 20일 평탄 후 마지막 10일 급락 + RSI 과매도 조건 성립하도록 구성
    closes = [100.0] * 10 + [100 - i * 3 for i in range(1, 11)]
    results = engine.evaluate(universe, {"AAA": closes})
    r = results[0]
    assert r.has_reversion is True


def test_backtest_summary_structure():
    # 완만한 상승 후 조정을 반복하는 합성 시계열 — 정확한 수치보단 계약(구조) 검증 목적
    closes = []
    price = 100.0
    for i in range(60):
        price += 1.5 if i % 10 < 7 else -3.0
        closes.append(price)
    out = engine.backtest_summary({"TEST": closes})
    assert out["method"] == "price_based_v2"
    # 5단계 중 실행 가능한 4종(강력매수/매수/매도/강력매도)이 성적표 행으로 나온다
    assert {row["kind"] for row in out["by_signal"]} == {"STRONG_BUY", "BUY", "SELL", "STRONG_SELL"}
    for row in out["by_signal"]:
        assert row["n"] >= 0
        if row["n"]:
            assert 0 <= row["winrate"] <= 100


def test_classify_five_tiers():
    assert engine.classify(2.5) == "STRONG_BUY"
    assert engine.classify(1.5) == "BUY"
    assert engine.classify(0.0) == "HOLD"
    assert engine.classify(-1.5) == "SELL"
    assert engine.classify(-2.5) == "STRONG_SELL"
    assert engine.is_buy("STRONG_BUY") and engine.is_buy("BUY")
    assert engine.is_sell("STRONG_SELL") and engine.is_sell("SELL")
    assert not engine.is_buy("HOLD") and not engine.is_sell("HOLD")


def test_backtest_summary_ignores_short_series():
    out = engine.backtest_summary({"SHORT": [100.0] * 10})
    assert all(row["n"] == 0 for row in out["by_signal"])


def test_pit_year_uses_only_disclosed_reports():
    # 사업보고서는 이듬해 3~4월 공시 → 2026-05는 2025년 보고서까지, 2026-02는 2024년까지
    years = [2023, 2024, 2025]
    assert engine._pit_year("2026-05-10", years) == 2025
    assert engine._pit_year("2026-02-10", years) == 2024
    assert engine._pit_year("2025-06-10", years) == 2024  # 6월 → 전년(2024) 보고서까지 공시됨
    assert engine._pit_year("2024-01-10", years) is None  # known=2022, 히스토리에 없음
    assert engine._pit_year("2020-05-10", years) is None  # 그 시점엔 아직 아무 보고서도 없음


def test_backtest_with_pit_reports_pit_method():
    closes = [100 - i for i in range(40)]
    dates = [f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}" for i in range(40)]
    hist = {"AAA": {"2025": {"net_income": 100.0, "equity": 1000.0, "roe": 10.0}}}
    out = engine.backtest_summary({"AAA": closes}, dates_by_ticker={"AAA": dates},
                                  fundamentals_history=hist)
    assert out["method"] == "pit_v3"


def test_factor_contribution_reports_each_factor_and_baseline():
    closes = [100 - i for i in range(40)]  # 하락 추세 → 낙폭/기술 매수 신호 다수
    out = engine.factor_contribution({"AAA": closes})
    labels = {f["factor"] for f in out["factors"]}
    assert "all" in labels and "technical" in labels and "reversion" in labels
    # 히스토리 없으면 fundamental 단독은 제외
    assert "fundamental" not in labels
    for f in out["factors"]:
        assert f["winrate"] is None or 0 <= f["winrate"] <= 100


def test_walk_forward_splits_into_windows():
    closes = [100 + (i % 7) - 3 for i in range(120)]
    out = engine.walk_forward({"AAA": closes}, windows=4)
    assert len(out["windows"]) == 4
    assert [s["window"] for s in out["windows"]] == [1, 2, 3, 4]


def test_signal_zones_compresses_consecutive_buy_days():
    # 20일 연속 하락 -> RSI(14)가 정의되는 인덱스 14부터 계속 과매도(0) -> BUY 구간 하나로 압축
    closes = [100 - i for i in range(20)]
    dates = [f"2026-01-{i + 1:02d}" for i in range(20)]
    zones = engine.signal_zones(dates, closes)
    assert len(zones) == 1
    z = zones[0]
    assert (z["start"], z["end"], z["kind"]) == ("2026-01-15", "2026-01-20", "BUY")
    assert isinstance(z["reasons"], list)  # 구간 시작 시점 판단 근거 동봉


def test_signal_zones_empty_for_flat_series():
    zones = engine.signal_zones(["2026-01-0" + str(i) for i in range(1, 6)], [100.0] * 5)
    assert zones == []
