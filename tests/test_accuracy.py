"""실측 성과 트래커 — signal_history × 실현수익 조인, 티어 적중률·매수 정밀도·팩터 IC."""

from signal_desk.signals import accuracy


def _closes(start=100.0, n=90, step=1.0):
    dates = [f"2026-01-{d:02d}" if d <= 31 else f"2026-02-{d-31:02d}" for d in range(1, n + 1)]
    closes = [start + step * i for i in range(n)]
    return dates, closes


def _cal(n: int) -> list[str]:
    """연속 거래일 n개(1월→2월). `_closes`와 같은 달력."""
    return [f"2026-01-{d:02d}" if d <= 31 else f"2026-02-{d-31:02d}" for d in range(1, n + 1)]


def _panel(n_dates=25, n_tickers=25, *, slope=1.0, factor="momentum", horizon=5, bars=90):
    """(rows, closes) — `n_dates`개 날짜 × `n_tickers`종목 패널.

    팩터 IC는 **날짜 단위**로 판정되므로(같은 날 200종목은 하나의 관측) 한 날짜만 있는 픽스처로는
    `factor_ic`가 나오지 않는다. 각 날짜에서 `factor` 값이 클수록 이후 수익이 높게 만든다.
    `slope=-1`이면 부호를 뒤집는다. `slope=0`이면 팩터와 수익이 무관(IC≈0).
    """
    dates = _cal(bars)
    # 복리로 만든다 — 선형 증감은 하락 종목의 가격이 음수로 내려가 수익률 단조성이 깨진다.
    closes = {}
    for i in range(n_tickers):
        r = (i - n_tickers / 2.0) * slope * 0.002
        closes[f"T{i}"] = (dates, [100.0 * (1.0 + r) ** k for k in range(bars)])
    rows = []
    for d in dates[:n_dates]:
        for i in range(n_tickers):
            row = {"date": d, "ticker": f"T{i}", "kind": "HOLD", "technical": 0,
                   "fundamental": 0, "valuation": 50, "reversion": 0, "qualitative": 0,
                   "flow": 0, "quality": 0, "momentum": 0, "short": 0, "score": 0}
            row[factor] = float(i)
            rows.append(row)
    return rows, closes


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


def _flat_universe(n_down: int, n_up: int, *, days: int = 40):
    """하락 n_down개 · 상승 n_up개로 이뤄진 유니버스 + 기준선 계산용 HOLD 행."""
    closes, rows = {}, []
    base = {"momentum": 0, "technical": 0, "fundamental": 0, "valuation": 50,
            "reversion": 0, "qualitative": 0, "flow": 0, "quality": 0}
    for i in range(n_down):
        t = f"DN{i}"
        closes[t] = _closes(start=100.0, n=days, step=-1.0)
        rows.append({"date": "2026-01-01", "ticker": t, "kind": "HOLD", **base})
    for i in range(n_up):
        t = f"UP{i}"
        closes[t] = _closes(start=100.0, n=days, step=1.0)
        rows.append({"date": "2026-01-01", "ticker": t, "kind": "HOLD", **base})
    return closes, rows


def test_sell_precision_can_be_below_base_rate():
    """하락장에서는 무작위 매도도 정밀도가 높다 — 66.7%가 기준선(70%) 미달일 수 있다."""
    closes, rows = _flat_universe(7, 3)
    for r in rows:  # 매도 3건: 하락 2 + 상승 1
        if r["ticker"] in ("DN0", "DN1", "UP0"):
            r["kind"] = "SELL"
    out = accuracy.realized_accuracy(rows, closes, horizons=(5,), primary=5)
    assert out["sell_precision_pct"] == 66.7
    assert out["baseline"]["down_pct"] == 70.0    # "항상 매도"의 성적
    assert out["baseline"]["up_pct"] == 30.0
    assert out["sell_lift_pp"] == -3.3            # 정밀도는 높은데 기준선 미달 → 음의 리프트
    assert out["baseline"]["sample"] == 10


