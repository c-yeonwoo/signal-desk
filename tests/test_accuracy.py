"""실측 성과 트래커 — signal_history × 실현수익 조인, 티어 적중률·매수 정밀도·팩터 IC."""

from signal_desk.signals import accuracy


def _closes(start=100.0, n=90, step=1.0):
    dates = [f"2026-01-{d:02d}" if d <= 31 else f"2026-02-{d-31:02d}" for d in range(1, n + 1)]
    closes = [start + step * i for i in range(n)]
    return dates, closes


def test_entry_is_next_trading_day():
    dates = ["2026-01-01", "2026-01-02", "2026-01-03"]
    assert accuracy._entry_index(dates, "2026-01-01") == 1   # 시그널 다음 거래일
    assert accuracy._entry_index(dates, "2026-01-03") is None  # 이후 봉 없음


def test_forward_returns_only_matured_horizons():
    dates, closes = _closes(start=100.0, n=10, step=10.0)  # 100,110,...,190
    # 시그널 2026-01-01 → 진입 idx1(=110). h=5 → idx6(=160): 160/110-1
    rets = accuracy._forward_returns(dates, closes, "2026-01-01", (5, 20))
    assert 5 in rets and abs(rets[5] - (160 / 110 - 1)) < 1e-9
    assert 20 not in rets                                   # 20일 미성숙 → 제외


def test_buy_precision_and_tier_hit_rate():
    # 오르는 종목 UP(매수가 맞음), 내리는 종목 DN(매수가 틀림)
    up_d, up_c = _closes(start=100.0, n=60, step=1.0)
    dn_d, dn_c = _closes(start=100.0, n=60, step=-1.0)
    closes = {"UP": (up_d, up_c), "DN": (dn_d, dn_c)}
    rows = [
        {"date": "2026-01-01", "ticker": "UP", "kind": "BUY", "momentum": 0.4, "technical": 1.0,
         "fundamental": 0, "valuation": 20, "reversion": 0, "qualitative": 0, "flow": 0.1, "quality": 3},
        {"date": "2026-01-01", "ticker": "DN", "kind": "BUY", "momentum": -0.4, "technical": -1.0,
         "fundamental": 0, "valuation": 80, "reversion": 0, "qualitative": 0, "flow": -0.1, "quality": 1},
    ]
    out = accuracy.realized_accuracy(rows, closes, horizons=(5, 20), primary=20)
    assert out["ready"] is True
    # 매수 2건 중 1건(UP)만 상승 → 정밀도 50%
    assert out["buy_precision_pct"] == 50.0
    assert out["buy_sample"] == 2
    buy20 = out["tiers"][20]["BUY"]
    assert buy20["n"] == 2 and buy20["hit_rate"] == 50.0


def test_sell_hit_rate_direction():
    dn_d, dn_c = _closes(start=100.0, n=40, step=-1.0)
    closes = {"DN": (dn_d, dn_c)}
    rows = [{"date": "2026-01-01", "ticker": "DN", "kind": "STRONG_SELL", "momentum": -0.5,
             "technical": -1, "fundamental": 0, "valuation": 50, "reversion": 0,
             "qualitative": 0, "flow": 0, "quality": 0}]
    out = accuracy.realized_accuracy(rows, closes, horizons=(5,), primary=5)
    # 매도 신호 + 실제 하락 → 방향 적중 100%
    assert out["tiers"][5]["STRONG_SELL"]["hit_rate"] == 100.0


def test_sell_precision_counts_declines():
    up_d, up_c = _closes(start=100.0, n=40, step=1.0)
    dn_d, dn_c = _closes(start=100.0, n=40, step=-1.0)
    closes = {"UP": (up_d, up_c), "DN": (dn_d, dn_c)}
    base = {"momentum": 0, "technical": 0, "fundamental": 0, "valuation": 50,
            "reversion": 0, "qualitative": 0, "flow": 0, "quality": 0}
    rows = [
        {"date": "2026-01-01", "ticker": "DN", "kind": "SELL", **base},
        {"date": "2026-01-01", "ticker": "DN", "kind": "STRONG_SELL", **base},
        {"date": "2026-01-01", "ticker": "UP", "kind": "SELL", **base},
    ]
    out = accuracy.realized_accuracy(rows, closes, horizons=(5,), primary=5)
    # 매도 3건 중 2건(DN)만 하락 → 66.7%. 매수 표본은 비어 있어도 무관
    assert out["sell_precision_pct"] == 66.7
    assert out["sell_sample"] == 3
    assert out["buy_precision_pct"] is None and out["buy_sample"] == 0


def test_factor_ic_sign_and_min_samples():
    # momentum이 높을수록 미래수익이 높은 구조 25종목 → IC>0
    closes, rows = {}, []
    for i in range(25):
        step = (i - 12) * 0.5           # -6 ~ +6 기울기
        d, c = _closes(start=100.0, n=30, step=step)
        t = f"T{i}"
        closes[t] = (d, c)
        rows.append({"date": "2026-01-01", "ticker": t, "kind": "HOLD", "momentum": float(i),
                     "technical": 0, "fundamental": 0, "valuation": 50, "reversion": 0,
                     "qualitative": 0, "flow": 0, "quality": 0})
    out = accuracy.realized_accuracy(rows, closes, horizons=(5,), primary=5)
    assert out["factor_ic"]["momentum"] is not None
    assert out["factor_ic"]["momentum"] > 0.9        # 단조 증가 → 강한 양의 IC
    # 표본 부족 팩터는 None (25<... 아님; 여기선 모두 25라 계산됨) → 별도 검증
    assert out["coverage"]["matured_primary"] == 25


def test_min_samples_returns_none_ic():
    d, c = _closes(start=100.0, n=30, step=1.0)
    rows = [{"date": "2026-01-01", "ticker": "A", "kind": "HOLD", "momentum": 0.1,
             "technical": 0, "fundamental": 0, "valuation": 50, "reversion": 0,
             "qualitative": 0, "flow": 0, "quality": 0}]
    out = accuracy.realized_accuracy(rows, {"A": (d, c)}, horizons=(5,), primary=5)
    assert out["factor_ic"]["momentum"] is None       # 1건 < 최소 표본


def test_unmatched_ticker_skipped():
    out = accuracy.realized_accuracy(
        [{"date": "2026-01-01", "ticker": "GHOST", "kind": "BUY"}], {}, horizons=(5,))
    assert out["ready"] is False
    assert out["coverage"]["rows"] == 1 and out["coverage"]["tickers_matched"] == 0
