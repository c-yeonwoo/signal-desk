from signal_desk import strategy
from signal_desk.signals import rebalance
from signal_desk.signals.engine import SignalResult


def _sig(ticker, kind, score=0.0):
    return SignalResult(ticker=ticker, name=ticker, score=score, kind=kind, confidence=0.6,
                        technical_score=0.0, fundamental_score=0.0, has_fundamental=False)


def test_propose_sell_trim_and_adds():
    holdings = [
        {"ticker": "AAA", "qty": 100, "avg_price": 100.0},   # SELL 시그널 → 매도
        {"ticker": "BBB", "qty": 100, "avg_price": 100.0},   # 비중 과다 → 축소
        {"ticker": "CCC", "qty": 1, "avg_price": 100.0},     # 소량·HOLD → 유지
    ]
    prices = {"AAA": [100.0], "BBB": [100.0], "CCC": [100.0], "NEW": [100.0]}
    names = {"AAA": "가", "BBB": "나", "CCC": "다", "NEW": "신규"}
    sigmap = {"AAA": _sig("AAA", "SELL", -1.5), "BBB": _sig("BBB", "HOLD", 0.0),
              "CCC": _sig("CCC", "HOLD", 0.0), "NEW": _sig("NEW", "BUY", 2.5)}
    plan = rebalance.propose(holdings, sigmap, prices, names, strategy.bot_params("balanced"))
    acts = {a["ticker"]: a["action"] for a in plan["actions"]}
    assert acts["AAA"] == "매도"          # 시그널 SELL
    assert acts["BBB"] == "축소"          # 비중 과다(BBB가 총액 대부분)
    assert "NEW" in [a["ticker"] for a in plan["adds"]]  # 미보유 강한 BUY 신규 편입 제안


def test_propose_empty_adds_when_no_slots():
    holdings = [{"ticker": f"T{i}", "qty": 10, "avg_price": 100.0} for i in range(10)]
    prices = {f"T{i}": [100.0] for i in range(10)}
    prices["NEW"] = [100.0]
    names = {f"T{i}": f"종목{i}" for i in range(10)}
    names["NEW"] = "신규"
    sigmap = {f"T{i}": _sig(f"T{i}", "HOLD") for i in range(10)}
    sigmap["NEW"] = _sig("NEW", "BUY", 2.5)
    plan = rebalance.propose(holdings, sigmap, prices, names, strategy.bot_params("balanced"))
    # 이미 목표 종목수(10)를 유지 중 → 신규 편입 슬롯 없음
    assert plan["adds"] == []


def test_band_keeps_within_and_acts_outside():
    # 균형형 목표비중 8%, 밴드 ±25% → 유지 구간 [6%, 10%]. 3종목 균등(각 33%)은 상단 초과 → 축소.
    holdings = [{"ticker": t, "qty": 100, "avg_price": 100.0} for t in ("AAA", "BBB", "CCC")]
    prices = {t: [100.0] for t in ("AAA", "BBB", "CCC")}
    names = {t: t for t in ("AAA", "BBB", "CCC")}
    sigmap = {t: _sig(t, "HOLD", 0.0) for t in ("AAA", "BBB", "CCC")}
    plan = rebalance.propose(holdings, sigmap, prices, names, strategy.bot_params("balanced"))
    assert {a["action"] for a in plan["actions"]} == {"축소"}  # 각 33% ≫ 밴드 상단 10% → 전부 축소


def test_band_holds_when_near_target():
    # 12종목 균등 → 각 ~8.3%, 목표 8% 밴드 [6%,10%] 내 → 전부 유지(리밸런싱 안 함)
    tickers = [f"T{i}" for i in range(12)]
    holdings = [{"ticker": t, "qty": 100, "avg_price": 100.0} for t in tickers]
    prices = {t: [100.0] for t in tickers}
    names = {t: t for t in tickers}
    sigmap = {t: _sig(t, "HOLD", 0.0) for t in tickers}
    plan = rebalance.propose(holdings, sigmap, prices, names, strategy.bot_params("balanced"))
    assert {a["action"] for a in plan["actions"]} == {"유지"}