def test_buy_lift_positive_when_selection_beats_market():
    closes, rows = _flat_universe(4, 6)
    for r in rows:  # 상승 종목만 골라 매수 → 정밀도 100%
        if r["ticker"] in ("UP0", "UP1", "UP2"):
            r["kind"] = "BUY"
    out = accuracy.realized_accuracy(rows, closes, horizons=(5,), primary=5)
    assert out["buy_precision_pct"] == 100.0
    assert out["baseline"]["up_pct"] == 60.0
    assert out["buy_lift_pp"] == 40.0
    assert out["lift_min_pp"] == accuracy.MIN_LIFT_PP


def test_flat_return_is_not_a_hit_on_either_side():
    """무변동은 매수도 매도도 적중이 아니다 — 티어 적중률과 정밀도가 같은 정의를 쓴다."""
    d, c = _closes(start=100.0, n=40, step=0.0)
    base = {"momentum": 0, "technical": 0, "fundamental": 0, "valuation": 50,
            "reversion": 0, "qualitative": 0, "flow": 0, "quality": 0}
    rows = [{"date": "2026-01-01", "ticker": "FLAT", "kind": "SELL", **base}]
    out = accuracy.realized_accuracy(rows, {"FLAT": (d, c)}, horizons=(5,), primary=5)
    assert out["sell_precision_pct"] == 0.0
    assert out["tiers"][5]["SELL"]["hit_rate"] == 0.0
    assert out["baseline"]["down_pct"] == 0.0 and out["baseline"]["up_pct"] == 0.0


def test_ci_half_width_exposes_small_samples():
    # n=20·p=60% → ±21.5%p. 리프트가 이보다 작으면 무정보.
    assert accuracy._ci_half_pp(60.0, 20) == 21.5
    assert accuracy._ci_half_pp(60.0, 500) == 4.3
    assert accuracy._ci_half_pp(None, 20) is None and accuracy._ci_half_pp(60.0, 0) is None


def test_factor_ic_sign_and_stats():
    """momentum이 높을수록 미래수익이 높은 패널 → IC>0 · 유의 · 통계가 전부 붙는다."""
    rows, closes = _panel(n_dates=25, n_tickers=25, slope=1.0)
    out = accuracy.realized_accuracy(rows, closes, horizons=(5,), primary=5)
    s = out["factor_ic_stats"]["momentum"]
    assert s["ic"] is not None and s["ic"] > 0.9      # 단조 증가 → 강한 양의 IC
    assert out["factor_ic"]["momentum"] == s["ic"]    # 계약: 두 키가 어긋나면 안 된다
    assert s["n_dates"] == 25 and s["breadth_median"] == 25
    assert s["significant"] is True and s["p"] is not None and s["p"] < 0.05
    assert s["blocked_reason"] is None
    assert s["independent_dates"] == 5                # 25일 / h5 중첩
    assert s["ci95"] is not None and s["horizon"] == 5


def test_ic_is_gated_by_dates_not_rows():
    """행이 아무리 많아도 **날짜**가 모자라면 IC를 내지 않는다.

    옛 구현은 `_MIN_IC_SAMPLES=20`을 행으로 셌다 — 하루치 200종목이 표본 200이 되어 문턱을
    즉시 통과했고, 실제로 `short IC −0.148`이 단 하루(200/2200행)에서 나온 값이었다.
    """
    rows, closes = _panel(n_dates=1, n_tickers=200, slope=1.0)
    out = accuracy.realized_accuracy(rows, closes, horizons=(5,), primary=5)
    s = out["factor_ic_stats"]["momentum"]
    assert s["n_pairs"] == 200 and s["n_dates"] == 1  # 행은 200, 관측은 1
    assert out["factor_ic"]["momentum"] is None
    assert "1/20일" in s["blocked_reason"]

    # 날짜만 채우면(종목 수는 오히려 적어도) 값이 나온다.
    rows, closes = _panel(n_dates=20, n_tickers=12, slope=1.0)
    out = accuracy.realized_accuracy(rows, closes, horizons=(5,), primary=5)
    assert out["factor_ic"]["momentum"] is not None


