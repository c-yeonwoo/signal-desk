"""레드팀 — 우리 도구가 거짓말하는지 기계적으로 검사한다.

## 왜 사람 리뷰가 아니라 이 파일인가

2026-07-26 하네스를 만들면서 세 번 틀렸고, 세 번 다 잡아낸 건 코드 리뷰가 아니라 **대조군**이었다
(안 산 기간에 수수료 차감 / 동점을 시총순 정렬 / 대조군만 위상 평균). 리뷰는 "방법론이 타당해
보인다"고 말하는 데 능하고, 대조군은 틀리면 그냥 빨간불이 켜진다.

여기 있는 검사들은 전부 **반증 가능한 형태**다. 통과가 성능을 보장하지는 않지만, 실패하면
그 숫자를 인용하면 안 된다는 것만은 확실하다.

| 검사 | 무엇을 잡나 |
|---|---|
| 셔플(누수) | 점수가 미래를 몰래 보는가 |
| 음성 대조군 | 신호가 없는 시장에서 엣지를 만들어내는가 |
| 양성 대조군 | 진짜 엣지를 놓치는가(도구 고장) |
| 미래 불변성 | replay 경로에 룩어헤드가 있는가 |
| base rate 동반 | 비율을 대조군 없이 내보내는가 |
| 표본·커버리지 차단 | 못 믿을 숫자에 판정을 내리는가 |
"""

from __future__ import annotations

import math
import random

import pytest

from signal_desk.signals import harness as hz
from signal_desk.signals.engine import SignalConfig


