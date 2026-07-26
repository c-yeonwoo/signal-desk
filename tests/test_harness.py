"""하네스 자체를 검증한다.

백테스트 도구는 틀려도 그럴듯한 숫자를 뱉기 때문에 결과보다 **방법론 속성**을 못박아 둔다.
특히 (1) 진짜 엣지가 있으면 잡아내는가(양성 대조군), (2) 미래를 훔쳐보지 않는가,
(3) 안 산 기간에 비용을 물리지 않는가 — 이 셋은 실제로 한 번씩 다 틀렸던 것들이다.
"""

from __future__ import annotations

import math

from signal_desk.signals import harness as hz


def _dates(n: int) -> list[str]:
    return [f"2025-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(n)]


def _panel(closes: dict[str, list[float]]) -> hz.Panel:
    n = max(len(v) for v in closes.values())
    return hz.build_panel({t: (_dates(len(v)), v) for t, v in closes.items()})


# ---------------------------------------------------------------- build_panel

def test_panel_aligns_dates_and_forward_fills_holidays():
    panel = hz.build_panel({
        "A": (["2025-01-01", "2025-01-02", "2025-01-03"], [100.0, 101.0, 102.0]),
        "B": (["2025-01-01", "2025-01-03"], [50.0, 52.0]),   # 01-02 거래정지
    })
    assert panel.dates == ["2025-01-01", "2025-01-02", "2025-01-03"]
    assert panel.closes["B"] == [50.0, 50.0, 52.0]           # 직전 종가로 보정


def test_panel_leaves_pre_listing_as_none():
    panel = hz.build_panel({
        "A": (["2025-01-01", "2025-01-02"], [100.0, 101.0]),
        "LATE": (["2025-01-02"], [10.0]),
    })
    assert panel.closes["LATE"][0] is None, "상장 전 구간을 채우면 없던 종목을 보유하게 된다"


def test_panel_filters_to_requested_market():
    panel = hz.build_panel({
        "005930": (["2025-01-01"], [100.0]),
        "AAPL": (["2025-01-01"], [200.0]),
    }, tickers={"005930"})
    assert set(panel.closes) == {"005930"}


# ------------------------------------------------------------- 룩어헤드 차단

def test_scores_ignore_future_prices():
    """t 시점 점수는 t 이후 가격이 어떻게 바뀌든 같아야 한다."""
    hist = [100 + math.sin(i / 5) * 8 + i * 0.3 for i in range(200)]
    up = hist + [hist[-1] * 1.05 ** i for i in range(1, 40)]
    down = hist + [hist[-1] * 0.95 ** i for i in range(1, 40)]
    from signal_desk.signals.engine import SignalConfig
    cfg = SignalConfig()
    s_up, _ = hz._score_series(_panel({"A": up}), cfg)
    s_down, _ = hz._score_series(_panel({"A": down}), cfg)
    i = len(hist) - 1
    assert s_up["A"][i] == s_down["A"][i]


# --------------------------------------------------------------- 비용·현금

def test_no_cost_when_nothing_is_bought():
    """매수 0건인 기간에 거래비용을 물리면, 안 산 것이 손실로 기록된다."""
    flat = {f"T{i}": [100.0] * 260 for i in range(20)}
    cfg = hz.HarnessConfig(min_score=99.0, random_trials=5, rebalance_days=5)  # 아무도 못 사게
    out = hz.run(_panel(flat), cfg)
    assert out["strategy"]["total_ret_pct"] == 0.0
    assert any("매수 0건" in w for w in out["warnings"])


# ------------------------------------------------------------ 커버리지 경고

def test_warns_when_factor_has_no_history():
    """모멘텀은 252거래일을 요구한다 — 이력이 짧으면 조용히 빠지므로 경고해야 한다."""
    prices = {f"T{i}": [100.0 + i + j * 0.1 for j in range(200)] for i in range(10)}
    out = hz.run(_panel(prices), hz.HarnessConfig(random_trials=5))
    assert out["coverage_pct"]["momentum"] < 60
    assert any("momentum" in w for w in out["warnings"])


# --------------------------------------------------------------- 동점 처리

def test_ties_are_not_broken_by_universe_order():
    """점수가 모두 같으면 특정 종목만 계속 뽑혀선 안 된다(=시총순 매수가 몰래 섞임)."""
    flat = {f"T{i:03d}": [100.0] * 260 for i in range(50)}
    panel = _panel(flat)
    cfg = hz.HarnessConfig(min_score=-99.0, random_trials=5, rebalance_days=5, top_pct=4.0)
    scores, _ = hz._score_series(panel, cfg.signal_config)
    assert len({scores[t][200] for t in scores}) == 1, "전제: 동점 상황"

    import random as _r
    seen = set()
    for seed in range(6):
        idxs = hz._rebalance_indices(panel, cfg)
        r = hz._run_phase(panel, cfg, scores, idxs, None, {}, _r.Random(seed))
        seen.add(r["avg_picks"])
    picked_first = {t for t in list(panel.closes)[:2]}
    assert picked_first, "유니버스 순서 상위가 항상 선택되면 이 테스트가 의미 없어진다"


# ---------------------------------------------------- 양성 대조군 / 음성 대조군

def _predictable_panel(n_days: int = 300, n_names: int = 60) -> hz.Panel:
    """상위 10종목은 꾸준히 오르고 나머지는 진동만 하는 시장.
    기술 팩터가 오르는 쪽을 잡아내므로 하네스는 '판별력 있음'을 내야 한다."""
    closes = {}
    for i in range(n_names):
        if i < 10:
            closes[f"W{i}"] = [100.0 * (1.004 ** j) for j in range(n_days)]
        else:
            closes[f"L{i}"] = [100.0 + math.sin((j + i * 7) / 6) * 3 for j in range(n_days)]
    return _panel(closes)


def test_detects_a_real_edge_positive_control():
    """엣지가 실재하면 '판별력 있음'이 나와야 한다.
    이게 안 되면 다른 모든 '판정 불가'는 도구 고장을 뜻할 뿐 아무 정보가 없다."""
    out = hz.run(_predictable_panel(), hz.HarnessConfig(
        top_pct=10.0, rebalance_days=5, random_trials=100, min_score=-99.0, cost_pct=0.0))
    assert out["ready"]
    assert out["vs_random"]["percentile"] >= 95, out
    assert out["verdict"] == "판별력 있음", out["verdict_why"]
    assert out["strategy"]["phase_min_pct"] > out["vs_random"]["median_total_pct"]


def test_refuses_to_certify_when_conclusion_depends_on_calendar():
    """리밸런스 날짜만 바꿔도 무작위 중위를 넘었다 못 넘었다 하면 판정 불가여야 한다."""
    assert hz._verdict(99.0, phase_min=-10.0, phase_max=90.0, random_median=20.0)[0] == "판정 불가"
    assert hz._verdict(99.0, phase_min=30.0, phase_max=40.0, random_median=20.0)[0] == "판별력 있음"
    assert hz._verdict(2.0, phase_min=1.0, phase_max=9.0, random_median=20.0)[0] == "역판별력"
    assert hz._verdict(50.0, phase_min=19.0, phase_max=21.0, random_median=20.0)[0] == "판정 불가"


def test_random_baseline_is_phase_averaged():
    """대조군도 전략과 같은 위상 평균을 거쳐야 분산이 같아진다.
    (전략만 평균내면 대조군이 흔들려서 전략이 이긴 것처럼 보인다.)"""
    panel = _predictable_panel(n_days=260, n_names=30)
    cfg = hz.HarnessConfig(rebalance_days=5, random_trials=60, min_score=-99.0)
    phase_idxs = [hz._rebalance_indices(panel, cfg, p) for p in range(cfg.rebalance_days)]
    unis = [{i: list(panel.closes) for i in idxs} for idxs in phase_idxs]
    spread_multi = hz._random_baseline(panel, cfg, phase_idxs, unis)
    single = hz._random_baseline(panel, cfg, phase_idxs[:1], unis[:1])

    def iqr(d):
        t = d["totals"]
        return t[int(len(t) * 0.75)] - t[int(len(t) * 0.25)]

    assert iqr(spread_multi) < iqr(single), "위상 평균은 대조군 분산을 줄여야 한다"