def test_thin_cross_sections_are_dropped_not_counted():
    """하루 횡단면이 종목 몇 개뿐이면 그 날의 IC는 노이즈다 → 날짜에서 뺀다."""
    rows, closes = _panel(n_dates=25, n_tickers=4, slope=1.0)   # 4종목 < min_breadth 10
    out = accuracy.realized_accuracy(rows, closes, horizons=(5,), primary=5)
    s = out["factor_ic_stats"]["momentum"]
    assert s["n_dates"] == 0 and s["thin_dates"] == 25
    assert out["factor_ic"]["momentum"] is None
    assert "종목 부족으로 버린 날짜 25개" in s["blocked_reason"]


def test_ic_with_no_relationship_is_not_significant():
    """팩터와 수익이 무관하면 IC는 값이 있어도 `significant=False`이고 이유가 붙는다.

    양성 대조군(위 test_factor_ic_sign_and_stats)과 짝이다 — 검사를 조일 때마다 통과하는
    케이스를 같이 두지 않으면 `판정 불가`가 정보인지 고장인지 알 수 없다.
    """
    rows, closes = _panel(n_dates=25, n_tickers=25, slope=0.0)  # 전 종목 동일 경로
    out = accuracy.realized_accuracy(rows, closes, horizons=(5,), primary=5)
    s = out["factor_ic_stats"]["momentum"]
    assert out["factor_ic"]["momentum"] is None
    assert s["significant"] is False