def _dates(n: int) -> list[str]:
    return [f"2025-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(n)]


def _tech_only() -> SignalConfig:
    """합성 시장은 300거래일이라 모멘텀(252일 필요)·낙폭과대가 거의 안 켜진다.
    그 상태로 기본 설정을 쓰면 커버리지 차단에 걸려 **모든 검사가 이유 없이 통과**한다.
    측정하는 것을 정확히 이름 붙인다 — 여기서 보는 건 기술 팩터 순위의 판별력이다."""
    from dataclasses import replace
    return replace(SignalConfig(), weight_reversion=0.0, weight_momentum=0.0)


def _panel(closes: dict[str, list[float]]) -> hz.Panel:
    return hz.build_panel({t: (_dates(len(v)), v) for t, v in closes.items()})


def _predictive_market(n_days: int = 320, n_names: int = 40, period: int = 30) -> hz.Panel:
    """기술 팩터가 **실제로** 앞날을 맞히는 시장(전방 5일 IC +0.31).

    처음엔 "꾸준히 오르는 종목"으로 양성 대조군을 만들었는데 기술 팩터가 그걸 계속 피했다.
    RSI·MACD 기반이라 과매수를 매도 신호로 읽기 때문이다 — 도구 결함이 아니라 팩터의 성격이다.
    양성 대조군은 **그 팩터가 원래 잘해야 하는 시장**이어야 한다. 그래서 종목마다 위상이 다른
    진동 시장을 쓴다: 지금 골에 있는 종목이 며칠 뒤 반등한다.
    """
    closes = {}
    for i in range(n_names):
        phase = i * period / n_names
        closes[f"S{i}"] = [100 + 12 * math.sin(2 * math.pi * (j + phase) / period) + j * 0.02
                           for j in range(n_days)]
    return _panel(closes)


def _random_walk_market(n_days: int = 900, n_names: int = 40, seed: int = 7) -> hz.Panel:
    """드리프트도 구조도 없는 시장 — 여기서 엣지가 나오면 그건 도구가 만든 것이다."""
    rng = random.Random(seed)
    closes = {}
    for i in range(n_names):
        px, path = 100.0, []
        for _ in range(n_days):
            px *= 1 + rng.gauss(0, 0.015)
            path.append(px)
        closes[f"R{i}"] = path
    return _panel(closes)


# ------------------------------------------------------------------ 누수 탐지

def test_shuffling_returns_destroys_any_edge():
    """점수와 수익률의 짝을 어긋나게 하면 판별력은 반드시 사라진다.
    셔플하고도 판별력이 남으면 점수가 미래를 보고 있다는 뜻이다."""
    panel = _predictive_market()
    honest = hz.run(panel, hz.HarnessConfig(top_pct=10.0, rebalance_days=5, random_trials=100,
                                            min_score=-99.0, cost_pct=0.0,
                                            signal_config=_tech_only()))
    shuffled = hz.run(panel, hz.HarnessConfig(top_pct=10.0, rebalance_days=5, random_trials=100,
                                              min_score=-99.0, cost_pct=0.0,
                                              shuffle_returns=True,
                                              signal_config=_tech_only()))
    assert honest["verdict"] == "판별력 있음", "전제: 셔플 전에는 엣지가 잡혀야 한다"
    assert shuffled["verdict"] != "판별력 있음", shuffled
    assert any("셔플" in w for w in shuffled["warnings"])


def test_costs_alone_cannot_manufacture_an_edge():
    """정보가 0인 전략이 '회전율이 낮아 비용을 덜 문다'는 이유만으로 판별력을 얻으면 안 된다.

    실제로 그랬다. 대조군이 매 기간 k종목을 새로 뽑아 회전율 100%로 비용을 최대로 무는 동안,
    점수 기반 전략은 점수가 지속적이라 회전율이 낮아 비용을 덜 물었다. 5년 실데이터에서
    셔플한(=정보 0) 전략이 백분위 100%로 '판별력 있음'을 받았다. 위 셔플 테스트가 이걸 놓친
    이유는 비용을 0으로 두고 돌렸기 때문이다 — 검사와 현실 사이의 그 틈에 버그가 살았다."""
    out = hz.run(_predictive_market(), hz.HarnessConfig(
        top_pct=10.0, rebalance_days=5, random_trials=100, min_score=-99.0,
        cost_pct=0.25, shuffle_returns=True, signal_config=_tech_only()))
    assert out["verdict"] != "판별력 있음", out


def test_the_null_pays_the_same_costs_as_the_strategy():
    """대조군은 전략과 같은 회전율로 비용을 물어야 한다.

    점수가 완전히 지속적이면 전략은 같은 종목을 계속 들고 있어 비용을 거의 안 문다. 매 기간
    k종목을 새로 뽑는 대조군은 회전율이 늘 100%라 비용을 최대로 문다. 그 차이만으로 전략은
    **정보가 하나도 없어도** 대조군을 이긴다 — 실데이터에서 셔플한 전략이 백분위 100%를 받은
    원인이 이거였다. 라벨 치환 대조군은 점수의 지속성을 그대로 물려받아 회전율이 맞는다."""
    panel = _random_walk_market()
    fixed = {t: [float(i) ] * len(panel.dates)      # 순위가 영원히 고정 → 회전율 0
             for i, t in enumerate(sorted(panel.closes))}
    cfgs = {c: hz.HarnessConfig(top_pct=10.0, rebalance_days=5, random_trials=40,
                                min_score=-999.0, cost_pct=c, signal_config=_tech_only())
            for c in (0.0, 0.5)}
    wealth = {}
    for cost, cfg in cfgs.items():
        phase_idxs = [hz._rebalance_indices(panel, cfg, p) for p in range(cfg.rebalance_days)]
        runs = [hz._run_phase(panel, cfg, fixed, idxs, None, {}, random.Random(cfg.seed))
                for idxs in phase_idxs]
        wealth[("strategy", cost)] = sum(r["equity"][-1] for r in runs) / len(runs)
        null = hz._null_distribution(panel, cfg, fixed, phase_idxs, None, {})
        wealth[("null", cost)] = 1 + null["median"] / 100

    drag = {side: wealth[(side, 0.5)] / wealth[(side, 0.0)] for side in ("strategy", "null")}
    assert drag["strategy"] < 1.0, "전제: 비용을 물리면 전략도 깎여야 한다"
    assert abs(drag["strategy"] - drag["null"]) < 0.05, (
        f"비용 부담이 전략 {drag['strategy']:.3f} vs 대조군 {drag['null']:.3f}로 어긋난다 — "
        f"회전율이 맞지 않는 대조군이다")


# --------------------------------------------------------------- 음성 대조군

@pytest.mark.parametrize("top_pct,hold", [(3.0, 5), (10.0, 20)])
def test_no_edge_is_found_in_a_random_walk(top_pct: float, hold: int):
    """신호가 없는 시장에서 '판별력 있음'이 나오면 도구가 엣지를 만들어낸 것이다."""
    out = hz.run(_random_walk_market(), hz.HarnessConfig(
        top_pct=top_pct, rebalance_days=hold, random_trials=150, min_score=-99.0,
        signal_config=_tech_only()))
    assert out["verdict"] != "판별력 있음", out


def test_inverted_scores_are_not_certified_in_a_random_walk():
    """부호를 뒤집어도 마찬가지 — 어느 방향이든 없는 엣지를 찾아내면 안 된다."""
    out = hz.run(_random_walk_market(seed=11), hz.HarnessConfig(
        top_pct=5.0, rebalance_days=5, random_trials=150, min_score=-99.0,
        invert_scores=True, signal_config=_tech_only()))
    assert out["verdict"] != "판별력 있음", out


# --------------------------------------------------------------- 양성 대조군

def test_a_real_edge_is_still_detected():
    """검사를 촘촘히 하다 보면 아무것도 통과 못 하게 만들기 쉽다.
    엣지가 실재할 때 잡히는지 매번 확인해야 나머지 '판정 불가'가 정보가 된다."""
    out = hz.run(_predictive_market(), hz.HarnessConfig(
        top_pct=10.0, rebalance_days=5, random_trials=100, min_score=-99.0, cost_pct=0.0,
        signal_config=_tech_only()))
    assert out["verdict"] == "판별력 있음", out["verdict_why"]


# ------------------------------------------------------- 룩어헤드(미래 불변성)

def _diverging_pair(n: int = 240) -> tuple[list[float], list[float], int]:
    hist = [100 + math.sin(i / 5) * 8 + i * 0.3 for i in range(n)]
    return hist + [hist[-1] * 1.06 ** i for i in range(1, 30)], \
        hist + [hist[-1] * 0.94 ** i for i in range(1, 30)], n - 1


def test_replay_signal_kinds_ignores_the_future():
    from signal_desk.signals import engine
    up, down, i = _diverging_pair()
    assert engine.replay_signal_kinds(up)[i] == engine.replay_signal_kinds(down)[i]


def test_chart_scores_ignore_the_future():
    from signal_desk.signals import engine
    up, down, i = _diverging_pair()
    s_up, _ = engine.chart_scores_and_zones(_dates(len(up)), up)
    s_down, _ = engine.chart_scores_and_zones(_dates(len(down)), down)
    assert s_up[i] == s_down[i]


def test_harness_scores_ignore_the_future():
    up, down, i = _diverging_pair()
    cfg = SignalConfig()
    s_up, *_ = hz._score_series(_panel({"A": up}), cfg)
    s_down, *_ = hz._score_series(_panel({"A": down}), cfg)
    assert s_up["A"][i] == s_down["A"][i]


def test_indicator_series_are_causal():
    """지표 시계열 자체가 미래를 안 쓰는지 — 위 셋의 공통 기반이라 따로 못박는다."""
    from signal_desk.signals import engine
    up, down, i = _diverging_pair()
    a, b = engine.compute_indicator_series(up), engine.compute_indicator_series(down)
    for key in a:
        if isinstance(a[key], list) and len(a[key]) > i:
            assert a[key][i] == b[key][i], f"{key}가 미래 가격에 반응한다"


# ---------------------------------------------------- base rate 동반 강제

def test_precision_metrics_always_ship_with_a_baseline():
    """CLAUDE.md 규칙의 강제. 정밀도류 비율은 기준선·리프트 없이 나갈 수 없다.
    -3% 조정장에서는 무작위 매도도 정밀도 60%가 나온다 — 절대값만 보면 매번 속는다."""
    from signal_desk.signals import accuracy
    rows, closes = _accuracy_fixture()
    out = accuracy.realized_accuracy(rows, closes)
    assert "baseline" in out and out["baseline"], out
    for side in ("buy", "sell"):
        if out.get(f"{side}_precision_pct") is not None:
            assert f"{side}_lift_pp" in out, f"{side} 정밀도에 리프트가 없다"
            assert f"{side}_precision_ci_pp" in out, f"{side} 정밀도에 신뢰구간이 없다"
    assert "lift_min_pp" in out


def test_zero_matured_says_why_it_is_zero():
    """감사 가설 큐에서 승격된 검사(2026-07-26).

    시세 캐시가 07-03에서 멈춘 채 시그널만 07-24까지 쌓여 `tickers_matched: 0`이 떴는데,
    화면에는 '누적 중'으로만 보였다. **성숙 대기(정상)와 수집 중단(고장)이 똑같이 0으로
    표시되면 고장을 몇 주씩 못 본다.** 0에는 항상 이유가 붙어야 한다.
    """
    from signal_desk.signals import accuracy
    dates = _dates(40)
    rows = [{"date": dates[30], "ticker": "T0", "kind": "BUY", "score": 1.5}]

    stale = {"T0": (dates[:10], [100.0 + i for i in range(10)])}   # 시세가 시그널보다 과거
    out = accuracy.realized_accuracy(rows, stale)
    cov = out["coverage"]
    assert cov["stale_prices"] is True
    assert "수집 중단" in cov["blocked_reason"]
    assert cov["price_data_to"] == dates[9]

    fresh = {"T0": (dates, [100.0 + i for i in range(40)])}        # 시세는 최신, 아직 미성숙
    cov2 = accuracy.realized_accuracy(rows, fresh)["coverage"]
    assert cov2["stale_prices"] is False
    assert cov2["blocked_reason"] is None or "성숙" in cov2["blocked_reason"]


def test_missing_universe_join_is_named_not_silent():
    """시그널 종목이 시세 캐시에 아예 없는 경우도 0이 아니라 문장으로 말해야 한다."""
    from signal_desk.signals import accuracy
    dates = _dates(40)
    rows = [{"date": dates[10], "ticker": "GHOST", "kind": "BUY", "score": 1.0}]
    cov = accuracy.realized_accuracy(rows, {"T0": (dates, [100.0] * 40)})["coverage"]
    assert cov["tickers_matched"] == 0
    assert "어긋" in cov["blocked_reason"], cov


def test_advisor_shadow_verdict_requires_significance():
    """표본 수만으로 판정하지 않는다 — 20쌍의 SE는 ±3.8%p다."""
    from signal_desk.signals import advisor_shadow
    src = advisor_shadow.summary.__doc__ or ""
    import inspect
    code = inspect.getsource(advisor_shadow)
    assert "delta_significant" in code
    assert "verdict_ready" in code
    idx = code.index("verdict_ready")
    line = code[idx:code.index("\n", idx)]
    assert "significant" in line, f"판정이 유의성과 무관하다: {line}"


def test_scoring_factors_are_snapshotted_and_in_factor_ic(tmp_path, monkeypatch):
    """점수에 들어가는 팩터가 PIT·factor_ic에서 빠져 있으면 그 팩터는 영원히 측정 불가.

    실측: short는 evaluate/combine·두뇌에 있는데 snapshot_signals와 FACTOR_COLS에 없었다.
    qualitative는 combine 밖(shadow)이라 SCORING_FACTORS에 없고 FACTOR_COLS에만 있어도 된다."""
    from signal_desk.signals import accuracy
    from signal_desk.signals.engine import SignalResult
    from signal_desk import store as store_mod

    missing_ic = set(accuracy.SCORING_FACTORS) - set(accuracy.FACTOR_COLS)
    assert not missing_ic, f"점수 팩터가 factor_ic 밖에 있다: {missing_ic}"

    monkeypatch.chdir(tmp_path)
    (tmp_path / "data/cache").mkdir(parents=True)
    sig = SignalResult(ticker="005930", name="삼성전자", score=1.0, kind="BUY",
                       confidence=0.5, technical_score=0.1, fundamental_score=0.0,
                       has_fundamental=False, reasons=[], short_ratio=0.18, has_short=True)
    store_mod.snapshot_signals([sig], date="2026-07-24")
    row = store_mod.load_signal_history().iloc[0]
    for col in accuracy.SCORING_FACTORS:
        assert col in row.index, f"PIT 스냅샷에 점수 팩터 '{col}' 없음"
    assert float(row["short"]) == 0.18


def _accuracy_fixture():
    """PIT 히스토리 + 종가. 20거래일 성숙 구간을 확보한다."""
    dates = _dates(60)
    closes = {}
    for i in range(6):
        px = [100.0 + j * (0.4 if i % 2 == 0 else -0.2) for j in range(60)]
        closes[f"T{i}"] = (dates, px)
    rows = []
    for d in dates[:20]:
        for i in range(6):
            rows.append({"date": d, "ticker": f"T{i}",
                         "kind": "BUY" if i % 2 == 0 else "SELL",
                         "score": 1.5 if i % 2 == 0 else -1.5})
    return rows, closes


# ------------------------------------------------------- 표본·커버리지 차단

def test_small_sample_cannot_be_certified():
    """표본 미달이면 백분위가 아무리 좋아도 판정하지 않는다."""
    v, why = hz._verdict(99.0, phase_min=50.0, phase_max=60.0, random_median=10.0,
                         periods=12, min_periods=30)
    assert v == "판정 불가" and "표본" in why


def test_low_coverage_factor_cannot_be_certified():
    """이력이 모자라 조용히 빠진 팩터의 결과에 '판별력 있음'을 붙이면 안 된다."""
    v, why = hz._verdict(99.0, phase_min=50.0, phase_max=60.0, random_median=10.0,
                         weak_factors=["momentum"])
    assert v == "판정 불가" and "momentum" in why


def test_coverage_gate_only_counts_weighted_factors():
    """가중치 0인 팩터는 애초에 쓰지 않으므로 차단 사유가 될 수 없다."""
    from dataclasses import replace
    cfg = replace(SignalConfig(), weight_momentum=0.0, weight_reversion=0.0)
    assert hz._weighted_factors(cfg) == {"technical"}
    assert "momentum" in hz._weighted_factors(SignalConfig())


def test_short_history_blocks_the_verdict_end_to_end():
    """지금 캐시(268거래일)에 20일 보유면 리밸런스가 7회뿐이다. 게이트가 실제로 막는지 확인."""
    out = hz.run(_random_walk_market(n_days=200), hz.HarnessConfig(
        top_pct=3.0, rebalance_days=20, random_trials=80, min_score=-99.0,
        signal_config=_tech_only()))
    assert out["verdict"] == "판정 불가"
    assert "표본" in out["verdict_why"], out["verdict_why"]