def test_pooled_ic_would_be_fooled_by_mixing_dates():
    """날짜를 섞으면 없는 상관이 생긴다 — 횡단면 IC는 여기서 0을 내야 한다.

    구조: 각 날짜 안에서는 팩터와 수익이 **무관**하지만, 날짜마다 팩터 수준과 시장 수익이
    같이 오르내린다(상승일엔 팩터도 높고 전 종목 수익도 양수). pooled 상관은 이걸
    팩터 판별력으로 읽는다 — 실제 `factor_ic`가 이 계산이었다.
    """
    n_d, n_t, bars = 24, 20, 90
    dates = _cal(bars)
    # 시장 경로: 날짜마다 다음 h5 구간이 오르거나 내린다(교대) — 전 종목 공통.
    closes, rows = {}, []
    path = [100.0]
    for k in range(1, bars):
        path.append(path[-1] * (1.03 if (k // 5) % 2 == 0 else 0.97))
    for i in range(n_t):
        closes[f"T{i}"] = (dates, list(path))       # 전 종목 동일 경로 → 횡단면 상관 0
    for j, d in enumerate(dates[:n_d]):
        level = 10.0 if (j // 5) % 2 == 0 else 0.0  # 오르는 구간엔 팩터 수준도 높다
        for i in range(n_t):
            rows.append({"date": d, "ticker": f"T{i}", "kind": "HOLD",
                         "momentum": level + (i % 2) * 0.001,  # 날짜 안에서는 사실상 무정보
                         "technical": 0, "fundamental": 0, "valuation": 50, "reversion": 0,
                         "qualitative": 0, "flow": 0, "quality": 0, "short": 0, "score": 0})
    pooled = accuracy._spearman_ic(
        [(r["momentum"], accuracy._forward_returns(*closes[r["ticker"]], r["date"], (5,))[5])
         for r in rows
         if 5 in accuracy._forward_returns(*closes[r["ticker"]], r["date"], (5,))])
    out = accuracy.realized_accuracy(rows, closes, horizons=(5,), primary=5)
    s = out["factor_ic_stats"]["momentum"]
    assert pooled is not None and abs(pooled) > 0.5, f"pooled가 속지 않으면 검사가 무의미: {pooled}"
    assert s["ic_mean"] is None or abs(s["ic_mean"]) < 0.05, s["ic_mean"]
    assert out["factor_ic"]["momentum"] is None      # 횡단면은 속지 않는다


def test_newey_west_se_is_floored_at_naive():
    """겹치는 창의 SE가 iid보다 **작다**고 주장하지 않는다(보수성 선택 · floored로 노출)."""
    # 완벽한 교대열 → 강한 음의 자기상관 → NW < 소박
    xs = [0.1 if i % 2 == 0 else -0.1 for i in range(20)]
    nw = accuracy._newey_west_se(xs, 4)
    assert nw["floored"] is True
    assert nw["se"] == nw["se_naive"]
    # 자기상관이 없으면 NW ≈ 소박(하한이 걸려도 값이 크게 안 바뀐다)
    flat = accuracy._newey_west_se([0.05, -0.05] * 2 + [0.0] * 16, 0)
    assert flat["se"] is not None and flat["se"] > 0


def test_t_two_sided_p_matches_table():
    """t-분포 p가 임계표와 맞아야 판정이 의미를 갖는다(정규근사는 소표본에서 p를 과소평가)."""
    assert abs(accuracy.t_two_sided_p(2.228, 10) - 0.05) < 0.001
    assert abs(accuracy.t_two_sided_p(3.169, 10) - 0.01) < 0.001
    assert abs(accuracy.t_two_sided_p(2.086, 20) - 0.05) < 0.001
    assert accuracy.t_two_sided_p(0.0, 10) == 1.0
    assert accuracy.t_two_sided_p(2.0, 0) is None


def test_unmatched_ticker_skipped():
    out = accuracy.realized_accuracy(
        [{"date": "2026-01-01", "ticker": "GHOST", "kind": "BUY"}], {}, horizons=(5,))
    assert out["ready"] is False
    assert out["coverage"]["rows"] == 1 and out["coverage"]["tickers_matched"] == 0


def test_nan_factor_skipped_in_ic_and_does_not_hang():
    """parquet NaN은 is not None을 통과한다 — IC에서 빼지 않으면 Spearman이 멈출 수 있다."""
    import math
    rows, closes = _panel(n_dates=25, n_tickers=30, slope=1.0)
    for r in rows:                       # score는 momentum과 같게, valuation은 일부 NaN
        r["score"] = r["momentum"]
        r["valuation"] = math.nan if int(r["ticker"][1:]) < 5 else float(r["ticker"][1:])
    out = accuracy.realized_accuracy(rows, closes, horizons=(5,), primary=5)
    assert out["factor_ic"]["momentum"] is not None
    assert out["factor_ic"]["score"] is not None and out["factor_ic"]["score"] > 0.9
    val = out["factor_ic_stats"]["valuation"]
    assert val["ic"] is not None                  # NaN 행만 제외하고 계산
    assert val["breadth_median"] == 25            # 30종목 중 NaN 5종목이 빠졌다


def test_interim_headline_when_primary_not_mature():
    """h20 미성숙이어도 h5 표본이 있으면 임시 헤드라인을 낸다 — 단 IC는 날짜 요건을 따로 본다.

    봉 21개면 h20은 어느 날짜도 성숙하지 못하고 h5는 15일만 성숙한다. 그래서 **정밀도는 임시로
    보여줄 수 있지만 IC는 못 낸다**(15 < 20일). 헤드라인이 떴다고 IC가 딸려 오는 게 아니다 —
    옛 구현은 행으로 세어 같은 상황에서 IC를 숫자로 냈다.
    """
    rows, closes = _panel(n_dates=15, n_tickers=25, slope=1.0, factor="fundamental", bars=21)
    out = accuracy.realized_accuracy(rows, closes, horizons=(5, 20), primary=20)
    assert out["primary_ready"] is False
    assert out["headline_horizon"] == 5
    assert out["ready"] is True
    assert out["factor_ic_horizon"] == 5
    assert out["coverage"]["interim_note"]
    s = out["factor_ic_stats"]["fundamental"]
    assert s["n_dates"] == 15 and out["factor_ic"]["fundamental"] is None
    assert "15/20일" in s["blocked_reason"]
